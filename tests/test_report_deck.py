"""Презентация по отчёту: план слайдов проверяется кодом, файл собирается кодом."""
import asyncio
import io
import json
from types import SimpleNamespace

from bank_audit.research.gptr import deck as D

REPORT = """## Резюме для руководителя проверки

[[VIZ:2]]

По нижней границе ПСК Сбербанк лучше обоих банков: 18,4% против 19,9% у ВТБ и 19,88% у Т-Банка [1][3][6].
Жалоб на потребкредит Сбера за 90 дней — 162, годом ранее было 157 [17].

## Что проверить

1. Запросить индивидуальные условия кредитования [2].
2. Взять выборку по теме «Ошибки в кредите» (34 жалобы) [17].

## Сбер против ВТБ и Т-Банка

| Характеристика | Сбербанк | ВТБ |
|---|---|---|
| ПСК от | 18,4% [1] | 19,9% [3] |

Сумма кредита — от 10 000 ₽ до 5 000 000 ₽ [5].

## Честные пробелы

- Страниц не прочитано: 2.
"""
SOURCES = [{"n": 1, "title": "Кредит наличными", "domain": "sberbank.ru", "published": "2026-11-01"},
           {"n": 3, "title": "Кредит наличными ВТБ", "domain": "vtb.ru"},
           {"n": 17, "title": "Жалобы: Сбербанк", "domain": "AuditLens"}]


def test_numbers_are_normalised_and_citations_are_not_numbers():
    assert D.numbers("ПСК 18,4% [6], сумма 5 000 000 ₽, три банка, 2 раза, 162 жалобы [17][3]") == {
        "18.4", "5000000", "162"}
    assert D.cites("рост [1][3], затем [6, 17]") == [1, 3, 6, 17]


def test_plan_is_parsed_from_fenced_answer_and_garbage_is_dropped():
    raw = "```json\n" + json.dumps({"title": "Потребкредиты", "subtitle": "Сравни условия", "slides": [
        {"kind": "bullets", "title": "Сбер дешевле по нижней границе", "bullets": ["ПСК 18,4% [1]", ""]},
        {"kind": "спасибо", "title": "Вопросы?"},
        {"kind": "table", "title": "Таблица", "columns": ["Характеристика"], "rows": [["a"]]},     # один столбец
        {"kind": "numbers", "title": "Числа", "items": [{"value": "162", "label": "жалоб", "src": "[17]"}]},
        {"kind": "bullets", "title": "", "bullets": ["без заголовка"]}]}, ensure_ascii=False) + "\n```\nготово"
    p = D.parse(raw)
    assert p["title"] == "Потребкредиты" and [s["kind"] for s in p["slides"]] == ["bullets", "numbers"]
    assert p["slides"][0]["bullets"] == ["ПСК 18,4% [1]"]
    assert D.parse("не JSON") == {"title": "", "subtitle": "", "slides": []}


def test_slide_cannot_say_more_than_the_report():
    plan = {"title": "t", "slides": [
        {"kind": "bullets", "title": "Сбер дешевле", "note": "", "bullets": [
            "ПСК у Сбербанка 18,4% против 19,9% у ВТБ [1][3]",
            "Разрыв составляет 1,5 п.п. [1]",                       # посчитано моделью — в отчёте нет
            "Ссылка на источник, которого нет [99]"]},
        {"kind": "numbers", "title": "Главные числа", "note": "", "items": [
            {"value": "162", "label": "жалобы за 90 дней", "src": "[17]"},
            {"value": "157", "label": "годом ранее", "src": "[17]"},
            {"value": "999", "label": "выдумано", "src": ""}]},
        {"kind": "table", "title": "Условия", "note": "", "columns": ["Характеристика", "Сбербанк", "ВТБ"],
         "rows": [["ПСК от", "18,4% [1]", "19,9% [3]"], ["Ставка", "21,7%", "25%"]]},
        {"kind": "bullets", "title": "Рост на 77% за год", "note": "", "bullets": ["что-то"]}]}
    out = D.check(plan, D.clean_report(REPORT), {1, 3, 17})
    assert [s["kind"] for s in out["slides"]] == ["bullets", "numbers", "table"]
    assert out["slides"][0]["bullets"] == ["ПСК у Сбербанка 18,4% против 19,9% у ВТБ [1][3]",
                                           "Ссылка на источник, которого нет"]
    assert [i["value"] for i in out["slides"][1]["items"]] == ["162", "157"]
    assert out["slides"][2]["rows"] == [["ПСК от", "18,4% [1]", "19,9% [3]"]]   # строка с чужими числами ушла
    assert out["dropped"] >= 4


def test_fallback_plan_uses_report_headings_and_skips_gaps():
    p = D.fallback_plan("Потребкредиты", "Сравни", REPORT)
    titles = [s["title"] for s in p["slides"]]
    assert titles == ["Резюме для руководителя проверки", "Что проверить", "Сбер против ВТБ и Т-Банка"]
    assert p["slides"][1]["kind"] == "checks" and p["slides"][1]["bullets"][0].startswith("Запросить")


def _client(answer: str):
    class Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])
    return Client()


def test_deck_is_built_into_a_real_pptx_with_sources_slide():
    from pptx import Presentation
    answer = json.dumps({"title": "Потребкредиты: Сбер, ВТБ и Т-Банк", "subtitle": "Сравни условия", "slides": [
        {"kind": "bullets", "title": "Сбер дешевле по нижней границе ПСК",
         "bullets": ["ПСК 18,4% против 19,9% у ВТБ [1][3]", "Жалоб за 90 дней — 162 [17]"], "note": "даты замеров разные"},
        {"kind": "numbers", "title": "Жалобы не растут", "items": [
            {"value": "162", "label": "за 90 дней", "src": "[17]"}, {"value": "157", "label": "годом ранее", "src": "[17]"}]},
        {"kind": "table", "title": "Условия по банкам", "columns": ["Характеристика", "Сбербанк", "ВТБ"],
         "rows": [["ПСК от", "18,4% [1]", "19,9% [3]"]]},
        {"kind": "checks", "title": "Что проверить", "bullets": ["Запросить индивидуальные условия [2]"]}]},
        ensure_ascii=False)
    data, info = asyncio.run(D.build(_client(answer), question="Сравни условия", title="Потребкредиты",
                                     report=REPORT, sources=SOURCES, model="m"))
    assert info == {"slides": 4, "dropped": 0, "fallback": False}
    prs = Presentation(io.BytesIO(data))
    texts = [" ".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in prs.slides]
    assert len(prs.slides) == 6                                  # титул + 4 + источники
    assert "Потребкредиты: Сбер, ВТБ и Т-Банк" in texts[0] and "Экспортировано из AuditLens" in texts[0]
    assert "Сбер дешевле по нижней границе ПСК" in texts[1] and "2 / 6" in texts[1]
    assert "Источники" in texts[5] and "sberbank.ru" in texts[5] and "[17]" in texts[5]
    assert "[2]" not in texts[5]                                 # источника 2 в списке нет — в «Источники» не попал


def test_deck_survives_a_silent_model():
    class Broken:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    raise RuntimeError("Connection error.")
    data, info = asyncio.run(D.build(Broken(), question="Сравни", title="Потребкредиты", report=REPORT,
                                     sources=SOURCES, model="m"))
    assert info["fallback"] and info["slides"] == 3 and data[:2] == b"PK"
