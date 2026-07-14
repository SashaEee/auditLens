"""Тесты маскировки персональных данных (pii_mask).

Без сети и БД: проверяются только регулярные выражения и логика замены.
"""
from __future__ import annotations

from bank_audit.loophole.pii_mask import mask, mask_dict, restore


# ---------------------------------------------------------------------------
# Телефон
# ---------------------------------------------------------------------------
def test_phone_plus7_with_spaces() -> None:
    text = "Звоните: +7 912 345 67 89 после 18:00."
    masked, repl = mask(text)
    assert "[PHONE_1]" in masked
    assert repl["[PHONE_1]"] == "+7 912 345 67 89"
    assert "912 345 67 89" not in masked


def test_phone_8_prefix_dashes() -> None:
    text = "тел. 8-916-123-45-67"
    masked, repl = mask(text)
    assert repl["[PHONE_1]"] == "8-916-123-45-67"
    assert "916-123-45-67" not in masked


def test_phone_parens_code() -> None:
    text = "Контактный телефон +7 (495) 123-45-67."
    masked, repl = mask(text)
    assert repl["[PHONE_1]"] == "+7 (495) 123-45-67"
    assert "[PHONE_1]" in masked


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def test_email_simple() -> None:
    text = "Пишите на ivanov@example.com по вопросам."
    masked, repl = mask(text)
    assert repl["[EMAIL_1]"] == "ivanov@example.com"
    assert "ivanov@example.com" not in masked


def test_email_with_subdomain_and_plus() -> None:
    text = "Обратная связь: i.petrov+bank@sub.example.co.uk"
    masked, repl = mask(text)
    assert repl["[EMAIL_1]"] == "i.petrov+bank@sub.example.co.uk"


# ---------------------------------------------------------------------------
# ФИО
# ---------------------------------------------------------------------------
def test_fio_three_words() -> None:
    text = "Заявление подал Иванов Иван Иванович 12.01.2024."
    masked, repl = mask(text)
    assert repl["[NAME_1]"] == "Иванов Иван Иванович"
    assert "Иванов Иван Иванович" not in masked


def test_fio_hyphenated_surname() -> None:
    text = "Ответственный: Петрова-Водкина Анна Сергеевна."
    masked, repl = mask(text)
    assert repl["[NAME_1]"] == "Петрова-Водкина Анна Сергеевна"


def test_fio_not_two_words() -> None:
    # Два слова — не ФИО, не маскируется.
    text = "Иванов Иван пришёл на встречу."
    masked, repl = mask(text)
    assert repl == {}
    assert masked == text


# ---------------------------------------------------------------------------
# Паспорт
# ---------------------------------------------------------------------------
def test_passport_two_pairs() -> None:
    text = "Паспорт: 45 07 123456 выдан ОВД."
    masked, repl = mask(text)
    assert repl["[PASSPORT_1]"] == "45 07 123456"
    assert "123456" not in masked


def test_passport_single_series() -> None:
    text = "Документ № 4507 123456."
    masked, repl = mask(text)
    assert repl["[PASSPORT_1]"] == "4507 123456"


# ---------------------------------------------------------------------------
# СНИЛС
# ---------------------------------------------------------------------------
def test_snils() -> None:
    text = "СНИЛС: 112-233-445-95."
    masked, repl = mask(text)
    assert repl["[SNILS_1]"] == "112-233-445-95"
    assert "112-233-445-95" not in masked


# ---------------------------------------------------------------------------
# ИНН
# ---------------------------------------------------------------------------
def test_inn_12_digits() -> None:
    text = "ИНН физлица: 771234567890."
    masked, repl = mask(text)
    assert repl["[INN_1]"] == "771234567890"


def test_inn_10_digits() -> None:
    text = "ИНН организации 7712345678."
    masked, repl = mask(text)
    assert repl["[INN_1]"] == "7712345678"


# ---------------------------------------------------------------------------
# Банковская карта
# ---------------------------------------------------------------------------
def test_card_with_spaces() -> None:
    text = "Карта 4111 1111 1111 1111 списана."
    masked, repl = mask(text)
    assert repl["[CARD_1]"] == "4111 1111 1111 1111"
    assert "4111" not in masked


def test_card_solid() -> None:
    text = "Номер карты 4111111111111111."
    masked, repl = mask(text)
    assert repl["[CARD_1]"] == "4111111111111111"


def test_card_with_dashes() -> None:
    text = "Карта-клиента 5555-5555-5555-5555."
    masked, repl = mask(text)
    assert repl["[CARD_1]"] == "5555-5555-5555-5555"


# ---------------------------------------------------------------------------
# Адрес
# ---------------------------------------------------------------------------
def test_address_simple() -> None:
    text = "Проживает: ул. Ленина, д. 15, кв. 3."
    masked, repl = mask(text)
    assert "[ADDRESS_1]" in masked
    assert repl["[ADDRESS_1]"].startswith("ул. Ленина, д. 15")
    assert "Ленина, д. 15" not in masked


def test_address_prospect_with_corpus() -> None:
    text = "Адрес: пр. Мира, д. 10, корп. 2."
    masked, repl = mask(text)
    assert "[ADDRESS_1]" in masked
    assert "Мира, д. 10" not in masked


# ---------------------------------------------------------------------------
# Порядок и приоритет
# ---------------------------------------------------------------------------
def test_phone_before_card() -> None:
    # Телефон не должен быть захвачен как 16-значная карта.
    text = "Тел. +7 999 123 45 67, карта 4111 1111 1111 1111."
    masked, repl = mask(text)
    assert repl["[PHONE_1]"] == "+7 999 123 45 67"
    assert repl["[CARD_1]"] == "4111 1111 1111 1111"


def test_short_numeric_not_masked() -> None:
    # Короткие числа (<10 цифр) не маскируются.
    text = "Сумма перевода 12345 рублей, код 6789."
    masked, repl = mask(text)
    assert repl == {}
    assert masked == text


# ---------------------------------------------------------------------------
# mask_dict
# ---------------------------------------------------------------------------
def test_mask_dict_nested() -> None:
    record = {
        "title": "Жалоба от Иванов Иван Иванович",
        "meta": {
            "phone": "+7 912 345 67 89",
            "email": "user@example.com",
            "tags": ["срочно", "карта 4111 1111 1111 1111"],
            "count": 42,
        },
    }
    masked = mask_dict(record)
    assert masked["meta"]["phone"] == "[PHONE_1]"
    assert masked["meta"]["email"] == "[EMAIL_1]"
    assert "[NAME_1]" in masked["title"]
    assert "[CARD_1]" in masked["meta"]["tags"][1]
    # Нечувствительные значения сохраняются.
    assert masked["meta"]["count"] == 42
    assert masked["meta"]["tags"][0] == "срочно"


def test_mask_dict_empty() -> None:
    assert mask_dict({}) == {}


# ---------------------------------------------------------------------------
# Идемпотентность
# ---------------------------------------------------------------------------
def test_idempotent() -> None:
    text = (
        "Клиент Иванов Иван Иванович, тел. +7 912 345 67 89, "
        "email ivanov@example.com, паспорт 45 07 123456, "
        "карта 4111 1111 1111 1111, ИНН 771234567890, "
        "СНИЛС 112-233-445-95, адрес ул. Ленина, д. 5."
    )
    masked_once, repl = mask(text)
    masked_twice, repl2 = mask(masked_once)
    assert masked_twice == masked_once
    assert repl2 == {}


# ---------------------------------------------------------------------------
# Восстановление оригинала
# ---------------------------------------------------------------------------
def test_restore_original() -> None:
    text = (
        "Клиент Иванов Иван Иванович, тел. +7 912 345 67 89, "
        "карта 4111 1111 1111 1111, СНИЛС 112-233-445-95."
    )
    masked, repl = mask(text)
    assert restore(masked, repl) == text


def test_restore_with_double_digit_index() -> None:
    # 11 телефонов → индексы 1..11, проверяем что [PHONE_1] не подставится
    # вместо [PHONE_10]/[PHONE_11] при restore.
    phones = " ".join(f"+7 900 000 {i:02d} 00" for i in range(1, 12))
    masked, repl = mask(phones)
    assert restore(masked, repl) == phones
