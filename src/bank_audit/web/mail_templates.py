"""Письма AuditLens: шаблоны уведомлений на корпоративную почту.

Письма собираются из тех же записей, что колокольчик (app_notice), поэтому
текст события в почте и в приложении один и тот же. Виды:

• event   — одно личное событие: упоминание, ответ, комментарий к вашему
            материалу, добавили в дело, поделились отчётом, ответ на обращение;
• batch   — несколько личных событий за 15 минут одним письмом;
• digest  — утренняя сводка непрочитанного, по делам;
• welcome — «уведомления теперь приходят на почту» (один раз).

Вёрстка рассчитана на корпоративный Outlook: таблицы, встроенные стили,
ширина 600 px, системные шрифты, без внешних картинок (их режут), светлая
схема (тёмную Outlook инвертирует сам). У каждого письма есть текстовая
версия. Ссылки ведут прямо к объекту: дело и обсуждение — #open?case=…,
отчёт — #ai?report=…, обращение — #open?inbox=…, настройки — #open?bell=settings.
"""
from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta, timezone

# ── оформление ───────────────────────────────────────────────────────────────
INK, INK2, INK3, INK4 = "#16181D", "#3C4049", "#6D727B", "#9AA0A8"
PAPER, SURFACE, HAIR = "#F4F4F1", "#FFFFFF", "#E7E6E1"
BRAND, LINK = "#1F4DFF", "#1F4DFF"
QUOTE_BG = "#F5F6FA"
FONT = "'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif"

KIND_LABEL = {
    "case_mention": "Упоминание", "case_reply": "Ответ", "case_msg": "Обсуждение",
    "case_added": "Доступ к делу", "case_role": "Доступ к делу", "case_removed": "Доступ к делу",
    "case_owner": "Доступ к делу", "case_left": "Участники дела", "case_items": "Новые материалы",
    "case_status": "Статус дела", "case_analysis": "Разбор ИИ", "case_deleted": "Дело",
    "case_restored": "Дело", "report_shared": "Отчёт ИИ", "ticket": "Обращение",
}
# личные события — уходят письмом сразу (раз в 15 минут пачкой); остальное — в сводку
PERSONAL = {"case_mention", "case_reply", "case_added", "case_owner", "report_shared", "ticket"}


def app_base() -> str:
    return (os.getenv("APP_BASE_URL") or "https://auditlens.uva-advanced.ru").rstrip("/")


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _msk(v) -> datetime | None:
    if not v:
        return None
    try:
        d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo
    return d.astimezone(ZoneInfo("Europe/Moscow"))


_MON = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
_MON_FULL = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря"]


def when(v, now: datetime | None = None) -> str:
    """«сегодня в 12:40», «вчера в 18:05», «3 окт в 09:10» — по Москве."""
    d = _msk(v)
    if not d:
        return ""
    n = _msk(now or datetime.now(timezone.utc))
    t = d.strftime("%H:%M")
    if d.date() == n.date():
        return f"сегодня в {t}"
    if d.date() == (n - timedelta(days=1)).date():
        return f"вчера в {t}"
    return f"{d.day} {_MON[d.month - 1]} в {t}"


def day_title(v) -> str:
    d = _msk(v) or _msk(datetime.now(timezone.utc))
    return f"{d.day} {_MON_FULL[d.month - 1]}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def link_for(n: dict) -> str:
    """Куда ведёт событие: дело (вкладка, сообщение), отчёт, обращение."""
    base, link = app_base(), str(n.get("link") or "")
    parts = link.split(":")
    if parts[0] == "case" and len(parts) > 1:
        q = f"case={parts[1]}"
        if len(parts) > 2:
            q += f"&tab={parts[2]}"
        if len(parts) > 3:
            q += f"&msg={parts[3]}"
        return f"{base}/#open?{q}"
    if parts[0] == "report" and len(parts) > 1:
        return f"{base}/#ai?report={parts[1]}"
    if parts[0] == "inbox" and len(parts) > 1:
        return f"{base}/#open?inbox={parts[1]}"
    return f"{base}/#open?bell=1"


def settings_url() -> str:
    return f"{app_base()}/#open?bell=settings"


def _case_of(n: dict) -> tuple[str | None, str | None]:
    """(номер дела, название) события — для группировки сводки."""
    link = str(n.get("link") or "")
    ref = n.get("ref") or {}
    if link.startswith("case:"):
        return link.split(":")[1], ref.get("case")
    if ref.get("case"):
        return f"t:{ref['case']}", ref.get("case")
    return None, None


# ── строительные блоки ───────────────────────────────────────────────────────

def _mark_mentions(text_: str) -> str:
    """Экранирует текст и подсвечивает @Имя Фамилия."""
    out = _e(text_)
    return re.sub(r"@([А-ЯЁA-Z][а-яёa-z\-]+(?: [А-ЯЁA-Z][а-яёa-z\-]+)?)",
                  lambda m: f'<span style="color:{LINK};font-weight:600">@{m.group(1)}</span>', out)


def _btn(label: str, url: str) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:22px 0 4px">'
            f'<tr><td bgcolor="{INK}" style="border-radius:8px">'
            f'<a href="{_e(url)}" target="_blank" style="display:inline-block;padding:12px 20px;font-family:{FONT};'
            f'font-size:14px;line-height:16px;font-weight:600;color:#FFFFFF;text-decoration:none;border-radius:8px">'
            f'{_e(label)}&nbsp;&rarr;</a></td></tr></table>')


def _quote(text_: str, who: str = "", when_: str = "") -> str:
    head = " · ".join(x for x in (who, when_) if x)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:16px 0 0">'
            f'<tr><td style="border-left:3px solid {BRAND};background:{QUOTE_BG};padding:12px 16px;border-radius:0 8px 8px 0">'
            + (f'<div style="font-family:{FONT};font-size:12px;line-height:16px;color:{INK3};margin:0 0 4px">{_e(head)}</div>'
               if head else "")
            + f'<div style="font-family:{FONT};font-size:15px;line-height:22px;color:{INK}">{_mark_mentions(text_)}</div>'
            f'</td></tr></table>')


def _facts(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><td style="padding:7px 0;border-bottom:1px solid {HAIR};font-family:{FONT};font-size:13px;'
        f'line-height:18px;color:{INK3};width:38%;vertical-align:top">{_e(k)}</td>'
        f'<td style="padding:7px 0;border-bottom:1px solid {HAIR};font-family:{FONT};font-size:14px;'
        f'line-height:18px;color:{INK};vertical-align:top">{_e(v)}</td></tr>'
        for k, v in rows if v)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:16px 0 0;border-top:1px solid {HAIR}">{cells}</table>')


def _p(text_: str, color: str = INK2, size: int = 15, top: int = 12) -> str:
    return (f'<p style="margin:{top}px 0 0;font-family:{FONT};font-size:{size}px;line-height:{int(size * 1.5)}px;'
            f'color:{color}">{text_}</p>')


def short_title(n: dict) -> str:
    """Событие без названия дела — для строк под заголовком этого дела в сводке:
    «В деле «X» 4 новых материала» → «4 новых материала»."""
    t, case = n.get("title") or "", (n.get("ref") or {}).get("case")
    if not case:
        return t
    q = f"«{case}»"
    for a, b in ((f" в деле {q}", ""), (f"В деле {q} ", ""), (f"Статус дела {q}", "Статус"),
                 (f" в дело {q}", " в дело"), (f"Дело {q}", "Дело"), (f"дела {q}", "дела"),
                 (f"Ваша роль в деле {q}", "Ваша роль"), (q, "")):
        t = t.replace(a, b)
    t = t.strip(" —:")
    return (t[:1].upper() + t[1:]) if t else (n.get("title") or "")


def _item_row(n: dict, now=None, show_case: bool = True) -> str:
    """Строка списка (пачка, сводка): рубрика, событие ссылкой, кто и когда, цитата строкой."""
    ref = n.get("ref") or {}
    label = KIND_LABEL.get(n.get("kind"), "Событие")
    snip = ref.get("snippet")
    meta = " · ".join(x for x in (n.get("actor_name"), when(n.get("updated_at"), now)) if x)
    return (f'<tr><td style="padding:12px 0;border-bottom:1px solid {HAIR}">'
            f'<div style="font-family:{FONT};font-size:11px;line-height:14px;letter-spacing:.06em;'
            f'text-transform:uppercase;color:{INK3}">{_e(label)}</div>'
            f'<a href="{_e(link_for(n))}" target="_blank" style="display:block;margin:3px 0 0;font-family:{FONT};'
            f'font-size:15px;line-height:21px;font-weight:600;color:{INK};text-decoration:none">'
            f'{_e(n.get("title") if show_case else short_title(n))}</a>'
            + (f'<div style="margin:3px 0 0;font-family:{FONT};font-size:13px;line-height:19px;color:{INK2}">'
               f'«{_mark_mentions(snip)}»</div>' if snip else "")
            + (f'<div style="margin:3px 0 0;font-family:{FONT};font-size:12px;line-height:16px;color:{INK3}">'
               f'{_e(meta)}</div>' if meta else "")
            + '</td></tr>')


def layout(*, preheader: str, eyebrow: str, title: str, body: str, reason: str,
           right: str = "") -> str:
    """Каркас письма: знак, рубрика, заголовок, тело, подвал с причиной и настройками."""
    pre = _e(preheader) + "&nbsp;&zwnj;" * 40
    return f"""<!DOCTYPE html>
<html lang="ru" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no">
<title>{_e(title)}</title>
<style>
  body{{margin:0;padding:0;background:{PAPER}}}
  a{{color:{LINK}}}
  @media (max-width:620px){{ .al-card{{padding:24px 20px !important}} .al-wrap{{padding:12px 8px !important}} }}
</style>
</head>
<body style="margin:0;padding:0;background:{PAPER}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;mso-hide:all">{pre}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{PAPER}" style="background:{PAPER}">
<tr><td align="center" class="al-wrap" style="padding:28px 12px">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px">
    <tr><td style="padding:0 4px 14px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
        <td style="vertical-align:middle">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
            <td width="24" height="24" bgcolor="{BRAND}" align="center" valign="middle" style="width:24px;height:24px;background:{BRAND};border-radius:6px;font-family:{FONT};font-size:14px;line-height:24px;font-weight:700;color:#FFFFFF">A</td>
            <td style="padding-left:9px;font-family:{FONT};font-size:15px;line-height:24px;font-weight:700;color:{INK};letter-spacing:-.01em">AuditLens</td>
          </tr></table>
        </td>
        <td align="right" style="vertical-align:middle;font-family:{FONT};font-size:12px;line-height:16px;color:{INK3}">{_e(right)}</td>
      </tr></table>
    </td></tr>
    <tr><td class="al-card" bgcolor="{SURFACE}" style="background:{SURFACE};border:1px solid {HAIR};border-radius:12px;padding:30px 32px">
      <div style="font-family:{FONT};font-size:11px;line-height:14px;letter-spacing:.07em;text-transform:uppercase;color:{INK3}">{_e(eyebrow)}</div>
      <h1 style="margin:8px 0 0;font-family:{FONT};font-size:22px;line-height:28px;font-weight:700;color:{INK};letter-spacing:-.01em">{_e(title)}</h1>
      {body}
    </td></tr>
    <tr><td style="padding:16px 8px 0;font-family:{FONT};font-size:12px;line-height:18px;color:{INK3}">
      {_e(reason)} Ответ на это письмо коллеги не увидят — отвечайте в AuditLens.<br>
      <a href="{_e(settings_url())}" target="_blank" style="color:{INK3};text-decoration:underline">Настроить уведомления</a>
      &nbsp;·&nbsp; AuditLens — инструмент внутреннего аудита
    </td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""


def _text(title: str, lines: list[str], url: str, reason: str) -> str:
    body = "\n".join(x for x in lines if x is not None)
    return (f"{title}\n\n{body}\n\nОткрыть: {url}\n\n—\n{reason} Ответ на это письмо коллеги не увидят — "
            f"отвечайте в AuditLens.\nНастроить уведомления: {settings_url()}\n")


# ── письма ───────────────────────────────────────────────────────────────────

def render_event(n: dict, now=None) -> dict:
    """Одно событие из колокольчика → письмо. Тема — текст уведомления."""
    kind, ref = n.get("kind"), n.get("ref") or {}
    case = ref.get("case")
    actor = n.get("actor_name") or ""
    wh = when(n.get("updated_at"), now)
    url = link_for(n)
    subject = n.get("title") or "Новое в AuditLens"
    eyebrow = KIND_LABEL.get(kind, "AuditLens") + (f" · {case}" if case else "")
    reason = f"Вы участвуете в деле «{case}»." if case else "Это уведомление AuditLens."
    snip = ref.get("snippet") or ""
    body, lines = "", []
    cta = "Открыть дело"

    if kind in ("case_mention", "case_reply", "case_msg"):
        title = ("Вас упомянули в обсуждении" if kind == "case_mention"
                 else "Комментарий к вашему материалу" if ref.get("on_item")
                 else "Ответ на ваше сообщение" if kind == "case_reply"
                 else "Новое в обсуждении дела")
        if ref.get("on_item") and ref.get("item"):
            body += _p(f'Материал: <b style="color:{INK}">{_e(ref["item"])}</b>', size=14)
        body += _quote(snip or subject, actor, wh)
        cta = "Открыть обсуждение"
        lines = [f"{actor}, {wh}:" if actor else "", f"«{snip}»" if snip else ""]
    elif kind in ("case_added", "case_role", "case_owner"):
        title = (f"Вам передали дело «{case}»" if kind == "case_owner"
                 else f"Вас добавили в дело «{case}»" if kind == "case_added"
                 else f"Новые права в деле «{case}»")
        eyebrow = "Доступ к делу"            # название дела — уже в заголовке
        facts = [("Ваши права", "владелец" if kind == "case_owner" else ref.get("role_label") or ""),
                 ("Через команду", ref.get("team") or ""),
                 ("Кто", actor), ("Когда", wh)]
        body += _facts(facts)
        body += _p("Дело откроется по кнопке ниже — или в любом разделе AuditLens кнопкой "
                   "«Аудит-дела» в верхней панели.", size=14, top=16)
        lines = [f"{k}: {v}" for k, v in facts if v]
    elif kind == "report_shared":
        title = "С вами поделились отчётом"
        rep = ref.get("report") or ""
        body += _p(f'<b style="color:{INK};font-size:17px">{_e(rep)}</b>', size=17, top=14)
        if ref.get("lead"):
            body += _p(_e(ref["lead"]), size=14)
        body += _p(_e(" · ".join(x for x in (actor, wh) if x)), color=INK3, size=13)
        cta, reason = "Открыть отчёт", "С вами поделились отчётом в AuditLens."
        eyebrow = "Отчёт ИИ"
        lines = [f"«{rep}»", f"{actor}, {wh}" if actor else wh]
    elif kind == "ticket":
        no = ref.get("no")
        title = ("Команда AuditLens ответила" if ref.get("reply")
                 else f"Новый статус: {ref.get('status_label') or 'обновлён'}")
        eyebrow = f"Обращение № {no}" if no else "Обращение"
        if snip:
            body += _quote(snip, "Команда AuditLens", wh)
        if ref.get("status_label") and ref.get("reply"):
            body += _p(f"Статус обращения: <b style=\"color:{INK}\">{_e(ref['status_label'])}</b>", size=14)
        body += _p("Уточнить или ответить можно в AuditLens: «Обратная связь» → «Мои обращения».",
                   size=14, top=14)
        cta, reason = "Открыть обращение", "Вы писали в «Обратную связь» AuditLens."
        lines = [f"«{snip}»" if snip else "", f"Статус: {ref.get('status_label')}" if ref.get("status_label") else ""]
    else:
        title = subject
        if snip:
            body += _quote(snip, actor, wh)
        else:
            body += _p(_e(" · ".join(x for x in (actor, wh) if x)), color=INK3, size=13)
        lines = [" · ".join(x for x in (actor, wh) if x)]

    body += _btn(cta, url)
    preheader = (f"{actor}: «{snip}»" if snip and actor else snip or
                 " · ".join(x for x in (case, actor) if x) or "Уведомление AuditLens")[:140]
    return {"subject": subject, "preheader": preheader, "url": url,
            "html": layout(preheader=preheader, eyebrow=eyebrow, title=title, body=body, reason=reason),
            "text": _text(title, lines, url, reason),
            "thread": f"case-{_case_of(n)[0]}" if _case_of(n)[0] else None}


def render_batch(ns: list[dict], now=None) -> dict:
    """Несколько личных событий за 15 минут — одним письмом."""
    if len(ns) == 1:
        return render_event(ns[0], now)
    first, k = ns[0], len(ns) - 1
    subject = f"{first.get('title')} и ещё {k} {_plural(k, 'событие', 'события', 'событий')}"
    title = f"{len(ns)} {_plural(len(ns), 'новое событие', 'новых события', 'новых событий')} для вас"
    rows = "".join(_item_row(n, now) for n in ns[:12])
    more = len(ns) - 12
    body = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:14px 0 0;border-top:1px solid {HAIR}">{rows}</table>'
            + (_p(f"И ещё {more} — в колокольчике AuditLens.", color=INK3, size=13) if more > 0 else "")
            + _btn("Открыть уведомления", f"{app_base()}/#open?bell=1"))
    reason = "Вас упомянули, вам ответили или открыли доступ в AuditLens."
    preheader = "; ".join(n.get("title") or "" for n in ns[:3])[:140]
    lines = [f"— {n.get('title')} ({' · '.join(x for x in (n.get('actor_name'), when(n.get('updated_at'), now)) if x)})"
             for n in ns[:20]]
    return {"subject": subject, "preheader": preheader, "url": f"{app_base()}/#open?bell=1",
            "html": layout(preheader=preheader, eyebrow="Новое в AuditLens", title=title, body=body,
                           reason=reason),
            "text": _text(title, lines, f"{app_base()}/#open?bell=1", reason), "thread": None}


def render_digest(ns: list[dict], now=None, name: str = "") -> dict:
    """Утренняя сводка непрочитанного: по делам, затем отчёты и обращения."""
    now = now or datetime.now(timezone.utc)
    groups: dict[str, dict] = {}
    other: list[dict] = []
    for n in ns:
        cid, ctitle = _case_of(n)
        if cid:
            g = groups.setdefault(cid, {"title": ctitle or "Дело", "items": [], "id": cid})
            g["items"].append(n)
        else:
            other.append(n)
    n_cases = len(groups)
    total = len(ns)
    day = day_title(now)
    subject = (f"Сводка AuditLens за {day}: {total} {_plural(total, 'событие', 'события', 'событий')}"
               + (f" в {n_cases} {_plural(n_cases, 'деле', 'делах', 'делах')}" if n_cases else ""))
    hello = f"{name.split()[0]}, доброе утро." if name else "Доброе утро."
    body = _p(f"{_e(hello)} Пока вас не было в AuditLens, накопилось вот что — "
              f"всё по ссылкам ведёт прямо к делу или отчёту.", size=15, top=12)
    text_lines = [hello, ""]
    for g in sorted(groups.values(), key=lambda x: -len(x["items"])):
        its = g["items"]
        msgs = sum(int(i.get("count") or 1) for i in its if i.get("kind") in ("case_msg", "case_mention", "case_reply"))
        mats = sum(int(i.get("count") or 1) for i in its if i.get("kind") == "case_items")
        summary = " · ".join(x for x in (
            f"{msgs} {_plural(msgs, 'сообщение', 'сообщения', 'сообщений')}" if msgs else "",
            f"{mats} {_plural(mats, 'материал', 'материала', 'материалов')}" if mats else "") if x)
        case_url = (f"{app_base()}/#open?case={g['id']}" if not str(g["id"]).startswith("t:")
                    else f"{app_base()}/#open?bell=1")
        body += (f'<div style="margin:24px 0 0;font-family:{FONT};font-size:11px;line-height:14px;'
                 f'letter-spacing:.07em;text-transform:uppercase;color:{INK3}">Дело</div>'
                 f'<a href="{_e(case_url)}" target="_blank" style="display:block;margin:4px 0 0;font-family:{FONT};'
                 f'font-size:17px;line-height:23px;font-weight:700;color:{INK};text-decoration:none">{_e(g["title"])}</a>'
                 + (f'<div style="margin:2px 0 0;font-family:{FONT};font-size:13px;line-height:18px;color:{INK3}">'
                    f'{_e(summary)}</div>' if summary else "")
                 + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                   f'style="margin:8px 0 0;border-top:1px solid {HAIR}">'
                 + "".join(_item_row(i, now, show_case=False) for i in its[:6]) + "</table>"
                 + (_p(f"И ещё {len(its) - 6} — в деле.", color=INK3, size=13, top=8) if len(its) > 6 else ""))
        text_lines += [f"Дело «{g['title']}»" + (f" — {summary}" if summary else ""),
                       *[f"  — {short_title(i)}" for i in its[:6]], ""]
    if other:
        body += (f'<div style="margin:24px 0 0;font-family:{FONT};font-size:11px;line-height:14px;'
                 f'letter-spacing:.07em;text-transform:uppercase;color:{INK3}">Отчёты и обращения</div>'
                 f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                 f'style="margin:8px 0 0;border-top:1px solid {HAIR}">'
                 + "".join(_item_row(i, now) for i in other[:8]) + "</table>")
        text_lines += ["Отчёты и обращения", *[f"  — {i.get('title')}" for i in other[:8]], ""]
    body += _btn("Открыть AuditLens", f"{app_base()}/#open?bell=1")
    reason = "Это утренняя сводка непрочитанного в AuditLens — приходит, только когда есть новое."
    preheader = "; ".join(g["title"] for g in groups.values())[:140] or (other[0].get("title") if other else "")
    return {"subject": subject, "preheader": preheader, "url": f"{app_base()}/#open?bell=1",
            "html": layout(preheader=preheader, eyebrow=f"Сводка за {day}",
                           title=f"{total} {_plural(total, 'событие', 'события', 'событий')} ждут вас", body=body,
                           reason=reason, right=day),
            "text": _text(f"Сводка AuditLens за {day}", text_lines, f"{app_base()}/#open?bell=1", reason),
            "thread": None}


def render_welcome(name: str = "") -> dict:
    """Один раз: уведомления теперь приходят на почту — что и когда, как настроить."""
    hello = f"{name.split()[0]}, здравствуйте!" if name else "Здравствуйте!"
    rows = [("Сразу", "вас упомянули, ответили на ваше сообщение или комментарий, добавили в дело, "
                      "поделились отчётом, команда ответила на обращение — одним письмом раз в 15 минут"),
            ("Утром", "сводка непрочитанного по вашим делам — только если есть новое"),
            ("Никогда", "то, что вы уже прочитали в AuditLens")]
    body = (_p(f"{_e(hello)} Теперь уведомления AuditLens приходят и на почту — чтобы не пропускать "
               f"важное, даже когда инструмент не открыт.", size=15)
            + _facts(rows)
            + _p("Что присылать, выбирается в колокольчике рядом с вашим именем внизу меню. "
                 "Там же можно отключить письма совсем.", size=14, top=16)
            + _btn("Настроить уведомления", settings_url()))
    reason = "Вы пользуетесь AuditLens."
    return {"subject": "Уведомления AuditLens теперь приходят на почту",
            "preheader": "Сразу — о личном, утром — сводка по делам. Настраивается в колокольчике.",
            "url": settings_url(),
            "html": layout(preheader="Сразу — о личном, утром — сводка по делам. Настраивается в колокольчике.",
                           eyebrow="Уведомления", title="Теперь и на почте", body=body, reason=reason),
            "text": _text("Уведомления AuditLens теперь приходят на почту",
                          [hello, ""] + [f"{k}: {v}" for k, v in rows], settings_url(), reason),
            "thread": None}


# ── примеры для галереи и тестовой отправки (вымышленные люди и дела) ───────────

def samples(now: datetime | None = None) -> dict[str, list[dict]]:
    now = now or datetime.now(timezone.utc)
    ago = lambda m: (now - timedelta(minutes=m)).isoformat()  # noqa: E731
    case = "Кредитные карты: навязанные услуги"
    ev = {
        "mention": {"kind": "case_mention", "title": f"Вас упомянули в деле «{case}»",
                    "actor_name": "Анна Смирнова", "link": "case:12:talk:55", "updated_at": ago(6),
                    "ref": {"case": case, "snippet": "@Павел Орлов посмотрите [3] — там сумма списания и дата, "
                                                     "это наш главный пример для запроса в подразделение."}},
        "reply": {"kind": "case_reply", "title": f"Ответ на ваше сообщение в деле «{case}»",
                  "actor_name": "Ирина Котова", "link": "case:12:talk:57", "updated_at": ago(14),
                  "ref": {"case": case, "snippet": "Согласна. Тариф на дату договора запрошу сегодня."}},
        "item_comment": {"kind": "case_reply", "title": f"Комментарий к вашему материалу в деле «{case}»",
                         "actor_name": "Анна Смирнова", "link": "case:12:talk:58", "updated_at": ago(25),
                         "ref": {"case": case, "on_item": True, "item": "Подключили страховку без согласия",
                                 "snippet": "Похожих жалоб в Казани ещё четыре — добавлю их в дело."}},
        "added": {"kind": "case_added", "title": f"Вас добавили в дело «{case}» — команда «Розница»",
                  "actor_name": "Павел Орлов", "link": "case:12", "updated_at": ago(40),
                  "ref": {"case": case, "role_label": "может добавлять", "team": "Розница"}},
        "report": {"kind": "report_shared",
                   "title": "С вами поделились отчётом «Ставки по вкладам у топ-10 банков в сентябре»",
                   "actor_name": "Ирина Котова", "link": "report:45", "updated_at": ago(65),
                   "ref": {"report": "Ставки по вкладам у топ-10 банков в сентябре",
                           "lead": "Ставки по вкладам на 3 месяца снизились в среднем на 0,4 п.п.; ставка "
                                   "Сбербанка на 0,6 п.п. ниже медианы рынка."}},
        "ticket": {"kind": "ticket", "title": "Команда AuditLens ответила на обращение № 17",
                   "actor_name": "Команда AuditLens", "link": "inbox:17", "updated_at": ago(90),
                   "ref": {"no": 17, "reply": True, "status_label": "В работе",
                           "snippet": "Спасибо! Разобрались: выпуск считает жалобы на 07:00, раздел — вживую. "
                                      "Добавим подпись «на 07:00»."}},
    }
    digest = [
        ev["mention"], ev["reply"],
        {"kind": "case_items", "title": f"В деле «{case}» 4 новых материала", "actor_name": "Анна Смирнова",
         "link": "case:12", "updated_at": ago(300), "count": 4, "ref": {"case": case}},
        {"kind": "case_msg", "title": f"В деле «{case}» 3 новых сообщения", "actor_name": "Ирина Котова",
         "link": "case:12:talk", "updated_at": ago(420), "count": 3,
         "ref": {"case": case, "snippet": "Предлагаю начать выборку с Казани и Самары."}},
        {"kind": "case_status", "title": "Статус дела «Ипотека: страхование»: В работе", "actor_name": "Павел Орлов",
         "link": "case:14", "updated_at": ago(600), "ref": {"case": "Ипотека: страхование",
                                                            "status_label": "В работе"}},
        {"kind": "case_analysis", "title": "В деле «Ипотека: страхование» новый разбор ИИ",
         "actor_name": "Павел Орлов", "link": "case:14:analysis", "updated_at": ago(640),
         "ref": {"case": "Ипотека: страхование"}},
        ev["report"], ev["ticket"],
    ]
    return {"event_" + k: [v] for k, v in ev.items()} | {
        "batch": [ev["mention"], ev["added"], ev["report"]],
        "digest": digest,
    }


TEMPLATES = {
    "event_mention": "Упоминание в обсуждении",
    "event_reply": "Ответ на ваше сообщение",
    "event_item_comment": "Комментарий к вашему материалу",
    "event_added": "Вас добавили в дело (через команду)",
    "event_report": "С вами поделились отчётом",
    "event_ticket": "Команда ответила на обращение",
    "batch": "Несколько личных событий одним письмом",
    "digest": "Утренняя сводка по делам",
    "welcome": "Приветственное: уведомления теперь на почте",
}


def render(tpl: str, data: list[dict] | None = None, name: str = "", now=None) -> dict:
    """Шаблон по имени: на примерах или на переданных событиях."""
    if tpl == "welcome":
        return render_welcome(name)
    data = data if data is not None else samples(now).get(tpl, [])
    if not data:
        raise KeyError(tpl)
    if tpl == "digest":
        return render_digest(data, now, name)
    if tpl == "batch":
        return render_batch(data, now)
    return render_event(data[0], now)


# ── галерея для владельца: как выглядят письма и «Отправить себе» ──────────────

def gallery_page(cards: list[dict], test_to: list[str], configured: bool, source: str) -> str:
    to = ", ".join(test_to) or "не задан (MAIL_TEST_TO)"
    state = ("готово к отправке" if configured and test_to
             else "почта не настроена" if not configured else "нет тестового адреса")
    blocks = []
    for c in cards:
        blocks.append(f"""
<section class="card" id="{_e(c['key'])}">
  <div class="head">
    <div><div class="lbl">{_e(c['label'])}{' · <b>ваши данные</b>' if c.get('mine') else ' · пример'}</div>
      <div class="subj">{_e(c['subject'])}</div>
      <div class="pre">{_e(c['preheader'])}</div></div>
    <div class="acts">
      <button class="ghost" onclick="tgl('{_e(c['key'])}')">Текст</button>
      <button onclick="send('{_e(c['key'])}', this)" {'disabled' if not (configured and test_to) else ''}>Отправить себе</button>
    </div>
  </div>
  <iframe title="{_e(c['label'])}" srcdoc="{_e(c['html'])}" loading="lazy"></iframe>
  <pre class="txt" hidden>{_e(c['text'])}</pre>
</section>""")
    other = "sample" if source == "mine" else "mine"
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Письма AuditLens</title>
<style>
body{{margin:0;background:{PAPER};font-family:{FONT};color:{INK}}}
.top{{position:sticky;top:0;z-index:2;background:rgba(244,244,241,.92);backdrop-filter:blur(8px);border-bottom:1px solid {HAIR};
  padding:14px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
.top h1{{font-size:18px;margin:0}} .top .m{{font-size:13px;color:{INK3}}} .top .sp{{flex:1}}
main{{max-width:1180px;margin:0 auto;padding:20px 24px 60px;display:grid;grid-template-columns:repeat(auto-fill,minmax(520px,1fr));gap:20px}}
.card{{background:#fff;border:1px solid {HAIR};border-radius:14px;overflow:hidden}}
.head{{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;border-bottom:1px solid {HAIR}}}
.head>div:first-child{{flex:1;min-width:0}}
.lbl{{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{INK3}}}
.subj{{font-weight:600;font-size:14px;margin-top:3px}} .pre{{font-size:12px;color:{INK3};margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.acts{{display:flex;gap:6px;flex:none}}
button{{font:inherit;font-size:12.5px;font-weight:600;border:0;border-radius:8px;padding:8px 12px;background:{INK};color:#fff;cursor:pointer}}
button.ghost{{background:none;color:{INK2};border:1px solid {HAIR}}} button:disabled{{opacity:.45;cursor:default}}
iframe{{display:block;width:100%;height:640px;border:0;background:{PAPER}}}
.txt{{margin:0;padding:14px 16px;font-size:12px;line-height:1.55;white-space:pre-wrap;background:#FAFAF8;border-top:1px solid {HAIR}}}
.toast{{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:{INK};color:#fff;font-size:13px;padding:10px 16px;border-radius:999px;display:none}}
a{{color:{LINK}}}
@media(max-width:600px){{main{{grid-template-columns:1fr;padding:12px}}}}
</style></head><body>
<div class="top"><h1>Письма AuditLens</h1>
  <span class="m">Тестовый адрес: <b>{_e(to)}</b> · {_e(state)} · рассылка сотрудникам выключена</span>
  <span class="sp"></span>
  <a class="m" href="?source={other}">{'Показать на примерах' if source == 'mine' else 'Показать на моих уведомлениях'}</a>
  <button onclick="sendAll(this)" {'disabled' if not (configured and test_to) else ''}>Отправить все себе</button>
</div>
<main>{''.join(blocks)}</main>
<div class="toast" id="toast"></div>
<script>
const SRC={source!r};
function toast(t){{const e=document.getElementById('toast');e.textContent=t;e.style.display='block';clearTimeout(window._tt);window._tt=setTimeout(()=>e.style.display='none',3800);}}
function tgl(k){{const p=document.querySelector('#'+k+' .txt');p.hidden=!p.hidden;}}
async function send(k,b){{b.disabled=true;const old=b.textContent;b.textContent='Отправляю…';
  try{{const r=await fetch('/api/admin/mail/test',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{template:k,source:SRC}})}});
    const d=await r.json();if(!r.ok)throw new Error(d.detail||r.status);b.textContent='Отправлено ✓';toast('Ушло на '+d.to+' — письма в Сбер идут 2–5 минут');}}
  catch(e){{b.textContent=old;b.disabled=false;toast('Не ушло: '+e.message);}}}}
async function sendAll(b){{b.disabled=true;const bs=[...document.querySelectorAll('.acts button:not(.ghost)')];
  for(const x of bs){{if(!x.disabled){{await send(x.closest('.card').id,x);await new Promise(r=>setTimeout(r,1200));}}}}b.textContent='Готово';}}
</script></body></html>"""
