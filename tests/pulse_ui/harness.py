"""Стенд «Пульса» для браузерных тестов: настоящая страница приложения, API — фикстуры.

Страница собирается так же, как её отдаёт сервер (web/app.py, _index_html_with_bust):
собранный app.js — только если его первая строка несёт sha256 текущего app.jsx, иначе
app.jsx компилируется в браузере Babel'ем. Всё остальное подменено на поддельном адресе
http://al.test/: статика — из src/bank_audit/web/static, /api/* — из
tests/fixtures/pulse_v2/ (по файлу на эндпоинт), неизвестный /api/* — {} с кодом 200.
Сеть и база не нужны.

    from tests.pulse_ui.harness import pulse_browser, open_pulse, assert_no_hscroll

    def test_x(pulse_browser):
        page = open_pulse(pulse_browser, tab="inbox", width=390, dark=True)
        assert "/api/admin/inbox?status=open" in page.calls
        assert_no_hscroll(page)

API стенда:
- launch_browser(playwright) → Browser; browser_session() — то же контекстным менеджером;
  pulse_browser — готовая pytest-фикстура (scope="module"). Нет Chromium — pytest.skip.
- open_pulse(page_or_browser, tab=None, width=1208, height=900, dark=False, me="owner",
  overrides=None, query=None, now=NOW, wait=".rv-tabs") → Page с атрибутами:
  page.calls — адреса запросов к /api/* («путь?запрос», в порядке отправки, все методы);
  page.posts — не-GET запросы: {"method", "url", "body"}; page.errors — необработанные
  исключения страницы и console.error (кроме «Failed to load resource» от 4xx/5xx и
  заметки Babel о размере файла);
  page.console — все сообщения консоли; page.al — состояние подмены (me, overrides).
- api_calls(page, prefix=None, method=None) — отбор из page.calls / page.posts.
- assert_no_hscroll(page) — нет горизонтальной прокрутки документа (с виновниками).
- shot(page, path, full_page=True) — снимок (каталоги создаются).
- no_emoji(page, root_selector='[data-page="pulse"]') — в тексте и подписях нет эмодзи.
- assert_page_ok(page) — нет ошибок страницы и экрана «Страница не смогла отрисоваться».
- load_fixture(name) — JSON из tests/fixtures/pulse_v2.

overrides — поправки одного теста, не трогая общие фикстуры:
  {"/api/admin/pulse": {...}}                 — ответ 200 на любой метод;
  {"/api/admin/inbox?status=new": (500, {"detail": "x"})} — код и тело, с отбором по запросу;
  {"POST /api/admin/inbox/118": {...}}        — только этот метод;
  {"/api/admin/pulse": lambda method, path, query, body: {...}} — вычисляемый ответ.
Ключ без «?» совпадает с путём при любом запросе; с «?» — когда все его параметры есть
в запросе с теми же значениями. Побеждает самый точный ключ.

me="owner" — владелец (is_admin), me="pulse_user" — коллега с доступом к «Пульсу»: ему,
как и на сервере, 403 у плашки «Турбулентность» (GET и POST), «Запустить прогон»,
«Пометить служебной», загрузки штата, «Разметить новые» и отметок срезов, а в
mail.people нет адресов почты.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import json
import mimetypes
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "bank_audit" / "web" / "static"
LOOPHOLE_STATIC = ROOT / "src" / "bank_audit" / "loophole" / "static"
FIXTURES = ROOT / "tests" / "fixtures" / "pulse_v2"
ORIGIN = "http://al.test"
NOW = "2026-11-05T10:03:00+03:00"            # «сегодня» фикстур: четверг, 10:03 МСК
FALLBACK_CHROMIUM = "/opt/pw-browsers/chromium"
WIDTHS = (390, 948, 1208, 1440)               # телефон, без меню, 1208 окна и 1208 контента
PULSE_TABS_V2 = ("overview", "people", "quality", "inbox", "systems", "results")
PULSE_TABS_V1 = ("people", "outcomes", "reach", "reports", "ai", "inbox", "data", "tech")
BUILD_TIMEOUT_MS = 90_000                     # Babel компилирует мегабайт JSX секунды
OWNER_ONLY_POST = ("/api/admin/agent-eval", "/api/admin/pulse/outcomes/label",
                   "/api/admin/banner", "/api/admin/pulse/reach")
CRASH_TEXT = "Страница не смогла отрисоваться"
# не ошибки страницы: ответы 4xx/5xx подмены (403 коллеге — штатно) и заметка Babel
_CONSOLE_NOISE = ("Failed to load resource", "[BABEL] Note")

mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("application/javascript", ".jsx")
mimetypes.add_type("application/javascript", ".mjs")

# 1×1 серый PNG — снимки экрана в обращениях (/api/inbox/file/{id})
_PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                     "1f15c4890000000d49444154789c63f8cfc0f01f0005000201a5f6ad6c0000000049454e44ae426082")


# ── страница как у сервера ───────────────────────────────────────────────────
def built_matches_jsx() -> str | None:
    """sha256 app.jsx, если app.js собран из него (первая строка сборки), — как
    web/app.py:_built_matches_jsx."""
    built, jsx = STATIC / "app.js", STATIC / "app.jsx"
    if not built.exists() or not jsx.exists():
        return None
    sha = hashlib.sha256(jsx.read_bytes()).hexdigest()
    with built.open(encoding="utf-8") as f:
        head = f.readline()
    return sha if f"sha256 {sha}" in head else None


def index_html() -> tuple[str, str]:
    """(html, режим): режим "built" — собранный app.js, "babel" — app.jsx в браузере.
    Те же замены, что web/app.py:_index_html_with_bust."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    sha = built_matches_jsx()
    if sha:
        html = re.sub(r'<script src="[^"]*babel[^"]*"></script>\s*', "", html)
        html = re.sub(r'<script type="text/babel" src="/static/app\.jsx[^"]*"></script>',
                      f'<script src="/static/app.js?v={sha[:12]}"></script>', html)
        return html, "built"
    v = int((STATIC / "app.jsx").stat().st_mtime)
    html = re.sub(r'src="/static/app\.jsx(?:\?v=[^"]*)?"', f'src="/static/app.jsx?v={v}"', html)
    return html, "babel"


# ── фикстуры ─────────────────────────────────────────────────────────────────
_CACHE: dict[str, Any] = {}


def whats_new_id() -> str | None:
    """id текущего окна «Что нового» (const WN в app.jsx). Стенд отдаёт его в
    prefs.whats_new: иначе окно (с 12.11) всплывает поверх «Пульса» у тех, кто «застал
    прежнюю версию», и перехватывает клики во всех проверках вида."""
    m = re.search(r'const WN=\{\s*id:"([^"]+)"', (STATIC / "app.jsx").read_text(encoding="utf-8"))
    return m.group(1) if m else None


def load_fixture(name: str) -> Any:
    """Копия JSON-фикстуры (правки теста не портят общий кэш)."""
    if name not in _CACHE:
        _CACHE[name] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return copy.deepcopy(_CACHE[name])


def _today() -> dt.date:
    return dt.datetime.fromisoformat(NOW).date()


def _is_off(day: str) -> bool:
    return dt.date.fromisoformat(day).weekday() >= 5 or day == "2026-11-04"


def _fit_series(series: list[dict], days: int, key: str = "d") -> list[dict]:
    """Сплошной ряд по дням под период: короче — хвост, длиннее — недельный ритм назад."""
    if not series:
        return series
    out = list(series)
    if days <= len(out):
        return out[-days:]
    by = {r[key]: r for r in out}
    while len(out) < days:
        first = dt.date.fromisoformat(out[0][key])
        new = first - dt.timedelta(days=1)
        tpl = by.get((new + dt.timedelta(days=7)).isoformat()) or out[0]
        row = {**tpl, key: new.isoformat()}
        if "off" in row:
            row["off"] = _is_off(row[key])
        by[row[key]] = row
        out.insert(0, row)
    return out


def _since(days: int) -> str:
    return (_today() - dt.timedelta(days=days - 1)).isoformat()


def _qbool(v: str | None) -> bool | None:
    return None if v is None else v.lower() in ("1", "true", "yes")


def _pulse(query: dict, me: str) -> dict:
    """/api/admin/pulse: период — из ?days (ряды по дням режутся или продлеваются),
    with_me / hidden_n / is_owner и почта — по правилам web/app.py:admin_metrics."""
    d = load_fixture("admin_pulse.json")
    days = max(3, min(int(query.get("days") or 14), 60))
    d["days"] = days
    for k in ("dau", "ai_per_day"):
        d[k] = _fit_series(d.get(k) or [], days)
    fb = d.get("ai_feedback") or {}
    if fb.get("by_day"):
        fb["by_day"] = _fit_series(fb["by_day"], days)
    lo = _since(days)
    for k in ("reports_per_day", "errors_per_day", "new_users"):
        d[k] = [r for r in d.get(k) or [] if r.get("d", "") >= lo]
    owner = me == "owner"
    wm = _qbool(query.get("me"))
    wm = (not owner) if wm is None else wm       # владелец помечен служебным — себя не считает
    hidden = 3                                   # владелец и две служебные учётки
    d["with_me"] = wm
    d["is_owner"] = owner
    d["hidden_n"] = hidden - 1 if owner else hidden
    d["excluded"] = (hidden - (1 if wm else 0)) if owner else (hidden + (0 if wm else 1))
    if not owner:
        for r in (d.get("mail") or {}).get("people") or []:
            r.pop("email", None)
    return d


def _users(query: dict) -> dict:
    d = load_fixture("admin_users.json")
    d["days"] = max(1, min(int(query.get("days") or 30), 365))
    return d


def _user_card(login: str) -> tuple[int, Any]:
    f = FIXTURES / f"admin_user_card.{login}.json"
    if f.exists():
        return 200, load_fixture(f.name)
    users = load_fixture("admin_users.json")
    row = next((r for r in users["users"] if r["username"] == login), None)
    if not row:
        return 404, {"detail": "user not found"}
    card = load_fixture("admin_user_card.json")
    lv = {"0": (0, 0), "1": (4, 180), "2": (14, 1100), "3": (38, 3300)}
    card["by_day"] = [{"d": d, "views": lv[c][0], "time_s": lv[c][1]}
                      for d, c in zip(users.get("act_days") or [], row.get("act14") or "") if c != "0"]
    card["user"] = {k: v for k, v in row.items() if k not in ("excluded", "today", "act14")}
    return 200, card


def _reports(query: dict) -> dict:
    d = load_fixture("admin_reports.json")
    rows = d["reports"]
    if _qbool(query.get("only_bad")):
        rows = [r for r in rows if (r.get("dislikes") or 0) > 0]
    if query.get("mode") == "quick":
        rows = [r for r in rows if r.get("mode") == "quick"]
    elif query.get("mode") == "deep":
        rows = [r for r in rows if r.get("mode") != "quick"]
    if query.get("username"):
        rows = [r for r in rows if r.get("username") == query["username"]]
    if query.get("q"):
        q = query["q"].strip().lower()
        rows = [r for r in rows if q in f"{r.get('question') or ''} {r.get('title') or ''}".lower()]
    rows = rows[:max(1, min(int(query.get("limit") or 200), 500))]
    return {"reports": rows, "total": len(rows),
            "deep": sum(1 for r in rows if r.get("mode") != "quick"),
            "quick": sum(1 for r in rows if r.get("mode") == "quick"),
            "bad": sum(1 for r in rows if (r.get("dislikes") or 0) > 0)}


def _inbox(query: dict) -> dict:
    st = query.get("status") or "open"
    d = load_fixture(f"admin_inbox.{st if st in ('new', 'open', 'all') else 'all'}.json")
    if query.get("kind"):
        d["tickets"] = [t for t in d["tickets"] if t.get("kind") == query["kind"]]
    if query.get("section"):
        d["tickets"] = [t for t in d["tickets"] if t.get("section") == query["section"]]
    return d


def _ticket(tid: int, state: dict) -> tuple[int, Any]:
    if tid in state.setdefault("tickets", {}):
        return 200, copy.deepcopy(state["tickets"][tid])
    f = FIXTURES / f"admin_inbox_ticket.{tid}.json"
    if f.exists():
        return 200, load_fixture(f.name)
    row = next((t for t in load_fixture("admin_inbox.all.json")["tickets"] if t["ticket_id"] == tid), None)
    if not row:
        return 404, {"detail": "ticket not found"}
    msgs = [{"role": "user", "body": row["body"], "at": row["created_at"]}]
    if row.get("answered_at"):
        msgs.append({"role": "team", "body": "Спасибо, посмотрим.", "at": row["answered_at"]})
    t = {k: v for k, v in row.items() if k not in ("n_files", "n_msgs", "unread", "answered_at")}
    return 200, {**t, "context": {}, "user_seen_at": row["created_at"], "team_seen_at": None,
                 "messages": msgs, "files": []}


def _outcomes(query: dict) -> dict:
    d = load_fixture("admin_pulse_outcomes.json")
    try:
        days = int(query.get("days") or 0)
    except ValueError:
        days = 0
    days = days if days in (0, 7, 14, 30, 90) else 0
    if days:
        d["period"] = {"days": days, "since": f"{_since(days)}T00:00:00+03:00"}
    else:
        d["period"] = {"days": 0, "since": None}
        d.pop("prev", None)
    return d


def _report(rid: int) -> dict:
    r = load_fixture("report.json")
    row = next((x for x in load_fixture("admin_reports.json")["reports"] if x["report_id"] == rid), None)
    r["report_id"] = rid
    if row and rid != 4187:
        r.update({"owner": row["username"], "owner_name": row["name"], "question": row["question"],
                  "title": row["title"], "banks": row.get("banks") or [],
                  "body": f"# {row['title']}\n\n{row['question']}\n\nКраткий вывод отчёта для стенда."})
        r["payload"] = {**(r.get("payload") or {}), "mode": row.get("mode") or "deep"}
    return r


class _Stub:
    """Состояние подмены API одной страницы: кто смотрит, поправки теста, изменения POST."""

    def __init__(self, me: str, overrides: dict | None):
        if me not in ("owner", "pulse_user"):
            raise ValueError("me: 'owner' или 'pulse_user'")
        self.me = me
        self.overrides = dict(overrides or {})
        self.state: dict = {}

    # ── поправки теста ──
    def match_override(self, method: str, path: str, query: dict) -> Any:
        best, score = None, -1
        for key, val in self.overrides.items():
            m, _, rest = key.partition(" ") if " " in key else ("", "", key)
            if m and m.upper() != method:
                continue
            kp, _, kq = rest.partition("?")
            if kp != path:
                continue
            want = dict(parse_qsl(kq, keep_blank_values=True))
            if any(query.get(k) != v for k, v in want.items()):
                continue
            s = (2 if m else 0) + len(want)
            if s > score:
                best, score = val, s
        return best if score >= 0 else _NO

    # ── ответы ──
    def respond(self, method: str, path: str, query: dict, body: Any) -> tuple[int, Any]:
        owner = self.me == "owner"
        if method == "GET":
            return self._get(path, query, owner)
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            return self._post(method, path, body, owner)
        return 200, {}

    def _get(self, path: str, query: dict, owner: bool) -> tuple[int, Any]:
        if path == "/api/me":
            me = load_fixture(f"me.{self.me}.json")
            me.setdefault("prefs", {}).setdefault("whats_new", whats_new_id())
            return 200, me
        if path in ("/api/admin/pulse", "/api/admin/metrics"):
            return 200, _pulse(query, self.me)
        if path == "/api/admin/users":
            return 200, _users(query)
        m = re.fullmatch(r"/api/admin/users/([^/]+)", path)
        if m:
            return _user_card(m.group(1))
        if path == "/api/admin/reports":
            return 200, _reports(query)
        if path == "/api/admin/complaints":
            d = load_fixture("admin_complaints.json")
            d["days"] = int(query.get("days") or 30)
            return 200, d
        if path == "/api/admin/inbox":
            return 200, _inbox(query)
        m = re.fullmatch(r"/api/admin/inbox/(\d+)", path)
        if m:
            return _ticket(int(m.group(1)), self.state)
        if path == "/api/admin/agent-eval":
            return 200, load_fixture(f"admin_agent_eval.{'deep' if query.get('engine') == 'deep' else 'hermes'}.json")
        if path == "/api/admin/pulse/slices":
            return 200, load_fixture("admin_pulse_slices.json")
        if path == "/api/admin/pulse/reach":
            d = load_fixture("admin_pulse_reach.json")
            if not owner:                       # как сервер: кто не нашёлся в штате — только владельцу
                d.pop("other_people", None)
            return 200, d
        if path == "/api/admin/pulse/costs":
            return 200, load_fixture("admin_pulse_costs.json")
        if path == "/api/admin/pulse/outcomes":
            return 200, _outcomes(query)
        if path == "/api/admin/pulse/outcomes.xlsx":
            return 200, _Raw(b"PK\x03\x04xlsx",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if path == "/api/reviews/slice-flags":
            return (200, load_fixture("reviews_slice_flags.json")) if owner else \
                (403, {"detail": "только команде AuditLens"})
        if path == "/api/admin/banner":
            if not owner:
                return 403, {"detail": "admin only"}
            return 200, copy.deepcopy(self.state.get("banner")) or load_fixture("admin_banner.json")
        m = re.fullmatch(r"/api/reports/(\d+)", path)
        if m:
            return 200, _report(int(m.group(1)))
        m = re.fullmatch(r"/api/admin/session/(\d+)", path)
        if m:
            d = load_fixture("admin_session.json")
            d["session"]["session_id"] = int(m.group(1))
            return 200, d
        if re.fullmatch(r"/api/inbox/file/\d+", path):
            return 200, _Raw(_PNG, "image/png")
        return 200, {}

    def _post(self, method: str, path: str, body: Any, owner: bool) -> tuple[int, Any]:
        b = body if isinstance(body, dict) else {}
        owner_only = (path in OWNER_ONLY_POST or path.startswith("/api/reviews/slice-flags/")
                      or re.fullmatch(r"/api/admin/users/[^/]+/hidden", path))
        if owner_only and not owner:
            return 403, {"detail": "admin only"}
        m = re.fullmatch(r"/api/admin/inbox/(\d+)", path)
        if m:
            code, t = _ticket(int(m.group(1)), self.state)
            if code != 200:
                return code, t
            st = b.get("status")
            stat_ru = load_fixture("admin_inbox.all.json")["statuses"]
            t["status_changed"] = bool(st and st in stat_ru and st != t["status"])
            if t["status_changed"]:
                t["status"], t["status_label"] = st, stat_ru[st]
                t["messages"].append({"role": "system", "body": f"Статус: {stat_ru[st]}", "at": NOW})
            if (b.get("reply") or "").strip():
                t["messages"].append({"role": "team", "body": b["reply"].strip(), "at": NOW})
            self.state["tickets"][int(m.group(1))] = copy.deepcopy(t)
            return 200, t
        if path == "/api/admin/agent-eval":
            return 200, {"started": True}
        if path == "/api/admin/pulse/outcomes/label":
            return 200, {"started": True, "pending": 12}
        if path == "/api/admin/banner":
            st = load_fixture("admin_banner.json")
            on = bool(b.get("active"))
            until = None
            if on and b.get("days"):
                until = (dt.datetime.fromisoformat(NOW) + dt.timedelta(days=int(b["days"]))).isoformat()
            st.update({"on": on, "active": on, "until": until, "v": NOW if on else st.get("v")})
            self.state["banner"] = st
            return 200, st
        m = re.fullmatch(r"/api/admin/users/([^/]+)/hidden", path)
        if m:
            return 200, {"ok": True, "hidden": bool(b.get("hidden"))}
        if path == "/api/admin/pulse/reach":
            return 200, load_fixture("admin_pulse_reach.json")
        if path.startswith("/api/reviews/slice-flags/"):
            return 200, {"ok": True, "decision": b.get("decision")}
        if path in ("/api/journal", "/api/track"):
            return 200, {"ok": True, "accepted": len(b.get("events") or [])}
        slug = path[len("/api/"):].strip("/").replace("/", "_")
        f = FIXTURES / f"post.{slug}.json"
        if f.exists():
            return 200, load_fixture(f.name)
        return 200, {"ok": True}


class _Raw:
    def __init__(self, data: bytes, ctype: str):
        self.data, self.ctype = data, ctype


_NO = object()


# ── маршрутизация ────────────────────────────────────────────────────────────
def _serve_file(route, base: Path, rel: str) -> None:
    p = base / unquote(rel)
    if ".." in Path(unquote(rel)).parts or not p.is_file():   # символьные ссылки разрешены
        route.fulfill(status=404, body="not found", content_type="text/plain")
        return
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype == "application/javascript":
        ctype += "; charset=utf-8"
    route.fulfill(status=200, body=p.read_bytes(), headers={"Content-Type": ctype})


def _handler(page) -> Callable:
    def handle(route) -> None:
        req = route.request
        url = req.url
        if not url.startswith(ORIGIN + "/"):
            route.abort()                       # наружу стенд не ходит
            return
        u = urlsplit(url)
        path = u.path
        if path.startswith("/api/"):
            _api(page, route, req, u)
        elif path.startswith("/static/loophole/"):
            _serve_file(route, LOOPHOLE_STATIC, path[len("/static/loophole/"):])
        elif path.startswith("/static/"):
            _serve_file(route, STATIC, path[len("/static/"):])
        else:                                   # «/» и SPA-фоллбэк, как у сервера
            html, _mode = index_html()
            route.fulfill(status=200, body=html, headers={"Content-Type": "text/html; charset=utf-8"})
    return handle


def _api(page, route, req, u) -> None:
    path = u.path
    query = dict(parse_qsl(u.query, keep_blank_values=True))
    full = path + (f"?{u.query}" if u.query else "")
    method = req.method.upper()
    page.calls.append(full)
    body = None
    if method != "GET":
        raw = req.post_data
        try:
            body = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            body = raw
        page.posts.append({"method": method, "url": full, "body": body})
    stub: _Stub = page.al
    ov = stub.match_override(method, path, query)
    if ov is not _NO:
        val = ov(method, path, query, body) if callable(ov) else ov
        status, data = (val if isinstance(val, tuple) else (200, val))
    else:
        status, data = stub.respond(method, path, query, body)
    if isinstance(data, _Raw):
        route.fulfill(status=status, body=data.data, headers={"Content-Type": data.ctype})
        return
    route.fulfill(status=status, body=json.dumps(data, ensure_ascii=False),
                  headers={"Content-Type": "application/json; charset=utf-8"})


# ── браузер ──────────────────────────────────────────────────────────────────
def launch_browser(playwright):
    """Chromium: сначала штатный, затем /opt/pw-browsers/chromium; иначе pytest.skip."""
    errors = []
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as e:  # noqa: BLE001 — нет штатного браузера Playwright
        errors.append(str(e).splitlines()[0])
    if Path(FALLBACK_CHROMIUM).exists():
        try:
            return playwright.chromium.launch(headless=True, executable_path=FALLBACK_CHROMIUM)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e).splitlines()[0])
    pytest.skip("Chromium для Playwright недоступен: " + " | ".join(errors))


@contextlib.contextmanager
def browser_session():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        browser = launch_browser(pw)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture(scope="module")
def pulse_browser():
    with browser_session() as browser:
        yield browser


def _hash(tab: str | None, query: Any) -> str:
    parts = []
    if tab:
        parts.append(f"tab={quote(str(tab))}")
    if isinstance(query, dict):
        parts += [f"{k}={quote(str(v))}" for k, v in query.items()]
    elif query:
        parts.append(str(query).lstrip("?&"))
    return "#pulse" + (("?" + "&".join(parts)) if parts else "")


def _init_script(dark: bool) -> str:
    theme = "dark" if dark else "light"
    # тема — до первой отрисовки (её читает и скрипт в <head>); заметка о новом меню —
    # прочитана; остальные заметки гасит /api/me (prefs.onboarded, mail_promo, has_email)
    return (f"try{{localStorage.setItem('auditlens-theme-choice','{theme}');"
            "localStorage.setItem('al-rename-1001b','1');}catch(e){}")


def open_pulse(page_or_browser, tab: str | None = None, width: int = 1208, height: int = 900,
               dark: bool = False, me: str = "owner", overrides: dict | None = None,
               query: Any = None, now: str | None = NOW, wait: str | None = ".rv-tabs"):
    """Открыть «Пульс» на стенде и дождаться отрисовки. Возвращает Page.

    page_or_browser — Browser (создаётся контекст МСК / ru-RU), BrowserContext или Page
    (повторное открытие: обновятся me, overrides и размер окна). tab — раздел в адресе
    (#pulse?tab=…); query — ещё параметры адреса: dict или строка «ticket=118&person=x».
    now — «текущее» время страницы (None — настоящие часы). wait — селектор внутри
    [data-page="pulse"], которого ждать (None — только смонтированный раздел)."""
    target = page_or_browser
    color = "dark" if dark else "light"
    if hasattr(target, "new_context"):                       # Browser
        ctx = target.new_context(viewport={"width": width, "height": height}, locale="ru-RU",
                                 timezone_id="Europe/Moscow", color_scheme=color)
        page = ctx.new_page()
    elif hasattr(target, "new_page"):                        # BrowserContext
        page = target.new_page()
        page.set_viewport_size({"width": width, "height": height})
        page.emulate_media(color_scheme=color)
    else:                                                    # Page
        page = target
        page.set_viewport_size({"width": width, "height": height})
        page.emulate_media(color_scheme=color)
    fresh = not hasattr(page, "al")
    page.al = _Stub(me, overrides)
    if fresh:
        page.calls, page.posts, page.errors, page.console = [], [], [], []

        def on_console(msg):
            text = msg.text
            page.console.append(f"{msg.type}: {text}")
            if msg.type == "error" and not any(x in text for x in _CONSOLE_NOISE):
                page.errors.append("console: " + text)

        page.on("pageerror", lambda e: page.errors.append(f"pageerror: {e}"))
        page.on("console", on_console)
        page.route("**/*", _handler(page))
        if now:
            page.clock.install(time=dt.datetime.fromisoformat(now))
            page.clock.resume()
    else:
        page.calls.clear(), page.posts.clear(), page.errors.clear(), page.console.clear()
    page.add_init_script(_init_script(dark))
    url = f"{ORIGIN}/{_hash(tab, query)}"
    if fresh or not page.url.startswith(ORIGIN):
        page.goto(url, wait_until="domcontentloaded")
    else:
        page.goto("about:blank")                             # смена только хэша не перезагружает
        page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector('[data-page="pulse"]', state="attached", timeout=BUILD_TIMEOUT_MS)
    if wait:
        page.wait_for_selector(f'[data-page="pulse"] {wait}', timeout=BUILD_TIMEOUT_MS)
    return page


# ── проверки ─────────────────────────────────────────────────────────────────
def api_calls(page, prefix: str | None = None, method: str | None = None) -> list:
    """Запросы к API: без method — адреса из page.calls; с method — записи page.posts."""
    if method and method.upper() != "GET":
        rows = [p for p in page.posts if p["method"] == method.upper()]
        return [p for p in rows if not prefix or p["url"].startswith(prefix)]
    return [c for c in page.calls if not prefix or c.startswith(prefix)]


_HSCROLL_JS = r"""() => {
  const d = document.documentElement, cw = d.clientWidth;
  const clips = el => { for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX; if (o !== "visible") return true; } return false; };
  const bad = [];
  for (const el of document.querySelectorAll("body *")) {
    const r = el.getBoundingClientRect();
    if (r.width && r.right > cw + 1 && getComputedStyle(el).position !== "fixed" && !clips(el))
      bad.push((el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") +
        (typeof el.className === "string" && el.className ? "." + el.className.trim().split(/\s+/).join(".") : "")) +
        " → " + Math.round(r.right));
    if (bad.length >= 8) break;
  }
  return {sw: d.scrollWidth, cw, bw: document.body.scrollWidth, bad};
}"""


def assert_no_hscroll(page) -> None:
    """Документ не прокручивается по горизонтали (scrollWidth == clientWidth)."""
    r = page.evaluate(_HSCROLL_JS)
    assert r["sw"] <= r["cw"] and r["bw"] <= r["cw"], (
        f"горизонтальная прокрутка: scrollWidth {r['sw']} / body {r['bw']} > clientWidth {r['cw']}; "
        f"вылезают: {r['bad']}")


def shot(page, path, full_page: bool = True) -> Path:
    """Снимок страницы; каталоги создаются. Возвращает путь."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(p), full_page=full_page)
    return p


# эмодзи и пиктограммы-символы: ✓ ✕ ✦ ⚑ ⚠ ★ 👍 и т. п.; стрелки, ₽, №, ×, ⌘ — не эмодзи
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️⌚⌛"
                    "⏩-⏳⏸-⏺Ⓜ〰〽㊗㊙]")

_TEXT_JS = r"""(sel) => {
  const root = document.querySelector(sel);
  if (!root) return null;
  const out = [root.innerText || ""];
  for (const el of root.querySelectorAll("[aria-label],[title],[data-tip],[placeholder],[alt]"))
    for (const a of ["aria-label", "title", "data-tip", "placeholder", "alt"])
      if (el.hasAttribute(a)) out.push(el.getAttribute(a));
  return out.join("\n");
}"""


def no_emoji(page, root_selector: str = '[data-page="pulse"]') -> bool:
    """В тексте и подписях (aria-label, title, data-tip, placeholder, alt) нет эмодзи."""
    text = page.evaluate(_TEXT_JS, root_selector)
    assert text is not None, f"нет элемента {root_selector}"
    found = []
    for line in text.splitlines():
        hits = _EMOJI.findall(line)
        if hits:
            found.append(f"{''.join(sorted(set(hits)))} в «{line.strip()[:80]}»")
    assert not found, "эмодзи в интерфейсе: " + "; ".join(found[:10])
    return True


def assert_page_ok(page) -> None:
    """Нет необработанных ошибок и аварийного экрана PageBoundary."""
    assert not page.errors, "ошибки страницы: " + " | ".join(page.errors[:5])
    crashed = page.locator(f'[data-page="pulse"] :text("{CRASH_TEXT}")').count()
    assert not crashed, "«Пульс» упал в PageBoundary"
