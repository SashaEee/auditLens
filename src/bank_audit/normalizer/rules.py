"""Словари и эвристики. Намеренно прозрачные и расширяемые."""

# Канонические алиасы банков -> slug. Все ключи в нижнем регистре, БЕЗ префиксов
# организационно-правовой формы (ПАО/АО/ООО/КБ — отрезаются normalize_bank_key).
BANK_ALIASES: dict[str, str] = {
    # ── Топ-20 ───────────────────────────────────────────────────────────────
    "сбер":            "sberbank", "сбербанк":   "sberbank", "сбер банк": "sberbank",
    "sber":            "sberbank", "sberbank":   "sberbank", "пао сбер":  "sberbank",
    "сбербанк россии": "sberbank",
    "втб":             "vtb", "vtb":            "vtb", "втб банк": "vtb",
    "тинькофф":        "tinkoff", "tinkoff":    "tinkoff",
    "т-банк":          "tinkoff", "т банк":     "tinkoff", "тбанк":     "tinkoff",
    "t-bank":          "tinkoff", "tcs":        "tinkoff",
    "т-инвестиции":    "tinkoff", "т инвестиции":"tinkoff",
    "тинькофф банк":   "tinkoff",
    "альфа":           "alfabank", "альфа-банк":"alfabank", "альфабанк":"alfabank",
    "alfa-bank":       "alfabank", "alfabank":  "alfabank", "альфа банк":"alfabank",
    "альфа-инвестиции":"alfabank",
    "газпромбанк":     "gazprombank", "гпб":    "gazprombank",
    "россельхозбанк":  "rshb", "рсхб":          "rshb",
    "открытие":        "otkritie", "открытие брокер":"otkritie",
    "открытие инвестиции":"otkritie",
    "райффайзен":      "raiffeisen", "raiffeisen":"raiffeisen",
    "райффайзенбанк":  "raiffeisen",
    "почта банк":      "pochtabank", "почтабанк":"pochtabank",
    "почта-банк":      "pochtabank",
    "мкб":             "mkb", "московский кредитный банк":"mkb",
    "московский кредитный":"mkb",
    "совкомбанк":      "sovcombank", "совком":  "sovcombank",
    "уралсиб":         "uralsib", "uralsib":    "uralsib",
    "хоум кредит":     "homecredit", "хоум":    "homecredit", "хоум банк":"homecredit",
    "homecredit":      "homecredit", "home credit":"homecredit",
    "ситибанк":        "citibank", "citibank":  "citibank",
    "юникредит":       "unicredit", "юникредитбанк":"unicredit",
    "unicredit":       "unicredit",
    "русский стандарт":"rsb", "рсб":            "rsb",
    "псб":             "psb",  "промсвязьбанк":"psb",
    "локо-банк":       "lokobank", "локо банк":"lokobank", "локо":"lokobank",
    "ак барс":         "akbars", "ак барс банк":"akbars", "акбарс":"akbars",
    "ак-барс":         "akbars", "akbars":     "akbars",
    "мтс банк":        "mtsbank", "мтс-банк":   "mtsbank", "мтсбанк":"mtsbank",
    "росбанк":         "rosbank", "rosbank":    "rosbank",
    "санкт-петербург": "bspb", "банк санкт-петербург":"bspb", "бспб":"bspb",
    "ренессанс":       "rencredit", "ренессанс кредит":"rencredit",
    "ренессанс банк":  "rencredit",
    "озон банк":       "ozonbank", "ozonbank":   "ozonbank", "ozon банк":"ozonbank",
    "ozon bank":       "ozonbank", "озонбанк":   "ozonbank",
    "яндекс банк":     "yandexbank", "яндексбанк":"yandexbank",
    "yandex банк":     "yandexbank", "yandex bank":"yandexbank",
    "дом рф":          "domrf",  "дом.рф":     "domrf", "домрф":"domrf",
    "абсолют банк":    "absolut", "абсолют":    "absolut",

    # ── Среднего размера ────────────────────────────────────────────────────
    "банк синара":     "sinara",  "синара":     "sinara",
    "примсоцбанк":     "primsoc",
    "челябинвестбанк": "chinvest", "челябинвест":"chinvest",
    "челиндбанк":      "chelindbank", "челинд": "chelindbank",
    "норвик банк":     "norvikbank", "норвик":  "norvikbank",
    "ланта":           "lanta", "ланта-банк":   "lanta", "лантa-банк":"lanta",
    "энергобанк":      "energobank",
    "энерготрансбанк": "energotrans",
    "прио":            "prio", "прио-внешторгбанк":"prio",
    "левобережный":    "levbank",
    "оренбург":        "oren", "банк оренбург":"oren",
    "вологжанин":      "vologzhanin",
    "хлынов":          "hlynov",  "банк хлынов":"hlynov",
    "газэнергобанк":   "gazenergobank",
    "русфинанс банк":  "rusfinance", "русфинанс":"rusfinance",
    "восточный":       "vostochny", "восточный экспресс":"vostochny",
    "интерпрогрессбанк":"ipb", "интерпрогресс":"ipb",
    "транскапиталбанк":"tcb", "тkb":            "tcb",
    "крайинвестбанк":  "krayinvest",

    # ── Жертвы fuzzy-склеек (аудит 22.07.2026) — явные алиасы, чтобы больше
    #    не всасывались в банки-магниты (Сбер/Тинькофф/Норвик/Ренессанс/Прио)
    "национальный банк сбережений": "nacionalnyj-bank-sberezhenij",
    "русский банк сбережений":      "russkij-bank-sberezhenij",
    "ик банк":         "ik-bank",  "ик-банк":    "ik-bank",
    "нс банк":         "ns-bank",  "нс-банк":    "ns-bank",
    "приобье":         "priobe",   "банк приобье":"priobe",
    "автокредитбанк":  "avtokreditbank",
    "ит банк":         "it-bank",  "ит-банк":    "it-bank",
    "юг-инвестбанк":   "jug-investbank", "юг инвестбанк":"jug-investbank",
    "металлинвестбанк":"metallinvestbank",
    "реалист банк":    "realistbank", "реалистбанк":"realistbank",
    "роял кредит банк":"rojal-kredit-bank",
    "рокетбанк":       "roketbank",
    "углеметбанк":     "uglemetbank",
    "зираат банк":     "ziraat-bank-moskva", "зираат банк москва":"ziraat-bank-moskva",

    # ── Брокеры/НПФ ─────────────────────────────────────────────────────────
    "финам":           "finam",
    "бкс":             "bks", "бкс банк":       "bks", "бкс мир инвестиций":"bks",
    "алор брокер":     "alor",
    "втб мои инвестиции":"vtb", "втб инвестиции":"vtb",
    "сбер инвестиции": "sberbank",
}


# Префиксы организационно-правовой формы — отбрасываются нормализатором ниже.
_LEGAL_FORM_PREFIXES = (
    "пао ", "оао ", "ао ", "зао ", "ооо ", "кб ", "акб ", "пjsc ",
    "акционерное общество ", "публичное акционерное общество ",
    "коммерческий банк ",
)


def normalize_bank_key(raw: str) -> str:
    """Нормализует имя банка для lookup в BANK_ALIASES.
    Шаги:
      lower → удаляем кавычки/скобки → collapse whitespace → отрезаем
      префиксы орг.-правовой формы (итеративно).
    """
    if not raw:
        return ""
    s = raw.lower()
    for ch in ("«", "»", "“", "”", '"', "'", "‘", "’", "(", ")"):
        s = s.replace(ch, " ")
    s = " ".join(s.split())
    changed = True
    while changed:
        changed = False
        for p in _LEGAL_FORM_PREFIXES:
            if s.startswith(p):
                s = s[len(p):].strip()
                changed = True
    return s


SBER_SLUGS = {"sberbank"}

# Темы жалоб: ключевые слова -> топик
COMPLAINT_TOPICS: dict[str, list[str]] = {
    "fees":          ["комиссия", "комиссии", "списали", "удержание", "плата за"],
    "rate_change":   ["снизили ставку", "понизили", "изменили условия", "процент упал"],
    "app_bugs":      ["приложение", "не работает", "висит", "тормозит", "вылетает"],
    "support":       ["поддержка", "оператор", "не отвечают", "ждал", "хамство", "грубость"],
    "card_block":    ["заблокировали карту", "блокировка", "арестовали счет", "арест счета"],
    "credit_terms":  ["навязали страховку", "страховка", "обманули", "скрытые условия"],
    "deposit_terms": ["вклад", "не начислили", "проценты не пришли"],
    "atm":           ["банкомат", "съел купюры", "не выдал"],
    "transfers":     ["перевод", "сбп", "не дошёл", "не дошел"],
}

POS_WORDS = {"спасибо", "доволен", "удобно", "быстро", "помогли", "рекомендую"}
NEG_WORDS = {"ужас", "обман", "плохо", "хамство", "не работает", "проблема", "жалоба", "не вернули"}
