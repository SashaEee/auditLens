"""HTML-презентация: макет пишет модель, код упаковывает, чистит и сверяет числа."""
import asyncio
import re
from types import SimpleNamespace

from bank_audit.research.gptr import deck_html as H

REPORT = """## Резюме для руководителя проверки

По нижней границе ПСК Сбербанк лучше обоих банков: 18,4% против 19,9% у ВТБ и 19,88% у Т-Банка [1][3][6].
Жалоб на потребкредит Сбера за 90 дней — 162 [17]. Сумма кредита — от 10 000 ₽ до 5 000 000 ₽ [5].

## Что проверить

1. Запросить индивидуальные условия кредитования [2].
"""
SOURCES = [{"n": 1, "title": "Кредит наличными", "domain": "sberbank.ru", "published": "2026-11-01"},
           {"n": 3, "title": "Кредит наличными ВТБ", "domain": "vtb.ru"},
           {"n": 17, "title": "Жалобы: Сбербанк", "domain": "AuditLens"}]
ANSWER = """Вот презентация:
```html
<style>
@import url("https://evil.example/x.css");
.slide .x-bar i{display:block;height:34px;background:var(--sber)}
.slide{position:relative;width:100px}
.slide .x-bg{background:url(https://evil.example/t.png);position:fixed}
@font-face{font-family:X;src:url(https://evil.example/x.woff2)}
</style>
<section class="slide cover" onclick="alert(1)"><p class="eyebrow">Потребкредиты</p>
<h1><em>Сбер</em> дешевле на входе</h1><p class="sub">Сравни условия</p>
<script>alert(1)</script><img src="https://evil.example/p.png"><a href="https://evil.example">ссылка</a></section>
<section class="slide" data-label="Главный вывод"><div class="body"><h1 class="title">ПСК 18,4% против 19,9% [1][3]</h1>
<div class="x-bar"><i style="width:92%;background:url(javascript:alert(1))"></i></div>
<svg viewBox="0 0 100 20"><defs><linearGradient id="g1"><stop offset="0" stop-color="#C92F33"/></linearGradient></defs>
<rect x="0" y="0" width="92" height="8" fill="url(#g1)"/><rect x="0" y="10" width="100" height="8" fill="url(https://evil.example/x)"/></svg>
<p class="statement pin">Разрыв 1,5 п.п. [1]</p></div></section>
<section class="slide" data-label="Жалобы"><div class="body"><div class="hero">162</div><p class="cap">жалобы за 90 дней [17]</p></div></section>
<section class="slide" data-label="Что проверить"><div class="body"><div class="rows"><div class="row"><span class="n">01</span>
<div class="t">Запросить индивидуальные условия [2]</div></div></div></div></section>
<section class="other"><p>не слайд</p></section>
```
"""


def test_answer_is_split_and_everything_unsafe_disappears():
    css, raw = H.split_answer(ANSWER)
    assert len(raw) == 4                                   # «не слайд» отброшен
    css = H.clean_css(css)
    assert "evil.example" not in css and "@import" not in css and "@font-face" not in css
    assert "position:absolute" in css and "fixed" not in css and "var(--sber)" in css
    slides = [H.sanitize(x) for x in raw]
    joined = "\n".join(slides)
    for bad in ("<script", "<img", "<a ", "onclick", "evil.example", "javascript:"):
        assert bad not in joined
    assert "ссылка" in joined                               # текст ссылки остался, адреса нет
    assert 'fill="url(#g1)"' in joined and "linearGradient" in joined and 'viewBox="0 0 100 20"' in joined
    assert 'data-label="Главный вывод"' in joined


def test_numbers_are_checked_without_cutting_the_layout():
    _, raw = H.split_answer(ANSWER)
    slides = [H.sanitize(x) for x in raw]
    before = list(slides)
    assert H.unknown_numbers(slides, REPORT) == ["1.5"]     # разность посчитала модель
    assert slides == before                                  # проверка ничего не меняет
    # точечная замена текста: применяется, только если находка одна и вне тегов
    slides, done = H.apply_fixes(slides, [
        {"find": "Разрыв 1,5 п.п.", "replace": "18,4% против 19,9%"},
        {"find": "class=", "replace": "x"},                          # внутри тегов — не трогаем
        {"find": "ПСК 18,4% против", "replace": "ПСК 17% против"},   # вносит чужое число
        {"find": "<p>", "replace": "<b>"}], REPORT)
    assert done == 1 and H.unknown_numbers(slides, REPORT) == []
    assert 'class="statement pin"' in slides[1] and "18,4% против 19,9% [1]" in slides[1]


def test_neighbour_cells_do_not_merge_into_one_number():
    cells = ['<section class="slide"><table><tr><td>2026</td><td>6</td><td>162</td></tr></table></section>']
    assert H.unknown_numbers(cells, "В 2026 году 162 жалобы") == []


def test_assembled_file_is_self_contained():
    css, raw = H.split_answer(ANSWER)
    slides = [H.sanitize(x) for x in raw]
    doc = H.assemble(H.clean_css(css), slides, title="Потребкредиты: Сбер и ВТБ", sources=SOURCES,
                     stamp="Экспортировано из AuditLens", note="Сверьте вручную: 1.5.")
    assert doc.startswith("<!doctype html>") and doc.count("<section") == 5       # 4 слайда + источники
    # ни одного обращения в сеть: шрифты внутри, внешних адресов нет
    assert not re.findall(r'(?:src|href)\s*=\s*"https?://', doc)
    assert "url(http" not in doc and 'url("http' not in doc and "url('http" not in doc
    assert doc.count("data:font/woff2;base64,") >= 6
    # шапка и нумерация — кодом
    assert "02 / 05 — Главный вывод" in doc and "05 / 05 — Источники" in doc
    assert 'class="al-cvtop"' in doc and "Подготовлено ИИ-помощником AuditLens" in doc
    # сноски и источники: только те, на кого сослались слайды и кто есть в списке
    assert '<sup class="cite">1,3</sup>' in doc and "[2]" in doc          # источника 2 в списке нет
    src = doc[doc.index("al-src"):]
    assert "sberbank.ru · 01.11.2026" in src and "vtb.ru" in src and "Сверьте вручную: 1.5." in src
    # механика листания защищена от стилей модели
    assert "width:1920px!important" in doc and "ArrowRight" in doc and "@media print" in doc


def _client(answers: list[str]):
    it = iter(answers)

    class Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(it)))])
    return Client()


def test_build_fixes_numbers_by_text_and_reports_what_is_left():
    fix = '{"fixes": [{"find": "Разрыв 1,5 п.п.", "replace": "18,4% против 19,9%"}]}'
    data, info = asyncio.run(H.build(_client([ANSWER, fix]), question="Сравни", title="Потребкредиты",
                                     report=REPORT, sources=SOURCES, model="m"))
    assert info["slides"] == 4 and info["fixed"] == 1 and info["unknown"] == []
    assert b"1,5" not in data.split(b"</style>")[-1]
    # исправление не удалось — файл всё равно собран, а число названо
    data, info = asyncio.run(H.build(_client([ANSWER, "не JSON"]), question="Сравни", title="Потребкредиты",
                                     report=REPORT, sources=SOURCES, model="m"))
    assert info["unknown"] == ["1.5"] and "сверьте вручную".encode() in data.lower()


def test_too_few_slides_is_an_error_not_an_empty_file():
    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(H.build(_client(['<section class="slide"><p>один</p></section>']), question="q",
                            title="t", report=REPORT, sources=SOURCES, model="m"))
