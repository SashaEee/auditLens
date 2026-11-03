"""Срезы «Аудита отзывов», которых нет в справочнике продуктов: зарплатные проекты,
УС (банкоматы и терминалы), выездной сервис, а с 03.11.2026 и направления аудита,
которых не было в разметке: самозанятые и «Своё дело», дети и молодёжь, участники СВО.

Срез — не продукт договора, а сюжет: зарплатная карта — это дебетовая карта,
банкомат — канал. Поэтому основная разметка (review_annotate) их не ловит, а
подсказки её каналов неточны: курьер маркетплейса — не выездной сервис банка,
POS-терминал магазина — не УС. Здесь — отдельная LLM-разметка срезов:

1. Кандидаты — полнотекстовый фильтр по индексу (с запасом на полноту) плюс
   подсказки основной разметки (канал «банкомат»/«курьер», проблема cash_atm).
2. Каскад сильных моделей с карточками «ДА, если / НЕ …, если» (см. «каскад s7»):
   у каждого решения — пункт определения и дословная цитата (review_slice.evidence),
   аудитор видит, почему жалоба в срезе.
3. Итог — review_slice (url, версия); в индекс review_index.slices попадают срезы из
   АКТИВНОЙ версии каждого среза (review_slice_active): новая версия переключается по
   срезу отдельно, когда разметка догнала и замер на эталоне не хуже прежней.
   Фильтр продукта во вкладке — SQL-функция al_product_is (migrations/102).

Качество — только против эталона, размеченного вслепую людьми или нами самими
(не моделью против модели: на s6 такой замер давал 0,88–1,0 при ручных 0,44–0,73).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time

from sqlalchemy import text

from .. import db
from . import review_annotate as RA

log = logging.getLogger(__name__)

# s7 (03.11.2026) — определения «входит / не входит» для всех срезов, сильные модели на каждом,
# новые срезы направлений аудита (самозанятые, дети и молодёжь, СВО); s6 — payroll (14.10)
SCHEMA = os.getenv("REVIEW_SLICES_SCHEMA", "s7")
# окно разметки перекрывает самый длинный период вкладки (12 мес.) с запасом
SINCE = os.getenv("REVIEW_SLICES_SINCE", "2025-09-01")
INFLIGHT = int(os.getenv("REVIEW_SLICES_INFLIGHT", "12"))
CAP_RUB = float(os.getenv("REVIEW_SLICES_CAP_RUB", "15000"))
DAILY_CAP_RUB = float(os.getenv("REVIEW_SLICES_DAILY_CAP_RUB", "2000"))    # доразметка за сутки (МСК)
_LOCK_KEY = 710_022_402

# ключ: (подпись в списке продуктов «Аудита отзывов», определение для модели)
SLICES: dict[str, tuple[str, str]] = {
    "payroll": ("Зарплатные проекты", (
        "предмет претензии — сам зарплатный проект. ДА, если: (1) условия зарплатной карты или счёта как "
        "зарплатных — тариф и льготы зарплатного клиента, плата за обслуживание после потери статуса "
        "зарплатной (увольнение, смена работодателя, перевод проекта), выпуск зарплатной карты по проекту "
        "работодателя; (2) зачисление зарплаты, аванса, отпускных через банк — не пришли, задержаны, "
        "зачислены частично или с ошибкой; (3) работодатель (юрлицо, ИП) о зарплатном проекте — подключение, "
        "реестры, перечисление зарплаты, аванса, отпускных сотрудникам, тарифы, перевод проекта. "
        "Льгота, потерянная из-за того, как зачисляется зарплата (ставка по кредиту, бесплатное "
        "обслуживание), — это (1), а не (в). "
        "НЕ payroll, если: (а) клиент лишь упоминает, что он зарплатный клиент или что на карту приходит "
        "зарплата, а претензия о другом — блокировка, перевыпуск, переводы, подписки, лимиты, кредит, "
        "обслуживание в офисе; (б) арест, взыскание или списание по исполнительному документу, инкассовому "
        "поручению или требованию налоговой, очерёдность списания со счёта — даже если списана зарплата, "
        "нарушен лимит удержаний или речь о платежах работодателя по зарплате; (в) зарплата — только доход "
        "(кредит, проверка доходов); общий сбой банка, при котором не проходят любые операции и "
        "среди прочего «зависли зарплаты», — тоже (а); (г) пенсии и пособия; (д) пакет услуг или подписка, где зарплата "
        "лишь одно из условий бесплатности, а претензия — о самом пакете. Статус зарплатного клиента "
        "(«зарплатный» пакет, надбавка, бонус за перевод зарплаты в банк) — это пункт (1).")),
    "us": ("УС — банкоматы и терминалы", (
        "устройства самообслуживания (УС): банкомат, платёжный терминал, инфокиоск, устройство приёма "
        "наличных. ДА, если претензия — о работе или доступности самого устройства: (1) сбой операции в "
        "УС — не выдал или не зачислил наличные, выдал не ту сумму, удержал или не вернул карту, операция "
        "зависла или прервалась, деньги списаны, а операция не прошла (в том числе в устройстве другого "
        "банка); (2) недоступность УС — устройств нет, не работают, не принимают купюры, нет нужной "
        "функции, очереди к ним; (3) безопасность у УС — скиммер, кража денег из лотка, мошенничество у "
        "устройства. НЕ us, если: (а) комиссия или лимит по тарифу карты за снятие или внесение, а "
        "устройство сработало штатно — в том числе плата за снятие в чужих банкоматах; это тариф карты; "
        "(б) кассовые и эквайринговые POS-терминалы магазинов и бизнеса; (в) операции в кассе у "
        "сотрудника отделения; (г) блокировка карты или операции банком (антифрод, 115-ФЗ, 161-ФЗ), если "
        "устройство само карту не удержало; (д) устройство лишь упомянуто, а претензия о другом.")),
    "field": ("Выездной сервис — курьеры и выезд", (
        "выездной сервис банка — сотрудник или курьер банка приезжает к клиенту. ДА, если претензия — о "
        "самом выезде или доставке, то есть к срокам, действиям или поведению того, кто приехал: "
        "(1) курьерская доставка банковской карты, документов, договора, "
        "SIM-карты банка — не привезли, сорвали сроки, привезли не то, курьер не явился или вёл себя "
        "некорректно, навязал или оформил продукт при доставке; (2) выездной менеджер или представитель "
        "банка — приезд к клиенту для оформления продукта, подписания документов, подтверждения "
        "личности, открытия счёта — не приехал, нарушил порядок, навязал продукт; (3) выездное "
        "обслуживание маломобильных, пожилых, больных клиентов на дому — отказ, срыв или отсутствие "
        "такой услуги. НЕ field, если: (а) инженер, техник или сервисная служба по эквайрингу, кассам, ККТ, "
        "терминалам оплаты у бизнеса — это эквайринг; (б) взыскание долга, коллекторы, выезд по долгу; "
        "(в) доставка товаров маркетплейсов, еды и других небанковских сервисов — кроме случая, когда "
        "небанковский продукт навязали при доставке банковской карты; (г) почта, если банк ничего не "
        "вёз; (д) доставка или выезд лишь упомянуты, а претензия о другом — условия кредита, "
        "блокировка, перевод, обслуживание в офисе. Курьер штатно привёз карту, а претензия — к продукту "
        "или к решению банка, принятому не на встрече (отказ или лимит после оформления, тариф, бонусы, "
        "проверки и анкеты банка), — это (д). Если же что-то пошло не так на самой встрече — курьер или "
        "представитель не предупредил о платных услугах, подключил или навязал их, выдал не ту карту, "
        "ввёл в заблуждение, процедура при встрече сорвалась или не сработала, — это (1) или (2), даже если "
        "дальше клиент спорит с банком.")),
    # направления аудита, которых не было в разметке отзывов (directions.py, 03.11.2026)
    "selfemp": ("Самозанятые и «Своё дело»", (
        "самозанятые (налог на профессиональный доход, НПД) и сервис банка «Своё дело». ДА, если "
        "претензия: (1) о регистрации или снятии с учёта самозанятого через банк, о сервисе «Своё "
        "дело» — подключение, работа, чеки, начисление и уплата налога через сервис; (2) о счёте, "
        "карте, поступлениях и переводах самозанятого как самозанятого — блокировка поступлений от "
        "клиентов, ограничения, тарифы для самозанятых; (3) о кредитах, продуктах и условиях для "
        "самозанятых. НЕ selfemp, если: (а) индивидуальный предприниматель или компания на другой "
        "системе налогообложения (УСН, ОСНО) — это РКО и бизнес; кроме случая, когда клиент просил "
        "оформить именно НПД, а банк оформил другой режим, — это (1); (б) клиент лишь упоминает, что он "
        "самозанятый, а претензия о другом — ипотека, обычный перевод, мошенничество без связи со "
        "статусом; (в) налоги физлица вне НПД — НДФЛ, вычеты.")),
    "youth": ("Дети и молодёжь", (
        "детские и молодёжные продукты и клиенты младше 25 лет. ДА, если: (1) претензия о детской "
        "или молодёжной карте, счёте, вкладе, приложении для детей — оформление, ограничения, "
        "родительский контроль, комиссии, лимиты; (2) клиент — несовершеннолетний или подросток, и "
        "претензия связана с возрастом: оформление продуктов, доступ, согласие родителей, мошенничество "
        "против подростка; (3) студенческие и молодёжные программы и тарифы. НЕ youth, если: (а) "
        "родитель лишь упоминает детей, а претензия о его собственном продукте — ипотека с "
        "материнским капиталом, кредит, вклад; (б) государственные пособия на детей, если претензия "
        "не о детском продукте банка; (в) клиент — совершеннолетний студент или молодой человек, а "
        "претензия не о молодёжном или студенческом продукте и не связана с возрастом (блокировка, "
        "перевод, кредит на общих условиях).")),
    "svo": ("Участники СВО", (
        "участники специальной военной операции (СВО), военнослужащие, мобилизованные и их семьи. "
        "ДА, если претензия связана с этим статусом: (1) кредитные каникулы и льготы для участников "
        "СВО — отказ, нарушение, начисленные проценты и штрафы; (2) выплаты военнослужащим и их "
        "зачисление — денежное довольствие, компенсации, единовременные выплаты; (3) обслуживание "
        "семьи участника — доверенности, доступ к счетам, наследство погибшего. НЕ svo, если: (а) "
        "статус лишь упомянут, а претензия о другом; (б) военная ипотека и общие программы для "
        "военных без связи с участием в СВО — это ипотека.")),
}
KEYS = tuple(SLICES)
LABELS = {k: v[0] for k, v in SLICES.items()}

# Кандидаты: префиксные лексемы по tsv индекса (берём с запасом — лишнее отсеет модель)
_TSQ = {
    "payroll": "зарплатн:* | зарплат:* | заработн:* | работодател:*",
    "us": "банкомат:* | терминал:* | купюр:* | самообслуживан:* | инкассац:* | инфокиоск:*",
    "field": "курьер:* | выездн:* | выезд:* | доставк:*",
    # «своё дело» фразой: лексема «сво» одна у «своё», «свой» и «СВО»
    "selfemp": "самозанят:* | нпд | сво <-> дел:*",
    # «дети» — точной лексемой ('дет'), не префиксом: префикс ловил бы «детали»
    "youth": ("детск:* | подростк:* | молодежн:* | школьник:* | студент:* | несовершеннолетн:* | "
              "сберкидс:* | ребен:* | дети"),
    "svo": "военнослужащ:* | мобилизова:* | (специальн:* <-> военн:*) | участник:* <-> сво",
}
_HINT = ("'atm' = ANY(coalesce(i.channels, '{}'))", "i.issue = 'cash_atm'",
         "'courier' = ANY(coalesce(i.channels, '{}'))")


def candidate_sql() -> str:
    """Условие «отзыв — кандидат хотя бы в один срез» (алиас индекса — i)."""
    parts = [f"i.tsv @@ to_tsquery('russian', '{q}')" for q in _TSQ.values()]
    return "(" + " OR ".join(parts + list(_HINT)) + ")"


SYSTEM = (
    "Ты — старший аудитор розничного банка. Для каждого отзыва клиента реши по КАЖДОМУ срезу ниже, "
    "относится ли претензия к срезу. Срез — только если это суть претензии или её существенная часть, а не "
    "упоминание вскользь. Применяй определения буквально: пункты «ДА, если» — основание включить, пункты "
    "«НЕ …, если» — основание исключить. Сомнение — не включай. Читай весь текст.\n\nСРЕЗЫ\n"
    + "\n".join(f"{k} — {v[1]}" for k, v in SLICES.items())
    + '\n\nОтвечай ТОЛЬКО JSON без пояснений: {"items":[{"n":1,"slices":[{"key":"<срез>","clause":"<пункт ДА: '
      '1|2|3>","quote":"<дословная цитата до 150 знаков>"}],"excluded":{"<срез>":"<пункт НЕ: а|б|в|г|д>"}}]} — '
      "по объекту на каждый отзыв, поле n обязательно. В slices — только срезы, к которым отзыв относится "
      "(пустой список — ни к одному); excluded — срезы, по которым сработал пункт «НЕ» (если были близкие "
      "признаки), иначе пустой объект. Цитату копируй из текста символ в символ.")


def flags(d) -> dict | None:
    """Ответ модели по одному отзыву → {срез: bool, …, "evidence": {срез: {clause, quote}},
    "excluded": {срез: пункт}} или None. Незнакомые срезы отбрасываются; ответ без списка
    slices — нет ответа (не «ни к одному срезу»: молчание модели не равно «нет»)."""
    if not isinstance(d, dict) or not isinstance(d.get("slices"), list):
        return None
    out: dict = {k: False for k in KEYS}
    ev: dict = {}
    for it in d["slices"]:
        if isinstance(it, dict) and it.get("key") in KEYS:
            out[it["key"]] = True
            ev[it["key"]] = {"clause": str(it.get("clause") or "")[:8],
                             "quote": str(it.get("quote") or "")[:300]}
    exc = d.get("excluded") if isinstance(d.get("excluded"), dict) else {}
    out["evidence"] = ev
    out["excluded"] = {k: str(v)[:8] for k, v in exc.items() if k in KEYS and not out[k]}
    return out


def tags_of(final: dict) -> list[str]:
    return [k for k in KEYS if final.get(k)]


# Длинные жалобы: суть бывает в конце («…и в итоге курьер так и не приехал»). Раньше
# текст резался до 3000 знаков, и такие случаи терялись; голова и хвост дают оба края.
_HEAD, _TAIL = int(os.getenv("REVIEW_SLICES_HEAD", "6500")), int(os.getenv("REVIEW_SLICES_TAIL", "1200"))


def clip(t: str) -> str:
    t = t or ""
    if len(t) <= _HEAD + _TAIL + 20:
        return t
    return t[:_HEAD] + "\n[…]\n" + t[-_TAIL:]


def _card(x: dict, n: int | None = None) -> str:
    head = f"[{n}] " if n else ""
    return f"{head}Банк: {x.get('bank') or '—'}.\nТекст:\n{x['text']}"


# ── каскад s7 (03.11.2026) ───────────────────────────────────────────────────
# s6: две дешёвые модели и арбитр по всем срезам, потом для каждого среза — пара
# сильных моделей на отзывы с признаком. Ручная проверка 03.11 показала, что беда
# была не в моделях, а в определениях («выезд инженера к бизнес-клиенту» прямо
# входил в выездной сервис), а замер качества шёл моделью против модели (0,88–1,0
# при ручных 0,44–0,73). s7 — определения «входит / не входит» и такой каскад:
#   A — сильная модель по пакету со всеми карточками: решение, пункт, цитата;
#   B — вторая сильная модель другого семейства: все положительные A и 10%
#       отрицательных (контроль полноты, выбор детерминирован по url);
#   C — третья модель только по срезам, где A и B разошлись; итог — большинство.
# Платим за решения, которые что-то меняют, а не за перезаписываемые проходы.
STRONG_A = os.getenv("REVIEW_SLICES_STRONG_A", "google/gemini-3.1-pro-preview")
STRONG_B = os.getenv("REVIEW_SLICES_STRONG_B", "anthropic/claude-opus-4.8")
STRONG_TIE = os.getenv("REVIEW_SLICES_STRONG_TIE", "openai/gpt-5.4")
CONFIRM_NEG = float(os.getenv("REVIEW_SLICES_CONFIRM_NEG", "0.10"))
BATCH = int(os.getenv("REVIEW_SLICES_BATCH", "8"))
# ₽ за 1 млн токенов (вход, выход) — тарифы облака из /v1/models (14.10.2026)
_PRICES_EXTRA = {"google/gemini-3.1-pro-preview": (341.6, 2049.6),
                 "anthropic/claude-opus-4.8": (854.0, 4270.0),
                 "openai/gpt-5.4": (427.0, 2562.0)}
for _m in (STRONG_A, STRONG_B, STRONG_TIE):
    RA._MAX_TOKENS.setdefault(_m, 8000)
    RA._SEM.setdefault(_m, int(os.getenv("REVIEW_SLICES_SEM_STRONG", "16")))


def _rub(model: str, tin: int, tout: int) -> float:
    pin, pout = RA.PRICES.get(model) or _PRICES_EXTRA.get(model) or (0.0, 0.0)
    return tin / 1e6 * pin + tout / 1e6 * pout


def confirm_negative(url: str, share: float = CONFIRM_NEG) -> bool:
    """Отрицательный ответ A всё равно идёт ко второй модели — для доли share отзывов,
    выбор по хэшу url (одинаков между прогонами): так видна полнота каскада."""
    h = int(hashlib.blake2b((url or "").encode(), digest_size=4).hexdigest(), 16)
    return (h % 10000) < share * 10000


async def _ask_batch(c: RA.Caller, model: str, items: list[dict]) -> tuple[dict[int, dict], float]:
    """Одна модель по пакету: {номер в пакете: flags} и рубли."""
    user = ("Реши по каждому отзыву и каждому срезу.\n\n"
            + "\n\n".join(_card(x, k + 1) for k, x in enumerate(items)))
    d, tin, tout = await c.ask(model, user)
    arr = [it for it in ((d or {}).get("items") or []) if isinstance(it, dict)] if isinstance(d, dict) else []
    by_n = {int(it["n"]): it for it in arr if str(it.get("n", "")).isdigit()}
    out = {}
    for k in range(len(items)):
        v = flags(by_n.get(k + 1))
        if v is not None:
            out[k] = v
    return out, _rub(model, tin, tout)


def decide(a: dict | None, b: dict | None, c: dict | None) -> tuple[dict, dict, str]:
    """Итог по срезам из ответов A, B (может отсутствовать — подтверждение не запрашивали)
    и C (только по спорным). Возвращает (final, evidence, status).
    • B не спрашивали — решает A (status single);
    • A и B согласны — их решение (agree);
    • разошлись — большинство из трёх (arbitrated); C молчит — осторожно «нет» (failed_tie)."""
    if a is None:
        return {}, {}, "failed"
    final, ev, status = {}, {}, "single" if b is None else "agree"
    for k in KEYS:
        if b is None or a[k] == b[k]:
            final[k] = a[k]
            src = a if a[k] else None
        else:
            if c is None:
                final[k] = False
                status = "failed_tie"
                src = None
            else:
                votes = [a[k], b[k], c[k]]
                final[k] = sum(votes) >= 2
                status = "arbitrated" if status != "failed_tie" else status
                src = next((v for v in (a, b, c) if v[k] == final[k] and v[k]), None)
        if final[k] and src:
            e = (src.get("evidence") or {}).get(k) or {}
            if not e:          # решение «да» другой модели, а основание у A или B
                e = next(((v.get("evidence") or {}).get(k) for v in (a, b, c) if v and v.get(k)
                          and (v.get("evidence") or {}).get(k)), {}) or {}
            ev[k] = e
    return final, ev, status


async def _process(ca: RA.Caller, items: list[dict]) -> tuple[list[dict], float]:
    ra, rub = await _ask_batch(ca, STRONG_A, items)
    # вторая модель: все положительные A и контрольная доля отрицательных
    need_b = [k for k in range(len(items)) if k in ra
              and (any(ra[k][key] for key in KEYS) or confirm_negative(items[k]["url"]))]
    rb: dict[int, dict] = {}
    if need_b:
        got, r = await _ask_batch(ca, STRONG_B, [items[k] for k in need_b])
        rub += r
        rb = {need_b[j]: v for j, v in got.items()}
    # третья — где разошлись (одним пакетом)
    need_c = [k for k in rb if any(ra[k][key] != rb[k][key] for key in KEYS)]
    rc: dict[int, dict] = {}
    if need_c:
        got, r = await _ask_batch(ca, STRONG_TIE, [items[k] for k in need_c])
        rub += r
        rc = {need_c[j]: v for j, v in got.items()}
    rows = []
    share = rub / max(1, len(items))
    for k, x in enumerate(items):
        a = ra.get(k)
        if a is None:
            continue                      # A не ответила — отзыв уйдёт в следующий прогон
        b = rb.get(k) if k in need_b else None
        if k in need_b and b is None:
            status_b = "b_missing"
        else:
            status_b = ""
        final, ev, status = decide(a, b, rc.get(k))
        if status_b:
            status = status_b
        for key, e in ev.items():         # цитата — дословно из текста или пусто
            q, ok = RA.fix_quote(e.get("quote") or "", x["text"]) if e.get("quote") else ("", False)
            e["quote"], e["quote_ok"] = q, ok
        show = lambda v: json.dumps({key: v[key] for key in KEYS} | {"excluded": v.get("excluded") or {}},  # noqa: E731
                                    ensure_ascii=False) if v else None
        rows.append({"url": x["url"], "schema": SCHEMA, "tags": tags_of(final), "status": status,
                     "a": show(a), "b": show(b), "arb": show(rc.get(k)),
                     "evidence": json.dumps(ev, ensure_ascii=False) if ev else None,
                     "quote": next((e["quote"] for e in ev.values() if e.get("quote")), ""),
                     "rub": round(share, 4)})
    if rows:
        with db.session() as s:
            s.execute(_UPSERT, rows)
    return rows, rub


def _spent(today: bool) -> float:
    with db.session() as s:
        return float(s.execute(text(
            "SELECT coalesce(sum(cost_rub), 0) FROM review_slice WHERE schema = :v"
            + (" AND created_at >= date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')"
               " AT TIME ZONE 'Europe/Moscow'" if today else "")), {"v": SCHEMA}).scalar() or 0)


def _todo(limit: int, urls: list[str] | None = None) -> list[dict]:
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT i.url, i.review_id, i.source, i.bank FROM review_index i
             WHERE i.kind IN ('complaint', 'mixed')
               AND (CAST(:u AS text[]) IS NOT NULL AND i.url = ANY(:u)
                    OR CAST(:u AS text[]) IS NULL AND i.dt >= CAST(:since AS timestamp)
                       AND i.dt <= now() AND {candidate_sql()})
               AND NOT EXISTS (SELECT 1 FROM review_slice s WHERE s.url = i.url AND s.schema = :v
                                  AND s.status NOT IN ('failed', 'failed_tie', 'b_missing'))
             ORDER BY i.dt DESC NULLS LAST LIMIT :n"""),
            {"u": urls, "since": SINCE, "v": SCHEMA, "n": limit}).mappings().all()
    return [dict(r) for r in rows]


# Ключ — (url, версия): новая версия определений не затирает прежнюю, пока не
# переключена (migrations/102). Неудавшиеся решения повторяются следующим прогоном.
_UPSERT = text("""
    INSERT INTO review_slice (url, schema, tags, status, a, b, arb, evidence, quote, cost_rub)
    VALUES (:url, :schema, :tags, :status, CAST(:a AS jsonb), CAST(:b AS jsonb), CAST(:arb AS jsonb),
            CAST(:evidence AS jsonb), :quote, :rub)
    ON CONFLICT (url, schema) DO UPDATE SET tags = EXCLUDED.tags,
        status = EXCLUDED.status, a = EXCLUDED.a, b = EXCLUDED.b, arb = EXCLUDED.arb,
        evidence = EXCLUDED.evidence, quote = EXCLUDED.quote,
        cost_rub = review_slice.cost_rub + EXCLUDED.cost_rub, created_at = now()
""")


def _try_lock():
    if db._engine is None:
        db.init()
    conn = db._engine.connect()
    got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar())
    conn.commit()
    if not got:
        conn.close()
        return None
    return conn


async def run(limit: int = 2000, urls: list[str] | None = None, daily: bool = True) -> dict:
    """Размечает срезы у неразмеченных кандидатов, от свежих к старым. Потолок трат —
    за сутки по Москве (daily) или общий по версии; второй процесс не стартует."""
    lock = await asyncio.to_thread(_try_lock)
    if lock is None:
        return {"skipped": "locked"}
    t0 = time.time()
    done = rub = 0
    stop = None
    try:
        from .bankiru_fts import bodies_for
        todo = await asyncio.to_thread(_todo, limit, urls)
        if not todo:
            return {"done": 0}
        cap = DAILY_CAP_RUB if daily else CAP_RUB
        base = await asyncio.to_thread(_spent, daily)
        c = RA.Caller(RA.Ledger())
        c.sys = SYSTEM
        sem = asyncio.Semaphore(INFLIGHT)

        async def one(part):
            nonlocal done, rub, stop
            if stop:
                return
            async with sem:
                if base + rub >= cap:
                    stop = "cap"
                    return
                bodies = await asyncio.to_thread(bodies_for, part)
                items = []
                for x in part:
                    t = (bodies.get(x["url"]) or {}).get("text") or ""
                    if len(t.strip()) >= 20:
                        items.append({**x, "text": clip(RA.scrub(t))})
                if not items:
                    return
                rows, r = await _process(c, items)
                done += len(rows)
                rub += r

        await asyncio.gather(*(one(todo[k:k + BATCH]) for k in range(0, len(todo), BATCH)))
        return {"done": done, "rub": round(rub, 2), "stop": stop, "seconds": round(time.time() - t0, 1)}
    finally:
        try:
            lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            lock.commit()
        except Exception:  # noqa: BLE001
            lock.invalidate()
        finally:
            lock.close()


def active() -> dict[str, str]:
    """Активная версия каждого среза (review_slice_active). Без таблицы — текущая SCHEMA."""
    try:
        with db.session() as s:
            got = dict(s.execute(text("SELECT key, schema FROM review_slice_active")).all())
    except Exception:  # noqa: BLE001 — миграция ещё не накатана
        got = {}
    return {k: got.get(k, SCHEMA) for k in KEYS}


# Срезы отзыва в индексе = срезы из АКТИВНОЙ версии каждого ключа. Если у отзыва ещё
# нет разметки активной версии (переключили на s7, а этот отзыв s7 не дошёл) — берётся
# прежняя версия этого ключа: иначе отзыв на время пересчёта выпал бы из среза.
# Отметка аудитора «не относится», подтверждённая нами (decision = 'excluded'), снимает
# срез с отзыва при любой версии разметки — и переразметка его не вернёт.
_DESIRED = """
    WITH act AS (SELECT key, schema FROM review_slice_active),
    v AS (SELECT s.url, s.schema, s.tags FROM review_slice s
           WHERE s.status NOT IN ('failed', 'failed_tie', 'b_missing') {url_filter}),
    x AS (
      SELECT v.url, a.key,
             bool_or(a.key = ANY(v.tags)) FILTER (WHERE v.schema = a.schema) AS act_on,
             bool_or(true) FILTER (WHERE v.schema = a.schema) AS has_act,
             (array_agg(a.key = ANY(v.tags) ORDER BY v.schema DESC)
                FILTER (WHERE v.schema <> a.schema))[1] AS prev_on
        FROM v CROSS JOIN act a GROUP BY v.url, a.key)
    SELECT url, coalesce(array_agg(key ORDER BY key) FILTER (
               WHERE CASE WHEN has_act THEN act_on ELSE coalesce(prev_on, false) END
                 AND NOT EXISTS (SELECT 1 FROM review_slice_feedback f
                                  WHERE f.url = x.url AND f.key = x.key AND f.decision = 'excluded')),
           '{{}}') AS tags
      FROM x GROUP BY url
"""


def apply_to_index(chunk: int = 1000, urls: list[str] | None = None) -> int:
    """Срезы активных версий → review_index.slices порциями (фоновые задачи тоже пишут
    индекс — крупный UPDATE ловил взаимную блокировку). Идемпотентно: пишет только
    отличающиеся строки."""
    with db.session() as s:
        if urls is None:
            urls = s.execute(text("SELECT DISTINCT url FROM review_slice")).scalars().all()
    n = 0
    for k in range(0, len(urls), chunk):
        part = list(urls[k:k + chunk])
        for attempt in range(5):
            try:
                with db.session() as s:
                    n += s.execute(text(f"""
                        UPDATE review_index i SET slices = d.tags
                          FROM ({_DESIRED.format(url_filter="AND s.url = ANY(:u)")}) d
                         WHERE i.url = d.url AND coalesce(i.slices, '{{}}') IS DISTINCT FROM d.tags"""),
                        {"u": part}).rowcount
                break
            except Exception:  # noqa: BLE001
                time.sleep(2 + attempt * 3)
    if n:
        log.info("срезы отзывов: в индекс перенесено %d", n)
    return n


READY_SHARE = float(os.getenv("REVIEW_SLICES_READY", "0.97"))


def _key_candidates(key: str) -> str:
    """Кандидаты одного среза (для готовности по срезу)."""
    parts = [f"i.tsv @@ to_tsquery('russian', '{_TSQ[key]}')"]
    if key == "us":
        parts += [_HINT[0], _HINT[1]]
    if key == "field":
        parts += [_HINT[2]]
    return "(" + " OR ".join(parts) + ")"


def coverage(days: int = 365) -> dict:
    """Готовность каждого среза: доля размеченных его кандидатов в активной версии за
    окно. Пока разметка не догнала (массовый прогон, новая версия), срез в списке
    продуктов не показывается: недоразмеченный срез показал бы заниженные числа.
    Возвращает {"ready": все ли готовы, "keys": {срез: {candidates, tagged, share, ready}}}."""
    act = active()
    keys: dict[str, dict] = {}
    with db.session() as s:
        for key in KEYS:
            n, tagged = s.execute(text(f"""
                SELECT count(*), count(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM review_slice s WHERE s.url = i.url AND s.schema = :v
                              AND s.status NOT IN ('failed', 'failed_tie', 'b_missing')))
                  FROM review_index i
                 WHERE i.kind IN ('complaint', 'mixed') AND i.dt >= now() - make_interval(days => :d)
                   AND i.dt <= now() AND i.dt >= CAST(:since AS timestamp) AND {_key_candidates(key)}"""),
                {"v": act[key], "d": days, "since": SINCE}).one()
            share = (tagged / n) if n else 1.0
            keys[key] = {"candidates": int(n), "tagged": int(tagged), "share": round(share, 4),
                         "ready": share >= READY_SHARE, "schema": act[key]}
    return {"ready": all(v["ready"] for v in keys.values()), "keys": keys}


# ── «Почему жалоба в срезе» и отметки «не относится» ─────────────────────────
# Аудитор видит основание — пункт определения и дословную цитату — и может отметить
# «не относится». Отметка не снимает срез сразу: решает команда (decide_feedback),
# иначе один клик менял бы числа у всех. Подтверждённая — снимает срез навсегда.

def evidence_for(urls: list[str]) -> dict[str, list[dict]]:
    """{url: [{key, label, clause, quote, schema, flagged}]} — срезы отзыва из активных
    версий (как в индексе) с основанием. У s6 основания по срезу нет — только общая
    цитата строки, если она дословная."""
    urls = [u for u in dict.fromkeys(urls or []) if u]
    if not urls:
        return {}
    try:
        with db.session() as s:
            # как в _DESIRED: срез из индекса, основание — из активной версии, а пока
            # её нет у отзыва — из самой свежей, где срез стоит
            rows = s.execute(text("""
                SELECT DISTINCT ON (i.url, k.key) i.url, k.key, s.schema,
                       s.evidence -> k.key AS ev, s.quote,
                       (SELECT count(*) FROM review_slice_feedback f
                         WHERE f.url = i.url AND f.key = k.key AND f.decision IS NULL) AS flagged
                  FROM review_index i
                 CROSS JOIN LATERAL unnest(coalesce(i.slices, '{}')) AS k(key)
                  JOIN review_slice s ON s.url = i.url AND k.key = ANY(s.tags)
                       AND s.status NOT IN ('failed', 'failed_tie', 'b_missing')
                  LEFT JOIN review_slice_active a ON a.key = k.key
                 WHERE i.url = ANY(:u)
                 ORDER BY i.url, k.key, (s.schema = a.schema) DESC NULLS LAST, s.schema DESC"""),
                {"u": urls}).mappings().all()
    except Exception as e:  # noqa: BLE001 — карточка не должна падать без основания
        log.warning("срезы: основание не прочиталось (%s)", e)
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        ev = r["ev"] if isinstance(r["ev"], dict) else {}
        quote = ev.get("quote") if ev.get("quote_ok") else None
        if r["schema"] == "s6" and not quote:
            quote = r["quote"] or None
        out.setdefault(r["url"], []).append({
            "key": r["key"], "label": LABELS.get(r["key"], r["key"]), "schema": r["schema"],
            "clause": ev.get("clause") or None, "quote": quote, "flagged": int(r["flagged"] or 0)})
    for v in out.values():
        v.sort(key=lambda x: KEYS.index(x["key"]) if x["key"] in KEYS else 99)
    return out


def flag(url: str, key: str, username: str, note: str | None = None) -> bool:
    """Отметка «не относится» от аудитора; повторная — обновляет комментарий и снова
    ставит на проверку, если команда раньше оставила срез. Подтверждённую не трогает."""
    if key not in KEYS or not url or not username:
        return False
    with db.session() as s:
        s.execute(text("""
            INSERT INTO review_slice_feedback (url, key, username, note) VALUES (:u, :k, :n, :c)
            ON CONFLICT (url, key, username) DO UPDATE SET note = coalesce(EXCLUDED.note, review_slice_feedback.note),
                created_at = CASE WHEN review_slice_feedback.decision IS NULL
                                  THEN review_slice_feedback.created_at ELSE now() END,
                decision = NULL, decided_by = NULL, decided_at = NULL
             WHERE review_slice_feedback.decision IS DISTINCT FROM 'excluded'"""),
            {"u": url, "k": key, "n": username, "c": (note or "").strip()[:1000] or None})
    return True


def open_flags(limit: int = 200) -> list[dict]:
    """Отметки, ждущие решения команды, — с основанием среза и началом текста."""
    with db.session() as s:
        rows = s.execute(text("""
            SELECT f.id, f.url, f.key, f.username, f.note, f.created_at, i.bank, i.dt,
                   left(coalesce(a.summary, ''), 400) AS summary
              FROM review_slice_feedback f
              LEFT JOIN review_index i ON i.url = f.url
              LEFT JOIN LATERAL (SELECT summary FROM review_annotation x WHERE x.url = f.url
                                  ORDER BY x.created_at DESC LIMIT 1) a ON true
             WHERE f.decision IS NULL ORDER BY f.created_at LIMIT :n"""), {"n": limit}).mappings().all()
    ev = evidence_for([r["url"] for r in rows])
    out = []
    for r in rows:
        e = next((x for x in ev.get(r["url"], []) if x["key"] == r["key"]), None)
        out.append({**{k: r[k] for k in ("id", "url", "key", "username", "note", "bank", "summary")},
                    "label": LABELS.get(r["key"], r["key"]), "created_at": str(r["created_at"]),
                    "date": str(r["dt"])[:10] if r["dt"] else None, "evidence": e})
    return out


def decide_flag(fid: int, decision: str, by: str) -> dict | None:
    """Решение команды по отметке: excluded — срез снимается с отзыва (и в индексе
    сразу), kept — остаётся. Решение по одной отметке закрывает все отметки этого
    отзыва и среза."""
    if decision not in ("excluded", "kept"):
        return None
    with db.session() as s:
        row = s.execute(text("SELECT url, key FROM review_slice_feedback WHERE id = :i"),
                        {"i": fid}).mappings().first()
        if not row:
            return None
        s.execute(text("""
            UPDATE review_slice_feedback SET decision = :d, decided_by = :b, decided_at = now()
             WHERE url = :u AND key = :k AND decision IS NULL"""),
            {"d": decision, "b": by, "u": row["url"], "k": row["key"]})
    apply_to_index(urls=[row["url"]])
    return {"url": row["url"], "key": row["key"], "decision": decision}

