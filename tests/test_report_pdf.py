"""PDF отчёта ИИ-аналитика и название отчёта (аудит PDF 29.09).

Было: название — сам вопрос («Сделай сравнительный анализ… и…»), источники —
треть файла, одна битая таблица визуализации ужимала весь PDF до 7 pt, цитаты
клиентов печатались с «>», дата — момент выгрузки.
"""
import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from bank_audit.ai import report_title as rt
from bank_audit.research.gptr import brief as B
from bank_audit.research.gptr import viz
from bank_audit.research.gptr.facts import ru_attribute
from bank_audit.web import pdf_export as P

MSK = timezone(timedelta(hours=3))

Q484 = ("Сделай сравнительный анализ по продукту «Эквайринг» в Сбере для сегмента малого и "
        "микробизнеса, включая торговый эквайринг (POS-терминалы), оплату по СБП / QR")


# ── название ─────────────────────────────────────────────────────────────────

def test_heuristic_title_drops_the_command():
    assert rt.heuristic_title(Q484).startswith("Сравнительный анализ по продукту «Эквайринг»")
    assert rt.heuristic_title("Сравни ставки по вкладам Сбера и ВТБ?") == "Ставки по вкладам Сбера и ВТБ"
    assert rt.heuristic_title("") == "Без названия"
    assert len(rt.heuristic_title("Сделай " + "очень " * 40 + "длинный вопрос")) <= rt.TITLE_MAX + 1


def test_clean_title_and_default_detection():
    assert rt.clean_title("«Отчёт: эквайринг для МСБ.»") == "Эквайринг для МСБ"
    assert rt.clean_title("«Своё дело» Сбербанка для самозанятых") == "«Своё дело» Сбербанка для самозанятых"
    # до 29.09 в базе лежало начало вопроса, обрезанное на 80-м знаке вместе с пробелом
    stored = " ".join(Q484.split())[:80]
    assert stored.endswith(" ") and rt.is_default_title(stored, Q484)
    assert not rt.is_default_title("Эквайринг для малого бизнеса", Q484)


def test_short_follow_up_is_titled_by_the_conversation(monkeypatch):
    async def down(*a, **k):
        return None
    monkeypatch.setattr(rt, "llm_title", down)
    t = asyncio.run(rt.title_for("да, сведи в таблицу", "…", ["Собери тарифы эквайринга Сбера"]))
    assert t == "Тарифы эквайринга Сбера"


def test_llm_title_rejects_replies_and_cleans(monkeypatch):
    class Resp:
        def __init__(self, text):
            self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]

    class Client:
        def __init__(self, text):
            self.chat = type("X", (), {"completions": type("Y", (), {"create": self._create})()})()
            self.text = text

        async def _create(self, **kw):
            assert "temperature" not in kw        # часть моделей её отвергает
            return Resp(self.text)
    assert asyncio.run(rt.llm_title("q", client=Client("«Тарифы НСПК для эквайера.»"))) == "Тарифы НСПК для эквайера"
    assert asyncio.run(rt.llm_title("q", client=Client("да"))) is None


def test_brief_returns_report_title():
    raw = json.dumps({"report_title": "«Эквайринг для малого бизнеса: Сбер против ВТБ.»",
                      "answer": "a", "sections": [{"key": "market", "title": "t", "focus": "f"}]},
                     ensure_ascii=False)
    b = B.parse(raw, {"market": 3})
    assert b.title == "Эквайринг для малого бизнеса: Сбер против ВТБ"


# ── визуализации ─────────────────────────────────────────────────────────────

def test_polish_splits_glued_table_rows_and_drops_empty_items():
    """Отчёт 473: модель нумеровала строки <ol> внутри таблицы, парсер слил 50
    ячеек в одну строку — таблица 3 727 px ужала весь PDF."""
    head = "<tr>" + "".join(f"<th>h{i}</th>" for i in range(5)) + "</tr>"
    body = "<tr>" + "".join(f"<td>c{i}</td>" for i in range(50)) + "</tr>"
    html = (f'<div class="viz"><ol><li> </li></ol><li></li><table><thead>{head}</thead>'
            f"<tbody>{body}</tbody></table><small>Показано фактов: 15 из 60 в разделе; объектов 5</small></div>")
    out = viz.polish(html)
    rows = [len(re.findall(r"<t[dh]\b", tr)) for tr in re.split(r"<tr\b", out)[1:]]
    assert rows == [5] * 11
    assert "<li" not in out and "<ol" not in out
    assert "Основано на 15 из 60 фактов раздела" in out and "Показано" not in out


def test_polish_translates_old_labels_and_drops_prompt_terms():
    out = viz.polish("<td>Сбербанк — СБП/QR, tariffs and commissions</td>"
                     "<td>Сбербанк — точка отсчёта · заявлено</td><small>рамкой выделена точка отсчёта</small>")
    assert "тарифы и комиссии" in out and "tariffs" not in out
    assert "точка отсчёта" not in out and "рамкой выделен свой банк" in out


def test_own_bank_is_green_in_viz_palette():
    assert "--sber" in viz.PALETTE
    p = viz.designer_prompt(section="market", title="t", question="q", anchor="sber",
                            labels={"sber": "Сбербанк"}, facts_text="", section_text="",
                            subjects=["sber"])
    assert "рамкой var(--sber)" in p and "Основано на {{meta:facts_used}}" in p


def test_ru_attribute():
    assert ru_attribute("tariffs and commissions") == "тарифы и комиссии"
    assert ru_attribute("max_rate") == "максимальная ставка"
    assert ru_attribute("Срок выпуска") == "Срок выпуска"
    assert ru_attribute("weird_thing") == "weird thing"


# ── разметка PDF ─────────────────────────────────────────────────────────────

SRC = {n: {"n": n, "url": f"https://x.ru/{n}"} for n in range(1, 60)}


def test_quotes_become_blocks_with_attribution():
    html = P._md_to_html("> «Сбер задержал перевод» — Тверь, 15.08.2026 [42]\n\nтекст", SRC)
    assert '<blockquote class="quote"><p>«Сбер задержал перевод»</p>' in html
    assert '<p class="q-src">Тверь, 15.08.2026 <sup class="cite"><a href="#src-42">42</a></sup></p>' in html
    assert "&gt;" not in html


def test_sections_numbered_like_toc_and_manual_subnumbers_follow():
    toc = []
    html = P._md_to_html("## Резюме\n\nа\n\n## Что проверять\n\n### 3. Болит у других\n\n### Прочее", SRC, toc)
    assert '<span class="hn">2.</span> Что проверять' in html
    assert "<h3>2.3. Болит у других</h3>" in html and "<h3>Прочее</h3>" in html
    assert [e["num"] for e in toc] == ["1.", "2."]


def test_citation_runs_are_ranges():
    html = P._md_to_html("факт [49][50][51][52][53][54][55][56] и [2][1][31][2]", SRC)
    assert '<a href="#src-49">49–56</a>' in html
    assert re.search(r'<sup class="cite"><a href="#src-1">1</a>, <a href="#src-2">2</a>, '
                     r'<a href="#src-31">31</a></sup>', html)


def test_lead_points_from_summary():
    md = ("## Резюме для руководителя проверки\n\n[[VIZ:2]]\n\n**Сбер уступает ВТБ [1].** Детали.\n\n"
          "Сбербанк занимает 3-е место: отставание 0,2–0,3 п.п. [1][4]. Дальше текст.\n\n"
          "### Подраздел\n\n**Не сюда.**\n\n## Что проверять\n\n**И не сюда.**")
    answer, theses = P._lead_points(md)
    assert answer == "Сбер уступает ВТБ."
    assert theses == ["Сбербанк занимает 3-е место: отставание 0,2–0,3 п.п."]


def test_honest_gaps_move_to_limits_with_context():
    md = ("## Резюме\n\nСбер берёт 300 000 ₽ за терминал [5]. Второе.\n\n"
          "## Честные пробелы\n\n- **Эвотор** — не нашлось.\n- Снято утверждений без опоры: 7 из 1043.\n")
    body, items = P._split_gaps(md)
    assert "Честные пробелы" not in body and len(items) == 2
    html = P._render_limits_section(
        items, {"unverified": [{"claim": "число 300 000", "issue": "не найдено"}],
                "unanswered": ["срок доставки"]},
        {"missing": [{"attribute": "**Эвотор** — не нашлось.", "missing_banks": []}]}, body, SRC)
    assert html.count("Эвотор") == 1                           # без дубля из «пробелов» прогона
    assert "Нет ответа в источниках: срок доставки" in html
    assert "<b>300 000</b> — «Сбер берёт 300 000 ₽ за терминал" in html


def test_sources_grouped_compact_quote_only_for_banks():
    html, counts = P._render_sources_section([
        {"n": 1, "url": "https://sberbank.ru/a", "title": "Тарифы", "source_kind": "bank_official",
         "excerpts": ["тариф 1% при оплате картой"], "fetched_at": "2026-09-28T10:00:00"},
        {"n": 2, "url": "https://www.banki.ru/r/1", "title": "Здравствуйте, " + "жалоба " * 30,
         "source_kind": "aggregator", "excerpts": ["длинная цитата"]},
        {"n": 3, "url": "#market?cat=deposit", "title": "AuditLens · Рынок: Вклады",
         "source_kind": "auditlens"}])
    assert counts == {"official": 1, "auditlens": 1, "reviews": 1, "other": 0}
    assert html.index("Сайты банков и регуляторов") < html.index("Данные AuditLens") < html.index("Агрегаторы")
    assert "«тариф 1% при оплате картой»" in html and "длинная цитата" not in html
    assert ">Рынок: Вклады<" in html and "#market" not in html
    assert "sberbank.ru · 28.09.2026" in html


def test_cover_title_date_fonts_and_toc():
    kw = dict(question=Q484, report_md="## Резюме для руководителя проверки\n\n**Эквайринг Сбера дороже рынка.** Текст.\n\n## Второй\n\nтекст",
              sources=[{"n": 1, "url": "https://sberbank.ru/a", "title": "t", "source_kind": "bank_official"}],
              title=" ".join(Q484.split())[:80], report_id=484,
              report_date=datetime(2026, 9, 26, 12, 9, tzinfo=MSK), author="Саша")
    html = P.build_pdf_html(**kw)
    cover = html[html.index('<section class="cover">'):html.index('<section class="body">')]
    assert '<h1 class="cover-title long">Сравнительный анализ по продукту' in cover
    assert "Вопрос аудитора" in cover and "26 сентября 2026 · отчёт № 484 · Саша" in cover
    assert "Главный вывод" in cover and "Эквайринг Сбера дороже рынка." in cover
    assert 'toc-page pending' in cover and "fonts.googleapis" not in html
    assert P._FONT_HOST in html
    assert "AuditLens · Сравнительный анализ по продукту" in html and "26.09.2026" in html
    numbered = P.build_pdf_html(**kw, toc_pages={"1 резюме для руководителя проверки": 2,
                                                  "2 второй": 3, "источники": 4})
    assert '<span class="toc-page">2</span>' in numbered and '<span class="toc-page">4</span>' in numbered


def test_pdf_filename():
    assert (P.pdf_filename('Эквайринг: Сбер/ВТБ "МСБ"', date(2026, 9, 29))
            == "AuditLens — Эквайринг Сбер ВТБ МСБ — 29.09.2026.pdf")


# ── настоящая печать ─────────────────────────────────────────────────────────

def test_wide_block_does_not_shrink_the_document():
    """Широкий блок уменьшается сам; основной текст остаётся 11 pt, оглавление
    получает номера страниц по закладкам первого прохода."""
    pdfplumber = pytest.importorskip("pdfplumber")
    wide = '<div class="viz"><div style="width:1200px">широкий блок</div></div>'
    md = ("## Резюме для руководителя проверки\n\n**Главный вывод отчёта для обложки.** "
          + "Абзац основного текста отчёта. " * 40 + "\n\n[[VIZ:0]]\n\n## Второй раздел\n\n"
          + "Текст второго раздела. " * 60)
    pdf = P.export_report_to_pdf(question="Сравни тарифы", report_md=md,
                                 viz=[{"n": 0, "html": wide}], title="Тарифы для теста",
                                 sources=[{"n": 1, "url": "https://a.ru", "title": "A",
                                           "source_kind": "bank_official"}])
    assert pdf[:4] == b"%PDF"
    import io
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        cover = doc.pages[0].extract_text()
        sizes = [round(c["size"]) for c in doc.pages[1].chars if c["text"].strip()]
    assert re.search(r"Резюме для руководителя проверки\s*\.*\s*2", cover)
    assert max(set(sizes), key=sizes.count) == 11
