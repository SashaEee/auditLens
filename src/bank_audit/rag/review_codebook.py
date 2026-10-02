"""Кодификатор отзывов: коды, подписи для аудитора, группы и класс риска.

Один источник для всех: разметка (review_annotate) берёт отсюда допустимые
коды, вкладка «Отзывы», сигналы и обзор — подписи и риск. Текст для модели
(определения и правила разграничения) живёт рядом, в
config/review_codebook/<версия>.txt, и обязан перечислять те же коды.

Класс риска ставится здесь, а не моделью: это суждение аудитора о том, чья
это ответственность, а не свойство отдельного отзыва.
  compliance — нарушение требований закона и регулятора;
  conduct    — недобросовестное отношение к клиенту, продажи;
  ops        — сбои процессов и обслуживания.
"""
from __future__ import annotations

VERSION = "v2.1-draft"

# код: (подпись, короткая подпись для чипа, группа, риск)
ISSUES: dict[str, tuple[str, str, str, str]] = {
    # Продажа и оформление
    "imposed_addon":      ("Навязанные допуслуги (страховка, подписка)", "Навязанная допуслуга", "sale", "conduct"),
    "without_consent":    ("Продукт или услуга без согласия клиента", "Без согласия", "sale", "conduct"),
    "refusal":            ("Отказ в продукте или услуге", "Отказ в продукте", "sale", "conduct"),
    "issuance_delay":     ("Задержка выдачи: карта, курьер, кредит", "Задержка выдачи", "sale", "ops"),
    # Условия и начисления
    "fees":               ("Спорные комиссии и платы", "Комиссии", "terms", "conduct"),
    "terms_change":       ("Одностороннее изменение условий", "Изменение условий", "terms", "conduct"),
    "loan_calc":          ("Ошибки в кредите: расчёт, погашение", "Расчёт по кредиту", "terms", "ops"),
    "deposit_interest":   ("Проценты по вкладам и накопительным счетам", "Проценты по вкладу", "terms", "ops"),
    "bonus":              ("Кэшбэк, бонусы, акции", "Кэшбэк и бонусы", "terms", "conduct"),
    # Операции
    "transfer_problem":   ("Переводы и платежи: задержка, потеря", "Переводы", "ops", "ops"),
    "wrong_debit":        ("Ошибочные и двойные списания банком", "Ошибочное списание", "ops", "ops"),
    "cash_atm":           ("Наличные и банкоматы", "Банкоматы", "ops", "ops"),
    "chargeback":         ("Оспаривание операций и возвраты (чарджбэк)", "Чарджбэк", "ops", "ops"),
    "limits":             ("Лимиты и отказы в операциях", "Лимиты", "ops", "ops"),
    # Блокировки
    "block_115":          ("Блокировки по 115-ФЗ", "115-ФЗ", "block", "compliance"),
    "block_161":          ("Блокировки по 161-ФЗ (антифрод)", "161-ФЗ", "block", "compliance"),
    "block_other":        ("Блокировки без объяснения причин", "Блокировка", "block", "conduct"),
    "funds_withheld":     ("Невозврат средств после блокировки или закрытия", "Невозврат средств", "block", "compliance"),
    # Мошенничество и данные
    "fraud_loss":         ("Хищения мошенниками", "Мошенничество", "fraud", "compliance"),
    "unauthorized_access": ("Несанкционированный доступ и операции", "Взлом, чужие операции", "fraud", "compliance"),
    "fraud_credit":       ("Кредиты и продукты, оформленные мошенниками", "Кредит мошенников", "fraud", "compliance"),
    "data_leak":          ("Утечка и передача персональных данных", "Персональные данные", "fraud", "compliance"),
    # Долги и принуждение
    "collection":         ("Взыскание долгов и коллекторы", "Взыскание", "debt", "conduct"),
    "third_party_calls":  ("Звонки третьим лицам по чужим долгам", "Звонки по чужим долгам", "debt", "conduct"),
    "restructuring":      ("Кредитные каникулы и реструктуризация", "Каникулы, реструктуризация", "debt", "compliance"),
    "credit_history":     ("Ошибки в кредитной истории", "Кредитная история", "debt", "compliance"),
    "enforcement":        ("Аресты и списания по исполнительным документам", "Исполнительные документы", "debt", "compliance"),
    "social_funds":       ("Списание пенсий и социальных выплат", "Соцвыплаты", "debt", "compliance"),
    "bankruptcy":         ("Банкротство клиента", "Банкротство", "debt", "compliance"),
    # Обслуживание
    "staff":              ("Грубость и некомпетентность сотрудников", "Сотрудники", "service", "conduct"),
    "support_access":     ("Недоступность поддержки", "Поддержка", "service", "ops"),
    "complaint_handling": ("Обращения не рассматриваются, отписки", "Работа с обращениями", "service", "compliance"),
    "branch_service":     ("Отделения: очереди, отказ обслужить", "Отделения", "service", "ops"),
    # Технологии
    "app_failure":        ("Сбои приложения и интернет-банка", "Сбой ДБО", "tech", "ops"),
    "access_id":          ("Вход, идентификация, смена данных", "Вход и идентификация", "tech", "ops"),
    # Документы и договоры
    "closing":            ("Закрытие продуктов и счетов", "Закрытие продукта", "docs", "ops"),
    "documents":          ("Справки, выписки, документы", "Документы", "docs", "ops"),
    "inheritance":        ("Наследство и счета умерших", "Наследство", "docs", "compliance"),
    # Маркетинг
    "spam_calls":         ("Навязчивые звонки и рассылки", "Спам-звонки", "mkt", "conduct"),
    # Прочее
    "other":              ("Прочее, вне кодификатора", "Прочее", "misc", "other"),
    "no_issue":           ("Без претензии", "Без претензии", "misc", "other"),
}

GROUPS: dict[str, str] = {
    "sale": "Продажа и оформление", "terms": "Условия и начисления", "ops": "Операции",
    "block": "Блокировки", "fraud": "Мошенничество и данные", "debt": "Долги и принуждение",
    "service": "Обслуживание", "tech": "Технологии", "docs": "Документы и договоры",
    "mkt": "Маркетинг", "misc": "Прочее",
}

# код: подпись. none — «без продукта»: в индекс пишется NULL
PRODUCTS: dict[str, str | None] = {
    "debit_card": "Дебетовая карта", "credit_card": "Кредитная карта",
    "consumer_loan": "Потребительский кредит", "mortgage": "Ипотека",
    "auto_loan": "Автокредит", "education_loan": "Образовательный кредит",
    "installment": "Рассрочка", "current_account": "Текущий счёт",
    "savings_account": "Накопительный счёт", "deposit": "Вклад",
    "transfers": "Переводы и платежи", "investments": "Инвестиции",
    "metals": "Драгметаллы и ОМС", "pension": "ПДС и НПФ", "insurance": "Страхование",
    "subscription": "Подписки и пакеты", "loyalty": "Бонусы и кэшбэк",
    "safe_escrow": "Ячейка, эскроу, аккредитив", "currency": "Валюта",
    "ecosystem": "Небанковские сервисы", "biz_account": "Бизнес: счёт и РКО",
    "biz_acquiring": "Бизнес: эквайринг", "biz_credit": "Бизнес: кредиты и гарантии",
    "none": None,
}

KINDS = ("complaint", "mixed", "question", "praise", "junk")
# что считается жалобой во всех счётчиках
COMPLAINT_KINDS = ("complaint", "mixed")
SEGMENTS = ("person", "business", "unclear")
CHANNELS = ("app", "branch", "support", "atm", "courier", "outbound", "partner", "collector", "web")
ESC = ("none", "threat", "filed")
ESC_TO = ("cbr", "court", "prosecutor", "rpn", "finombudsman", "police", "fas")
VULNERABLE = ("pensioner", "disabled", "ill", "minor", "svo", "low_income")

GROUP_OF = {k: v[2] for k, v in ISSUES.items()}


def issue_obj(code: str | None) -> dict | None:
    """Код проблемы → {key,label,short,risk,group} для API и чипов."""
    v = ISSUES.get(code or "")
    if not v:
        return None
    return {"key": code, "label": v[0], "short": v[1], "group": v[2],
            "group_label": GROUPS.get(v[2]), "risk": v[3]}


def complaint_issues() -> list[dict]:
    """Все проблемы, которые бывают у жалобы (без «без претензии»)."""
    return [issue_obj(k) for k in ISSUES if k != "no_issue"]


def product_label(code: str | None) -> str | None:
    return PRODUCTS.get(code or "")


PRODUCT_CODE_BY_LABEL = {v.lower(): k for k, v in PRODUCTS.items() if v}
