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
    # «приехал представитель», «встреча с представителем» — выезд без слов «курьер» и
    # «выезд»: тест пропусков 04.11.2026 (400 жалоб вне кандидатов, слепая разметка)
    "field": "курьер:* | выездн:* | выезд:* | доставк:* | (представител:* & (приех:* | приезжа:* | встреч:*))",
    # «своё дело» фразой: лексема «сво» одна у «своё», «свой» и «СВО»
    "selfemp": "самозанят:* | нпд | сво <-> дел:*",
    # «дети» — точной лексемой ('дет'), не префиксом: префикс ловил бы «детали»
    # «образовательный кредит», «вуз» — три из трёх пропусков среза в тесте 04.11.2026
    "youth": ("детск:* | подростк:* | молодежн:* | школьник:* | студент:* | несовершеннолетн:* | "
              "сберкидс:* | ребен:* | дети | образовательн:* | вуз"),
    "svo": "военнослужащ:* | мобилизова:* | (специальн:* <-> военн:*) | участник:* <-> сво",
}
_HINT = ("'atm' = ANY(coalesce(i.channels, '{}'))", "i.issue = 'cash_atm'",
         "'courier' = ANY(coalesce(i.channels, '{}'))")


# ── Сито по смыслу (04.11.2026) ─────────────────────────────────────────────
# Кандидаты по словам не видят жалоб без ключевых слов: «таксую», «карта СВОи»,
# «оплата обучения». Тест пропусков 04.11.2026: 7 срезовых жалоб на 400 случайных
# некандидатов (1,75 % — порядка 1 600 на 92 тыс.). Сито — лёгкая модель по изложению
# жалобы из разметки, на полноту: отмечает «может относиться», решает каскад. На тех
# 400: отмечено 11, из них 5 из 7 срезовых (оба пропуска — «приехал представитель»,
# их теперь ловит _TSQ); по полному тексту — 52 отмечено и 6 из 7, то есть вчетверо
# больше платной разметки ради одной находки. Сходство векторов с размеченными
# (kNN) пробовали: у выездного тысячи шумных кандидатов, молодёжь и самозанятые почти
# не отделяются от фона.
SIEVE = os.getenv("REVIEW_SLICES_SIEVE", "1") == "1"
SIEVE_MODEL = os.getenv("REVIEW_SLICES_SIEVE_MODEL", "google/gemini-3.1-flash-lite")
SIEVE_BATCH = int(os.getenv("REVIEW_SLICES_SIEVE_BATCH", "30"))
SIEVE_DAILY_RUB = float(os.getenv("REVIEW_SLICES_SIEVE_DAILY_RUB", "800"))
SIEVE_INFLIGHT = int(os.getenv("REVIEW_SLICES_SIEVE_INFLIGHT", "8"))
_SIEVE_SPEND = "sieve"          # строка журнала трат review_slice_spend
_SIEVE_ANY = ("EXISTS (SELECT 1 FROM review_slice_sieve sv WHERE sv.url = i.url"
              " AND cardinality(sv.keys) > 0)")


def _sieve_key(key: str) -> str:
    return f"EXISTS (SELECT 1 FROM review_slice_sieve sv WHERE sv.url = i.url AND '{key}' = ANY(sv.keys))"


def candidate_sql(sieve: bool | None = None) -> str:
    """Условие «отзыв — кандидат хотя бы в один срез» (алиас индекса — i): слова и
    каналы, а с ситом (SIEVE, миграция 110) — и отмеченные им."""
    parts = [f"i.tsv @@ to_tsquery('russian', '{q}')" for q in _TSQ.values()] + list(_HINT)
    if SIEVE if sieve is None else sieve:
        parts.append(_SIEVE_ANY)
    return "(" + " OR ".join(parts) + ")"


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
            # A не ответила — строка failed с её долей трат: повторится следующим прогоном,
            # а деньги попадут в суточный потолок (иначе тот же пакет оплачивался каждый
            # час мимо потолка, ревью 04.11.2026)
            rows.append({"url": x["url"], "schema": SCHEMA, "tags": [], "status": "failed",
                         "a": None, "b": None, "arb": None, "evidence": None, "quote": "",
                         "rub": round(share, 4)})
            continue
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
    _log_spend(rub)
    return rows, rub


_TODAY = "(now() AT TIME ZONE 'Europe/Moscow')::date"


def _spent(today: bool) -> float:
    """Траты версии: всего (по строкам) или за сегодня по Москве (по журналу трат —
    review_slice_spend, миграция 105; без него — как раньше, по строкам)."""
    with db.session() as s:
        if today:
            try:
                return float(s.execute(text(f"""SELECT coalesce(sum(rub), 0) FROM review_slice_spend
                                                WHERE schema = :v AND day = {_TODAY}"""),
                                       {"v": SCHEMA}).scalar() or 0)
            except Exception:  # noqa: BLE001 — миграция ещё не накатана
                s.rollback()
        return float(s.execute(text(
            "SELECT coalesce(sum(cost_rub), 0) FROM review_slice WHERE schema = :v"
            + (" AND created_at >= date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')"
               " AT TIME ZONE 'Europe/Moscow'" if today else "")), {"v": SCHEMA}).scalar() or 0)


def _log_spend(rub: float, schema: str | None = None) -> None:
    if rub <= 0:
        return
    try:
        with db.session() as s:
            s.execute(text(f"""INSERT INTO review_slice_spend (day, schema, rub) VALUES ({_TODAY}, :v, :r)
                               ON CONFLICT (day, schema) DO UPDATE SET rub = review_slice_spend.rub + EXCLUDED.rub"""),
                      {"v": schema or SCHEMA, "r": round(rub, 4)})
    except Exception:  # noqa: BLE001 — журнал трат не должен ронять разметку
        log.warning("срезы: траты не записались в журнал", exc_info=True)


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


SIEVE_SYSTEM = (
    "Ты отбираешь жалобы клиентов банка, которые МОГУТ относиться к особым срезам аудита. Это первичное "
    "сито: лучше отметить лишнее, чем пропустить, — дальше каждую отмеченную проверит сильная модель. "
    "Срез — если по изложению видно или правдоподобно, что суть жалобы подходит под определение, даже если "
    "ключевых слов нет (таксист, курьер-доставщик, мастер, ремесленник — возможно самозанятый; студент, "
    "школьник, ребёнок, образовательный кредит — молодёжь; участник СВО, мобилизованный, «СВОи» — СВО и т. п.)."
    "\n\nСРЕЗЫ\n" + "\n".join(f"{k} — {v[0]}: {v[1]}" for k, v in SLICES.items())
    + '\n\nОтвет строго JSON: {"items":[{"n":1,"slices":["<ключ среза>", ...]}, ...]} по каждому n; '
      "пустой список — ни к одному.")
RA._MAX_TOKENS.setdefault(SIEVE_MODEL, 4000)
RA._SEM.setdefault(SIEVE_MODEL, int(os.getenv("REVIEW_SLICES_SIEVE_SEM", "16")))


def _sieve_todo(limit: int) -> list[dict]:
    """Жалобы окна вне кандидатов по словам, с изложением, ещё не просеянные."""
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT i.url, a.summary FROM review_index i
            JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
            WHERE i.kind IN ('complaint', 'mixed') AND i.dt >= CAST(:since AS timestamp)
              -- coalesce: у жалобы без темы (issue NULL) условие по словам — NULL, а не
              -- «нет», и NOT отбросил бы её из сита молча
              AND i.dt <= now() AND NOT coalesce({candidate_sql(sieve=False)}, false)
              AND coalesce(a.summary, '') <> ''
              AND NOT EXISTS (SELECT 1 FROM review_slice_sieve sv WHERE sv.url = i.url)
            ORDER BY i.dt DESC NULLS LAST, i.url LIMIT :n"""),
            {"sv": RA.SCHEMA, "since": SINCE, "n": limit}).mappings().all()
    return [dict(r) for r in rows]


def sieve_parse(d, n: int) -> dict[int, list[str]]:
    """Ответ сита → {номер: [срезы]}; номера без ответа в словарь не попадают (жалоба
    просеется в следующий раз), незнакомые ключи отбрасываются."""
    arr = (d or {}).get("items") if isinstance(d, dict) else None
    out: dict[int, list[str]] = {}
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict) or not str(it.get("n", "")).isdigit():
            continue
        k = int(it["n"])
        sl = it.get("slices")
        if 1 <= k <= n and isinstance(sl, list):
            out[k - 1] = [x for x in KEYS if x in sl]
    return out


async def sieve(limit: int = 3000, *, daily: bool = True, caller=None) -> dict:
    """Просеять до limit жалоб вне кандидатов по словам: отмеченные моделью становятся
    кандидатами (candidate_sql), решение — за каскадом. Потолок трат — свой, за сутки
    по Москве (SIEVE_DAILY_RUB, строка «sieve» в журнале трат); daily=False — без него."""
    if not SIEVE:
        return {"skipped": "off"}
    todo = await asyncio.to_thread(_sieve_todo, limit)
    if not todo:
        return {"sieved": 0}
    spent = 0.0
    if daily:
        try:
            with db.session() as s:
                spent = float(s.execute(text(f"""SELECT coalesce(sum(rub), 0) FROM review_slice_spend
                                                 WHERE schema = :v AND day = {_TODAY}"""),
                                        {"v": _SIEVE_SPEND}).scalar() or 0)
        except Exception:  # noqa: BLE001
            spent = 0.0
    c = caller or RA.Caller(RA.Ledger())
    c.sys = SIEVE_SYSTEM            # свой Caller: общий с чужой подсказкой не делим
    sem = asyncio.Semaphore(max(1, SIEVE_INFLIGHT))
    st = {"sieved": 0, "flagged": 0, "unanswered": 0, "rub": 0.0, "stop": None}
    per: dict[str, int] = {}

    async def one(part: list[dict]) -> None:
        async with sem:
            if daily and spent + st["rub"] >= SIEVE_DAILY_RUB:
                st["stop"] = "cap"
                return
            user = "\n\n".join(f"[{k + 1}] {(x['summary'] or '')[:1500]}" for k, x in enumerate(part))
            d, tin, tout = await c.ask(SIEVE_MODEL, user)
            st["rub"] += _rub(SIEVE_MODEL, tin, tout)
            got = sieve_parse(d, len(part))
            rows = [{"u": part[k]["url"], "k": keys, "m": SIEVE_MODEL} for k, keys in got.items()]
            st["unanswered"] += len(part) - len(rows)
            if rows:
                def write():
                    with db.session() as s:
                        s.execute(text("""INSERT INTO review_slice_sieve (url, keys, model)
                                          VALUES (:u, :k, :m)
                                          ON CONFLICT (url) DO UPDATE SET keys = EXCLUDED.keys,
                                              model = EXCLUDED.model, at = now()"""), rows)
                await asyncio.to_thread(write)
            st["sieved"] += len(rows)
            for r in rows:
                if r["k"]:
                    st["flagged"] += 1
                    for k in r["k"]:
                        per[k] = per.get(k, 0) + 1

    await asyncio.gather(*(one(todo[k:k + SIEVE_BATCH]) for k in range(0, len(todo), SIEVE_BATCH)))
    await asyncio.to_thread(_log_spend, st["rub"], _SIEVE_SPEND)
    st["rub"] = round(st["rub"], 2)
    st["per_key"] = per
    return st


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
    if SIEVE:
        parts.append(_sieve_key(key))
    return "(" + " OR ".join(parts) + ")"


def coverage(days: int = 365) -> dict:
    """Готовность каждого среза: доля размеченных его кандидатов в активной версии за
    окно. Пока разметка не догнала (массовый прогон, новая версия), срез в списке
    продуктов не показывается: недоразмеченный срез показал бы заниженные числа.
    Размеченным считается кандидат с годной строкой активной версии или текущей SCHEMA —
    той, что пишет разметка: пока срез на прежней версии, новые жалобы получают строки
    только новой, и индекс берёт их (_DESIRED); без этого готовность падала день за днём
    и срез пропадал из списка (ревью 04.11.2026).
    Возвращает {"ready": все ли готовы, "keys": {срез: {candidates, tagged, share, ready}}}."""
    act = active()
    keys: dict[str, dict] = {}
    with db.session() as s:
        for key in KEYS:
            n, tagged = s.execute(text(f"""
                SELECT count(*), count(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM review_slice s WHERE s.url = i.url AND s.schema IN (:v, :cur)
                              AND s.status NOT IN ('failed', 'failed_tie', 'b_missing')))
                  FROM review_index i
                 WHERE i.kind IN ('complaint', 'mixed') AND i.dt >= now() - make_interval(days => :d)
                   AND i.dt <= now() AND i.dt >= CAST(:since AS timestamp) AND {_key_candidates(key)}"""),
                {"v": act[key], "cur": SCHEMA, "d": days, "since": SINCE}).one()
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
                       s.evidence -> k.key AS ev, s.quote, s.tags,
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
        # у s6 цитата одна на строку и с текстом не сверялась (fix_quote появился в s7):
        # дословным основанием её не показываем — только пункт и цитату s7
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
        # отметка — только к жалобе из индекса, у которой этот срез стоит: адрес приходит
        # от пользователя и потом показывается команде ссылкой (ревью 04.11.2026)
        ok = s.execute(text("SELECT 1 FROM review_index WHERE url = :u AND :k = ANY(coalesce(slices, '{}'))"),
                       {"u": url, "k": key}).first()
        if not ok:
            return False
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
        n = s.execute(text("""
            UPDATE review_slice_feedback SET decision = :d, decided_by = :b, decided_at = now()
             WHERE url = :u AND key = :k AND decision IS NULL"""),
            {"d": decision, "b": by, "u": row["url"], "k": row["key"]}).rowcount
    if not n:              # уже решили (другой человек из команды по устаревшему списку)
        return {"url": row["url"], "key": row["key"], "decision": None, "already": True}
    apply_to_index(urls=[row["url"]])
    return {"url": row["url"], "key": row["key"], "decision": decision}


# Замер на эталоне 04.11.2026: 260 жалоб (100 положительных s6 и 160 отвергнутых
# кандидатов), два слепых разметчика и арбитр (согласие 98,8%); field переразмечен
# вслепую по окончательному определению. (точность, полнота) s7; s6 — точность.
GOLD_0411 = {"payroll": (0.92, 0.96, 0.73), "us": (0.95, 0.90, 0.60), "field": (0.85, 0.97, 0.73),
             "selfemp": (0.94, 1.0, None), "youth": (1.0, 1.0, None), "svo": (1.0, 0.85, None)}


def status() -> dict:
    """Состояние срезов для «Пульса»: активная версия, готовность разметки, траты,
    отметки «не относится» и замер на эталоне."""
    cov = coverage()
    with db.session() as s:
        spend = {r[0]: (int(r[1]), float(r[2] or 0)) for r in s.execute(text(
            "SELECT schema, count(*), sum(cost_rub) FROM review_slice GROUP BY 1")).all()}
        act = {r[0]: (r[1], r[2], r[3]) for r in s.execute(text(
            "SELECT key, schema, switched_at, note FROM review_slice_active")).all()}
        flags = dict(s.execute(text("""SELECT key, count(*) FROM review_slice_feedback
                                        WHERE decision IS NULL GROUP BY 1""")).all())
    keys = []
    for k in KEYS:
        c = cov["keys"].get(k) or {}
        g = GOLD_0411.get(k)
        sch, at, note = act.get(k, (SCHEMA, None, None))
        keys.append({"key": k, "label": LABELS[k], "schema": sch,
                     "switched_at": at.isoformat() if at else None, "note": note,
                     "candidates": c.get("candidates"), "tagged": c.get("tagged"),
                     "share": c.get("share"), "ready": c.get("ready"),
                     "open_flags": int(flags.get(k, 0)),
                     "gold": {"precision": g[0], "recall": g[1], "precision_s6": g[2]} if g else None})
    sv = None
    try:
        with db.session() as s:
            r = s.execute(text("""SELECT count(*), count(*) FILTER (WHERE cardinality(keys) > 0),
                                         (SELECT coalesce(sum(rub), 0) FROM review_slice_spend WHERE schema = :v)
                                    FROM review_slice_sieve"""), {"v": _SIEVE_SPEND}).one()
            sv = {"sieved": int(r[0]), "flagged": int(r[1]), "rub": round(float(r[2] or 0))}
    except Exception:  # noqa: BLE001 — до миграции 110 сита нет
        pass
    return {"keys": keys, "spend": {k: {"rows": v[0], "rub": round(v[1])} for k, v in spend.items()},
            "schema": SCHEMA, "sieve": sv}

