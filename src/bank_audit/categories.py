"""Единый источник правды по категориям продуктов.

До этого знание жило в 4 рассинхронизированных местах: enum БД (001_init.sql),
CATS_ORDER/CAT_LABELS во фронте, _CAT_RU в digest/writer.py (с битыми ключами
autocredit/credit_card) и Literal в models.py. Фронт получает всё через
/api/meta/categories.

У каждой витринной категории есть СВОЯ сопоставимая метрика (metric): ставка
у вкладов/кредитов, стоимость обслуживания у дебетовых карт, грейс-период у
кредитных. Пустых категорий на витрине нет — то, что нечем сравнивать и нечем
наполнить, вынесено в RETIRED.
"""
from __future__ import annotations

# Семантика витрины и СОПОСТАВИМАЯ МЕТРИКА по категориям (аудит 22–23.07.2026).
#   metric — поле, по которому строится атлас/ранг: у вкладов и кредитов ставка,
#     у дебетовых карт годовая стоимость обслуживания, у кредитных — грейс-период
#     (у карт «ставки» как класса нет: sravni отдаёт промо-минимумы «от 0%»);
#   metric_lower_is_better — направление: дешевле обслуживание = лучше,
#     длиннее грейс = лучше, ниже кредитная ставка = лучше;
#   secondary — доп. колонка витрины (кешбэк, ПСК «от»), не участвует в ранге.
CATEGORIES: list[dict] = [
    {"id": "deposit",     "label": "Вклады",          "ru": "вклады",
     "metric": "rate_pct",   "metric_label": "Ставка",     "metric_unit": "%",
     "metric_lower_is_better": False,
     "rate_label": "Ставка",     "show_rate": True,  "show_bar": True,  "show_terms": True},
    {"id": "savings_account", "label": "Накопительные счета", "ru": "накопительные счета",
     "metric": "rate_pct",   "metric_label": "Ставка",     "metric_unit": "%",
     "metric_lower_is_better": False,
     "rate_label": "Ставка",     "show_rate": True,  "show_bar": True,  "show_terms": False,
     "caveat": "Ставка накопительного счёта плавающая и часто зависит от остатка и трат — "
               "сравнивайте базовую, а не промо на первые месяцы"},
    # ПСК вместо ставки «от»: у 17 из 30 лидеров рынка разрыв ПСК−ставка больше
    # 5 пп при медианном разрыве 0 — рекламная граница давала фору тизерам
    # (аудит 11.08.2026). Фолбэк на rate_pct задан в market_atlas.
    {"id": "credit",      "label": "Кредиты",         "ru": "кредиты",
     "metric": "psk_min",    "metric_label": "ПСК от",     "metric_unit": "%",
     "metric_lower_is_better": True,
     "rate_label": "Ставка от",  "show_rate": True,  "show_bar": True,  "show_terms": True,
     "caveat": "Сравнение по полной стоимости кредита (353-ФЗ): ставка «от» — рекламная "
               "граница, у 17 из 30 лидеров рынка ПСК выше неё более чем на 5 пп"},
    {"id": "mortgage",    "label": "Ипотека",         "ru": "ипотека",
     "metric": "rate_pct",   "metric_label": "Ставка от",  "metric_unit": "%",
     "metric_lower_is_better": True,
     "rate_label": "Ставка от",  "show_rate": True,  "show_bar": True,  "show_terms": False,
     "caveat": "Минимальные ставки программ: господдержка и рыночные смешаны — сравнивайте внутри программы"},
    {"id": "card_credit", "label": "Кредитные карты", "ru": "кредитные карты",
     "metric": "grace_days", "metric_label": "Грейс-период", "metric_unit": " дн",
     "metric_lower_is_better": False,
     "rate_label": "ПСК от",     "show_rate": True,  "show_bar": False, "show_terms": False,
     "secondary": "cashback_pct",
     "caveat": "Сравнение по грейс-периоду: ставка у карт — промо-минимум ПСК («от 0%» у рассрочек), ранжировать по ней некорректно"},
    {"id": "card_debit",  "label": "Дебетовые карты", "ru": "дебетовые карты",
     "metric": "fee_service", "metric_label": "Обслуживание", "metric_unit": " ₽/год",
     "metric_lower_is_better": True,
     "rate_label": None,         "show_rate": False, "show_bar": False, "show_terms": False,
     "secondary": "cashback_pct",
     "caveat": "Сравнение по безусловной стоимости обслуживания (руб./год); разовая плата за выпуск — отдельно"},
    {"id": "auto_loan",   "label": "Автокредиты",     "ru": "автокредиты",
     "metric": "psk_min",    "metric_label": "ПСК от",     "metric_unit": "%",
     "metric_lower_is_better": True,
     "rate_label": "Ставка от",  "show_rate": True,  "show_bar": True,  "show_terms": False,
     "caveat": "Сравнение по полной стоимости кредита: у Сбера ставка «от» 16,4 проц. при "
               "ПСК 23,3 проц., и ранг по ставке давал фору банкам с тизерными условиями"},
]

# Категории, снятые с витрины (нет источника/сопоставимой метрики) — чтобы в
# интерфейсе не было пустых вкладок. metals: ОМС-раздела с ставками у sravni
# нет вовсе, доход по металлам курсовой — нужен отдельный источник котировок.
RETIRED = {"metals": "ОМС: источник котировок не подключён"}

CAT_IDS = [c["id"] for c in CATEGORIES]
# ниже = лучше по СТАВКЕ (для витрины/сортировки офферов)
LOWER_IS_BETTER = {c["id"] for c in CATEGORIES
                   if c["metric"] == "rate_pct" and c["metric_lower_is_better"]}
CAT_META = {c["id"]: c for c in CATEGORIES}
CAT_LABEL = {c["id"]: c["label"] for c in CATEGORIES}

# Русские названия для LLM-промптов дайджеста (шире витринных категорий)
CAT_RU = {**{c["id"]: c["ru"] for c in CATEGORIES},
          "savings": "накопительные счета", "transfers": "переводы",
          "microloan": "микрозаймы", "other": "прочее"}


# Программы с господдержкой: ставка установлена государством и одинакова у всех
# банков — из рыночного сравнения исключаются (иначе «Сбер #2 на рынке кредитов»
# из-за образовательного кредита под 3%). Аудит 23.07.2026.
import re as _re

_SUBSIDIZED_RE = _re.compile(
    r"господдержк|гос\.\s*поддержк|семейн\w*\s*(ипотек|программ)?|"
    r"минпромторг|госпрограмм|субсиди\w*\s*(производител|автопроизводител)?|"
    r"первый\s+автомобиль|семейный\s+автомобиль|"
    r"\bit[- ]?ипотек|ит[- ]?ипотек|военн\w*\s*ипотек|дальневосточн|"
    r"сельск\w*\s*ипотек|материнск|образовательн|"
    r"для\s+семей\s+с\s+детьми|льготн\w*\s+(для\s+семей|ипотек)|"
    r"\b0[,.]1\s*%|субсиди",          # 0,1% — субсидия застройщика, не ставка банка
    _re.IGNORECASE)

# Субсидии существуют только у кредитных продуктов. У вкладов «Семейная копилка»
# и у карт «С льготным периодом» (это грейс!) — не субсидии (аудит 23.07.2026).
_SUBSIDY_CATEGORIES = {"mortgage", "credit", "auto_loan"}


def is_subsidized(title: str | None, category: str | None = None,
                  rate: float | None = None, key_rate: float | None = None) -> bool:
    """Программа с субсидией (государство или застройщик): ставку задаёт не банк.
    Образовательные кредиты в РФ субсидируются под 3% для всех банков.

    Кроме названий работает ЧИСЛОВОЙ страж: рыночная ипотека не может стоить
    сильно дешевле ключевой ставки — всё, что ниже (КС − 3 пп), субсидировано
    независимо от того, как программа названа («Для семей с детьми» и т.п.).
    """
    if category is not None and category not in _SUBSIDY_CATEGORIES:
        return False
    if title and _SUBSIDIZED_RE.search(title):
        return True
    # Числовой страж на ВСЕ кредитные категории, а не только ипотеку: рыночный
    # кредит не может стоить сильно дешевле ключевой ставки. В автокредитах
    # лидером рынка стоял «0,01 проц.» (субсидия автопроизводителя), в
    # потребкредитах — «1,0 проц.» Яндекс Банка при ПСК от 22 проц.; оба
    # задавали медиану и разрыв, против которых мерили Сбер (аудит 11.08.2026).
    if (category in ("mortgage", "auto_loan", "credit")
            and rate is not None and key_rate and rate < key_rate - 3.0):
        return True
    return False


# Не кредитные организации, попадающие в витрины sravni (застройщики, сервисы
# подбора). Сравнивать их с банками нельзя — аудит 23.07.2026.
_NON_BANK_RE = _re.compile(
    r"подбор\s+квартир|онлайн[- ]заявк|заявка\s+в\s+несколько|"
    r"^пик$|^самолет$|^а101$|застройщик|яндекс\s+вертикал|"
    r"агентство\s+недвижимост|^дом[- ]?клик|маркетплейс|сервис\s+подбор",
    _re.IGNORECASE)


def is_non_bank(name: str | None) -> bool:
    return bool(name and _NON_BANK_RE.search(name.strip()))


# Тот же фильтр для SQL (POSIX-regex Postgres): витрина и счётчики категорий
NON_BANK_SQL_RE = (r"подбор\s+квартир|онлайн[- ]заявк|заявка\s+в\s+несколько|"
                   r"^пик$|^самолет$|^а101$|застройщик|яндекс\s+вертикал|"
                   r"агентство\s+недвижимост|^дом[- ]?клик|маркетплейс|сервис\s+подбор")


# ── Сегменты и подсегменты (аудит вкладки «Рынок» 11.08.2026) ────────────────
# В одном ранжире лежали премиальная карта за 47 880 руб./год и детская за 0,
# беззалоговый кредит и кредит под залог недвижимости, новостройка и вторичка.
# Сравнение внутри такой смеси даёт аудитору неверный вывод о позиции банка,
# поэтому ранг считается внутри (категория, сегмент, подсегмент).
import re as _re2

SEGMENTS = (("premium", "Премиальный"), ("private", "Private"),
            ("kids", "Детские"), ("youth", "Молодёжные"),
            ("pension", "Пенсионные"), ("mass", "Массовый"))

_SEGMENT_RULES: list[tuple] = [
    ("private", _re2.compile(r"private|приват", _re2.I)),
    # «Премиум» без «премиальная»: сверка с текстом тарифа (SEGMENT_MISMATCH)
    # показала 9 продуктов, которые правило по названию не относило к премиуму —
    # «Пакет услуг "Премиум"», «На новый автомобиль — Премиум»
    ("premium", _re2.compile(r"привилег|премьер|премиум|премиал|premium|only\b|signature|"
                             r"infinite|supreme|первый|прайм|world elite|platinum", _re2.I)),
    ("kids", _re2.compile(r"детск|юниор|junior|подростк|kids", _re2.I)),
    ("youth", _re2.compile(r"молод[её]жн|студен|teen", _re2.I)),
    ("pension", _re2.compile(r"пенсион", _re2.I)),
]

# Подсегмент — «что именно за продукт» внутри категории. Ключ словаря — наша
# категория, значение — правила по названию (порядок важен: первое совпадение).
_SUBSEGMENT_RULES: dict[str, list[tuple]] = {
    "mortgage": [
        ("refin",     _re2.compile(r"рефинанс", _re2.I)),
        ("subsidized", _re2.compile(r"семейн|господдержк|it[- ]?специал|ит[- ]?специал|"
                                    r"дальневосточн|арктическ|сельск|военн|материнск", _re2.I)),
        ("pledge",    _re2.compile(r"под залог|залог недвиж|нецелев", _re2.I)),
        ("house",     _re2.compile(r"строительств|\bижс\b|частн\w+ дом|таунхаус|коттедж", _re2.I)),
        ("new",       _re2.compile(r"новостройк|первичн|строящ", _re2.I)),
        ("secondary", _re2.compile(r"вторичн|готов\w+ жиль", _re2.I)),
        ("commercial", _re2.compile(r"коммерческ|апартамент|гараж|машино", _re2.I)),
    ],
    "credit": [
        ("refin",  _re2.compile(r"рефинанс", _re2.I)),
        ("pledge", _re2.compile(r"под залог|залог", _re2.I)),
        ("auto",   _re2.compile(r"авто", _re2.I)),
        ("cash",   _re2.compile(r"наличны|на любые цели|потребит", _re2.I)),
    ],
    "card_credit": [
        ("installment", _re2.compile(r"рассрочк|халва|свобода|совесть", _re2.I)),
        ("classic",     _re2.compile(r".", _re2.I)),
    ],
}


def classify_segment(title: str | None) -> str:
    """Сегмент клиента по названию продукта (mass — по умолчанию)."""
    t = title or ""
    for seg, rx in _SEGMENT_RULES:
        if rx.search(t):
            return seg
    return "mass"


def classify_sub_segment(category: str | None, title: str | None) -> str | None:
    """Вид продукта внутри категории: новостройка/вторичка/залог/рефинанс…"""
    rules = _SUBSEGMENT_RULES.get(category or "")
    if not rules:
        return None
    t = title or ""
    for sub, rx in rules:
        if rx.search(t):
            return sub
    return None
