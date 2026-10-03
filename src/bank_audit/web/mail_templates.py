"""Письма AuditLens: шаблоны уведомлений на корпоративную почту.

Письма собираются из тех же записей, что колокольчик (app_notice), поэтому
текст события в почте и в приложении один и тот же. Виды:

• event   — одно личное событие: упоминание, ответ, комментарий к вашему
            материалу, добавили в дело, поделились отчётом, ответ на обращение;
• batch   — несколько личных событий за 15 минут одним письмом;
• digest  — утренняя сводка непрочитанного, по делам;
• welcome — «уведомления теперь приходят на почту» (один раз).

Вёрстка рассчитана на корпоративный Outlook: таблицы, встроенные стили,
ширина 600 px, системные шрифты, светлая схема. Внешних картинок нет (их режут):
логотип — PNG внутри самого письма (cid:auditlens-logo, см. logo() и
inline_images()); для превью в браузере его подставляет for_browser().
У каждого письма есть текстовая версия. Ссылки ведут прямо к объекту: дело и
обсуждение — #open?case=…, отчёт — #ai?report=…, обращение — #open?inbox=…,
настройки — #open?bell=settings.
"""
from __future__ import annotations

import base64
import hashlib
import html
import math
import os
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

# ── оформление: светлая тема интерфейса в hex (oklch в почте не работает) ──────
INK, INK2, INK3, INK4 = "#0D1014", "#3F4348", "#656970", "#9499A0"
PAPER, SURFACE, SOFT = "#F3F2EE", "#FFFFFF", "#F5F4F1"
HAIR = "#E3E1DE"
BRAND = LINK = "#1F4DFF"            # синий штрих знака
MENTION_BG = "#EAEFFF"
# Шрифт: Segoe UI первым — Outlook на Windows берёт только первый в списке; system-ui —
# системный на iPhone/Mac/Android. Без -apple-system и без кавычек: мобильный клиент
# корпоративной почты, встретив -apple-system, выбрасывает font-family целиком и рисует
# письмо с засечками (проверено тестовым письмом 03.10: строки с ним — с засечками,
# остальные варианты — без).
FONT = "Segoe UI, system-ui, Roboto, Helvetica Neue, Arial, sans-serif"

# Точка у рубрики — семья события: обсуждение, доступ, само дело, отчёт, обращение
TALK, ACCESS, CASE_C, REPORT, TICKET = BRAND, "#683EB6", "#374960", "#036EAE", "#9D6400"
KIND = {
    "case_mention": ("Упоминание", TALK), "case_reply": ("Ответ", TALK), "case_msg": ("Обсуждение", TALK),
    "case_added": ("Доступ к делу", ACCESS), "case_role": ("Доступ к делу", ACCESS),
    "case_removed": ("Доступ к делу", ACCESS), "case_owner": ("Доступ к делу", ACCESS),
    "case_left": ("Участники дела", ACCESS), "case_items": ("Новые материалы", CASE_C),
    "case_status": ("Статус дела", CASE_C), "case_analysis": ("Разбор ИИ", CASE_C),
    "case_deleted": ("Дело", CASE_C), "case_restored": ("Дело", CASE_C),
    "report_shared": ("Отчёт", REPORT), "ticket": ("Обращение", TICKET),
}
KIND_LABEL = {k: v[0] for k, v in KIND.items()}
# личные события — уходят письмом сразу (раз в 15 минут пачкой); остальное — в сводку
PERSONAL = {"case_mention", "case_reply", "case_added", "case_owner", "report_shared", "ticket"}
TEAM = "Команда AuditLens"

# что дают права — строкой под карточкой дела в письме «вас добавили»
ROLE_HINT = {
    "только смотрит": "Вы можете читать материалы и обсуждение дела.",
    "может добавлять": "Вы можете добавлять материалы и писать в обсуждении.",
    "владелец": "Теперь вы управляете делом: участники, статус и архив.",
}
NOTE_CASE = "Ответ на это письмо коллеги не увидят — отвечайте в AuditLens."
NOTE_TICKET = "Ответ на это письмо команда не увидит — пишите в AuditLens."
NOTE_AUTO = "Письмо отправлено автоматически, отвечать на него не нужно."
# Внешняя (не корпоративная) почта получает письма без подробностей — см. redact()
PRIVATE_NOTE = ("Письмо пришло на личную почту, поэтому в нём нет подробностей — названий дел, "
                "имён и цитат. Всё остальное — в AuditLens.")


def app_base() -> str:
    return (os.getenv("APP_BASE_URL") or "http://localhost:8000").rstrip("/")


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _nb(s) -> str:
    """Неразрывные пробелы: «сегодня в 16:28», «№ 17» не рвутся между строк."""
    return str(s or "").replace(" ", "\u00a0")


def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip(" ,.;:—") + "…"


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
_WEEKDAY = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


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


def _weekday_title(v) -> str:
    d = _msk(v) or _msk(datetime.now(timezone.utc))
    return f"{_WEEKDAY[d.weekday()].capitalize()}, {d.day} {_MON_FULL[d.month - 1]}"


_NAME_WORD = re.compile(r"^[A-ZА-ЯЁ][a-zа-яё']+(?:-[A-ZА-ЯЁa-zа-яё][a-zа-яё']+)*$")
_PATRONYMIC = re.compile(r"(?:ович|евич|ьич|ична|инична|овна|евна)$", re.I)


def first_name(name: str | None) -> str:
    """Имя для обращения в письме: «Анна Смирнова» → «Анна»;
    «Смирнова Анна Павловна» (как в адресной книге) → «Анна».
    Логин вместо имени («ivanov-123») → "" — письмо поздоровается без имени."""
    words = (name or "").split()
    if not words or not all(_NAME_WORD.match(w) for w in words):
        return ""
    if len(words) == 3 and _PATRONYMIC.search(words[2]):
        return words[1]
    return words[0]


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


def _case_no(n: dict) -> str | None:
    parts = str(n.get("link") or "").split(":")
    return parts[1] if parts[0] == "case" and len(parts) > 1 and parts[1] else None


def redact(n: dict) -> dict:
    """Событие для личной почты: только что произошло. Без названий дел и отчётов,
    имён коллег и цитат — номер дела остаётся (по нему ничего не узнать, а ссылка
    ведёт прямо к делу), номер и статус вашего обращения — тоже."""
    kind, ref, cnt = n.get("kind"), n.get("ref") or {}, int(n.get("count") or 1)
    no = _case_no(n)
    d = f" №\u00a0{no}" if no else ""
    many = lambda one, few, lots: f"{cnt} {_plural(cnt, one, few, lots)}"  # noqa: E731
    if kind == "case_mention":
        t = f"Вас упомянули в деле{d}" if no else "Вас упомянули в обсуждении"
    elif kind == "case_reply":
        t = (f"Комментарий к вашему материалу в деле{d}" if ref.get("on_item")
             else f"Ответ на ваше сообщение в деле{d}")
    elif kind == "case_msg":
        t = f"Новое сообщение в деле{d}" if cnt <= 1 else \
            f"В деле{d} {many('новое сообщение', 'новых сообщения', 'новых сообщений')}"
    elif kind == "case_items":
        t = f"В деле{d} новый материал" if cnt <= 1 else \
            f"В деле{d} {many('новый материал', 'новых материала', 'новых материалов')}"
    elif kind == "case_added":
        t = f"Вас добавили в дело{d}"
    elif kind == "case_role":
        t = f"Ваши права в деле{d}: {ref.get('role_label')}" if ref.get("role_label") \
            else f"Ваши права в деле{d} изменились"
    elif kind == "case_removed":
        t = f"Вас убрали из дела{d}"
    elif kind == "case_owner":
        t = f"Вам передали дело{d} — теперь вы владелец"
    elif kind == "case_left":
        t = f"Участник вышел из дела{d}"
    elif kind == "case_status":
        t = (f"Дело{d} перенесено в архив" if ref.get("archived") is True
             else f"Дело{d} возвращено из архива" if ref.get("archived") is False
             else f"Статус дела{d}: {ref['status_label']}" if ref.get("status_label")
             else f"Статус дела{d} изменился")
    elif kind == "case_analysis":
        t = f"В деле{d} новый разбор ИИ"
    elif kind == "case_deleted":
        t = f"Дело{d} удалено"
    elif kind == "case_restored":
        t = f"Дело{d} снова доступно"
    elif kind == "report_shared":
        t = "С вами поделились отчётом"
    elif kind == "ticket":
        tn = f" №\u00a0{ref['no']}" if ref.get("no") else ""
        t = (f"Команда AuditLens ответила на обращение{tn}" if ref.get("reply")
             else f"Новый статус обращения{tn}")
    else:
        t = "Новое событие в AuditLens"
    keep = {"on_item": ref.get("on_item"), "no": ref.get("no"), "reply": ref.get("reply"),
            "status_label": ref.get("status_label") if kind in ("ticket", "case_status") else None,
            "case": f"Дело{d}" if no else None}
    return {"id": n.get("id"), "kind": kind, "title": t, "actor_name": "", "link": n.get("link"),
            "updated_at": n.get("updated_at"), "count": cnt,
            "ref": {k: v for k, v in keep.items() if v}}


def _kind(n: dict) -> tuple[str, str]:
    """Рубрика и цвет точки; комментарий к материалу — отдельная рубрика."""
    if n.get("kind") == "case_reply" and (n.get("ref") or {}).get("on_item"):
        return "Комментарий", TALK
    return KIND.get(n.get("kind"), ("Событие", CASE_C))


def _section(kind: str | None) -> str:
    """Подпись справа в шапке: из какого раздела письмо."""
    if kind == "report_shared":
        return "ИИ-помощник"
    if kind == "ticket":
        return "Обратная связь"
    if str(kind or "").startswith("case_"):
        return "Аудит-дела"
    return "Уведомления"


# ── логотип: PNG внутри письма ────────────────────────────────────────────────

LOGO_CID = "auditlens-logo"


def _rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


@lru_cache(maxsize=1)
def logo() -> tuple[bytes, int, int]:
    """Знак и «AuditLens» — как в меню интерфейса. Возвращает (png, ширина, высота
    в точках письма).

    Картинка втрое крупнее места в письме — чёткая на ретине. Фон прозрачный,
    вокруг букв белый ореол в 2 px: на белой карточке его не видно, а тёмная тема
    Outlook перекрашивает фон письма, но не картинки — без ореола тёмная надпись
    утонула бы. Контуры знака — те же, что у SVG в меню (поле 100×100)."""
    import io

    from PIL import Image, ImageDraw, ImageFilter

    from .export_brand import _pil_font
    k, ss = 3, 4                    # плотность картинки; рисуем ещё вчетверо крупнее — ровные края
    S = k * ss
    H, pad, mark, gap, size = 28, 2, 28, 10, 17     # высота, запас под ореол, знак, зазор, кегль
    s = mark / 100
    mx, my = pad - 10.5 * s, (H - mark) / 2          # левый край буквы — вплотную к запасу
    word, track = "AuditLens", -0.015 * size         # трекинг — как у названия в меню
    font = _pil_font("sans_sb", size * S)
    tx = mx + 89.5 * s + gap
    tw = sum(font.getlength(ch) / S for ch in word) + track * (len(word) - 1)
    W = int(math.ceil(tx + tw + pad))

    img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def pts(seq):
        return [((mx + x * s) * S, (my + y * s) * S) for x, y in seq]
    d.polygon(pts([(47.5, 13), (59.5, 13), (89.5, 89), (75.5, 89)]), fill=_rgb(BRAND) + (255,))
    letter = Image.new("L", img.size, 0)
    ld = ImageDraw.Draw(letter)
    ld.polygon(pts([(47.5, 13), (57.5, 13), (83.5, 89), (66.5, 89), (58.5, 67),
                    (36.5, 67), (27.5, 89), (10.5, 89)]), fill=255)
    ld.polygon(pts([(47.5, 36), (56.5, 58), (38.5, 58)]), fill=0)     # «дыра» буквы — прозрачная
    img.paste(Image.new("RGBA", img.size, _rgb(INK) + (255,)), (0, 0), letter)
    # название: середина заглавных — на середине знака
    top = font.getbbox("H", anchor="ls")[1] / S
    base, x = my + 51 * s - top / 2, tx
    for ch in word:
        d.text((x * S, base * S), ch, font=font, fill=_rgb(INK) + (255,), anchor="ls")
        x += font.getlength(ch) / S + track

    img = img.convert("RGBa").resize((W * k, H * k), Image.LANCZOS).convert("RGBA")
    a = img.getchannel("A").filter(ImageFilter.GaussianBlur(k))
    halo = Image.new("RGBA", img.size, (255, 255, 255, 0))
    halo.putalpha(a.point(lambda v: 255 if v > 20 else v * 12))
    halo.alpha_composite(img)
    buf = io.BytesIO()
    halo.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), W, H


def _logo_img() -> str:
    _, w, h = logo()
    return (f'<img src="cid:{LOGO_CID}" width="{w}" height="{h}" alt="AuditLens" '
            f'style="display:block;width:{w}px;height:{h}px;border:0;outline:none;text-decoration:none;'
            f'font-family:{FONT};font-size:17px;line-height:{h}px;font-weight:600;color:{INK}">')


def inline_images(html_: str) -> list[tuple[str, bytes]]:
    """Картинки, на которые письмо ссылается через cid: — их вкладывает mailer."""
    return [(LOGO_CID, logo()[0])] if f"cid:{LOGO_CID}" in html_ else []


def for_browser(html_: str) -> str:
    """Превью в браузере: вложенную картинку — прямо в страницу."""
    if f"cid:{LOGO_CID}" not in html_:
        return html_
    data = base64.b64encode(logo()[0]).decode()
    return html_.replace(f"cid:{LOGO_CID}", f"data:image/png;base64,{data}")


# ── строительные блоки ───────────────────────────────────────────────────────

def _mark_mentions(text_: str) -> str:
    """Экранирует текст и подсвечивает @Имя Фамилия."""
    out = _e(text_)
    return re.sub(r"@([А-ЯЁA-Z][а-яёa-z\-]+(?: [А-ЯЁA-Z][а-яёa-z\-]+)?)",
                  lambda m: f'<span style="color:{LINK};background:{MENTION_BG};border-radius:4px;'
                            f'font-weight:600">@{m.group(1)}</span>', out)


def _tbl(inner: str, style: str = "", width: bool = True) -> str:
    w = ' width="100%"' if width else ""
    st = f' style="{style}"' if style else ""
    return f'<table role="presentation"{w} cellpadding="0" cellspacing="0" border="0"{st}>{inner}</table>'


def _font(size: int, line: int, color: str = INK, weight: int = 400, extra: str = "") -> str:
    return (f"font-family:{FONT};font-size:{size}px;line-height:{line}px;font-weight:{weight};"
            f"color:{color};mso-line-height-rule:exactly;{extra}")


def _initials(name: str) -> str:
    p = (name or "").split()
    return ((p[0][:1] if p else "") + (p[1][:1] if len(p) > 1 else "")).upper() or "А"


# мягкие пары «фон — буквы»; цвет — по имени, одинаковый в каждом письме
_AV = [("#E6ECFF", "#2440B8"), ("#EFE8FA", "#5A35A0"), ("#E2F0E7", "#1F5E37"),
       ("#FAEEDB", "#7A4C08"), ("#E6EBF1", "#33465E"), ("#F9E5E6", "#9E2B2F")]


def _marker(color: str, size: int = 28) -> str:
    """Вместо аватара, когда имени нет (письмо на личную почту): точка цвета события."""
    return _tbl(f'<tr><td width="{size}" height="{size}" align="center" valign="middle" bgcolor="{SOFT}" '
                f'style="width:{size}px;height:{size}px;border-radius:{size // 2}px;background:{SOFT};'
                f'{_font(10, size, color)}">&#9679;</td></tr>', width=False)


def _avatar(name: str, size: int = 32) -> str:
    """Кружок с инициалами (Outlook на Windows рисует квадрат со скруглением 0 — это нормально)."""
    if name == TEAM:
        bg, fg, ini = INK, "#FFFFFF", "AL"
    else:
        bg, fg = _AV[hashlib.md5((name or "").encode()).digest()[0] % len(_AV)]
        ini = _initials(name)
    fs = 12 if size >= 30 else 11
    return _tbl(f'<tr><td width="{size}" height="{size}" align="center" valign="middle" bgcolor="{bg}" '
                f'style="width:{size}px;height:{size}px;border-radius:{size // 2}px;background:{bg};'
                f'{_font(fs, size, fg, 600, "letter-spacing:.02em")}">{_e(ini)}</td></tr>', width=False)


def _eyebrow(label: str, color: str) -> str:
    return (f'<div style="{_font(11, 16, INK3, 600, "letter-spacing:.08em;text-transform:uppercase")}">'
            f'<span style="color:{color};font-size:10px;letter-spacing:0">&#9679;</span>'
            f'&nbsp;&nbsp;{_e(label)}</div>')


def _h1(title: str, sub: str = "") -> str:
    return (f'<h1 class="al-h1" style="margin:10px 0 0;{_font(24, 31, INK, 600, "letter-spacing:-.015em")}">'
            f'{_e(title)}</h1>'
            + (f'<p style="margin:6px 0 0;{_font(15, 22, INK3)}">{_e(sub)}</p>' if sub else ""))


def _p(text_: str, color: str = INK2, size: int = 15, top: int = 14) -> str:
    return f'<p style="margin:{top}px 0 0;{_font(size, round(size * 1.5), color)}">{text_}</p>'


def _byline(name: str, when_: str, size: int = 28) -> str:
    meta = (f'<span style="font-weight:600;color:{INK}">{_e(name)}</span>'
            + (f'<span style="color:{INK3}">&nbsp;&nbsp;·&nbsp;&nbsp;{_e(_nb(when_))}</span>' if when_ else ""))
    return (f'<td width="{size}" valign="middle" style="width:{size}px">{_avatar(name, size)}</td>'
            f'<td valign="middle" style="padding-left:12px;{_font(13, 18, INK2)}">{meta}</td>')


def _message(name: str, when_: str, text_: str, top: int = 24) -> str:
    """Сообщение как в обсуждении дела: аватар, имя и время, текст в «облачке»."""
    meta = (f'<span style="font-weight:600;color:{INK}">{_e(name)}</span>'
            + (f'<span style="color:{INK3}">&nbsp;&nbsp;·&nbsp;&nbsp;{_e(_nb(when_))}</span>' if when_ else ""))
    bubble = _tbl(f'<tr><td bgcolor="{SOFT}" style="background:{SOFT};border-radius:4px 14px 14px 14px;'
                  f'padding:12px 16px;{_font(15, 23, INK)}">{_mark_mentions(text_)}</td></tr>',
                  "margin:6px 0 0")
    return _tbl(f'<tr><td width="32" valign="top" style="width:32px;padding-top:1px">{_avatar(name)}</td>'
                f'<td valign="top" style="padding-left:12px">'
                f'<div style="{_font(13, 18, INK2)}">{meta}</div>{bubble}</td></tr>',
                f"margin:{top}px 0 0")


def _card(label: str, title: str, lead: str = "", *, accent: str = "", url: str = "",
          rows: list[tuple[str, str]] | None = None, top: int = 22) -> str:
    """Карточка объекта: дело, отчёт, материал. Подпись, название, пояснение, сведения."""
    edge = f"border-left:3px solid {accent};" if accent else ""
    name = (f'<a href="{_e(url)}" target="_blank" style="{_font(17, 23, INK, 600, "text-decoration:none")}">'
            f'{_e(title)}</a>' if url else f'<span style="{_font(17, 23, INK, 600)}">{_e(title)}</span>')
    facts = "".join(
        f'<tr><td width="38%" valign="top" style="padding:9px 0;border-top:1px solid {HAIR};{_font(13, 19, INK3)}">'
        f'{_e(k)}</td><td valign="top" style="padding:9px 0;border-top:1px solid {HAIR};{_font(14, 19, INK)}">'
        f'{_e(v)}</td></tr>' for k, v in (rows or []) if v)
    return _tbl(
        f'<tr><td style="border:1px solid {HAIR};{edge}border-radius:12px;padding:16px 20px 16px">'
        f'<div style="{_font(11, 16, INK3, 600, "letter-spacing:.08em;text-transform:uppercase")}">{_e(label)}</div>'
        f'<div style="margin:5px 0 0">{name}</div>'
        + (f'<div style="margin:6px 0 0;{_font(14, 21, INK2)}">{_e(lead)}</div>' if lead else "")
        + (_tbl(facts, "margin:14px 0 0") if facts else "")
        + '</td></tr>', f"margin:{top}px 0 0;border-collapse:separate")


def _pill(label: str, value: str, top: int = 16) -> str:
    return _tbl(f'<tr><td bgcolor="{SOFT}" style="background:{SOFT};border-radius:999px;padding:6px 12px;'
                f'{_font(13, 17, INK3)}">{_e(label)}:&nbsp;<b style="font-weight:600;color:{INK}">{_e(value)}</b>'
                f'</td></tr>', f"margin:{top}px 0 0;border-collapse:separate", width=False)


def _btn(label: str, url: str, top: int = 28) -> str:
    """Кнопка, которая остаётся кнопкой и в Outlook (отступы — у ячейки через mso-padding-alt)."""
    return _tbl(f'<tr><td bgcolor="{INK}" style="background:{INK};border-radius:10px;mso-padding-alt:13px 22px">'
                f'<a href="{_e(url)}" target="_blank" style="display:inline-block;padding:13px 22px;'
                f'{_font(14, 18, "#FFFFFF", 600, "text-decoration:none;border-radius:10px")}">'
                f'{_e(label)}&nbsp;&nbsp;&rarr;</a></td></tr>', f"margin:{top}px 0 0;border-collapse:separate",
                width=False)


def _kpis(items: list[tuple[int, str]]) -> str:
    """Полоса крупных чисел в сводке."""
    w = 100 // len(items)
    cells = "".join(
        f'<td width="{w}%" valign="top" style="padding:14px 18px;'
        f'{f"border-left:1px solid {HAIR};" if i else ""}">'
        f'<div style="{_font(26, 30, INK, 600, "letter-spacing:-.02em")}">{n}</div>'
        f'<div style="margin:3px 0 0;{_font(12, 16, INK3)}">{_e(label)}</div></td>'
        for i, (n, label) in enumerate(items))
    return _tbl(f'<tr>{cells}</tr>', f"margin:22px 0 0;border:1px solid {HAIR};border-radius:12px;"
                                     f"border-collapse:separate").replace("<table ", '<table class="al-kpi" ', 1)


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
    """Строка ленты (пачка, сводка): аватар, событие ссылкой, цитата, рубрика · кто · когда."""
    ref = n.get("ref") or {}
    label, color = _kind(n)
    actor = n.get("actor_name") or ""
    snip = _clip(ref.get("snippet"), 170)
    meta = " · ".join(x for x in (label, actor, _nb(when(n.get("updated_at"), now))) if x)
    face = _avatar(actor, 28) if actor else _marker(color)
    return (f'<tr><td style="padding:14px 0;border-top:1px solid {HAIR}">'
            + _tbl(f'<tr><td width="28" valign="top" style="width:28px;padding-top:2px">{face}</td>'
                   f'<td valign="top" style="padding-left:12px">'
                   f'<a href="{_e(link_for(n))}" target="_blank" '
                   f'style="{_font(15, 21, INK, 600, "text-decoration:none")}">'
                   f'{_e(n.get("title") if show_case else short_title(n))}</a>'
                   + (f'<div style="margin:3px 0 0;{_font(14, 20, INK2)}">«{_mark_mentions(snip)}»</div>'
                      if snip else "")
                   + f'<div style="margin:5px 0 0;{_font(12, 17, INK3)}">'
                     f'<span style="color:{color};font-size:9px">&#9679;</span>&nbsp;&nbsp;{_e(meta)}</div>'
                   + '</td></tr>')
            + '</td></tr>')


def _feed(rows: list[str], top: int = 20) -> str:
    return _tbl("".join(rows), f"margin:{top}px 0 0")


def layout(*, preheader: str, title: str, body: str, reason: str, note: str = NOTE_AUTO,
           section: str = "") -> str:
    """Каркас письма: карточка с логотипом и разделом, тело, подвал с причиной и настройками."""
    pre = _e(preheader) + "&nbsp;&zwnj;" * 60
    return f"""<!DOCTYPE html>
<html lang="ru" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no">
<title>{_e(title)}</title>
<!--[if gte mso 9]><xml><o:OfficeDocumentSettings><o:AllowPNG/><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
<!--[if mso]><style>table,td,div,p,a,span,h1,b{{font-family:"Segoe UI",Arial,sans-serif !important}}</style><![endif]-->
<style>
  :root{{color-scheme:light only;supported-color-schemes:light only}}
  body{{margin:0;padding:0;background:{PAPER};-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%}}
  table{{border-collapse:collapse;mso-table-lspace:0;mso-table-rspace:0}}
  img{{-ms-interpolation-mode:bicubic}}
  a{{color:{LINK}}}
  @media (max-width:620px){{
    .al-wrap{{padding:12px 8px 28px !important}}
    .al-pad{{padding-left:22px !important;padding-right:22px !important}}
    .al-h1{{font-size:21px !important;line-height:28px !important}}
    .al-kpi td{{padding:12px 12px !important}}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:{PAPER}" bgcolor="{PAPER}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;mso-hide:all">{pre}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{PAPER}" style="background:{PAPER}">
<tr><td align="center" class="al-wrap" style="padding:32px 12px 40px">
  <!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;border-collapse:separate">
    <tr><td bgcolor="{SURFACE}" style="background:{SURFACE};border:1px solid {HAIR};border-radius:16px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td class="al-pad" style="padding:22px 32px 20px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
            <td valign="middle">{_logo_img()}</td>
            <td align="right" valign="middle" style="{_font(12, 16, INK3)}">{_e(section)}</td>
          </tr></table>
        </td></tr>
        <tr><td height="1" bgcolor="{HAIR}" style="height:1px;line-height:1px;font-size:1px;background:{HAIR}">&nbsp;</td></tr>
        <tr><td class="al-pad" style="padding:30px 32px 34px">
{body}
        </td></tr>
      </table>
    </td></tr>
    <tr><td class="al-pad" style="padding:22px 33px 0;{_font(12, 19, INK3)}">
      <p style="margin:0">{_e(reason)}</p>
      <p style="margin:4px 0 0">{_e(note)}</p>
      <p style="margin:14px 0 0"><a href="{_e(app_base())}/" target="_blank" style="color:{INK2};text-decoration:underline">Открыть AuditLens</a>&nbsp;&nbsp;·&nbsp;&nbsp;<a href="{_e(settings_url())}" target="_blank" style="color:{INK2};text-decoration:underline">Настроить уведомления</a></p>
      <p style="margin:14px 0 0;color:{INK4}">AuditLens — инструмент внутреннего аудита</p>
    </td></tr>
  </table>
  <!--[if mso]></td></tr></table><![endif]-->
</td></tr>
</table>
</body>
</html>"""


def _text(head: str, title: str, lines: list[str], cta: str, url: str, reason: str,
          note: str = NOTE_AUTO) -> str:
    body = "\n".join(x for x in lines if x is not None).strip("\n")
    return (f"AuditLens · {head}\n\n{title}\n\n{body}\n\n{cta}: {url}\n\n—\n{reason}\n{note}\n"
            f"Настроить уведомления: {settings_url()}\n")


# ── письма ───────────────────────────────────────────────────────────────────

def _render_event_private(n: dict, now=None) -> dict:
    """Событие на личную почту: что произошло и куда нажать — без подробностей."""
    r = redact(n)
    kind, ref = r["kind"], r["ref"]
    label, color = _kind(n)
    url = link_for(n)
    wh = when(n.get("updated_at"), now)
    cta = ("Открыть обсуждение" if kind in ("case_mention", "case_reply", "case_msg")
           else "Открыть отчёт" if kind == "report_shared" else "Открыть обращение" if kind == "ticket"
           else "Открыть дело")
    body = (_eyebrow(label, color) + _h1(r["title"], wh)
            + (_pill("Статус обращения", ref["status_label"]) if kind == "ticket" and ref.get("status_label")
               else "")
            + _p(_e(PRIVATE_NOTE), color=INK2, size=14, top=18) + _btn(cta, url))
    reason = "Вы получили это письмо, потому что подключили эту почту к уведомлениям AuditLens."
    note = NOTE_TICKET if kind == "ticket" else NOTE_CASE if kind != "report_shared" else NOTE_AUTO
    return {"subject": r["title"].replace("\u00a0", " "), "preheader": "Подробности — в AuditLens", "url": url,
            "html": layout(preheader="Подробности — в AuditLens", title=r["title"], body=body, reason=reason,
                           note=note, section=_section(kind)),
            "text": _text(label, r["title"].replace("\u00a0", " "), [wh, "", PRIVATE_NOTE], cta, url, reason, note),
            "thread": f"case-{_case_of(n)[0]}" if _case_of(n)[0] else None}


def render_event(n: dict, now=None, private: bool = False) -> dict:
    """Одно событие из колокольчика → письмо. Тема — текст уведомления.
    private — на личную почту: без названий, имён и цитат (redact)."""
    if private:
        return _render_event_private(n, now)
    kind, ref = n.get("kind"), n.get("ref") or {}
    case = ref.get("case")
    actor = n.get("actor_name") or ""
    wh = when(n.get("updated_at"), now)
    url = link_for(n)
    subject = n.get("title") or "Новое в AuditLens"
    label, color = _kind(n)
    reason = (f"Вы получили это письмо, потому что участвуете в деле «{case}»." if case
              else "Вы получили это письмо, потому что пользуетесь AuditLens.")
    note, snip, sub, inner, lines, cta = NOTE_CASE, ref.get("snippet") or "", "", "", [], "Открыть дело"

    if kind in ("case_mention", "case_reply", "case_msg"):
        on_item = bool(ref.get("on_item") and ref.get("item"))
        title = ("Вас упомянули в обсуждении" if kind == "case_mention"
                 else "Комментарий к вашему материалу" if on_item
                 else "Ответ на ваше сообщение" if kind == "case_reply"
                 else "Новое в обсуждении дела")
        sub = f"в деле «{case}»" if case else ""
        if on_item:
            inner += _card("Ваш материал", ref["item"], accent=HAIR)
        inner += _message(actor, wh, snip or subject, top=18 if on_item else 24)
        cta = "Открыть обсуждение"
        lines = [f"Материал: {ref['item']}" if on_item else None,
                 f"{actor}, {wh}:" if actor else None, f"«{snip}»" if snip else None]
    elif kind in ("case_added", "case_role", "case_owner"):
        role = "владелец" if kind == "case_owner" else ref.get("role_label") or ""
        title = ("Вам передали дело" if kind == "case_owner"
                 else "Вас добавили в дело" if kind == "case_added" else "Ваши права в деле изменились")
        rows = [("Ваши права", role), ("Через команду", ref.get("team") or ""), ("Кто", actor), ("Когда", wh)]
        inner += _card("Дело", case or "", url=url, rows=rows)
        hint = ROLE_HINT.get(role, "")
        if ref.get("team"):
            hint = (hint + " " if hint else "") + (f"Доступ выдан через команду «{ref['team']}»: "
                                                   f"пока вы в ней, дело остаётся у вас.")
        if hint:
            inner += _p(_e(hint), color=INK2, size=14, top=16)
        lines = [f"Дело: «{case}»"] + [f"{k}: {v}" for k, v in rows if v] + ([hint] if hint else [])
    elif kind == "report_shared":
        title = "С вами поделились отчётом"
        rep = ref.get("report") or ""
        inner += _tbl(f"<tr>{_byline(actor, wh)}</tr>", "margin:20px 0 0") if actor else ""
        inner += _card("Отчёт ИИ-помощника", rep, ref.get("lead") or "", accent=REPORT, url=url, top=16)
        cta = "Открыть отчёт"
        reason = "Вы получили это письмо, потому что с вами поделились отчётом в AuditLens."
        note = NOTE_AUTO
        lines = [f"«{rep}»", ref.get("lead") or None, "", f"{actor}, {wh}" if actor else wh]
    elif kind == "ticket":
        no = ref.get("no")
        if ref.get("reply"):
            title, sub = "Команда AuditLens ответила", (f"на ваше обращение №\u00a0{no}" if no else "")
        else:
            title, sub = "Новый статус обращения", (f"Обращение №\u00a0{no}" if no else "")
        if snip:
            inner += _message(TEAM, wh, snip)
        if ref.get("status_label"):
            inner += _pill("Статус обращения", ref["status_label"])
        inner += _p("Уточнить или ответить — в AuditLens: «Обратная связь» → «Мои обращения».",
                    color=INK3, size=14, top=18)
        cta, note = "Открыть обращение", NOTE_TICKET
        reason = "Вы получили это письмо, потому что писали в «Обратную связь» AuditLens."
        lines = [f"«{snip}»" if snip else None,
                 f"Статус: {ref.get('status_label')}" if ref.get("status_label") else None]
    else:
        title = subject
        if snip:
            inner += _message(actor, wh, snip)
        elif actor or wh:
            inner += _tbl(f"<tr>{_byline(actor, wh)}</tr>", "margin:20px 0 0")
        lines = [" · ".join(x for x in (actor, wh) if x), f"«{snip}»" if snip else None]

    body = _eyebrow(label, color) + _h1(title, sub) + inner + _btn(cta, url)
    preheader = _clip(f"{actor}: «{snip}»" if snip and actor else snip or
                      " · ".join(x for x in (case, actor, wh) if x) or "Уведомление AuditLens", 140)
    return {"subject": subject, "preheader": preheader, "url": url,
            "html": layout(preheader=preheader, title=subject, body=body, reason=reason, note=note,
                           section=_section(kind)),
            "text": _text(label, title + (f" {sub}" if sub else ""), lines, cta, url, reason, note),
            "thread": f"case-{_case_of(n)[0]}" if _case_of(n)[0] else None}


def render_batch(ns: list[dict], now=None, private: bool = False) -> dict:
    """Несколько личных событий за 15 минут — одним письмом."""
    if len(ns) == 1:
        return render_event(ns[0], now, private)
    if private:
        ns = [redact(n) for n in ns]
    first, k = ns[0], len(ns) - 1
    url = f"{app_base()}/#open?bell=1"
    subject = (f"{first.get('title')} и ещё {k} {_plural(k, 'событие', 'события', 'событий')}"
               .replace("\u00a0", " "))
    title = f"{len(ns)} {_plural(len(ns), 'новое событие', 'новых события', 'новых событий')} для вас"
    more = len(ns) - 12
    body = (_eyebrow("Новое для вас", BRAND) + _h1(title)
            + _feed([_item_row(n, now) for n in ns[:12]])
            + (_p(f"И ещё {more} — в колокольчике AuditLens.", color=INK3, size=13) if more > 0 else "")
            + (_p(_e(PRIVATE_NOTE), color=INK2, size=14, top=18) if private else "")
            + _btn("Открыть уведомления", url))
    reason = "Вы получили это письмо, потому что вас упомянули, вам ответили или открыли доступ в AuditLens."
    preheader = _clip("; ".join(n.get("title") or "" for n in ns[:3]), 140)
    lines = [f"— {n.get('title')} ({' · '.join(x for x in (n.get('actor_name'), when(n.get('updated_at'), now)) if x)})"
             for n in ns[:20]]
    return {"subject": subject, "preheader": preheader, "url": url,
            "html": layout(preheader=preheader, title=subject, body=body, reason=reason, note=NOTE_CASE,
                           section="Уведомления"),
            "text": _text("Новое для вас", title, lines, "Открыть уведомления", url, reason, NOTE_CASE),
            "thread": None}


def render_digest(ns: list[dict], now=None, name: str = "", private: bool = False) -> dict:
    """Утренняя сводка непрочитанного: крупные числа, затем по делам, затем отчёты и обращения.
    private — на личную почту: одной лентой, без названий дел, имён и цитат."""
    now = now or datetime.now(timezone.utc)
    groups: dict[str, dict] = {}
    other: list[dict] = []
    for n in ns:
        cid, ctitle = _case_of(n)
        if cid:
            groups.setdefault(cid, {"title": ctitle or "Дело", "items": [], "id": cid})["items"].append(n)
        else:
            other.append(n)
    n_cases, total = len(groups), len(ns)
    if private:                       # группы считаем по исходным событиям, показываем — обезличенные
        groups, other = {}, [redact(n) for n in ns]
    mine = sum(1 for n in ns if n.get("kind") in PERSONAL)
    day = day_title(now)
    url = f"{app_base()}/#open?bell=1"
    subject = (f"Сводка AuditLens за {day}: {total} {_plural(total, 'событие', 'события', 'событий')}"
               + (f" в {n_cases} {_plural(n_cases, 'деле', 'делах', 'делах')}" if n_cases else ""))
    fn = first_name(name)
    hello = f"{fn}, доброе утро." if fn else "Доброе утро."
    title = f"{total} {_plural(total, 'событие ждёт', 'события ждут', 'событий ждут')} вас"
    kpi = [(total, _plural(total, "событие", "события", "событий"))]
    if n_cases:
        kpi.append((n_cases, _plural(n_cases, "дело", "дела", "дел")))
    if mine:
        kpi.append((mine, "лично вам"))
    body = (_eyebrow(f"Сводка за {day}", BRAND) + _h1(title)
            + _p(f"{_e(hello)} Пока вас не было в AuditLens, накопилось вот что — "
                 f"каждая строка ведёт прямо к делу или отчёту.", top=12)
            + (_kpis(kpi) if len(kpi) > 1 else ""))
    text_lines = [hello, ""]
    for g in sorted(groups.values(), key=lambda x: -len(x["items"])):
        its = g["items"]
        msgs = sum(int(i.get("count") or 1) for i in its if i.get("kind") in ("case_msg", "case_mention", "case_reply"))
        mats = sum(int(i.get("count") or 1) for i in its if i.get("kind") == "case_items")
        summary = " · ".join(x for x in (
            f"{msgs} {_plural(msgs, 'сообщение', 'сообщения', 'сообщений')}" if msgs else "",
            f"{mats} {_plural(mats, 'материал', 'материала', 'материалов')}" if mats else "") if x)
        case_url = (f"{app_base()}/#open?case={g['id']}" if not str(g["id"]).startswith("t:") else url)
        body += (f'<div style="margin:32px 0 0;{_font(11, 16, INK3, 600, "letter-spacing:.08em;text-transform:uppercase")}">'
                 f'Дело</div>'
                 f'<a href="{_e(case_url)}" target="_blank" style="display:block;margin:4px 0 0;'
                 f'{_font(18, 24, INK, 600, "text-decoration:none;letter-spacing:-.01em")}">{_e(g["title"])}</a>'
                 + (f'<div style="margin:3px 0 0;{_font(13, 18, INK3)}">{_e(summary)}</div>' if summary else "")
                 + _feed([_item_row(i, now, show_case=False) for i in its[:6]], top=12)
                 + (_p(f"И ещё {len(its) - 6} — в деле.", color=INK3, size=13, top=8) if len(its) > 6 else ""))
        text_lines += [f"Дело «{g['title']}»" + (f" — {summary}" if summary else ""),
                       *[f"  — {short_title(i)}" for i in its[:6]], ""]
    if other:
        head, cap = ("События", 20) if private else ("Отчёты и обращения", 8)
        body += (f'<div style="margin:32px 0 0;{_font(11, 16, INK3, 600, "letter-spacing:.08em;text-transform:uppercase")}">'
                 f'{head}</div>'
                 + _feed([_item_row(i, now) for i in other[:cap]], top=10)
                 + (_p(f"И ещё {len(other) - cap} — в колокольчике AuditLens.", color=INK3, size=13, top=8)
                    if len(other) > cap else ""))
        text_lines += [head, *[f"  — {i.get('title')}".replace("\u00a0", " ") for i in other[:cap]], ""]
    if private:
        body += _p(_e(PRIVATE_NOTE), color=INK2, size=14, top=22)
        text_lines += [PRIVATE_NOTE, ""]
    body += _btn("Открыть AuditLens", url, top=32)
    reason = "Это утренняя сводка непрочитанного в AuditLens — приходит, только когда есть новое."
    preheader = ("Подробности — в AuditLens" if private else
                 _clip("; ".join(g["title"] for g in groups.values()) or (other[0].get("title") if other else ""), 140))
    return {"subject": subject, "preheader": preheader, "url": url,
            "html": layout(preheader=preheader, title=subject, body=body, reason=reason,
                           section=_weekday_title(now)),
            "text": _text(f"Сводка за {day}", title, text_lines, "Открыть AuditLens", url, reason),
            "thread": None}


def render_welcome(name: str = "", private: bool = False) -> dict:
    """Первое письмо на подключённый адрес: что и когда приходит, как настроить.
    private — адрес не корпоративный: предупреждаем, что письма будут без подробностей."""
    fn = first_name(name)
    hello = f"{fn}, здравствуйте!" if fn else "Здравствуйте!"
    rows = [("Сразу", TALK, "вас упомянули, ответили на ваше сообщение или комментарий, добавили в дело, "
                            "поделились отчётом, команда ответила на обращение — одним письмом раз в 15\u00a0минут"),
            ("Утром", CASE_C, "в рабочие дни около 8:00 — сводка непрочитанного по вашим делам, "
                              "только если есть новое"),
            ("Никогда", INK4, "то, что вы уже прочитали в AuditLens")]
    table = "".join(
        f'<tr><td width="112" valign="top" style="width:112px;padding:14px 0;border-top:1px solid {HAIR};'
        f'{_font(14, 21, INK, 600)}"><span style="color:{c};font-size:9px">&#9679;</span>&nbsp;&nbsp;{_e(k)}</td>'
        f'<td valign="top" style="padding:14px 0;border-top:1px solid {HAIR};{_font(14, 21, INK2)}">{_e(v[:1].upper() + v[1:])}</td></tr>'
        for k, c, v in rows)
    pre = "Сразу — о личном, утром — сводка по делам. Настраивается в колокольчике."
    body = (_eyebrow("Уведомления", BRAND) + _h1("Теперь и на почте")
            + _p(f"{_e(hello)} Теперь уведомления AuditLens приходят и на почту — чтобы не пропускать "
                 f"важное, даже когда инструмент не открыт.", top=14)
            + _tbl(table, "margin:22px 0 0")
            + (_p("Адрес не корпоративный, поэтому письма придут без подробностей: что произошло и "
                  "ссылка — без названий дел, имён коллег и цитат.", color=INK2, size=14, top=18)
               if private else "")
            + _p("Что присылать, выбирается в колокольчике рядом с вашим именем внизу меню. "
                 "Там же письма отключаются совсем.", color=INK3, size=14, top=18)
            + _btn("Настроить уведомления", settings_url()))
    reason = "Вы получили это письмо, потому что пользуетесь AuditLens."
    return {"subject": "Уведомления AuditLens теперь приходят на почту", "preheader": pre,
            "url": settings_url(),
            "html": layout(preheader=pre, title="Уведомления AuditLens теперь приходят на почту", body=body,
                           reason=reason, section="Уведомления"),
            "text": _text("Уведомления", "Теперь и на почте", [hello, ""] + [f"{k}: {v}" for k, _, v in rows],
                          "Настроить уведомления", settings_url(), reason),
            "thread": None}


def render_verify(code: str, email: str, name: str = "", ttl_min: int = 30) -> dict:
    """Код подтверждения адреса. Кнопка открывает AuditLens и подтверждает сама:
    код уходит в адрес после #, на сервер в ссылке он не попадает."""
    fn = first_name(name)
    hello = f"{fn}, здравствуйте!" if fn else "Здравствуйте!"
    pretty = f"{code[:3]}\u00a0{code[3:]}"
    url = f"{app_base()}/#open?bell=settings&mailcode={code}"
    box = _tbl(f'<tr><td align="center" bgcolor="{SOFT}" style="background:{SOFT};border-radius:12px;'
               f'padding:18px 12px;{_font(32, 40, INK, 600, "letter-spacing:.14em")}">{pretty}</td></tr>',
               "margin:22px 0 0;border-collapse:separate")
    body = (_eyebrow("Подтверждение почты", BRAND) + _h1("Код подтверждения")
            + _p(f"{_e(hello)} Адрес <b style=\"font-weight:600;color:{INK}\">{_e(email)}</b> указали в "
                 f"AuditLens для уведомлений. Введите код в колокольчике: «Что присылать» → «На почту».", top=14)
            + box
            + _p(f"Код действует {ttl_min} минут. Можно и не вводить: кнопка откроет AuditLens и подтвердит "
                 f"адрес сама.", color=INK3, size=14, top=16)
            + _btn("Подтвердить почту", url))
    reason = ("Письмо пришло, потому что этот адрес указали в AuditLens. Если это были не вы — просто "
              "удалите его: без кода адрес не подключится.")
    subject = f"Код подтверждения AuditLens: {code[:3]} {code[3:]}"
    return {"subject": subject, "preheader": f"Код {code[:3]} {code[3:]} — действует {ttl_min} минут",
            "url": url,
            "html": layout(preheader=f"Код {code[:3]} {code[3:]} — действует {ttl_min} минут", title=subject,
                           body=body, reason=reason, section="Уведомления"),
            "text": _text("Подтверждение почты", f"Код подтверждения: {code}",
                          [hello, f"Адрес {email} указали в AuditLens для уведомлений.",
                           f"Код действует {ttl_min} минут."], "Подтвердить почту", url, reason),
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
                   "actor_name": TEAM, "link": "inbox:17", "updated_at": ago(90),
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
    "verify": "Код подтверждения почты",
    "event_mention_private": "Упоминание — на личную почту (без подробностей)",
    "batch_private": "Несколько событий — на личную почту",
    "digest_private": "Утренняя сводка — на личную почту",
}


def render(tpl: str, data: list[dict] | None = None, name: str = "", now=None,
           private: bool = False) -> dict:
    """Шаблон по имени: на примерах или на переданных событиях.
    «…_private» — вариант для личной почты (без подробностей)."""
    if tpl.endswith("_private"):
        tpl, private = tpl[:-len("_private")], True
    if tpl == "welcome":
        return render_welcome(name, private)
    if tpl == "verify":
        return render_verify("482913", "anna.smirnova@example.org", name)
    data = data if data is not None else samples(now).get(tpl, [])
    if not data:
        raise KeyError(tpl)
    if tpl == "digest":
        return render_digest(data, now, name, private)
    if tpl == "batch":
        return render_batch(data, now, private)
    return render_event(data[0], now, private)


# ── галерея для владельца: как выглядят письма и «Отправить себе» ──────────────

def gallery_page(cards: list[dict], test_to: list[str], configured: bool, source: str,
                 stats: dict | None = None) -> str:
    to = ", ".join(test_to) or "не задан (MAIL_TEST_TO)"
    st = stats or {}
    usage = (f"почту подключили: {st.get('users', 0)} (корпоративных {st.get('corporate', 0)}, "
             f"личных {st.get('private', 0)}) · писем за сутки: {st.get('sent_day', 0)}"
             + (f", не ушло: {st['failed_day']}" if st.get("failed_day") else "")) if st else ""
    ok = configured and bool(test_to)
    state = "готово к отправке" if ok else "почта не настроена" if not configured else "нет тестового адреса"
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
      <button onclick="send('{_e(c['key'])}', this)" {'' if ok else 'disabled'}>Отправить себе</button>
    </div>
  </div>
  <div class="frame"><iframe title="{_e(c['label'])}" srcdoc="{_e(for_browser(c['html']))}" onload="fit(this)"></iframe></div>
  <pre class="txt" hidden>{_e(c['text'])}</pre>
</section>""")
    other = "sample" if source == "mine" else "mine"
    data = base64.b64encode(logo()[0]).decode()
    _, lw, lh = logo()
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Письма AuditLens</title>
<style>
body{{margin:0;background:{PAPER};font-family:{FONT};color:{INK};-webkit-font-smoothing:antialiased}}
.top{{position:sticky;top:0;z-index:2;background:rgba(243,242,238,.92);backdrop-filter:blur(10px);border-bottom:1px solid {HAIR};
  padding:12px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
.top h1{{font-size:15px;font-weight:600;margin:0;color:{INK3}}} .top .m{{font-size:13px;color:{INK3}}} .top .sp{{flex:1}}
.seg{{display:inline-flex;border:1px solid {HAIR};border-radius:9px;padding:2px;background:#fff}}
.seg button{{background:none;color:{INK3};padding:6px 10px;border-radius:7px}} .seg button.on{{background:{INK};color:#fff}}
main{{max-width:1240px;margin:0 auto;padding:20px 24px 60px;display:grid;grid-template-columns:repeat(auto-fill,minmax(560px,1fr));gap:20px;align-items:start}}
.card{{background:#fff;border:1px solid {HAIR};border-radius:14px;overflow:hidden}}
.head{{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;border-bottom:1px solid {HAIR}}}
.head>div:first-child{{flex:1;min-width:0}}
.lbl{{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{INK3}}}
.subj{{font-weight:600;font-size:14px;margin-top:3px}} .pre{{font-size:12px;color:{INK3};margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.acts{{display:flex;gap:6px;flex:none}}
button{{font:inherit;font-size:12.5px;font-weight:600;border:0;border-radius:8px;padding:8px 12px;background:{INK};color:#fff;cursor:pointer}}
button:active{{transform:scale(.96)}}
button.ghost{{background:none;color:{INK2};border:1px solid {HAIR}}} button:disabled{{opacity:.45;cursor:default}}
.frame{{background:{PAPER};display:flex;justify-content:center}}
iframe{{display:block;width:100%;height:700px;border:0;background:{PAPER};transition:width .25s ease}}
body.phone main{{grid-template-columns:repeat(auto-fill,minmax(400px,1fr))}}
body.phone iframe{{width:375px}}
.txt{{margin:0;padding:14px 16px;font-size:12px;line-height:1.55;white-space:pre-wrap;background:#FAFAF8;border-top:1px solid {HAIR}}}
.toast{{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:{INK};color:#fff;font-size:13px;padding:10px 16px;border-radius:999px;display:none}}
a{{color:{LINK}}}
@media(max-width:640px){{main{{grid-template-columns:1fr;padding:12px}}}}
</style></head><body>
<div class="top"><img src="data:image/png;base64,{data}" width="{lw}" height="{lh}" alt="AuditLens"><h1>Письма</h1>
  <span class="m">Тестовый адрес: <b>{_e(to)}</b> · {_e(state)} · по адресам из системы входа рассылка выключена{(" · " + _e(usage)) if usage else ""}</span>
  <span class="sp"></span>
  <span class="seg"><button class="on" onclick="view(this,false)">Компьютер</button><button onclick="view(this,true)">Телефон</button></span>
  <a class="m" href="?source={other}">{'Показать на примерах' if source == 'mine' else 'Показать на моих уведомлениях'}</a>
  <button onclick="sendAll(this)" {'' if ok else 'disabled'}>Отправить все себе</button>
</div>
<main>{''.join(blocks)}</main>
<div class="toast" id="toast"></div>
<script>
const SRC={source!r};
function fit(f){{try{{f.style.height=(f.contentDocument.documentElement.scrollHeight+4)+'px';}}catch(e){{}}}}
function view(b,phone){{document.body.classList.toggle('phone',phone);b.parentNode.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
  setTimeout(()=>document.querySelectorAll('iframe').forEach(fit),300);}}
function toast(t){{const e=document.getElementById('toast');e.textContent=t;e.style.display='block';clearTimeout(window._tt);window._tt=setTimeout(()=>e.style.display='none',3800);}}
function tgl(k){{const p=document.querySelector('#'+k+' .txt');p.hidden=!p.hidden;}}
async function send(k,b){{b.disabled=true;const old=b.textContent;b.textContent='Отправляю…';
  try{{const r=await fetch('/api/admin/mail/test',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{template:k,source:SRC}})}});
    const d=await r.json();if(!r.ok)throw new Error(d.detail||r.status);b.textContent='Отправлено ✓';toast('Ушло на '+d.to+' — письма в Сбер идут 2–5 минут');}}
  catch(e){{b.textContent=old;b.disabled=false;toast('Не ушло: '+e.message);}}}}
async function sendAll(b){{b.disabled=true;const bs=[...document.querySelectorAll('.acts button:not(.ghost)')];
  for(const x of bs){{if(!x.disabled){{await send(x.closest('.card').id,x);await new Promise(r=>setTimeout(r,1200));}}}}b.textContent='Готово';}}
</script></body></html>"""
