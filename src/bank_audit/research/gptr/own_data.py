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
- "days": 7 — вопрос про эту неделю или всплеск; 90 — по умолчанию; 180 или 365 —
  если спрошен длинный период;
- "complaints": true, если ответу нужны жалобы клиентов — вопрос о жалобах, проблемах
  клиентов, качестве, рисках продукта или общий разбор продукта для проверки; false —
  вопрос только о сравнении условий и ставок, только о лазейках или о тексте нормы;
- "loopholes": true, если ответу нужны схемы обхода условий и злоупотреблений — вопрос
  о лазейках, уязвимостях, мошенничестве или общий разбор продукта для проверки;
  false — вопрос о жалобах на конкретную тему, о сравнении условий, о регулировании;
- "category": категория витрины «Рынок» из перечня (ключ) или null;
- "market": true, если ответу нужны условия продуктов банков — ставки, комиссии,
  сравнение с конкурентами, позиция на рынке; иначе false.
Ответ — строго JSON: {"product": ..., "themes": [...], "days": ..., "complaints": ...,
"loopholes": ..., "category": ..., "market": ...}"""

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
    if product and not product_named(product, question):
        log.info("срез данных: продукт «%s» в вопросе не назван — без продукта", product)
        product = None
    themes = [t for t in (data.get("themes") or []) if t in T.THEME_KEYS][:3]
    try:
        days = int(data.get("days") or 90)
    except (TypeError, ValueError):
        days = 90
    days = min((7, 30, 90, 180, 365), key=lambda d: abs(d - days))
    category = data.get("category") if data.get("category") in T.CATEGORY_IDS else None
    nature = (getattr(plan, "question_nature", "") or "").lower()

    def flag(key: str, default: bool) -> bool:
        v = data.get(key)
        return default if v is None else v is not False

    return {"product": product, "themes": themes, "days": days,
            "complaints": flag("complaints", data.get("relevant") is not False),
            "loopholes": flag("loopholes", nature != "regulatory") and nature != "regulatory",
            "category": category, "market": flag("market", False) and bool(category)}


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
    for t in (ov.get("themes") or [])[:8 if anchor else 5]:
        ch = t.get("change_pct")
        rows.append(line(f"Жалобы: тема «{t['label']}»", _n(t["n"]),
                         f"Тема «{t['label']}»: {_n(t['n'])} жалоб ({_f(t.get('pct'))}% всех)"
                         + (f", к прошлому периоду {_pct(ch)}"
                            + (" (значимо)" if t.get("change_significant") else "")
                            if ch is not None else "") + "."))
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
        if len(banks) > 1:
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
               f"{_f(ov_w.get('norm_per_week'))} (×{_f(ov_w.get('ratio'))}; у рынка "
               f"×{_f(ov_w.get('market_ratio'))}).")
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
        od.fact(subject=slug,
                attribute=f"Жалоба: {label or (themes[0] if themes else 'прочее')}",
                value=(it.get("summary") or verbatim)[:300], verbatim=verbatim, url=url,
                date=str(it.get("date") or "")[:10])
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
    for slug in subjects:
        bank = _bank_name(slug, labels)
        try:
            done = _complaints_for_bank(od, slug, bank, scope, slug == anchor, signal_themes)
        except Exception as e:  # noqa: BLE001 — банк без корпуса не должен ронять сбор
            log.info("жалобы %s: %s", bank, type(e).__name__)
            continue
        # Жалобы-цитаты по предмету — конкурентам и точке отсчёта, если темы не
        # разобраны (для темы они уже пришли выборкой сигнала или лентой).
        if slug != anchor or not done:
            query = (getattr(plan, "product", "") or question)[:120]
            res = _j("complaint_search", query=query, bank=bank, product=scope["product"],
                     days=max(scope["days"], 90),
                     limit=_COMPLAINTS_PER_BANK if slug == anchor else 8)
            _complaint_quotes(od, slug, bank, res.get("complaints") or [], None)


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
    txt = (f"Раздел «Уязвимости»: всего лазеек по всем банкам — {_n(st.get('all_banks_total'))} "
           f"(с {_dm(st.get('collected_since'))}), найдено за 30 дней — "
           f"{_n(st.get('all_banks_found_last_30d'))}; с меткой «{bank}» — "
           f"{_n(st.get('this_bank_tagged_total'))}. Все оценки предварительные: "
           f"человеком не проверены.")
    od.page(s_url, "AuditLens · Уязвимости: сводка раздела", [txt], "loopholes")
    od.fact(subject=anchor, attribute="Лазейки: сводка раздела",
            value=_n(st.get("all_banks_total")), verbatim=txt, url=s_url, stance=LOOPHOLE)
    for r in own + other:
        url = r.get("url") or f"#loophole?record={r['record_id']}"
        title = (r.get("title") or "").strip()
        lines = [x for x in (
            f"Схема: {title}" if title else "",
            f"Почему лазейка: {r['why_loophole']}" if r.get("why_loophole") else "",
            (r.get("text") or "").strip(),
            f"Источник: {r.get('source') or '—'}; опубликовано {_dm(r.get('published'))}; "
            f"найдено системой {_dm(r.get('found'))}; статус: предварительная оценка модели.",
        ) if x]
        od.page(url, f"AuditLens · Уязвимости: {title[:80]}", lines, "loopholes")
        tag = r.get("bank_tag") or ""
        subj = anchor if r.get("about_bank") else (tag if tag in labels else "")
        od.fact(subject=subj, attribute="Лазейка", value=title[:200],
                verbatim=f"Схема: {title}", url=url, stance=LOOPHOLE,
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
    metric, unit = mo.get("metric") or "метрика", mo.get("metric_unit") or ""
    label = mo.get("label") or cat
    caveat = mo.get("how_to_compare")

    def slug_for(name: str) -> str:
        if name in by_name:
            return by_name[name]
        return anchor if name == T.SBER or T._same_bank(T.SBER, name) else ""

    groups = (mo.get("by_bank") or {}).items() if mo.get("by_bank") else \
        [("", {"best": mo.get("top") or [], "offers_total": mo.get("offers_total")})]
    for name, d in groups:
        offers = (d.get("best") or [])[:_OFFERS_PER_BANK if name else 10]
        if not offers:
            continue
        url = T.link_market(cat, name or None)
        head = (f"Витрина «Рынок», категория «{label}», {name or 'лидеры рынка'}: "
                f"предложений — {_n(d.get('offers_total'))}; лучшие по метрике «{metric}» "
                f"({'меньше — лучше' if mo.get('lower_is_better') else 'больше — лучше'}).")
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
    url = T.link_market(cat)
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


# ── вход ─────────────────────────────────────────────────────────────────────

async def collect(client, model: str, question: str, plan) -> OwnData:
    """Всё собственное: срез, аналитика и цитаты жалоб, лазейки. Синхронные
    части — в пуле потоков; сбор идёт параллельно веб-поиску."""
    import asyncio
    od = OwnData()
    state = runstate.current()
    scope = await scope_for(client, model, question, plan)
    od.scope = scope

    def work():
        runstate.bind(state)
        for need, fn, args in ((scope["complaints"], collect_complaints, (od, plan, scope, question)),
                               (scope["loopholes"], collect_loopholes, (od, plan, question)),
                               (scope["market"], collect_market, (od, plan, scope))):
            if not need:
                continue
            try:
                fn(*args)
            except Exception:
                log.exception("собственные данные: %s", fn.__name__)
    await asyncio.to_thread(work)
    state.own_meta.update(od.meta)
    log.info("собственные данные: срез %s; страниц %d, фактов %d (жалоб %d, лазеек %d, "
             "рынок %d)", scope, len(od.pages), len(od.facts), od.complaints, od.loopholes,
             od.market)
    return od
