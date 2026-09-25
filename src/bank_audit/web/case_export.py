"""Выгрузка аудит-дела в Excel и Word — с разметкой жалоб и комментариями.

Excel — рабочая таблица: фильтр, закреплённая шапка, одна строка на материал.
Word — приложение к рабочему файлу проверки: разбор дела и материалы по одному
с сутью, цитатой и комментарием аудитора. Собирается на сервере: у выгрузки
один формат независимо от того, кто и откуда её взял.
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timezone

_ESC = {"filed": "обратился", "threat": "грозит"}
_TO = {"cbr": "ЦБ", "court": "суд", "rpn": "Роспотребнадзор", "fas": "ФАС",
       "prosecutor": "прокуратура", "finombudsman": "финомбудсмен", "police": "полиция"}
_VULN = {"pensioner": "пенсионер", "low_income": "низкий доход", "svo": "участник СВО",
         "minor": "несовершеннолетний", "disabled": "инвалид", "ill": "тяжелобольной"}
_RISK = {"compliance": "комплаенс", "conduct": "практики", "ops": "операции"}
_KIND = {"document": "документ", "review": "жалоба", "offer": "продукт", "report": "отчёт"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")


def _ru_date(d: str | None) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(d or ""))
    return f"{m[3]}.{m[2]}.{m[1]}" if m else ""


def _amount(v) -> str:
    return f"{v:,.0f} ₽".replace(",", " ") if v is not None else ""


def _row(n: int, it: dict) -> dict:
    r = it.get("review") or {}
    doc = it["kind"] == "document"
    return {
        "№": n, "Тип": _KIND.get(it["kind"], it["kind"]),
        "Дата": _ru_date(r.get("date") or (str(it.get("fetched_at") or "")[:10] if doc else "")),
        "Банк": r.get("bank") or it.get("bank_name") or "",
        "Продукт": r.get("product") or "", "Город": r.get("city") or "",
        "Площадка": r.get("source") or "",
        "Главная проблема": r.get("issue_label") or "",
        "Класс риска": _RISK.get(r.get("risk") or "", ""),
        "Эскалация": _ESC.get(r.get("esc") or "", ""),
        "Куда": ", ".join(_TO.get(x, x) for x in r.get("esc_to") or []) if r.get("esc") in _ESC else "",
        "Уязвимый клиент": ", ".join(_VULN.get(x, x) for x in r.get("vulnerable") or []),
        "Без согласия": "да" if r.get("no_consent") else "",
        "Ввели в заблуждение": "да" if r.get("misled") else "",
        "Сумма": _amount(r.get("amount")),
        "Суть": r.get("summary") or it.get("title") or "",
        "Цитата": r.get("quote") or "",
        "Комментарий аудитора": it.get("note") or "",
        "Приобщил": it.get("added_by") or "",
        "Ссылка": it.get("url") or "",
    }


_WIDTH = {"№": 5, "Тип": 10, "Дата": 11, "Банк": 16, "Продукт": 18, "Город": 14,
          "Площадка": 12, "Главная проблема": 30, "Класс риска": 12, "Эскалация": 11,
          "Куда": 18, "Уязвимый клиент": 16, "Без согласия": 9, "Ввели в заблуждение": 11,
          "Сумма": 13, "Суть": 60, "Цитата": 50, "Комментарий аудитора": 40,
          "Приобщил": 14, "Ссылка": 40}


def to_xlsx(case: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    def put(ws, row, col, v):
        c = ws.cell(row=row, column=col, value=v)
        # текст жалобы чужой: «=…» Excel исполнил бы как формулу
        if isinstance(v, str) and v.startswith("="):
            c.data_type = "s"
        return c

    wb = Workbook()
    ws = wb.active
    ws.title = "Материалы"
    rows = [_row(n, it) for n, it in enumerate(case.get("items") or [], 1)]
    cols = list(_WIDTH)
    head = PatternFill("solid", fgColor="EDEBE6")
    for j, k in enumerate(cols, 1):
        c = put(ws, 1, j, k)
        c.font = Font(bold=True)
        c.fill = head
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(j)].width = _WIDTH[k]
    wrap = Alignment(vertical="top", wrap_text=True)
    for i, r in enumerate(rows, 2):
        for j, k in enumerate(cols, 1):
            c = put(ws, i, j, r[k])
            c.alignment = wrap
            if k == "Ссылка" and r[k]:
                c.hyperlink = r[k]
                c.font = Font(color="1F4E9A", underline="single")
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(rows) + 1}"

    meta = wb.create_sheet("Дело")
    info = [("Дело", case.get("title") or ""), ("Владелец", case.get("owner") or ""),
            ("Открыто команде", "да" if case.get("shared") else "нет"),
            ("Материалов", len(rows)), ("Выгружено", _now()),
            ("Цель и примечание", case.get("note") or ""),
            ("Разбор (ИИ)", case.get("analysis") or "")]
    meta.column_dimensions["A"].width = 20
    meta.column_dimensions["B"].width = 110
    for i, (k, v) in enumerate(info, 1):
        put(meta, i, 1, k).font = Font(bold=True)
        put(meta, i, 2, v).alignment = wrap
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _md_runs(par, text_: str) -> None:
    """**жирный** из разбора модели — жирным, остальное как есть."""
    for k, part in enumerate(re.split(r"\*\*(.+?)\*\*", text_)):
        if part:
            par.add_run(part).bold = bool(k % 2)


def to_docx(case: dict) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10.5)
    doc.add_heading(f"Аудит-дело: {case.get('title') or ''}", level=0)
    items = case.get("items") or []
    meta = doc.add_paragraph()
    meta.add_run(f"Владелец: {case.get('owner') or '—'} · материалов: {len(items)} · "
                 f"выгружено {_now()}").font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    if case.get("note"):
        p = doc.add_paragraph()
        p.add_run("Цель: ").bold = True
        p.add_run(case["note"])
    if case.get("analysis"):
        doc.add_heading("Разбор материалов (ИИ)", level=1)
        for line in case["analysis"].splitlines():
            s_ = line.strip()
            if not s_:
                continue
            if s_.startswith("#"):
                doc.add_heading(s_.lstrip("#").strip(), level=2)
            elif re.match(r"^[-*•]\s+", s_):
                _md_runs(doc.add_paragraph(style="List Bullet"), re.sub(r"^[-*•]\s+", "", s_))
            else:
                _md_runs(doc.add_paragraph(), s_)
    doc.add_heading("Материалы", level=1)
    for n, it in enumerate(items, 1):
        r = _row(n, it)
        head = " · ".join(x for x in (r["Дата"], r["Банк"], r["Продукт"], r["Город"]) if x)
        doc.add_heading(f"[{n}] {r['Тип']}{' · ' + head if head else ''}", level=3)
        if r["Главная проблема"]:
            p = doc.add_paragraph()
            p.add_run("Проблема: ").bold = True
            p.add_run(r["Главная проблема"] + (f" ({r['Класс риска']})" if r["Класс риска"] else ""))
        flags = [x for x in (
            f"{r['Эскалация']}: {r['Куда']}" if r["Эскалация"] and r["Куда"] else r["Эскалация"],
            f"уязвимый клиент: {r['Уязвимый клиент']}" if r["Уязвимый клиент"] else "",
            "без согласия" if r["Без согласия"] else "",
            f"сумма {r['Сумма']}" if r["Сумма"] else "") if x]
        if flags:
            p = doc.add_paragraph()
            p.add_run("Признаки: ").bold = True
            p.add_run("; ".join(flags))
        if r["Суть"]:
            p = doc.add_paragraph()
            p.add_run("Суть: ").bold = True
            p.add_run(r["Суть"])
        if r["Цитата"]:
            doc.add_paragraph().add_run(f"«{r['Цитата']}»").italic = True
        if r["Комментарий аудитора"]:
            p = doc.add_paragraph()
            p.add_run("Комментарий аудитора: ").bold = True
            p.add_run(r["Комментарий аудитора"])
        if r["Ссылка"]:
            doc.add_paragraph().add_run(r["Ссылка"]).font.color.rgb = RGBColor(0x1F, 0x4E, 0x9A)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
