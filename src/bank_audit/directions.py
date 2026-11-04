"""Направления аудита — общая ось всех разделов.

Руководство считает работу по двенадцати направлениям (ипотека, драгметаллы,
выездное обслуживание, дети и молодёжь, самозанятые, кредитные карты,
эквайринг, УС, инвестиции, потребкредиты, участники СВО, зарплатные проекты).
До 03.11.2026 у каждого раздела был свой словарь тем, и ни один не совпадал с
этим списком: профиль «Для вас» узнавал 4 направления из 12 (описание
«самозанятые, сервис "Своё дело"» превращалось в пустой список, и лента
строилась по случайным кликам), в «Аудите отзывов» не было самозанятых, детей
и СВО, ИИ на вопрос о «Своём деле» находил жалобы на СберБизнес.

Здесь — одно место, где направление связано с каждым разделом:
• topic + keywords — тема профиля «Для вас» и новостей (userdata._PRODUCT_KEYWORDS
  размечает этими выражениями и текст профиля, и сами новости);
• review_products / review_slices / review_vulnerable — как направление
  выглядит в разметке «Аудита отзывов»;
• review_gap — если в отзывах направления пока нет, это записано явно, с причиной.
tests/test_directions.py не даёт добавить направление, не связав его со всеми
разделами, и не даёт пробелу появиться молча.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Direction:
    code: str
    name: str
    topic: str                       # тема профиля и новостей (слаг)
    topic_label: str                 # подпись темы на чипе
    keywords: str                    # регулярное выражение темы (без учёта регистра)
    review_products: tuple[str, ...] = ()
    review_slices: tuple[str, ...] = ()
    review_vulnerable: tuple[str, ...] = ()
    review_gap: str = ""             # почему в отзывах направления пока нет
    extra: dict = field(default_factory=dict, compare=False, hash=False)


DIRECTIONS: tuple[Direction, ...] = (
    Direction("mortgage", "Ипотека", "ipoteka", "ипотека", r"ипотек",
              review_products=("Ипотека",)),
    Direction("metals", "Операции с драгметаллами", "metals", "драгметаллы",
              r"драгметалл|драгоценн\w*\s+металл|\bомс\b|обезличенн\w*\s+металл|слитк|инвестиционн\w*\s+монет",
              review_products=("Драгметаллы и ОМС",)),
    Direction("field", "Выездное обслуживание", "field", "выездное обслуживание",
              r"курьер|выездн\w*\s+(?:менеджер|обслуживан|сотрудник|сервис)|доставк\w*\s+карт",
              review_slices=("field",)),
    Direction("youth", "Дети и молодёжь", "youth", "дети и молодёжь",
              r"детск\w*\s+карт|подростк|молод[её]жн|школьник|студент|сберкидс|сбер\s?kids|несовершеннолет",
              review_slices=("youth",), review_vulnerable=("minor",)),
    Direction("selfemp", "Самозанятые", "selfemp", "самозанятые",
              r"самозанят|сво[её]\s+дел|\bнпд\b|налог\w*\s+на\s+профессиональн",
              review_slices=("selfemp",)),
    Direction("credit_card", "Кредитные карты", "credit_card", "кредитные карты",
              r"кредитн\w* карт|кредитк", review_products=("Кредитная карта",)),
    Direction("acquiring", "Эквайринг", "acquiring", "эквайринг", r"эквайринг",
              review_products=("Бизнес: эквайринг",)),
    Direction("atm", "Устройства самообслуживания", "atm", "банкоматы и УС",
              r"банкомат|устройств\w*\s+самообслуживан|терминал\w*\s+самообслуживан|инфокиоск",
              review_slices=("us",)),
    Direction("invest", "Инвестиционные продукты", "invest", "инвестиции",
              r"инвестиц|брокерск|\bиис\b|облигац|\bпиф\b|доверительн\w*\s+управлен|\bисж\b",
              review_products=("Инвестиции",)),
    Direction("consumer", "Потребительские кредиты", "consumer_loan", "потребкредиты",
              r"потребит\w* кред|кредит наличн|наличными", review_products=("Потребительский кредит", "Рассрочка")),
    Direction("svo", "Участники СВО", "svo", "участники СВО",
              r"\bсво\b|специальн\w*\s+военн|военнослужащ|мобилизован|участник\w*\s+боев",
              review_slices=("svo",), review_vulnerable=("svo",)),
    Direction("payroll", "Зарплатные проекты", "payroll", "зарплатные проекты",
              r"зарплатн\w*\s+(?:проект|карт|клиент)", review_slices=("payroll",)),
)
OTHER = ("other", "Другое (вклады, дебетовые карты, переводы, РКО, страхование)")

BY_CODE = {d.code: d for d in DIRECTIONS}


def keyword_rules() -> list[tuple[re.Pattern, str]]:
    """(выражение, тема) для профиля и новостей."""
    return [(re.compile(d.keywords, re.I), d.topic) for d in DIRECTIONS]


def topic_labels() -> dict[str, str]:
    return {d.topic: d.topic_label for d in DIRECTIONS}


def review_maps() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Продукт / срез / уязвимая группа «Аудита отзывов» → код направления. В первом
    словаре — и подписи срезов: фильтр продукта во вкладке, подписки и выгрузки хранят
    именно подпись («Зарплатные проекты»), а не ключ среза."""
    from .rag.review_slices import SLICES
    prod, sl, vul = {}, {}, {}
    for d in DIRECTIONS:
        for p in d.review_products:
            prod[p] = d.code
        for s in d.review_slices:
            sl[s] = d.code
            if s in SLICES:
                prod[SLICES[s][0]] = d.code
        for v in d.review_vulnerable:
            vul[v] = d.code
    return prod, sl, vul


DIR_PREFIX = "dir:"


def product_fn_sql() -> str:
    """Текст SQL-функции al_product_is: продукт, особый срез или направление аудита
    (want = 'dir:<код>' — продукты ИЛИ срезы направления). Миграция с этой функцией
    генерируется отсюда, тест сверяет: иначе фильтр в SQL разойдётся с кодом."""
    from .rag.review_slices import SLICES

    def q(v: str) -> str:
        return "'" + v.replace("'", "''") + "'"

    parts = ["product = want"]
    for key, (label, _d) in SLICES.items():
        parts.append(f"(want = {q(label)} AND {q(key)} = ANY(coalesce(slices, '{{}}')))")
    for d in DIRECTIONS:
        conds = []
        if d.review_products:
            conds.append("product = ANY(ARRAY[" + ", ".join(q(x) for x in d.review_products) + "])")
        if d.review_slices:
            conds.append("coalesce(slices, '{}') && ARRAY[" + ", ".join(q(x) for x in d.review_slices) + "]")
        if conds:
            parts.append(f"(want = {q(DIR_PREFIX + d.code)} AND ({' OR '.join(conds)}))")
    body = "\n        OR ".join(parts)
    return ("CREATE OR REPLACE FUNCTION al_product_is(product text, slices text[], want text)\n"
            "RETURNS boolean LANGUAGE sql IMMUTABLE AS $$\n"
            f"    SELECT {body}\n$$;")


def by_product_param(product: str | None) -> Direction | None:
    """Направление по значению фильтра продукта 'dir:<код>' (иначе None)."""
    if product and product.startswith(DIR_PREFIX):
        return BY_CODE.get(product[len(DIR_PREFIX):])
    return None


def product_label(product: str | None) -> str | None:
    """Подпись фильтра для людей: 'dir:mortgage' → «Направление: Ипотека»."""
    d = by_product_param(product)
    return f"Направление: {d.name}" if d else product

