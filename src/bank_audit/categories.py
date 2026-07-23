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
    {"id": "credit",      "label": "Кредиты",         "ru": "кредиты",
     "metric": "rate_pct",   "metric_label": "Ставка от",  "metric_unit": "%",
     "metric_lower_is_better": True,
     "rate_label": "Ставка от",  "show_rate": True,  "show_bar": True,  "show_terms": True},
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
     "metric": "rate_pct",   "metric_label": "Ставка от",  "metric_unit": "%",
     "metric_lower_is_better": True,
     "rate_label": "Ставка от",  "show_rate": True,  "show_bar": True,  "show_terms": False},
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
    if (category == "mortgage" and rate is not None and key_rate
            and rate < key_rate - 3.0):
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
