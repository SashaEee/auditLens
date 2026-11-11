"""Собственные данные AuditLens в отчёте: аналитика жалоб, жалобы, лазейки.

ЗАЧЕМ. Замер 26.09: на «почему на этой неделе выросли жалобы на чарджбэк»
отчёт написал «факт недельного роста не подтверждается» и «дословных жалоб
клиентов в фактах нет» — при том что вкладка «Отзывы» и быстрый агент
называли сюжет «5 из 15 — билеты на отменённый концерт». Причины:
  • жалобы брались из старого корпуса banki.ru прямым перебором по векторам,
    и у Сбера запрос упирался в таймаут — отчёт не получал ничего;
  • аналитики жалоб (сигналы, нормы, темы с динамикой, эскалация против
    рынка, группы похожих) отчёт не видел вовсе;
  • раздела «Уязвимости» в отчёте не было.

Теперь отчёт берёт данные тем же слоем, что вкладки и быстрый агент
(ai/agent_tools): все площадки, LLM-разметка, проверенные цитаты. Данные
входят как СТРАНИЦЫ ДАННЫХ — короткий датированный текст среза с адресом той
же вкладки — и проходят ту же проверку, что веб: каждое число есть в тексте
страницы, каждая цитата — её подстрока. Факты из них собирает код, а не
модель (правило проекта: числа считает код, модель формулирует). Жалобы
приходят уже с пересказом и дословной цитатой из разметки — пересказывать их
моделью второй раз незачем.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from ...ai import agent_tools as T
from . import runstate

log = logging.getLogger(__name__)

# Стороны фактов. «Наблюдается» — голос клиента (раздел «Голос клиента»),
# «лазейка» — раздел «Уязвимости» (свой раздел отчёта).
OBSERVED = "observed"
LOOPHOLE = "loophole"

_MAX_BANKS = int(os.getenv("GPTR_OWN_BANKS", "4"))
_COMPLAINTS_PER_BANK = int(os.getenv("GPTR_OWN_COMPLAINTS", "25"))
_LOOPHOLES = int(os.getenv("GPTR_OWN_LOOPHOLES", "10"))


@dataclass
class OwnData:
    """Страницы и факты собственных данных одного прогона."""
    pages: dict[str, str] = field(default_factory=dict)       # url → текст
    facts: list[dict] = field(default_factory=list)            # kwargs для FactRegistry.add
    meta: dict[str, dict] = field(default_factory=dict)        # url → заголовок, вид
    scope: dict = field(default_factory=dict)
    complaints: int = 0
    loopholes: int = 0
    market: int = 0
    knowledge: int = 0           # документов из базы знаний (сохранённые копии)
    knowledge_live: int = 0      # страниц базы знаний, перечитанных сегодня вживую
    news: int = 0
    # Что искали в собственных данных и не нашли — строками для «Честных пробелов»
    notes: list[str] = field(default_factory=list)

    def page(self, url: str, title: str, lines: list[str], kind: str) -> str:
        text = "\n".join([title, *lines])
        self.pages[url] = text
        self.meta[url] = {"title": title, "kind": kind}
        return text

    def fact(self, *, subject: str, attribute: str, value: str, verbatim: str, url: str,
             stance: str = OBSERVED, unit: str = "", date: str = "") -> None:
        text = self.pages.get(url, "")
        # Цитата обязана быть подстрокой страницы — иначе критик её снимет,
        # а сверка чисел не найдёт опоры. Проверяем здесь же.
        if len(verbatim) < 12 or verbatim not in text:
            return
        self.facts.append({"subject": subject, "attribute": attribute, "value": value,
                           "unit": unit, "verbatim": verbatim, "url": url,
                           "stance": stance, "date": date, "support": "дословно"})


# ── срез вопроса ─────────────────────────────────────────────────────────────

_SCOPE_SYSTEM = """Ты выбираешь срез СОБСТВЕННЫХ ДАННЫХ AuditLens для аудиторского отчёта.
По вопросу аудитора и плану выбери:
- "product": продукт из перечня, только если вопрос прямо про этот продукт; иначе null.
  Вопрос о теме жалоб без продукта («жалобы на чарджбэк») — null: срез задаёт тема;
- "themes": 0–3 ключа тем жалоб из перечня, только если вопрос про конкретную проблему
  (для «почему растут жалобы на X» — тема X); иначе [];
- "search": 0–2 короткие поисковые фразы СЛОВАМИ КЛИЕНТА, если вопрос про конкретную
  ситуацию или услугу, которой нет ни среди продуктов, ни среди тем («курьер не привёз
  карту», «не пересчитали проценты после досрочного погашения»). Название продукта
  сюда не пиши — его задаёт "product". Иначе [];
- "days": 7 — вопрос про эту неделю или всплеск; 90 — по умолчанию; 180 или 365 —
  если спрошен длинный период;
- "complaints": true почти всегда: жалобы клиентов — реальный опыт с продуктом, и в
  сравнении банков они показывают, что болит у каждого. false — только если вопрос о
  тексте нормативного документа или только о лазейках;
- "loopholes": true почти всегда: схемы обхода условий продукта (раздел «Аудит уязвимостей») —
  предмет аудита и в сравнении банков, и в разборе жалоб. false — только если вопрос
  о тексте нормативного документа;
- "category": категория витрины «Рынок» из перечня (ключ) или null;
- "market": true, если ответу нужны условия продуктов банков — ставки, комиссии,
  сравнение с конкурентами, позиция на рынке; иначе false.
Ответ — строго JSON: {"product": ..., "themes": [...], "search": [...], "days": ...,
"complaints": ..., "loopholes": ..., "category": ..., "market": ...}"""

# Слова подписей продуктов, которые ничего не различают: «карта» есть и в
# дебетовой, и в кредитной, «кредит» — в половине перечня.
_GENERIC = {"карта", "счёт", "счет", "кредит", "бизнес"}


def product_named(label: str, text_: str) -> bool:
    """Продукт назван в тексте: хоть одно различающее слово подписи встречается
    основой. Замер 26.09: на «почему выросли жалобы на чарджбэк» модель
    выбрала «Дебетовую карту» — и отчёт получил 21 жалобу недели вместо 15 по
    сигналу банка. Продукт, которого в вопросе нет, срез сужать не должен."""
    low = (text_ or "").lower()
    stems = [w[:5] for w in re.findall(r"[а-яёa-z]{3,}", label.lower()) if w not in _GENERIC]
    return any(st in low for st in stems)


async def scope_for(client, model: str, question: str, plan) -> dict:
    """Срез собственных данных под вопрос: продукт, темы и категория — из
    перечней; какие данные нужны — решает модель, продукт проверяет код."""
    from .facts import call_model
    user = (f"# Вопрос\n{question}\n\n# Что хочет аудитор\n"
            f"{getattr(plan, 'intent_summary', '') or '—'}\n\n# Предмет\n"
            f"{getattr(plan, 'product', '') or '—'}\n\n# Продукты\n"
            + "; ".join(T.PRODUCT_LABELS) + "\n\n# Темы (ключ — подпись)\n" + T.THEMES_HELP
            + "\n\n# Категории «Рынка» (ключ — подпись)\n" + T.CATEGORIES_HELP)
    kw: dict = {"model": model, "temperature": 0.0, "max_tokens": 300,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": _SCOPE_SYSTEM},
                             {"role": "user", "content": user}],
                "extra_body": {"thinking": {"type": "disabled"}}}
    try:
        resp = await call_model(client, model, kw)
        data = json.loads((resp.choices[0].message.content or "").strip())
    except Exception as e:  # noqa: BLE001 — без среза отчёт строится на сводке банка
        log.info("срез данных: %s — беру сводку по банку", type(e).__name__)
        data = {}
    return normalize_scope(data, question, plan)


def normalize_scope(data: dict, question: str, plan) -> dict:
    product = data.get("product") if data.get("product") in T.PRODUCT_LABELS else None
    # Продукт должен быть назван — в вопросе или в том, как его понял планировщик.
    # Уточнение «подробнее про досрочное погашение» после сравнения потребкредитов
    # продукта не называет, и срез терял его: жалобы шли по всем продуктам банка
    # (живая проверка 11.11). Предмет плана — тоже слова аудитора, только раскрытые.
    named_in = " ".join([question or "", getattr(plan, "product", "") or "",
                         " ".join(getattr(plan, "product_synonyms", None) or [])])
    if product and not product_named(product, named_in):
        log.info("срез данных: продукт «%s» в вопросе не назван — без продукта", product)
        product = None
    themes = [t for t in (data.get("themes") or []) if t in T.THEME_KEYS][:3]
    search = [re.sub(r"\s+", " ", str(x)).strip()[:80] for x in (data.get("search") or [])
              if isinstance(x, str) and len(x.strip()) >= 6][:2]
    try:
        days = int(data.get("days") or 90)
    except (TypeError, ValueError):
        days = 90
    days = min((7, 30, 90, 180, 365), key=lambda d: abs(d - days))
    category = data.get("category") if data.get("category") in T.CATEGORY_IDS else None
    nature = (getattr(plan, "question_nature", "") or "").lower()
    task = getattr(plan, "task", None)

    def flag(key: str, default: bool) -> bool:
        v = data.get(key)
        return default if v is None else v is not False

    if task is None:
        # План без типа задачи (старый формат) — прежнее правило.
        complaints = flag("complaints", True) or nature not in ("regulatory",)
        loopholes = nature != "regulatory"
    else:
        # Жалобы и лазейки — там, где они относятся к вопросу аудитора: сравнение
        # (владелец 26.09: «куда делись разделы с отзывами», «лазейки также не
        # попали»), продукт, жалобы, план проверки, разбор новости. В справке,
        # цифрах рынка и тексте нормы они превращались в общий жалобный фон Сбера
        # (аудит 11.11: раздел жалоб в 76 % отчётов) — там только по прямой просьбе.
        asked_c = bool(_ASKED_COMPLAINTS.search(question or ""))
        asked_l = bool(_ASKED_LOOPHOLES.search(question or ""))
        audit_task = task in ("compare", "product", "complaints", "check", "news")
        complaints = asked_c or bool(getattr(plan, "needs_complaints", False)) or audit_task
        loopholes = asked_l or (audit_task and nature != "regulatory")
    return {"product": product, "themes": themes, "search": search, "days": days,
            "complaints": complaints, "loopholes": loopholes,
            # список банков по числу жалоб — когда вопрос о жалобах или сравнении;
            # в рассказе о продукте Сбера конкурентов аудитор не просил
            "rivals": task in (None, "compare", "complaints"),
            "category": category, "market": flag("market", False) and bool(category)}


_ASKED_COMPLAINTS = re.compile(r"жалоб|отзыв|претензи|обращени|недовольн|клиентск\w+ (опыт|бол)", re.I)
_ASKED_LOOPHOLES = re.compile(r"лазейк|уязвим|мошенн|схем\w* (обход|мошен)|злоупотребл|фрод", re.I)
# Слова, которые есть почти в любой жалобе на банк и предмета вопроса не различают.
_COMMON_WORDS = {"банк", "банка", "банке", "сбер", "сбербанк", "сбербанка", "сбербанке", "карта",
                 "карты", "счёт", "счет", "счета", "кредит", "кредита", "сервис", "сервиса", "услуга",
                 "услуги", "продукт", "продукта", "клиент", "клиента", "клиентов", "клиенты", "жалобы",
                 "жалоб", "отзывы", "отзывов", "проблемы", "проблем", "условия", "условий", "тарифы",
                 "тарифов", "физлиц", "физических", "юрлиц", "россии", "рынок", "рынка", "данные",
                 "информация", "информацию", "анализ", "сравнение", "онлайн", "через", "для"}


def subject_stems(plan, question: str) -> list[str]:
    """Основы слов, по которым жалоба опознаётся как «про предмет вопроса»."""
    src = " ".join([getattr(plan, "product", "") or "",
                    " ".join(getattr(plan, "product_synonyms", None) or [])])
    if not src.strip():
        src = question or ""
    words = [w for w in re.findall(r"[a-zа-яё]{4,}", src.lower()) if w not in _COMMON_WORDS]
    return list(dict.fromkeys(w[:6] if len(w) > 6 else w[:max(4, len(w) - 1)] for w in words))[:12]


def bank_level(plan, scope: dict, question: str) -> bool:
    """Вопрос про жалобы на банк в целом («сколько жалоб на Сбер»), а не про
    продукт: только тогда уместна сводка по всему банку."""
    if getattr(plan, "task", None) is None:
        return True                      # старый формат плана — как раньше
    if scope.get("product") or scope.get("themes"):
        return False
    return getattr(plan, "task", "") == "complaints" and not subject_stems(plan, "")


# ── жалобы ───────────────────────────────────────────────────────────────────

def _j(tool: str, **kw) -> dict:
    return json.loads(T.BY_NAME[tool].fn(**kw))


def _n(x) -> str:
    try:
        return f"{int(round(float(x))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _f(x, d: int = 1) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return (f"{v:.{d}f}".rstrip("0").rstrip(".") if d else f"{v:.0f}").replace(".", ",")


def _pct(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return ("+" if v > 0 else "") + _f(v, 0) + "%"


def _dm(iso: str | None) -> str:
    s = str(iso or "")[:10]
    return f"{s[8:10]}.{s[5:7]}.{s[:4]}" if len(s) == 10 else s


def _bank_name(slug: str, labels: dict[str, str]) -> str:
    return labels.get(slug) or slug


def _complaints_for_bank(od: OwnData, slug: str, bank: str, scope: dict, anchor: bool,
                         signal_themes: list[str]) -> list[str]:
    """Страницы жалоб по банку; возвращает разобранные темы (для точки отсчёта)."""
    product, days = scope["product"], scope["days"]
    base_days = max(days, 90)          # сводке нужен квартал: неделя без базы не читается
    ov = _j("complaints_overview", bank=bank, product=product, days=base_days)
    if ov.get("error") or not ov.get("complaints"):
        return []
    who = bank + (f" · {product}" if product else "")
    url = T.link_reviews(bank, product, days=base_days)
    lines: list[str] = []

    def line(attribute: str, value: str, text_: str, unit: str = ""):
        lines.append(text_)
        return (attribute, value, unit, text_)

    rows = [line("Жалобы: число за период", _n(ov["complaints"]),
                 f"Жалоб за {base_days} дней (по {_dm(ov.get('as_of'))}): {_n(ov['complaints'])}"
                 + (f"; за прошлые {base_days} дней — {_n(ov.get('prev_period'))}, изменение "
                    f"{_pct(ov.get('change_pct'))}" if ov.get("prev_period") is not None else "")
                 + ".")]
    if ov.get("share_of_all_bank_complaints_pct") is not None and not product:
        rows.append(line("Жалобы: доля среди банков", _f(ov["share_of_all_bank_complaints_pct"]),
                         f"Доля в жалобах на все банки: {_f(ov['share_of_all_bank_complaints_pct'])}%, "
                         f"{ov.get('rank_by_complaints')}-е место из {ov.get('banks_in_corpus')}.",
                         "%"))
    if ov.get("escalation_pct") is not None:
        rows.append(line("Жалобы: эскалация", _f(ov["escalation_pct"]),
                         f"Эскалация: {_f(ov['escalation_pct'])}% жалоб — угроза или обращение в ЦБ, "
                         f"суд, надзор (у рынка {_f(ov.get('market_escalation_pct'))}%); уже "
                         f"обратились — {_f(ov.get('escalation_filed_pct'))}%"
                         + ("; отличие от рынка значимо." if ov.get("escalation_differs_significantly")
                            else "; отличие от рынка в пределах случайного."), "%"))
    # Все темы, что отдаёт сводка (до 14), у КАЖДОГО банка и граница списка.
    # Урезанный список (8 у Сбера) отчёт читал как «этой темы у Сбера нет» и
    # объявлял «уникальными» темы конкурентов, которые у Сбера тоже есть (26.09).
    themes_all = ov.get("themes") or []
    for t in themes_all:
        ch = t.get("change_pct")
        rows.append(line(f"Жалобы: тема «{t['label']}»", _n(t["n"]),
                         f"Тема «{t['label']}»: {_n(t['n'])} жалоб ({_f(t.get('pct'))}% всех)"
                         + (f", к прошлому периоду {_pct(ch)}"
                            + (" (значимо)" if t.get("change_significant") else "")
                            if ch is not None else "") + "."))
    if themes_all:
        tail = themes_all[-1].get("n")
        rows.append(line("Жалобы: темы вне списка", _n(tail),
                         f"Выше — {len(themes_all)} тем с наибольшим числом жалоб; у тем вне "
                         f"списка — не больше {_n(tail)} жалоб каждая (это не значит, что "
                         f"таких жалоб нет)."))
    if anchor:
        for fl in [x for x in ov.get("risk_flags") or [] if x.get("significant")][:4]:
            rows.append(line(f"Жалобы: признак «{fl['flag']}»", _f(fl.get("pct")),
                             f"Признак «{fl['flag']}» ({fl.get('group')}): {_f(fl.get('pct'))}% "
                             f"жалоб против {_f(fl.get('market_pct'))}% у рынка.", "%"))
        hot = [c for c in ov.get("cities") or [] if c.get("above_normal")][:3]
        for c in hot:
            rows.append(line("Жалобы: город выше обычного", _n(c["n"]),
                             f"Город выше обычной доли: {c['city']} — {_n(c['n'])} жалоб "
                             f"(индекс {_f(c.get('index'))})."))
        # Динамика по месяцам и конкуренты по числу жалоб. Без них отчёт на
        # «жалобы на вклады за 90 дней: динамика» писал «динамику построить
        # невозможно» и «сопоставить с конкурентами нельзя» (замер 26.09).
        tr = _trend_line(bank, product, base_days)
        if tr:
            rows.append(line("Жалобы: по месяцам", tr[0], tr[1]))
        banks = [b for b in ov.get("banks_by_complaints") or [] if b.get("n")]
        if len(banks) > 1 and scope.get("rivals", True):
            rows.append(line("Жалобы: банки по числу жалоб", _n(len(banks)),
                             f"Банки по числу жалоб за {base_days} дней"
                             + (f" (продукт «{product}»)" if product else "") + ": "
                             + "; ".join(f"{b['bank']} — {_n(b['n'])} ({_f(b.get('pct'))}%)"
                                         for b in banks) + "."))
    title = f"AuditLens · Отзывы: {who} — сводка жалоб за {base_days} дней"
    od.page(url, title, lines, "complaints")
    for attribute, value, unit, text_ in rows:
        od.fact(subject=slug, attribute=attribute, value=value, unit=unit, verbatim=text_,
                url=url, date=str(ov.get("as_of") or "")[:10])

    if not anchor:
        return []
    # Сигналы недели и разбор тем среза — только для точки отсчёта
    sig = _j("complaint_signals", bank=bank, product=product)
    s_url = T.link_reviews(bank, product, days=7)
    s_lines, s_rows = [], []
    for s in sig.get("signals") or []:
        txt = (f"Всплеск недели по {_dm(sig.get('week_end'))}: «{s['label']}» — "
               f"{_n(s['week'])} жалоб при норме {_f(s.get('norm_per_week'))} в неделю "
               f"(×{_f(s.get('ratio'))}); у рынка ×{_f(s.get('market_ratio'))}"
               + (f", {s['market_note']}" if s.get("market_note") else "")
               + (f"; {s.get('top_city_share_pct')}% — {s['top_city']}" if s.get("top_city") else "")
               + ".")
        s_lines.append(txt)
        s_rows.append((f"Жалобы: всплеск «{s['label']}»", _n(s["week"]), txt))
    ov_w = sig.get("overall") or {}
    if ov_w.get("week") is not None:
        txt = (f"Всего жалоб за неделю по {_dm(sig.get('week_end'))}: {_n(ov_w['week'])} при норме "
               f"{_f(ov_w.get('norm_per_week'))} (×{_f(ov_w.get('ratio'))} по всем площадкам"
               + (f"; по banki.ru банк ×{_f(ov_w.get('cmp_ratio'))}, рынок ×{_f(ov_w.get('market_ratio'))}"
                  if ov_w.get("cmp_ratio") is not None and ov_w.get("market_ratio") is not None else "")
               + ").")
        s_lines.append(txt)
        s_rows.append(("Жалобы: всего за неделю", _n(ov_w["week"]), txt))
    if not sig.get("signals"):
        s_lines.append(f"Значимых всплесков жалоб за неделю по {_dm(sig.get('week_end'))} нет.")
    if s_lines:
        od.page(s_url, f"AuditLens · Отзывы: {who} — сигналы недели", s_lines, "complaints")
        for attribute, value, text_ in s_rows:
            od.fact(subject=slug, attribute=attribute, value=value, verbatim=text_, url=s_url,
                    date=str(sig.get("week_end") or "")[:10])

    themes = list(dict.fromkeys([*scope["themes"], *signal_themes]))[:4]
    if not themes:
        # Вопрос не про одну проблему — берём крупнейшие темы среза: группы
        # похожих и жалобы по ним дают сюжеты, а не случайную выборку поиска
        # (поиском по «вклады Сбербанка» отчёт получал 7 жалоб из 35).
        themes = [t["theme"] for t in (ov.get("themes") or [])[:3] if t.get("theme")]
    for key in themes:
        tdays = 7 if (key in signal_themes or days <= 7) else days
        th = _j("complaint_theme", theme=key, bank=bank, product=product, days=tdays)
        label = th.get("label") or key
        t_url = T.link_reviews(bank, product, key, tdays)
        g_lines, g_rows = [], []
        if th.get("total") is not None:
            txt = (f"Жалоб по теме «{label}» за {tdays} дней: {_n(th['total'])}"
                   + (" (все жалобы недельного сигнала)." if tdays <= 7 else "."))
            g_lines.append(txt)
            g_rows.append((f"Жалобы: тема «{label}» за {tdays} дней", _n(th["total"]), txt))
        for g in th.get("similar_groups") or []:
            if not g.get("n"):
                continue
            cities = ", ".join((g.get("cities") or [])[:3])
            txt = (f"Группа похожих жалоб по теме «{label}»: {_n(g['n'])} жалоб "
                   f"({_dm(g.get('first'))}–{_dm(g.get('last'))}"
                   + (f"; {cities}" if cities else "")
                   + (f"; {g['escalation_threats']} с угрозой эскалации"
                      if g.get("escalation_threats") else "")
                   + f"): {g.get('summary') or ''}"
                   + (f" Пример: «{g['quote']}»" if g.get("quote") else ""))
            g_lines.append(txt)
            g_rows.append((f"Жалобы: группа похожих «{label}»", _n(g["n"]), txt))
        if g_lines:
            od.page(t_url, f"AuditLens · Отзывы: {who} — тема «{label}»", g_lines, "complaints")
            for attribute, value, text_ in g_rows:
                od.fact(subject=slug, attribute=attribute, value=value, verbatim=text_,
                        url=t_url, date=str(th.get("week_end") or "")[:10])
        _complaint_quotes(od, slug, bank, th.get("complaints") or [], label)
    return themes


_MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _trend_line(bank: str, product: str | None, days: int) -> tuple[str, str] | None:
    """Помесячный ряд жалоб строкой страницы: значение факта и сама строка."""
    months = max(4, min(13, days // 30 + 2))
    try:
        tr = T._rd().trend(bank, product, months) or {}
    except Exception as e:  # noqa: BLE001 — ряд не обязателен
        log.info("жалобы %s: ряд по месяцам — %s", bank, type(e).__name__)
        return None
    series = [x for x in tr.get("series") or [] if x.get("ym")]
    if len(series) < 3:
        return None

    def ym(x) -> str:
        y, m = x["ym"].split("-")
        return f"{_MONTHS[int(m) - 1]} {y}"
    parts = [f"{ym(x)} — {_n(x['n'])}" + (" (месяц не завершён)" if x.get("partial") else "")
             for x in series]
    txt = "Жалобы по месяцам (по дате отзыва): " + "; ".join(parts) + "."
    if tr.get("baseline") is not None:
        txt += f" Медиана завершённых месяцев — {_f(tr['baseline'])}."
        spikes = [x for x in series if x.get("spike")]
        txt += (" Пики выше обычного: " + ", ".join(
            f"{ym(x)} ({_pct(x.get('pct_vs_median'))} к медиане)" for x in spikes) + "."
                if spikes else " Месяцев выше обычного уровня нет.")
    return ", ".join(_n(x["n"]) for x in series), txt


# Значки сюжета — словами, понятными без вкладки.
_BADGE_RU = {"слился": "вобрал похожий сюжет", "слились": "вобрал похожий сюжет"}


def _plural(n, one: str, few: str, many: str) -> str:
    try:
        a = abs(int(n)) % 100
    except (TypeError, ValueError):
        return many
    b = a % 10
    return many if 10 < a < 20 else few if 1 < b < 5 else one if b == 1 else many


def _stories_for_bank(od: OwnData, slug: str, bank: str, scope: dict, limit: int) -> int:
    """Сюжеты банка по срезу — страницей и фактами; жалобы-примеры — цитатами."""
    product = scope["product"]
    days = max(scope["days"], 90)
    st = _j("complaint_stories", bank=bank, product=product, days=days, limit=limit)
    # Сюжет с одной жалобой в срезе — это одна жалоба, а не повторяющаяся история.
    stories = [x for x in st.get("stories") or [] if (x.get("complaints_in_period") or 0) >= 2]
    if st.get("error") or not stories:
        return 0
    who = bank + (f" · {product}" if product else "")
    url = st.get("link") or (T.link_reviews(bank, product, days=days) + "&view=stories")
    lines: list[str] = []
    rows: list[tuple[str, str, str]] = []
    for x in stories:
        marks = [_BADGE_RU.get(m, m) for m in (x.get("badges") or []) if m]
        g = x.get("grew_this_week") or {}
        if g.get("week") is not None:
            marks.append(f"за неделю {_n(g['week'])} при норме {_f(g.get('norm_per_week'))} "
                         f"(×{_f(g.get('ratio'))})")
        txt = (f"Сюжет «{x['title']}»: {_n(x['complaints_in_period'])} "
               f"{_plural(x['complaints_in_period'], 'жалоба', 'жалобы', 'жалоб')} за {days} дней"
               + (f" (всего {_n(x['complaints_all_time'])}, первая {_dm(x.get('first_seen'))}, "
                  f"последняя {_dm(x.get('last_seen'))})" if x.get("complaints_all_time") else "")
               + (f"; {', '.join(marks)}" if marks else "")
               + (f"; проблема — {x['problem']}" if x.get("problem") else "")
               + (f". Суть: {x['what']}" if x.get("what") else "."))
        lines.append(txt)
        rows.append((f"Жалобы: сюжет «{x['title'][:80]}»", _n(x["complaints_in_period"]), txt))
    total = st.get("total_stories")
    if total and total > len(stories):
        lines.append(f"Показаны {len(stories)} сюжетов с наибольшим числом жалоб из {_n(total)}.")
    od.page(url, f"AuditLens · Отзывы: {who} — сюжеты за {days} дней", lines, "complaints")
    for attribute, value, text_ in rows:
        od.fact(subject=slug, attribute=attribute, value=value, verbatim=text_, url=url,
                date=str((st.get("period") or {}).get("to") or "")[:10])
    for x in stories:
        _complaint_quotes(od, slug, bank, (x.get("examples") or [])[:2 if limit > 3 else 1],
                          f"сюжет «{x['title'][:60]}»")
    return len(stories)


def _theme_quotes(od: OwnData, slug: str, bank: str, scope: dict, per_theme: int = 4) -> int:
    """Темы вопроса у конкурента: сколько жалоб, группы похожих и несколько цитат —
    по разметке, без поиска по словам."""
    product, days = scope["product"], max(scope["days"], 30)
    who = bank + (f" · {product}" if product else "")
    got = 0
    for key in scope["themes"][:2]:
        th = _j("complaint_theme", theme=key, bank=bank, product=product, days=days)
        if th.get("error") or not th.get("total"):
            continue
        label = th.get("label") or key
        url = T.link_reviews(bank, product, key, days)
        lines = [f"Жалоб по теме «{label}» за {days} дней: {_n(th['total'])}."]
        rows = [(f"Жалобы: тема «{label}» за {days} дней", _n(th["total"]), lines[0])]
        for g in (th.get("similar_groups") or [])[:3]:
            if not g.get("n"):
                continue
            txt = (f"Группа похожих жалоб по теме «{label}»: {_n(g['n'])} жалоб "
                   f"({_dm(g.get('first'))}–{_dm(g.get('last'))}): {g.get('summary') or ''}"
                   + (f" Пример: «{g['quote']}»" if g.get("quote") else ""))
            lines.append(txt)
            rows.append((f"Жалобы: группа похожих «{label}»", _n(g["n"]), txt))
        od.page(url, f"AuditLens · Отзывы: {who} — тема «{label}»", lines, "complaints")
        for attribute, value, text_ in rows:
            od.fact(subject=slug, attribute=attribute, value=value, verbatim=text_, url=url)
        before = od.complaints
        _complaint_quotes(od, slug, bank, (th.get("complaints") or [])[:per_theme], label)
        got += od.complaints - before
    return got


def _complaint_quotes(od: OwnData, slug: str, bank: str, items: list[dict],
                      label: str | None) -> None:
    """Жалобы как страницы: текст клиента; факт — пересказ разметки и дословная
    цитата, которую разметка уже сверила с текстом."""
    meta = runstate.current().review_meta
    for it in items:
        url = it.get("url") or ""
        text_ = (it.get("text") or "").strip()
        quote = (it.get("quote") or "").strip()
        if not url or not text_ or url in od.pages:
            continue
        body = text_ if quote and quote in text_ else f"{text_}\n{quote}".strip()
        od.pages[url] = body
        od.meta[url] = {"title": f"Жалоба клиента · {bank}", "kind": "review"}
        meta[url] = {"bank": bank, "date": it.get("date") or "", "product": it.get("product") or "",
                     "subject": slug}
        verbatim = quote if quote and quote in body else _first_sentence(body)
        themes = it.get("themes") or []
        city = (it.get("city") or "").strip()
        # Город — в значении: цитата в отчёте подписывается «город, дата».
        od.fact(subject=slug,
                attribute=f"Жалоба: {label or (themes[0] if themes else 'прочее')}",
                value=(it.get("summary") or verbatim)[:300] + (f" ({city})" if city else ""),
                verbatim=verbatim, url=url, date=str(it.get("date") or "")[:10])
        od.complaints += 1


def _first_sentence(text_: str) -> str:
    m = re.search(r"^(.{30,280}?[.!?])(\s|$)", text_, re.S)
    return (m.group(1) if m else text_[:200]).strip()


def collect_complaints(od: OwnData, plan, scope: dict, question: str) -> None:
    labels = dict(getattr(plan, "subject_labels", None) or {})
    subjects = [s for s in (getattr(plan, "subjects", None) or []) if s][:_MAX_BANKS]
    if not subjects:
        return
    from .dossier import anchor_of
    anchor = anchor_of(plan) or subjects[0]
    signal_themes: list[str] = []
    if scope["days"] <= 7 or re.search(r"всплеск|вырос|растут|рост|недел", question, re.I):
        sig = _j("complaint_signals", bank=_bank_name(anchor, labels), product=scope["product"])
        signal_themes = [s["theme"] for s in (sig.get("signals") or [])[:2]
                         if not scope["themes"] or s["theme"] in scope["themes"]]
    # Сводка по продукту или теме — когда срез их опознал; сводка по всему банку —
    # только на вопрос о банке в целом. Иначе отчёт о сервисе, которого нет в
    # справочнике продуктов жалоб, получал общий жалобный фон Сбера: 2 485 жалоб
    # за 90 дней, блокировки, справки (аудит 11.11, «Своё дело», драгметаллы).
    overview = bool(scope["product"] or scope["themes"]) or bank_level(plan, scope, question)
    stems = subject_stems(plan, question)
    state = runstate.current()

    def per_bank(slug: str) -> OwnData:
        runstate.bind(state)                 # поток пула: контекст прогона не наследуется
        part = OwnData(scope=od.scope)
        bank = _bank_name(slug, labels)
        done: list[str] = []
        stories = quotes = 0
        if overview:
            try:
                done = _complaints_for_bank(part, slug, bank, scope, slug == anchor, signal_themes)
            except Exception as e:  # noqa: BLE001 — банк без корпуса не должен ронять сбор
                log.info("жалобы %s: %s", bank, type(e).__name__)
                return part
            # Сюжеты — повторяющиеся истории клиентов по размеченным жалобам: то, что
            # аудитор видит во вкладке и чего у отчёта не было. У точки отсчёта —
            # подробно, у остальных — три главных («что болит у других»).
            try:
                stories = _stories_for_bank(part, slug, bank, scope, 6 if slug == anchor else 3)
            except Exception as e:  # noqa: BLE001
                log.info("сюжеты %s: %s", bank, type(e).__name__)
            # У конкурента темы вопроса разбираем по разметке (тема — главная
            # проблема жалобы), а не поиском по названию продукта.
            if slug != anchor and scope["themes"]:
                try:
                    quotes = _theme_quotes(part, slug, bank, scope)
                except Exception as e:  # noqa: BLE001
                    log.info("темы %s: %s", bank, type(e).__name__)
        # Поиск по словам и смыслу — когда вопрос про ситуацию, которой нет в
        # перечнях (её фразы дал срез), или когда разметка ничего не дала. Раньше
        # он шёл всегда и искал название продукта: до трёх запросов на банк, по
        # 20–30 секунд каждый, а находил общие жалобы на продукт.
        need_search = bool(scope.get("search")) or (not done and not stories and not quotes)
        if need_search:
            try:
                _on_topic(part, slug, bank, plan, scope, question, stems, overview,
                          _COMPLAINTS_PER_BANK if slug == anchor else 8)
            except Exception as e:  # noqa: BLE001
                log.info("жалобы по предмету %s: %s", bank, type(e).__name__)
        return part

    # Банки — одновременно. Поиск жалоб по предмету — это до трёх запросов к
    # корпусу на банк с проверкой смысла моделью; по очереди два банка занимали
    # 100 секунд (замер 11.11), и в продолжении отчёта это был весь «сбор».
    if len(subjects) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(subjects)) as pool:
            parts = list(pool.map(per_bank, subjects))
    else:
        parts = [per_bank(subjects[0])]
    for part in parts:                       # в порядке объектов: точка отсчёта — первой
        od.pages.update(part.pages)
        od.meta.update(part.meta)
        od.facts.extend(part.facts)
        od.notes.extend(part.notes)
        od.complaints += part.complaints


def _on_topic(od: OwnData, slug: str, bank: str, plan, scope: dict, question: str,
              stems: list[str], overview: bool, limit: int) -> None:
    """Жалобы именно по предмету вопроса: поиск по корпусу и отсев тех, где
    предмет не назван. Без сводки (overview=False) число найденных — отдельный
    факт; ничего не нашлось — строка в «Честные пробелы», а не раздел."""
    days = max(scope["days"], 90)
    # Ищем ситуацию словами клиента (их дал срез), а не название продукта: продукт
    # и так задан фильтром. Фраз нет — предмет вопроса и один синоним.
    asks = list(scope.get("search") or [])
    if not asks:
        asks = [(getattr(plan, "product", "") or question)[:120]]
        asks += [str(x)[:80] for x in (getattr(plan, "product_synonyms", None) or [])[:1]]
    items, seen, total, link = [], set(), 0, ""
    for ask in dict.fromkeys(a for a in asks if a.strip()):
        res = _j("complaint_search", query=ask, bank=bank, product=scope["product"],
                 days=days, limit=limit)
        link = link or res.get("link") or ""
        total = max(total, int(res.get("by_words") or 0))
        for it in res.get("complaints") or []:
            if it.get("url") and it["url"] not in seen:
                seen.add(it["url"])
                items.append(it)
    if overview or getattr(plan, "task", None) is None:
        picked = items                      # срез уже сузил выборку — отсев не нужен
    else:
        def about(it: dict) -> bool:
            low = " ".join(str(it.get(k) or "") for k in ("text", "summary", "quote", "product")
                           ).lower().replace("ё", "е")
            return any(st.replace("ё", "е") in low for st in stems)
        picked = [it for it in items if about(it)] if stems else []
    subject = (getattr(plan, "product", "") or "").strip() or "предмету вопроса"
    if not picked:
        if not overview:
            od.notes.append(f"{bank}: жалоб по теме «{subject}» за {days} дней в данных "
                            f"раздела «Аудит отзывов» не найдено.")
        return
    if not overview:
        n = max(total, len(picked)) if total and total <= 5 * len(picked) else len(picked)
        url = link or T.link_reviews(bank, scope["product"], days=days)
        txt = (f"Жалоб на {bank} по теме «{subject}» за {days} дней в разделе «Аудит отзывов» "
               f"найдено: {_n(n)}. Это жалобы, где предмет назван в тексте; сводка по банку "
               f"в целом к вопросу не относится и в отчёт не включена.")
        od.page(url, f"AuditLens · Отзывы: {bank} — жалобы по теме «{subject}»", [txt], "complaints")
        od.fact(subject=slug, attribute="Жалобы: по предмету вопроса", value=_n(n),
                verbatim=txt, url=url)
    _complaint_quotes(od, slug, bank, picked[:limit], None)


# ── лазейки ──────────────────────────────────────────────────────────────────

def collect_loopholes(od: OwnData, plan, question: str) -> None:
    labels = dict(getattr(plan, "subject_labels", None) or {})
    from .dossier import anchor_of
    anchor = anchor_of(plan)
    if not anchor:
        return
    bank = _bank_name(anchor, labels)
    query = (getattr(plan, "product", "") or question)[:120]
    lh = _j("loopholes", query=query, bank=bank, limit=_LOOPHOLES + 6)
    recs = lh.get("records") or []
    own = [r for r in recs if r.get("about_bank")][:_LOOPHOLES]
    other = [r for r in recs if not r.get("about_bank")][:4]
    if not own and not other:
        return
    st = lh.get("stats") or {}
    s_url = "#loophole"
    txt = (f"Раздел «Аудит уязвимостей»: всего лазеек по всем банкам — {_n(st.get('all_banks_total'))} "
           f"(с {_dm(st.get('collected_since'))}), найдено за 30 дней — "
           f"{_n(st.get('all_banks_found_last_30d'))}; с меткой «{bank}» — "
           f"{_n(st.get('this_bank_tagged_total'))}. Все оценки предварительные: "
           f"экспертом не проверены — это возможные уязвимости, а не зафиксированные схемы.")
    od.page(s_url, "AuditLens · Уязвимости: сводка раздела", [txt], "loopholes")
    od.fact(subject=anchor, attribute="Лазейки: сводка раздела",
            value=_n(st.get("all_banks_total")), verbatim=txt, url=s_url, stance=LOOPHOLE)
    for r in own + other:
        url = r.get("url") or f"#loophole?record={r['record_id']}"
        title = (r.get("headline") or r.get("title") or "").strip()
        # подпись по типу и проверке: раньше каждая запись шла как «Схема: …» (аудит 03.10)
        kind = ("Мошенническая схема" if r.get("type") == "мошенническая схема"
                else "Возможная уязвимость")
        state = ("проверено экспертом ЦК КС" if r.get("expert_checked")
                 else "оценка модели, экспертом не проверена")
        lines = [x for x in (
            f"{kind} ({state}): {title}" if title else "",
            f"Суть: {r['summary']}" if r.get("summary") else "",
            f"Почему лазейка: {r['why_loophole']}" if r.get("why_loophole") else "",
            f"Модель сомневается: {r['model_doubt']}" if r.get("model_doubt") else "",
            (r.get("text") or "").strip(),
            f"Источник: {r.get('source') or '—'}; опубликовано {_dm(r.get('published'))}; "
            f"найдено системой {_dm(r.get('found'))}; статус: {state}.",
        ) if x]
        od.page(url, f"AuditLens · Уязвимости: {title[:80]}", lines, "loopholes")
        tag = r.get("bank_tag") or ""
        subj = anchor if r.get("about_bank") else (tag if tag in labels else "")
        od.fact(subject=subj, attribute="Лазейка", value=title[:200],
                verbatim=f"{kind} ({state}): {title}", url=url, stance=LOOPHOLE,
                date=str(r.get("found") or "")[:10])
        if r.get("why_loophole"):
            od.fact(subject=subj, attribute="Лазейка: почему", value=r["why_loophole"][:300],
                    verbatim=f"Почему лазейка: {r['why_loophole']}", url=url, stance=LOOPHOLE,
                    date=str(r.get("found") or "")[:10])
        od.loopholes += 1


# ── рынок ────────────────────────────────────────────────────────────────────
# Замер 26.09: на «сравни вклады Сбера с ВТБ и Газпромбанком» отчёт приписал
# ВТБ 19% (это промо Сбера на 3 месяца) — ставки он собирал из веба, где у
# каждого банка своя витрина и свои даты. Вкладка «Рынок» сравнивает все банки
# по одной методике на одну дату; отчёт получает её как страницы данных.

MARKET = "market"
_OFFERS_PER_BANK = int(os.getenv("GPTR_OWN_OFFERS", "5"))


def _range(a, b, unit: str) -> str:
    if a is None and b is None:
        return ""
    if a is None or b is None or float(a) == float(b):
        return f"{_f(a if a is not None else b, 2)}{unit}"
    return f"{_f(a, 2)}–{_f(b, 2)}{unit}"


def _offer_line(r: dict, metric: str, unit: str) -> str:
    seg = dict(T._cm.SEGMENTS).get(r.get("segment") or "", "")
    bits = [f"{metric} {_f(r.get('metric_value'), 2)}{unit}"]
    if r.get("rate_min") is not None or r.get("rate_max") is not None:
        bits.append("ставка " + _range(r.get("rate_min"), r.get("rate_max"), "%"))
    if r.get("psk_min") is not None or r.get("psk_max") is not None:
        bits.append("ПСК " + _range(r.get("psk_min"), r.get("psk_max"), "%"))
    if r.get("term_months_min") is not None or r.get("term_months_max") is not None:
        bits.append("срок " + _range(r.get("term_months_min"), r.get("term_months_max"), " мес."))
    if r.get("amount_min"):
        bits.append(f"сумма от {_n(r['amount_min'])} ₽")
    for key, name in (("fee_open", "выпуск"), ("fee_service", "обслуживание")):
        if r.get(key) not in (None, ""):
            bits.append(f"{name}: {r[key]}")
    if r.get("grace_days"):
        bits.append(f"льготный период {_n(r['grace_days'])} дн.")
    if r.get("cashback_pct"):
        bits.append(f"кэшбэк до {_f(r['cashback_pct'])}%")
    for key, name in (("capitalization", "капитализация"), ("replenishable", "пополнение"),
                      ("early_withdraw", "досрочное снятие")):
        if r.get(key) is not None:
            bits.append(f"{name}: {'да' if r[key] else 'нет'}")
    if seg and seg != "Массовый":
        bits.append(f"сегмент: {seg.lower()}")
    for key in ("rate_requires", "conditions"):
        if r.get(key):
            bits.append(f"условие: {str(r[key]).strip()[:220]}")
    if r.get("valid_from"):
        bits.append(f"данные на {_dm(r['valid_from'])}")
    txt = f"{r.get('bank_name')}, «{(r.get('title') or '').strip()}»: " + "; ".join(bits) + "."
    if r.get("implausible_reason"):
        txt += f" Сомнение витрины: {r['implausible_reason']}."
    return txt


def collect_market(od: OwnData, plan, scope: dict) -> None:
    cat = scope.get("category")
    if not (cat and scope.get("market")):
        return
    labels = dict(getattr(plan, "subject_labels", None) or {})
    subjects = [s for s in (getattr(plan, "subjects", None) or []) if s][:_MAX_BANKS]
    from .dossier import anchor_of
    anchor = anchor_of(plan) or (subjects[0] if subjects else "")
    by_name = {_bank_name(s, labels): s for s in subjects}
    others = [n for n, s in by_name.items() if s != anchor]
    mp = _j("market_position", category=cat)
    as_of = _dm(mp.get("as_of"))
    mo = _j("market_offers", category=cat, banks=others or None)
    # Лидеры рынка — всегда. Вопрос «выдели топ-5 и позицию Сбера» кондуктор
    # разворачивает в список банков (Альфа, ВТБ, ГПБ), и отчёт получал только их:
    # топ «по витрине» без Яндекс Банка с 16% (26.09).
    top = _j("market_offers", category=cat, limit=10) if others else mo
    metric, unit = mo.get("metric") or "метрика", mo.get("metric_unit") or ""
    label = mo.get("label") or cat
    caveat = mo.get("how_to_compare")

    def slug_for(name: str) -> str:
        if name in by_name:
            return by_name[name]
        return anchor if name == T.SBER or T._same_bank(T.SBER, name) else ""

    groups = [("", {"best": top.get("top") or [], "offers_total": top.get("offers_total")})]
    groups += list((mo.get("by_bank") or {}).items())
    for name, d in groups:
        offers = (d.get("best") or [])[:_OFFERS_PER_BANK if name else 10]
        if not offers:
            continue
        url = T.link_market(cat, name or None)
        head = (f"Витрина «Рынок», категория «{label}», {name or 'лидеры рынка'}: "
                f"предложений — {_n(d.get('offers_total'))}; ниже — {len(offers)} лучших по "
                f"метрике «{metric}» "
                f"({'меньше — лучше' if mo.get('lower_is_better') else 'больше — лучше'}), "
                f"не все продукты: отсутствие срока или продукта в этом списке не значит, "
                f"что у банка его нет. Витрина — сверочные цифры без подробностей "
                f"условий.")
        lines = [head] + [_offer_line(r, metric, unit) for r in offers]
        if caveat:
            lines.append(f"Как сравнивать: {caveat}")
        od.page(url, f"AuditLens · Рынок: {label} — {name or 'лидеры'}", lines, MARKET)
        for r, txt in zip(offers, lines[1:]):
            # банк вне плана — своим именем: без субъекта предложение попало бы
            # в «Карту условий» Сбера как общий факт о предмете
            od.fact(subject=slug_for(r.get("bank_name") or name) or (r.get("bank_name") or name),
                    attribute=f"{metric} (витрина «Рынок»)",
                    value=_f(r.get("metric_value"), 2), unit=unit.strip(), verbatim=txt,
                    url=url, stance="declared", date=str(r.get("valid_from") or "")[:10])
            od.market += 1
    cell = next((c for c in mp.get("cells") or [] if c.get("category") == cat), None)
    if not cell or not anchor:
        return
    # свой адрес: страница лидеров уже живёт на #market?cat=…
    url = T.link_market(cat) + "&view=position"
    lines = []
    if cell.get("degenerate"):
        lines.append(f"Место Сбербанка в категории «{label}» на {as_of} не определено: "
                     f"метрика «{cell.get('metric_label')}» не различает банки.")
    elif cell.get("rank"):
        lines.append(
            f"Место Сбербанка в категории «{label}» по метрике «{cell.get('metric_label')}» "
            f"на {as_of}: {cell['rank']}-е из {cell.get('n_banks')} банков; лучшее "
            f"предложение «{cell.get('title')}» — {_f(cell.get('value'), 2)}"
            f"{cell.get('metric_unit') or ''}; разрыв с медианой рынка — "
            f"{_f(cell.get('gap_median'), 2)}{cell.get('gap_unit') or ''}, с лидером — "
            f"{_f(cell.get('gap_leader'), 2)}{cell.get('gap_unit') or ''}.")
    seg_names = dict(T._cm.SEGMENTS)
    for c in cell.get("comparable") or []:
        lines.append(
            f"Сегмент «{seg_names.get(c.get('segment'), c.get('segment'))}»: "
            f"{c.get('rank')}-е из {c.get('n_banks')}; у Сбербанка «{c.get('title')}» — "
            f"{_f(c.get('value'), 2)}{cell.get('metric_unit') or ''}, медиана рынка — "
            f"{_f(c.get('median'), 2)}, лидер — {_f(c.get('leader'), 2)}.")
    low = label.lower()
    lines += [f"Оговорка методики: {x}." for x in mp.get("method_caveats") or []
              if low[:5] in str(x).lower()]
    if not lines:
        return
    od.page(url, f"AuditLens · Рынок: {label} — место Сбербанка", lines, MARKET)
    for txt in lines:
        if txt.startswith(("Место", "Сегмент")):
            od.fact(subject=anchor, attribute="Место на рынке (витрина «Рынок»)",
                    value=txt.split(": ", 1)[-1][:120], verbatim=txt, url=url,
                    stance="declared", date=str(mp.get("as_of") or "")[:10])
            od.market += 1


# ── база знаний ──────────────────────────────────────────────────────────────
# Аудит 11.11: в базе знаний 14 387 документов 24 банков, быстрый режим ищет в
# ней в 56 % ответов, а отчёт не обращался ни разу и заново ходил на сайты,
# закрытые защитой от ботов. Документы отдаются отчёту как прочитанные страницы:
# факты из них выписывает тот же слой, цитаты сверяются так же.

KNOWLEDGE = "knowledge"
_KB_DOCS = int(os.getenv("GPTR_KB_DOCS", "8"))             # документов на точку отсчёта
_KB_DOCS_OTHER = int(os.getenv("GPTR_KB_DOCS_OTHER", "3"))  # на каждого конкурента
_KB_CHARS = int(os.getenv("GPTR_KB_CHARS", "12000"))        # знаков одного документа
_KB_MAX_AGE = int(os.getenv("GPTR_KB_MAX_AGE_DAYS", "120"))
# Сколько ждём живую страницу, прежде чем взять её сохранённую копию.
_KB_LIVE_S = float(os.getenv("GPTR_KB_LIVE_S", "25"))
# Сайты, где условия меняются без предупреждения: витрины и сравнения. Сайты
# банков добавляются из справочника.
_VOLATILE_HOSTS = ("banki.ru", "sravni.ru", "finuslugi.ru", "bankiros.ru", "vbr.ru", "brobank.ru",
                   "mainfin.ru", "vsezaimyonline.ru")


def is_volatile(url: str) -> bool:
    """Страница, чьё содержание меняется задним числом: тарифы и условия на
    сайте банка, витрина агрегатора. Закон, акт регулятора, статья, обзор —
    датированные документы: их сохранённая копия остаётся верной.

    Владелец 11.11: тарифы меняются часто, и копия из базы знаний не должна
    выдаваться за сегодняшние условия. Такие страницы читаем вживую, копию
    берём, только когда страница не открылась, — и с датой копии.
    """
    from urllib.parse import urlparse

    from ..entity_extractor import _BANK_DOMAINS
    from ..v2.tools.web_tools import REGULATOR_DOMAINS
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    if not host:
        return False

    def under(h: str, domains) -> bool:
        return any(h == d or h.endswith("." + d) for d in domains if d)
    if under(host, REGULATOR_DOMAINS) or host.endswith(".gov.ru"):
        return False
    return under(host, _BANK_DOMAINS.values()) or under(host, _VOLATILE_HOSTS)


def _read_live(urls: list[str], state, wait_s: float) -> dict[str, str]:
    """Читает страницы вживую, не дольше wait_s на все. Возвращает прочитанные."""
    from concurrent.futures import ThreadPoolExecutor, wait

    def one(url: str) -> str:
        from .scraper import AuditLensScraper
        try:
            return AuditLensScraper(url, state=state).scrape()[0] or ""
        except Exception as e:  # noqa: BLE001 — не открылась: останется копия
            log.info("база знаний, живое чтение %s: %s", url[:70], type(e).__name__)
            return ""
    if not urls:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(8, len(urls)))
    futs = {pool.submit(one, u): u for u in urls}
    done, _late = wait(futs, timeout=wait_s)
    pool.shutdown(wait=False, cancel_futures=True)
    out: dict[str, str] = {}
    for f in done:
        url = futs[f]
        try:
            text_ = f.result()
        except Exception:  # noqa: BLE001
            text_ = ""
        if len(text_) >= 400 and url not in state.unreadable:
            out[url] = text_
    return out


def _kb_text(document_id: int, hit_idx: list[int], words: set[str], budget: int) -> str:
    """Текст документа под вопрос: найденные поиском фрагменты и соседние, плюс
    фрагменты со словами вопроса, в порядке документа. Короткий — целиком."""
    rows = T._q("SELECT dc.idx, dc.text FROM document_chunk dc WHERE dc.document_id = :i "
                "AND NOT EXISTS (SELECT 1 FROM document_chunk_excluded x "
                "WHERE x.chunk_id = dc.chunk_id) ORDER BY dc.idx", {"i": int(document_id)})
    chunks = {r["idx"]: (r["text"] or "") for r in rows}
    if not chunks:
        return ""
    if sum(len(t) for t in chunks.values()) <= budget:
        return "\n\n".join(chunks[i] for i in sorted(chunks))
    want: list[int] = []
    for i in hit_idx:
        want += [i, i + 1]
    scored = sorted(((sum(1 for w in words if w in t.lower()), i) for i, t in chunks.items()),
                    key=lambda x: (-x[0], x[1]))
    want += [i for sc, i in scored if sc > 0]
    picked, left = [], budget
    for i in dict.fromkeys(want):
        t = chunks.get(i)
        if not t or left <= 0:
            continue
        picked.append(i)
        left -= len(t)
    out, prev = [], None
    for i in sorted(picked):
        if prev is not None and i != prev + 1:
            out.append("[…]")
        out.append(chunks[i])
        prev = i
    return "\n\n".join(out)[:budget + 200]


def collect_knowledge(od: OwnData, plan, question: str, state=None) -> None:
    """Документы базы знаний по предмету вопроса — в страницы прогона."""
    from ...rag.retriever import hybrid_search
    state = state or runstate.current()
    subjects = [s for s in (getattr(plan, "subjects", None) or []) if s][:_MAX_BANKS]
    from .dossier import anchor_of
    anchor = anchor_of(plan) or (subjects[0] if subjects else "")
    topic = (getattr(plan, "product", "") or "").strip() or (question or "")[:200]
    extra = " ".join((getattr(plan, "topics", None) or [])[:4])
    query = f"{topic} {extra}".strip()[:300]
    words = {w for w in re.findall(r"[a-zа-яё]{5,}", query.lower()) if w not in _COMMON_WORDS}
    stems = [st.replace("ё", "е") for st in subject_stems(plan, question)]
    targets: list[tuple[str | None, int]] = [(s, _KB_DOCS if s == anchor else _KB_DOCS_OTHER)
                                             for s in subjects] or [(None, _KB_DOCS)]
    found: dict[str, tuple[str, dict]] = {}          # url → (текст копии, сведения)
    for slug, n in targets:
        try:
            res = hybrid_search(query, limit=n, per_doc=4, bank_slugs=[slug] if slug else None,
                                max_age_days=_KB_MAX_AGE)
        except Exception as e:  # noqa: BLE001 — без базы знаний отчёт идёт на сайты
            log.info("база знаний %s: %s", slug or "все", type(e).__name__)
            continue
        for g in (res.get("groups") or [])[:n]:
            url = (g.get("url") or "").strip()
            if not url or url in state.preloaded or url in found:
                continue
            text_ = _kb_text(g["document_id"], [h.get("idx") for h in g.get("hits") or []
                                                 if h.get("idx") is not None], words, _KB_CHARS)
            if len(text_) < 400:
                continue
            # Документ обязан говорить о предмете вопроса. Без банка в запросе поиск
            # приносил близкое «по буквам»: на «ОМС» — обзор ЦБ о страховании.
            low = text_.lower().replace("ё", "е")
            hit = sum(1 for w in stems if w in low)
            if stems and hit < (1 if slug else min(2, len(stems))):
                continue
            title = (g.get("title") or "").strip()
            page = (f"# {title}\n\n" if title else "") + text_
            found[url] = (page, {"doc": g["document_id"], "title": title,
                                 "loaded": str(g.get("fetched_at") or "")[:10],
                                 "link": T.link_doc(g["document_id"]), "subject": slug or ""})
    # Изменчивые страницы (тарифы на сайте банка, витрины) — сначала вживую:
    # копия из базы знаний могла устареть. Не открылась — берём копию, но с её
    # датой и строкой в пробелах. Датированные документы (законы, акты, обзоры)
    # берём из базы сразу: их текст задним числом не меняется.
    volatile = [u for u in found if is_volatile(u)]
    live = _read_live(volatile, state, _KB_LIVE_S)
    stale: list[str] = []
    for url, (page, meta) in found.items():
        if url in live:
            # прочитана сегодня: обычная страница прогона, разбирается вне очереди
            state.preloaded[url] = live[url]
            state.note_page(url, live[url])
            od.knowledge_live += 1
            continue
        if url in volatile:
            meta["fallback"] = True
            stale.append(meta["loaded"])
            if meta["loaded"]:
                state.page_dates[url] = meta["loaded"]      # факты получат дату копии
        state.preloaded[url] = page
        state.kb_meta[url] = meta
        state.note_page(url, page)
        od.knowledge += 1
    if stale:
        dates = sorted(d for d in stale if d)
        od.notes.append(
            f"Страниц, которые сейчас не открылись и взяты из сохранённой копии базы знаний: "
            f"{len(stale)}" + (f" (самая ранняя копия — от {_ru_date(dates[0])})" if dates else "")
            + ". Условия на них могли измениться — сверьте на сайте.")


def _ru_date(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else iso


# ── ссылки из вопроса ────────────────────────────────────────────────────────
_URL_RX = re.compile(r"https?://[^\s)»\"'<>\]]+")


def collect_named_urls(od: OwnData, question: str, state=None) -> None:
    """Страницы, на которые аудитор сам дал ссылку («разбери новость: …»),
    читаются первыми и разбираются вне очереди. Не открылась (платный доступ,
    защита от ботов) — берём текст той же новости из ленты AuditLens."""
    state = state or runstate.current()
    for url in dict.fromkeys(u.rstrip(".,;:!?") for u in _URL_RX.findall(question or "")):
        if len(state.preloaded) >= 12 or url in state.preloaded:
            continue
        text_ = ""
        try:
            from .scraper import AuditLensScraper
            text_ = AuditLensScraper(url, state=state).scrape()[0] or ""
        except Exception as e:  # noqa: BLE001 — ссылка могла не открыться
            log.info("ссылка из вопроса %s: %s", url[:70], type(e).__name__)
        if len(text_) < 400 or url in state.unreadable:
            res = _j("news_find", url=url)
            row = next(iter(res.get("news") or []), None)
            if row and len((row.get("text") or "")) >= 200:
                text_ = "\n\n".join(x for x in (f"# {row.get('title') or ''}".strip(),
                                                 row.get("text") or "") if x)
                when = str(row.get("ts") or "")[:10]
                if when:
                    state.page_dates.setdefault(url, when)
            else:
                od.notes.append(f"Ссылку из вопроса прочитать не удалось: {url}.")
                continue
        state.preloaded[url] = text_
        state.note_page(url, text_)


# ── новости ──────────────────────────────────────────────────────────────────

NEWS = "news"
_NEWS_MAX = int(os.getenv("GPTR_OWN_NEWS", "6"))


def collect_news(od: OwnData, plan, question: str) -> None:
    """Новости AuditLens по предмету вопроса за 60 дней: заголовок и суть с датой.
    Берутся только те, где предмет назван — близкие «по смыслу» отсеиваются."""
    topic = (getattr(plan, "product", "") or "").strip() or (question or "")[:200]
    res = _j("news_find", query=topic[:200], days=60)
    stems = subject_stems(plan, question)
    for r in (res.get("news") or []):
        url, title = (r.get("url") or "").strip(), (r.get("title") or "").strip()
        body = (r.get("text") or "").strip()
        if not url or len(title) < 12 or url in od.pages:
            continue
        low = f"{title} {body}".lower().replace("ё", "е")
        if stems and not any(st.replace("ё", "е") in low for st in stems):
            continue
        when = str(r.get("ts") or "")[:10]
        lines = [x for x in (body, f"Суть: {r['summary']}" if r.get("summary") else "",
                             f"Источник: {r.get('source') or '—'}; дата: {_dm(when)}.") if x]
        od.page(url, title, lines, NEWS)
        od.fact(subject="", attribute="Новость", value=(r.get("summary") or title)[:300],
                verbatim=title, url=url, date=when)
        od.news += 1
        if od.news >= _NEWS_MAX:
            break


# ── вход ─────────────────────────────────────────────────────────────────────

async def collect(client, model: str, question: str, plan, state=None) -> OwnData:
    """Всё собственное: срез, аналитика и цитаты жалоб, лазейки. Синхронные
    части — в пуле потоков; сбор идёт параллельно веб-поиску."""
    import asyncio
    od = OwnData()
    state = state or runstate.current()
    runstate.bind(state)
    scope = await scope_for(client, model, question, plan)
    od.scope = scope

    def work():
        runstate.bind(state)
        has_task = getattr(plan, "task", None) is not None
        for need, fn, args in ((has_task, collect_named_urls, (od, question, state)),
                               (has_task, collect_knowledge, (od, plan, question, state)),
                               (scope["complaints"], collect_complaints, (od, plan, scope, question)),
                               (scope["loopholes"], collect_loopholes, (od, plan, question)),
                               (scope["market"], collect_market, (od, plan, scope)),
                               (has_task, collect_news, (od, plan, question))):
            if not need:
                continue
            try:
                fn(*args)
            except Exception:
                log.exception("собственные данные: %s", fn.__name__)
    await asyncio.to_thread(work)
    state.own_meta.update(od.meta)
    state.own_scope = dict(scope)
    log.info("собственные данные: срез %s; страниц %d, фактов %d (жалоб %d, лазеек %d, "
             "рынок %d, база знаний %d, новости %d)", scope, len(od.pages), len(od.facts),
             od.complaints, od.loopholes, od.market, od.knowledge, od.news)
    return od
