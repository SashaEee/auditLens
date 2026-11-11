"""Режим отчёта не запускается на реплику в продолжение разговора."""
import pytest

from bank_audit.ai.analyst import is_followup_reply

HISTORY = [{"role": "user", "content": "Жалобы на вклады Сбера"},
           {"role": "assistant", "content": "Нашёл 35 жалоб за 90 дней. " * 20}]


@pytest.mark.parametrize("reply", [
    "хочу", "да", "Да, собери, пожалуйста.", "собери", "сформируй файл",
    "сведи эту информацию в таблицу", "Покажи все жалобы", "Сколько всего таких жалоб?",
    "Из какого источника доля рынка Сбербанка 0,87040",
    "да давай, формируй блок схему для слайда", "добавь ссылки на источник",
    "покажи ссылку на banki.ru на это обращение", "новая задача",
])
def test_reply_to_previous_answer_is_not_a_new_research(reply):
    assert is_followup_reply(reply, HISTORY)


@pytest.mark.parametrize("question", [
    "сравни с другими банками", "Сравни текущие вклады до 3-х месяцев", "выездное обслуживание",
    "Сервис Свое Дело для самозанятых в Сбере", "найди информацию про выездной сервис",
    "жалобы на эквайринг за 2026 год", "да, сравни ещё с ВТБ",
    "Добавь в таблицу по продукту драгметаллы для банков Сбербанк, Альфа-Банк, Т-Банк, ВТБ, "
    "Газпромбанк, Россельхозбанк, Совкомбанк данные о ставках и тарифах",
])
def test_new_question_stays_a_research(question):
    assert not is_followup_reply(question, HISTORY)


def test_first_message_is_never_a_reply():
    assert not is_followup_reply("хочу", [])
    assert not is_followup_reply("сформируй файл", [{"role": "user", "content": "x"}])


# ── воронка уточняющих вопросов ──────────────────────────────────────────────

def test_bank_question_is_dropped_when_comparison_is_not_asked():
    from bank_audit.ai.clarify import drop_bank_questions
    res = {"complete": False, "reason": "x", "questions": [
        {"id": "banks", "question": "Какие банки сравнить?", "type": "multi", "options": []},
        {"id": "product", "question": "По какому продукту смотреть жалобы?", "type": "single", "options": []}]}
    out = drop_bank_questions(res, asks_compare=False)
    assert [q["id"] for q in out["questions"]] == ["product"]
    assert drop_bank_questions(res, asks_compare=True) is res
    only_banks = {"complete": False, "questions": [res["questions"][0]]}
    assert drop_bank_questions(only_banks, asks_compare=False)["complete"] is True


def test_funnel_prompt_no_longer_recommends_top4_by_default():
    from bank_audit.ai import clarify
    p = clarify.SYSTEM_PROMPT_CLARIFY
    assert "ТОЛЬКО если аудитор просит сравнение" in p and "Только Сбер, без сравнения" in p
    assert "предложи топ-4" not in p
