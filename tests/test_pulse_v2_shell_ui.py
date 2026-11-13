"""«Пульс» v2 в браузере: каркас страницы и форма фикстур стенда.

Стенд — tests/pulse_ui/harness.py (настоящая страница, API из tests/fixtures/pulse_v2).
Первая группа — общие проверки любой версии страницы: отрисовка без ошибок, пункт меню
у владельца и коллеги с доступом, переключение вкладок, нет горизонтальной прокрутки.
Вторая — договор оболочки (CONTRACT §1, §3–§5, §10): шесть разделов и прежние ключи,
бриф по фикстурам, бейдж обращений, ссылки на обращение и человека, период только в
«Людях» и «Качестве ИИ», меню «⋯» владельца и коллеги, поиск Ctrl+K вместо палитры
ИИ-помощника, опрос только на экране, слои закрываются при уходе, ссылки колокольчика.
Разделы — заглушки фундамента или готовые: проверки опираются только на оболочку.

Снимки: PULSE_SHOTS=<каталог> python -m pytest tests/test_pulse_v2_shell_ui.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.pulse_ui import harness as H
from tests.pulse_ui.harness import (
    api_calls,
    assert_no_hscroll,
    assert_page_ok,
    load_fixture,
    no_emoji,
    open_pulse,
)

PULSE = '[data-page="pulse"]'
TABS = f'{PULSE} [role="tablist"] [role="tab"]'
SHOTS = os.getenv("PULSE_SHOTS")


@pytest.fixture(scope="module")
def browser():
    with H.browser_session() as b:
        yield b


def _shot(page, name: str) -> None:
    if SHOTS:
        H.shot(page, os.path.join(SHOTS, name + ".png"), full_page=False)


# ── форма фикстур — без браузера ─────────────────────────────────────────────
def test_fixture_shapes_follow_backend(monkeypatch):
    """Каждый ключ, который отдаёт сервер, есть в фикстуре: стенд не расходится с API."""
    from bank_audit.web import telemetry as T
    monkeypatch.setattr(T, "_rows", lambda sql, params=None: [])
    monkeypatch.setattr(T, "_scalar", lambda sql, params=None: 0)
    m = T.metrics(14, [])
    fx = load_fixture("admin_pulse.json")
    assert set(m) <= set(fx), set(m) - set(fx)
    for k in ("today", "features", "segments", "ai_feedback", "retention", "mail", "persona",
              "ingest", "collect", "search", "news_quality", "personalization", "topics"):
        if isinstance(m[k], dict):
            assert set(m[k]) <= set(fx[k]), (k, set(m[k]) - set(fx[k]))
    d = T.users_directory(30, [])
    ux = load_fixture("admin_users.json")
    assert set(d) <= set(ux), set(d) - set(ux)
    # строка человека — все псевдонимы колонок _DIRECTORY и поля, что дописывает Python
    head = T._DIRECTORY.split("FROM app_user au")[0]
    cols = set(re.findall(r"\bAS\s+([a-z_]+)", head)) | {"username"}
    assert len(cols) > 15, cols
    for row in ux["users"]:
        assert cols | {"online", "today", "excluded"} <= set(row), cols - set(row)
    card = load_fixture("admin_user_card.akovaleva.json")
    assert {"user", "profile", "by_day", "pages", "questions", "reports", "ratings", "errors",
            "trail"} == set(card)


def test_fixtures_carry_contract_fields():
    """Новые поля CONTRACT §6 — в фикстурах, чтобы фронт проверялся и с ними."""
    m = load_fixture("admin_pulse.json")
    h = m["health"]
    assert len(h["days"]) == 14 and h["days"][-1] == "2026-11-05" and h["off"] == [1, 2, 8, 9, 12]
    for k in ("server", "digest", "reviews", "news", "slices", "rsearch", "web", "ingest"):
        assert len(h[k]) == 14 and set(h[k]) <= {"o", "w", "b", "n"}, k
    assert all("off" in x for x in m["dau"])
    assert {"ai_total_prev", "reports_prev"} <= set(m["features"])
    assert sum(x["good"] for x in m["ai_feedback"]["by_day"]) == m["ai_feedback"]["likes"]
    assert sum(x["bad"] for x in m["ai_feedback"]["by_day"]) == m["ai_feedback"]["dislikes"]
    seg = m["retention"]["segments"]
    assert set(m["retention"]["seg_of"].values()) <= {"core", "regular", "once", "churned"}
    assert len(m["retention"]["seg_of"]) == seg["core"] + seg["regular"] + seg["once"] + seg["churned"]
    u = load_fixture("admin_users.json")
    assert len(u["act_days"]) == 14 and u["act_off"] == h["off"]
    assert all(re.fullmatch(r"[0-3]{14}", r["act14"]) for r in u["users"])
    # сюжет макета: 64 человека, 23 сегодня, 41 вопрос, 3 обращения ждут ответа
    t = m["today"]
    assert (t["users_total"], t["active"], t["ai"], m["inbox"]["unread"]) == (64, 23, 41, 3)
    assert u["total"] == 64 and u["today"] == 23


def test_harness_serves_index_like_server():
    """Стенд отдаёт ту же страницу, что сервер: app.js при совпадении сборки, иначе Babel."""
    from bank_audit.web import app as A
    html, mode = H.index_html()
    assert html == A._index_html_with_bust()
    assert mode == ("built" if '<script src="/static/app.js?v=' in html else "babel")


def test_fixture_json_is_valid_utf8():
    for p in sorted(H.FIXTURES.glob("*.json")):
        json.loads(p.read_text(encoding="utf-8"))


# ── сигналы: независимый расчёт по правилам CONTRACT §5 и §10 ────────────────
def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


_WORD_M = ["ноль", "один", "два", "три", "четыре", "пять"]
_WORD_N = ["ноль", "одно", "два", "три", "четыре", "пять"]


def _word(n: int, g: str) -> str:
    return (_WORD_N if g == "n" else _WORD_M)[n] if 0 <= n <= 5 else str(n)


def _brief_tail(ni: int, nt: int) -> str:
    """Вторая фраза брифа: «Три сбоя и четыре дела ждут вас.» (числа прописью до пяти)."""
    cap = lambda s: s[:1].upper() + s[1:]
    if ni and nt:
        return (f"{cap(_word(ni, 'm'))} {_plural(ni, 'сбой', 'сбоя', 'сбоев')} и "
                f"{_word(nt, 'n')} {_plural(nt, 'дело', 'дела', 'дел')} ждут вас.")
    if ni:
        return f"{cap(_word(ni, 'm'))} {_plural(ni, 'сбой', 'сбоя', 'сбоев')}."
    if nt:
        return f"{cap(_word(nt, 'n'))} {_plural(nt, 'дело ждёт', 'дела ждут', 'дел ждут')} вас."
    return "Всё спокойно."


def _expected_signals(m: dict, owner: bool, flags_ok: bool) -> tuple[list[str], list[str]]:
    """Ключи пунктов очереди: сломано (incidents) и ждёт решения (todo) — те же правила,
    что у прежнего сторожа puGuardItems, плюс уточнения CONTRACT §10."""
    now = dt.datetime.fromisoformat(H.NOW)
    today = now.date().isoformat()
    inc, todo = [], []
    t = m.get("today") or {}
    if (t.get("errors") or 0) > 0:
        inc.append("errors")
    q = (m.get("ingest") or {}).get("queue") or {}
    if (q.get("dropped") or 0) > 0:
        inc.append("ingest_dropped")
    if q.get("workers") == 0:
        inc.append("ingest_workers")
    dg = m.get("digest") if isinstance(m.get("digest"), list) else []
    if any(x.get("status") and x["status"] != "ok" for x in dg):
        inc.append("digest_failed")
    if dg and dg[0].get("d") and dg[0]["d"] < today and now.hour >= 8:
        inc.append("digest_missing")
    rs = (m.get("review_sources") or {}).get("sources") or []
    if any(x.get("status") in ("встал", "просел") for x in rs):
        inc.append("reviews_down")
    if any(x.get("status") == "всплеск" and (x.get("norm") or 0) > 0 for x in rs):
        inc.append("reviews_surge")
    if ((m.get("search") or {}).get("gateway") or {}).get("breaker_open"):
        inc.append("search_breaker")
    for k in ("hermes", "deep"):
        e = (m.get("eval") or {}).get(k)
        if not e or e.get("score") is None:
            continue
        if e.get("delta") is not None and e["delta"] <= -5:
            inc.append("eval_drop_" + k)
        elif e["score"] < 75:
            inc.append("eval_low_" + k)
        if k == "hermes" and (e.get("age_days") or 0) > 8:
            inc.append("eval_age")
    if any(x.get("last_error") for x in ((m.get("news_quality") or {}).get("stream") or {}).get("sources") or []):
        inc.append("news_feeds")
    rp = m.get("reviews_pipeline") or {}
    if (rp.get("pending_old") or 0) > 50:
        inc.append("slices_lag")
    if rp.get("slices_not_ready"):
        inc.append("slices_not_ready")
    if rp.get("dedup_age_h") is not None and rp["dedup_age_h"] > 30:
        inc.append("dedup_age")
    if rp.get("stories_age_h") is not None and rp["stories_age_h"] > 50:
        inc.append("stories_age")
    if rp.get("canary_age_days") is not None and rp["canary_age_days"] > 9:
        inc.append("canary_age")
    if (m.get("slice_canary") or [{}])[0].get("alert"):
        inc.append("canary_alert")
    if ((m.get("reviews_search") or {}).get("failed") or 0) > 0:
        inc.append("rsearch_failed")
    ib = m.get("inbox") or {}
    if (ib["unanswered"] if ib.get("unanswered") is not None else ib.get("unread") or 0) > 0:
        todo.append("inbox")
    if ((m.get("ai_feedback") or {}).get("dislikes") or 0) > 0:
        todo.append("complaints")
    if (m.get("retention") or {}).get("at_risk"):
        todo.append("at_risk")
    if owner and ((m.get("proposals") or {}).get("pending") or 0) > 0:
        todo.append("proposals")
    if flags_ok and (rp.get("flags_open") or 0) > 0 and (rp.get("flags_oldest_days") or 0) >= 3:
        todo.append("slice_flags")
    if m.get("features") is not None:
        rl = (m["features"].get("ratings_last") or [None])[0]
        if rl is None or rl.get("age_days") is None or rl["age_days"] >= 14:
            todo.append("ratings")
    return inc, todo


_SIG_JS = """([m, x]) => { const s = puSignals(m, x);
  const pick = a => a.map(i => ({key: i.key, lv: i.lv, title: i.title, meta: i.meta, act: i.act,
    to: i.to, href: i.href, sys: i.sys}));
  return {incidents: pick(s.incidents), todo: pick(s.todo), sys: s.sys}; }"""


def _pulse_fx(me: str = "owner", **patch) -> dict:
    """Ответ /api/admin/pulse так, как его отдаёт стенд, с поправками полей верхнего уровня."""
    d = H._pulse({"days": "14"}, me)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    return d


def test_signals_follow_rules_on_fixtures(browser):
    """puSignals на фикстурах и их вариантах совпадает с независимым расчётом; пункты
    ведут в нужные разделы и якоря; состояние систем сегодня — из тех же правил."""
    page = open_pulse(browser)
    m = _pulse_fx()
    s = page.evaluate(_SIG_JS, [m, {"days": 14, "isOwner": True, "flags": True}])
    exp_inc, exp_todo = _expected_signals(m, owner=True, flags_ok=True)
    assert sorted(i["key"] for i in s["incidents"]) == sorted(exp_inc)
    assert [i["key"] for i in s["todo"]] == exp_todo == ["inbox", "complaints", "at_risk", "proposals"]
    # сначала сломанное, потом деградирующее
    lv = [i["lv"] for i in s["incidents"]]
    assert lv == sorted(lv, key=lambda v: 0 if v == "bad" else 1)
    by = {i["key"]: i for i in s["incidents"] + s["todo"]}
    assert by["errors"]["lv"] == "warn" and by["errors"]["title"] == "2 ошибки сервера сегодня"
    assert by["errors"]["to"] == {"section": "systems", "anchor": "pu2-sys-server"}
    assert by["digest_failed"]["to"]["anchor"] == "pu2-sys-digest"
    assert by["reviews_down"]["title"].startswith("Отзовик просел: 42 отзыва")
    assert by["inbox"]["title"] == "3 обращения ждут первого ответа"
    assert by["inbox"]["meta"] == "Старейшему 2 дня." and by["inbox"]["to"]["section"] == "inbox"
    assert by["complaints"]["to"] == {"section": "quality", "anchor": "pu2-complaints"}
    assert by["at_risk"]["to"] == {"section": "people", "anchor": "pu2-risk"}
    assert by["at_risk"]["act"] == "Написать"
    assert by["proposals"]["href"] == "#sources" and by["proposals"]["to"] is None
    assert s["sys"] == {"server": "w", "digest": "b", "reviews": "b", "news": "o", "slices": "o",
                        "rsearch": "o", "web": "o", "ingest": "o"}

    variants = [
        # ошибки сервера: 1–9 — внимание, от 10 — сбой
        ({"today": {"errors": 12}}, True, True),
        # коллеге без прав владельца заявки на источники не показываются
        ({}, False, True),
        # отметки срезов старше 3 дней — только если сервер их отдаёт (не 403)
        ({"reviews_pipeline": {"flags_oldest_days": 4}}, True, True),
        ({"reviews_pipeline": {"flags_oldest_days": 4}}, True, False),
        # проверочный набор упал на 6 баллов и давно не прогонялся → «Качество ИИ»
        ({"eval": {"hermes": {"score": 70, "delta": -6, "age_days": 10, "d": "2026-10-26"}}}, True, True),
        # оценок не ставили 20 дней; блока features нет — это «нет данных», а не пункт
        ({"features": {"ratings_last": [{"kind": "ai_answer", "d": "2026-10-16", "age_days": 20}]}}, True, True),
        ({"features": None}, True, True),
        # прежний сервер без unanswered: по unread и oldest_new_days
        ({"inbox": {"new": 5, "unread": 5, "oldest_new_days": 4}}, True, True),
    ]
    for patch, owner, flags in variants:
        mm = _pulse_fx(**patch)
        if patch.get("features", 0) is None:
            mm.pop("features")
        if "inbox" in patch:
            mm["inbox"] = patch["inbox"]
        s = page.evaluate(_SIG_JS, [mm, {"days": 14, "isOwner": owner, "flags": flags}])
        exp_inc, exp_todo = _expected_signals(mm, owner=owner, flags_ok=flags)
        assert sorted(i["key"] for i in s["incidents"]) == sorted(exp_inc), patch
        assert [i["key"] for i in s["todo"]] == exp_todo, (patch, owner, flags)
        by = {i["key"]: i for i in s["incidents"] + s["todo"]}
        if "today" in patch:
            assert by["errors"]["lv"] == "bad" and by["errors"]["title"] == "12 ошибок сервера сегодня"
            assert s["sys"]["server"] == "b"
        if "eval" in patch:
            for k in ("eval_drop_hermes", "eval_age"):
                assert by[k]["lv"] == "warn" and by[k]["to"] == {"section": "quality", "anchor": "pu2-eval"}
        if patch.get("features"):
            assert by["ratings"]["title"] == "Оценок ответам ИИ не ставили 20 дней"
            assert by["ratings"]["lv"] == "warn" and by["ratings"]["to"]["section"] == "quality"
        if "inbox" in patch:
            assert by["inbox"]["title"] == "5 обращений ждут первого ответа"
            assert by["inbox"]["meta"] == "Старейшему 4 дня."
    # пустой ответ: ни пунктов, ни выдуманного «в норме» — службы «нет данных»
    s = page.evaluate(_SIG_JS, [{}, {"days": 14}])
    assert s["incidents"] == [] and s["todo"] == [] and set(s["sys"].values()) == {"n"}
    page.context.close()


# ── любая версия «Пульса» ────────────────────────────────────────────────────
def _nav(page, label: str):
    return page.locator(".rail .nav-item", has_text=re.compile(rf"^\s*(\d+\s*)?{re.escape(label)}\s*$"))


def _nav_pulse(page):
    return page.locator(".rail .nav-item", has_text=re.compile(r"^\s*Пульс\s*$"))


def test_owner_opens_pulse_from_hash(browser):
    page = open_pulse(browser)
    assert_page_ok(page)
    nav = _nav_pulse(page)
    assert nav.count() == 1 and "active" in (nav.get_attribute("class") or "")
    assert any(c.startswith("/api/admin/pulse?") and "days=" in c for c in page.calls), page.calls
    assert page.locator(f'{PULSE} [role="tablist"]').is_visible()
    assert page.locator(TABS).count() >= 6
    assert page.locator(f'{PULSE} [role="tabpanel"]').count() == 1
    page.context.close()


def test_pulse_user_opens_pulse_without_owner_requests(browser):
    page = open_pulse(browser, me="pulse_user")
    assert_page_ok(page)
    assert _nav_pulse(page).count() == 1
    # плашка «Турбулентность» — только владельцу: коллеге её состояние не запрашивается
    assert not api_calls(page, "/api/admin/banner")
    page.context.close()


def test_no_pulse_menu_without_rights(browser):
    me = {**load_fixture("me.pulse_user.json"), "can_pulse": False, "is_admin": False}
    page = open_pulse(browser, overrides={"/api/me": me}, wait=None)
    page.wait_for_function("() => document.querySelectorAll('.rail .nav-item').length > 3")
    assert _nav_pulse(page).count() == 0
    assert not page.errors, page.errors
    page.context.close()


@pytest.mark.parametrize("width", H.WIDTHS)
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_every_tab_renders_without_hscroll(browser, width, dark):
    """Каждая вкладка отрисовывается без ошибок и без горизонтальной прокрутки."""
    page = open_pulse(browser, width=width, dark=dark)
    assert page.evaluate("document.documentElement.classList.contains('dark')") is dark
    n = page.locator(TABS).count()
    for i in range(n):
        tab = page.locator(TABS).nth(i)
        tab.scroll_into_view_if_needed()
        tab.click()
        page.wait_for_function(
            "(i) => document.querySelectorAll('[data-page=\"pulse\"] [role=\"tablist\"] [role=\"tab\"]')[i]"
            ".getAttribute('aria-selected') === 'true'", arg=i)
        page.wait_for_timeout(300)                   # блоки со своей загрузкой
        label = tab.inner_text().splitlines()[0]
        assert_page_ok(page)
        try:
            assert_no_hscroll(page)
        except AssertionError as e:
            raise AssertionError(f"вкладка «{label}», {width}px: {e}") from None
        if i == 0:
            _shot(page, f"{width}-{'dark' if dark else 'light'}-overview")
    page.context.close()


# ── оболочка v2 (CONTRACT §1, §3, §4, §10) ───────────────────────────────────
V2_TABS = ["Сводка", "Люди", "Качество ИИ", "Обращения", "Системы", "Итоги"]
V2_OWNER_TABS = V2_TABS + ["Сеть ВСП"]        # «Сеть ВСП» (11.11) — только владельцу, последней


def _selected(page) -> str:
    return page.locator(f'{TABS}[aria-selected="true"]').get_attribute("id") or ""


def _wait_calls(page, prefix: str, n: int = 1, timeout: int = 5000) -> list:
    """Дождаться n запросов с префиксом (page.calls пополняется перехватом маршрута)."""
    for _ in range(timeout // 50):
        got = api_calls(page, prefix)
        if len(got) >= n:
            return got
        page.wait_for_timeout(50)
    return api_calls(page, prefix)


def _open_menu(page):
    page.locator(f"{PULSE} .pu2-band .rv-ib").click()
    pop = page.locator(f"{PULSE} .pu2-band .rv-menu-pop")
    pop.wait_for()
    return pop


def _menu_labels(pop) -> list[str]:
    return [t.splitlines()[0].strip() for t in pop.locator('[role="menuitem"]').all_inner_texts()]


def test_v2_shell_sections_and_aliases(browser):
    """Шесть разделов в порядке макета (у владельца седьмой — «Сеть ВСП»); прежние ключи
    вкладок ведут в новые разделы."""
    page = open_pulse(browser)
    labels = [t.splitlines()[0].strip() for t in page.locator(TABS).all_inner_texts()]
    assert labels == V2_OWNER_TABS
    assert [page.locator(TABS).nth(i).get_attribute("id") for i in range(6)] == \
        [f"pu2-tab-{k}" for k in H.PULSE_TABS_V2]
    assert _selected(page) == "pu2-tab-overview"
    assert page.locator(f"{PULSE} #pu2-panel").get_attribute("aria-labelledby") == "pu2-tab-overview"
    aliases = {"people": "people", "outcomes": "results", "reach": "results", "reports": "quality",
               "ai": "quality", "inbox": "inbox", "data": "systems", "tech": "systems",
               "quality": "quality", "systems": "systems", "results": "results",
               "overview": "overview", "nonsense": "overview"}
    for old, new in aliases.items():
        page = open_pulse(page, tab=old)
        assert _selected(page) == f"pu2-tab-{new}", old
        assert page.evaluate("localStorage.getItem('al-pulse-tab')") in (new, None)
        # адрес отражает раздел: ссылку можно переслать
        page.wait_for_function("(k) => location.hash === '#pulse?tab=' + k", arg=new)
    # без раздела в адресе — сохранённый выбор, в том числе прежним ключом
    page.evaluate("localStorage.setItem('al-pulse-tab', 'tech')")
    page = open_pulse(page)
    assert _selected(page) == "pu2-tab-systems"
    page.evaluate("localStorage.removeItem('al-pulse-tab')")
    page = open_pulse(page)
    assert _selected(page) == "pu2-tab-overview"
    assert_page_ok(page)
    page.context.close()


def test_v2_tabs_keyboard_and_address(browser):
    """Стрелки, Home и End переключают разделы и переносят фокус; адрес — #pulse?tab=…"""
    page = open_pulse(browser)
    page.locator("#pu2-tab-overview").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_function("() => document.activeElement && document.activeElement.id === 'pu2-tab-people'")
    assert _selected(page) == "pu2-tab-people"
    page.keyboard.press("End")                        # у владельца последняя — «Сеть ВСП»
    page.wait_for_function("() => document.activeElement.id === 'pu2-tab-vsp'")
    page.keyboard.press("ArrowRight")                 # по кругу
    page.wait_for_function("() => document.activeElement.id === 'pu2-tab-overview'")
    page.keyboard.press("ArrowLeft")
    page.wait_for_function("() => document.activeElement.id === 'pu2-tab-vsp'")
    page.keyboard.press("Home")
    page.wait_for_function("() => document.activeElement.id === 'pu2-tab-overview'")
    page.locator("#pu2-tab-systems").click()
    page.wait_for_function("() => location.hash === '#pulse?tab=systems'")
    assert page.evaluate("localStorage.getItem('al-pulse-tab')") == "systems"
    # у выбранной вкладки tabIndex 0, у остальных −1 (одна точка табуляции)
    assert page.evaluate("""() => [...document.querySelectorAll('[data-page="pulse"] [role=tab]')]
        .map(b => b.tabIndex).join(',')""") == "-1,-1,-1,-1,0,-1,-1"
    assert_page_ok(page)
    page.context.close()


@pytest.mark.parametrize("me", ["owner", "pulse_user"])
def test_v2_brief_from_fixtures(browser, me):
    """Бриф собран шаблоном по числам ответа: люди и вопросы сегодня, сбои и дела очереди."""
    page = open_pulse(browser, me=me)
    m = _pulse_fx(me)
    t = m["today"]
    inc, todo = _expected_signals(m, owner=me == "owner", flags_ok=me == "owner")
    brief = page.locator(f"{PULSE} h1.pu2-brief").inner_text()
    head = (f"{t['active']} {_plural(t['active'], 'человек', 'человека', 'человек')} сегодня, "
            f"{t['ai']} {_plural(t['ai'], 'вопрос', 'вопроса', 'вопросов')} ИИ-помощнику.")
    assert head == "23 человека сегодня, 41 вопрос ИИ-помощнику."
    assert brief.startswith(head), brief
    tail = _brief_tail(len(inc), len(todo))
    # владелец: три сбоя и четыре дела; коллеге заявки на источники не показываются
    assert tail == ("Три сбоя и четыре дела ждут вас." if me == "owner" else "Три сбоя и три дела ждут вас.")
    assert brief.endswith(tail), brief
    rt = m["retention"]
    sub = page.locator(f"{PULSE} .pu2-brief-sub").inner_text()
    d = rt["active_7d"] - rt["active_prev_7d"]
    assert f"За неделю активны {rt['active_7d']} человека — на {d} больше, чем неделей раньше." in sub
    assert "Проверочный набор ИИ — 81,5 из 100." in sub
    eb = page.locator(f"{PULSE} .pu2-eb").inner_text()
    assert "Пульс" in eb and "четверг, 5 ноября" in eb and "обновлено в 10:03" in eb
    assert "⚠" not in eb
    foot = page.locator(f"{PULSE} .pu2-foot").inner_text()
    assert "Ошибки, скорость и сбор данных — по всем учёткам" in foot
    assert "Открытие чужого отчёта пишется в журнал" in foot
    if me == "owner":
        assert "Считаются коллеги: без вас и без 2 служебных учёток" in foot
    else:
        assert "Считаются все, включая вас, кроме 3 служебных учёток" in foot
    assert_page_ok(page)
    page.context.close()


def test_v2_brief_variants(browser):
    """Пятое дело (отметки срезов старше 3 дней) — только тем, кому сервер их отдаёт; пустой
    день — «Всё спокойно»; ошибка обновления — без значков, с «Повторить»."""
    flags = {"/api/admin/pulse": lambda method, path, query, body: _pulse_fx(
        "owner", days=int(query.get("days") or 14), reviews_pipeline={"flags_oldest_days": 4})}
    page = open_pulse(browser, overrides=flags)
    page.wait_for_function("""() => document.querySelector('[data-page="pulse"] h1.pu2-brief')
        .innerText.includes('пять дел')""")
    assert page.locator(f"{PULSE} h1.pu2-brief").inner_text().endswith("Три сбоя и пять дел ждут вас.")
    assert api_calls(page, "/api/reviews/slice-flags")
    page.context.close()

    flags_403 = {**flags, "/api/reviews/slice-flags": (403, {"detail": "только команде AuditLens"})}
    page = open_pulse(browser, overrides=flags_403)
    _wait_calls(page, "/api/reviews/slice-flags")
    page.wait_for_timeout(200)
    assert page.locator(f"{PULSE} h1.pu2-brief").inner_text().endswith("Три сбоя и четыре дела ждут вас.")
    page.context.close()

    calm = _pulse_fx(today={"errors": 0}, digest=[{"section": "headline", "status": "ok", "d": "2026-11-05"}],
                     review_sources={"sources": []}, inbox={"unread": 0, "unanswered": 0},
                     ai_feedback={"dislikes": 0}, retention={"at_risk": []}, proposals={"pending": 0})
    page = open_pulse(browser, overrides={"/api/admin/pulse": calm})
    brief = page.locator(f"{PULSE} h1.pu2-brief").inner_text()
    assert brief == "23 человека сегодня, 41 вопрос ИИ-помощнику. Всё спокойно.", brief
    # бейджа «Обращений» нет, когда ждать нечего
    assert page.locator("#pu2-tab-inbox .pu2-cnt").count() == 0
    page.context.close()

    state = {"n": 0}

    def flaky(method, path, query, body):
        state["n"] += 1
        return _pulse_fx() if state["n"] == 1 else (500, {"detail": "сбой"})
    page = open_pulse(browser, overrides={"/api/admin/pulse": flaky})
    page.clock.run_for(61_000)
    page.wait_for_function("""() => document.querySelector('[data-page="pulse"] .pu2-eb')
        .innerText.includes('обновление не удалось')""")
    eb = page.locator(f"{PULSE} .pu2-eb")
    assert "показаны данные на 10:03" in eb.inner_text() and "⚠" not in eb.inner_text()
    # прежние данные остаются на экране
    assert page.locator(f"{PULSE} h1.pu2-brief").inner_text().startswith("23 человека сегодня")
    eb.get_by_role("button", name="Повторить").click()
    _wait_calls(page, "/api/admin/pulse", 3)
    assert_page_ok(page)
    page.context.close()


@pytest.mark.parametrize("width", [1208, 390])
def test_v2_inbox_badge(browser, width):
    """Бейдж «Обращений» — m.inbox.unread; виден и на телефоне, с подписью для диктора."""
    page = open_pulse(browser, width=width)
    m = load_fixture("admin_pulse.json")
    cnt = page.locator("#pu2-tab-inbox .pu2-cnt")
    assert cnt.inner_text().strip() == str(m["inbox"]["unread"]) == "3"
    assert cnt.is_visible()
    assert "ждут ответа: 3" in page.locator("#pu2-tab-inbox .vh").inner_text()
    assert page.locator(f"{TABS} .pu2-cnt").count() == 1
    page.context.close()


def test_v2_ticket_and_person_links(browser):
    """#pulse?tab=inbox&ticket=118 открывает «Обращения» и просит открыть обращение 118;
    ?ticket без раздела — тоже «Обращения»; ?person=логин открывает лист человека."""
    page = open_pulse(browser, tab="inbox", query={"ticket": 118})
    assert _selected(page) == "pu2-tab-inbox"
    panel = page.locator(f"{PULSE} #pu2-panel")
    assert panel.get_attribute("data-ticket") == "118"
    # раздел (готовый) открывает карточку обращения 118 — или заглушка без запросов
    page.wait_for_timeout(400)
    if page.locator("#pu2-ticket").count():
        assert "118" in page.locator("#pu2-ticket").inner_text()
        assert api_calls(page, "/api/admin/inbox/118")
    # адрес хранит раздел и обращение: ссылку можно переслать, обновление его не теряет
    page.wait_for_function("() => location.hash === '#pulse?tab=inbox&ticket=118'")
    page.locator("#pu2-tab-people").click()
    page.wait_for_function("() => location.hash === '#pulse?tab=people'")
    page = open_pulse(page, query="ticket=118")
    assert _selected(page) == "pu2-tab-inbox"
    assert page.locator(f"{PULSE} #pu2-panel").get_attribute("data-ticket") == "118"
    page = open_pulse(page, tab="people", query={"person": "akovaleva"})
    assert _selected(page) == "pu2-tab-people"
    dlg = page.locator('.rv-ovl-card[role="dialog"]')
    dlg.wait_for()
    assert re.search(r"akovaleva|Ковалёва", (dlg.get_attribute("aria-label") or "") + dlg.inner_text())
    page.wait_for_function("() => location.hash === '#pulse?tab=people&person=akovaleva'")
    page.keyboard.press("Escape")
    dlg.wait_for(state="detached")
    page.wait_for_function("() => location.hash === '#pulse?tab=people'")
    assert_page_ok(page)
    page.context.close()


def test_v2_period_only_in_people_and_quality_and_saved(browser):
    """Переключатель 7/14/30 — только в «Людях» и «Качестве ИИ»; выбор запрашивает период
    и сохраняется (al-pulse-days) до следующего открытия."""
    page = open_pulse(browser)
    period = page.locator(f'{PULSE} .rv-tabs-r [role="group"]', has_text="14")
    for k in H.PULSE_TABS_V2:
        page.locator(f"#pu2-tab-{k}").click()
        page.wait_for_timeout(150)
        assert period.count() == (1 if k in ("people", "quality") else 0), k
    page.locator("#pu2-tab-people").click()
    btn = period.get_by_role("button", name="30 дн")
    assert period.get_by_role("button", name="14 дн").get_attribute("aria-pressed") == "true"
    btn.click()
    assert _wait_calls(page, "/api/admin/pulse?days=30"), page.calls
    assert btn.get_attribute("aria-pressed") == "true"
    assert page.evaluate("localStorage.getItem('al-pulse-days')") == "30"
    page = open_pulse(page, tab="quality")
    assert api_calls(page, "/api/admin/pulse")[0] == "/api/admin/pulse?days=30"
    period = page.locator(f'{PULSE} .rv-tabs-r [role="group"]', has_text="30")
    assert period.get_by_role("button", name="30 дн").get_attribute("aria-pressed") == "true"
    period.get_by_role("button", name="7 дн").click()
    assert _wait_calls(page, "/api/admin/pulse?days=7")
    assert page.evaluate("localStorage.getItem('al-pulse-days')") == "7"
    page.evaluate("localStorage.removeItem('al-pulse-days')")
    assert_page_ok(page)
    page.context.close()


def test_v2_menu_owner(browser):
    """Меню «⋯» владельца: считать мои действия, плашка, Excel, служебные учётки."""
    page = open_pulse(browser)
    pop = _open_menu(page)
    assert _menu_labels(pop) == ["Считать мои действия", "Плашка «Турбулентность»",
                                 "Скачать итоги в Excel", "Служебные учётки: 2"]
    no_emoji(page, f"{PULSE} .rv-menu-pop")
    # «Считать мои действия» — запрос с me=true, выбор хранится, подвал пересказывает
    pop.get_by_role("menuitem", name=re.compile("Считать мои действия")).click()
    assert _wait_calls(page, "/api/admin/pulse?days=14&me=true")
    assert page.evaluate("localStorage.getItem('al-pulse-me')") == "1"
    page.wait_for_function("""() => document.querySelector('[data-page="pulse"] .pu2-foot')
        .innerText.includes('Считаются все, включая вас, кроме 2 служебных учёток')""")
    pop = _open_menu(page)
    assert "Считаются все, включая вас" in pop.inner_text()
    pop.get_by_role("menuitem", name=re.compile("Считать мои действия")).click()
    assert _wait_calls(page, "/api/admin/pulse?days=14&me=false")
    assert page.evaluate("localStorage.getItem('al-pulse-me')") == "0"
    # «Плашка» ведёт в «Системы»
    pop = _open_menu(page)
    pop.get_by_role("menuitem", name=re.compile("Турбулентность")).click()
    page.wait_for_function("() => location.hash === '#pulse?tab=systems'")
    assert _selected(page) == "pu2-tab-systems"
    # Excel — за период раздела «Итоги» (al-pu-oc-days) и с выбором «считать меня»
    page.evaluate("localStorage.setItem('al-pu-oc-days', '30')")
    pop = _open_menu(page)
    with page.expect_download() as dl:
        pop.get_by_role("menuitem", name=re.compile("Скачать итоги в Excel")).click()
    assert "/api/admin/pulse/outcomes.xlsx?days=30&me=false" in dl.value.url
    page.evaluate("localStorage.removeItem('al-pu-oc-days'); localStorage.removeItem('al-pulse-me')")
    assert_page_ok(page)
    page.context.close()


def test_v2_menu_pulse_user(browser):
    """У коллеги с доступом нет пункта плашки (403 на сервере) и нет призыва помечать учётки."""
    page = open_pulse(browser, me="pulse_user")
    pop = _open_menu(page)
    assert _menu_labels(pop) == ["Считать мои действия", "Скачать итоги в Excel", "Служебные учётки: 3"]
    assert "Турбулентность" not in pop.inner_text()
    assert "помечаются в карточке" not in pop.inner_text()
    page.keyboard.press("Escape")
    pop.wait_for(state="detached")
    assert not api_calls(page, "/api/admin/banner")
    assert_page_ok(page)
    page.context.close()


def _ctrl_k(page):
    page.keyboard.press("Control+KeyK")


def test_v2_ctrl_k_opens_pulse_search_not_ai_palette(browser):
    """Ctrl+K на «Пульсе» — его поиск, даже когда ИИ-помощник уже открывался и остался в
    памяти; на странице ИИ-помощника — его палитра; Esc закрывает поиск."""
    page = open_pulse(browser)
    _nav(page, "ИИ-помощник").click()
    page.wait_for_function("() => location.hash.startsWith('#ai')")
    page.wait_for_timeout(800)
    _ctrl_k(page)
    page.locator(".cp-list").wait_for()
    assert page.locator(".pu2-pal").count() == 0, "скрытый «Пульс» перехватил Ctrl+K"
    page.keyboard.press("Escape")
    page.locator(".cp-list").wait_for(state="detached")
    _nav_pulse(page).click()
    page.wait_for_function("() => location.hash.startsWith('#pulse')")
    page.wait_for_timeout(300)
    _ctrl_k(page)
    pal = page.locator(".pu2-pal")
    pal.wait_for()
    page.wait_for_timeout(300)
    assert page.locator(".cp-list").count() == 0, "открылась палитра ИИ-помощника"
    assert page.evaluate("document.activeElement.classList.contains('pu2-pal-in')")
    assert page.locator('.rv-ovl-card[role="dialog"]').get_attribute("aria-label") == "Поиск по «Пульсу»"
    no_emoji(page, ".pu2-pal")
    _shot(page, "1208-palette")
    page.keyboard.press("Escape")
    pal.wait_for(state="detached")
    # кнопка поиска в полосе разделов открывает то же окно
    page.locator(f"{PULSE} .pu2-find").click()
    pal.wait_for()
    page.keyboard.press("Escape")
    pal.wait_for(state="detached")
    assert_page_ok(page)
    page.context.close()


def test_v2_palette_finds_person_ticket_and_section(browser):
    """Поиск находит человека по фамилии (ё = е), обращение по номеру и раздел;
    Enter открывает лист человека, раздел «Обращения» с обращением, раздел."""
    page = open_pulse(browser)
    _ctrl_k(page)
    inp = page.locator(".pu2-pal-in")
    inp.wait_for()
    _wait_calls(page, "/api/admin/users?")
    inp.fill("ковалева")
    opt = page.locator(".pu2-pal-i", has_text="Анна Ковалёва")
    opt.wait_for()
    assert opt.get_attribute("aria-selected") == "true"
    page.keyboard.press("Enter")
    page.locator(".pu2-pal").wait_for(state="detached")
    dlg = page.locator('.rv-ovl-card[role="dialog"]')
    dlg.wait_for()
    assert re.search(r"akovaleva|Ковалёва", (dlg.get_attribute("aria-label") or "") + dlg.inner_text())
    _shot(page, "1208-person-sheet")
    page.keyboard.press("Escape")
    dlg.wait_for(state="detached")
    # обращение по номеру
    _ctrl_k(page)
    page.locator(".pu2-pal-in").fill("№118")
    page.locator(".pu2-pal-i", has_text="№ 118").first.wait_for()
    page.keyboard.press("Enter")
    page.locator(".pu2-pal").wait_for(state="detached")
    page.wait_for_function("""() => document.getElementById('pu2-panel')
        && document.getElementById('pu2-panel').dataset.ticket === '118'""")
    assert _selected(page) == "pu2-tab-inbox"
    # раздел; стрелки выбирают
    _ctrl_k(page)
    page.locator(".pu2-pal-in").fill("сист")
    page.locator(".pu2-pal-i", has_text="Системы").wait_for()
    page.keyboard.press("Enter")
    page.wait_for_function("() => location.hash === '#pulse?tab=systems'")
    _ctrl_k(page)
    page.locator(".pu2-pal-in").wait_for()
    first = page.locator(".pu2-pal-i").nth(0).inner_text()
    page.keyboard.press("ArrowDown")
    assert page.locator(".pu2-pal-i").nth(1).get_attribute("aria-selected") == "true", first
    page.locator(".pu2-pal-in").fill("zzzz")
    page.locator(".pu2-pal-e", has_text="Ничего не нашлось").wait_for()
    page.keyboard.press("Escape")
    assert_page_ok(page)
    page.context.close()


def test_v2_palette_phone_full_screen(browser):
    """На 390 поиск во весь экран, с кнопкой «Закрыть поиск», без прокрутки вбок."""
    page = open_pulse(browser, width=390, height=800)
    page.locator(f"{PULSE} .pu2-find").click()
    card = page.locator('.rv-ovl-card[role="dialog"]')
    card.wait_for()
    page.wait_for_timeout(300)
    box = card.bounding_box()
    assert box["width"] >= 380 and box["height"] >= 780, box
    assert_no_hscroll(page)
    _shot(page, "390-palette")
    page.get_by_role("button", name="Закрыть поиск").click()
    card.wait_for(state="detached")
    # меню «⋯» на телефоне не вылезает за край
    _open_menu(page)
    assert_no_hscroll(page)
    _shot(page, "390-menu")
    page.context.close()


def test_v2_polling_only_while_visible(browser):
    """/api/admin/pulse раз в 60 с, пока «Пульс» на экране; на другой странице — ни
    запроса и ни одной записи в адрес; по возвращении — сразу свежие данные."""
    page = open_pulse(browser)
    _wait_calls(page, "/api/admin/pulse")
    page.calls.clear()
    page.clock.run_for(61_000)
    assert len(_wait_calls(page, "/api/admin/pulse")) == 1
    _nav(page, "Аудит отзывов").click()
    page.wait_for_function("() => location.hash.startsWith('#reviews')")
    page.wait_for_timeout(300)
    page.calls.clear()
    page.clock.run_for(185_000)
    page.wait_for_timeout(300)
    assert api_calls(page, "/api/admin/pulse") == [], "скрытый «Пульс» опрашивает сервер"
    assert page.evaluate("location.hash").startswith("#reviews"), "скрытый «Пульс» переписал адрес"
    _nav_pulse(page).click()
    assert len(_wait_calls(page, "/api/admin/pulse")) == 1
    page.wait_for_function("() => location.hash === '#pulse?tab=overview'")
    assert_page_ok(page)
    page.context.close()


def test_v2_layers_close_when_pulse_hidden(browser):
    """Лист человека не остаётся висеть поверх другой страницы (цифры 1–9 меняют страницу
    и при открытом листе); возврат в «Пульс» не открывает его снова."""
    page = open_pulse(browser, query={"person": "akovaleva"})
    dlg = page.locator('.rv-ovl-card[role="dialog"]')
    dlg.wait_for()
    page.keyboard.press("1")                          # «01» — «Аудит отзывов»
    page.wait_for_function("() => location.hash.startsWith('#reviews')")
    dlg.wait_for(state="detached")
    _nav_pulse(page).click()
    page.wait_for_function("() => location.hash.startsWith('#pulse')")
    page.wait_for_timeout(400)
    assert dlg.count() == 0
    assert_page_ok(page)
    page.context.close()


def _bell(link: str, nid: int) -> dict:
    last = {"id": nid, "kind": "team_ticket", "title": "Новое обращение", "link": link,
            "actor": "akovaleva", "actor_name": "Анна Ковалёва",
            "updated_at": "2026-11-05T09:58:00+03:00", "ref": {}}
    return {"/api/bell/unread": {"unread": 1, "last": last, "inbox": {"unread": 0}}}


def test_v2_bell_links_open_pulse_and_other_pages(browser):
    """Заметка колокольчика с #pulse?tab=inbox&ticket=118 открывает «Пульс» с обращением
    с любой страницы; ссылка на другую страницу (#reviews?…) — переход по адресу."""
    page = open_pulse(browser, overrides=_bell("#pulse?tab=inbox&ticket=118", 9))
    _nav(page, "Новостные обзоры").click()
    page.wait_for_function("() => location.hash.startsWith('#overview')")
    toast = page.locator(".bx-toast")
    toast.wait_for()
    toast.get_by_role("button", name="Открыть").click()
    page.wait_for_function("() => location.hash.startsWith('#pulse')")
    page.wait_for_function("""() => document.getElementById('pu2-panel')
        && document.getElementById('pu2-panel').dataset.ticket === '118'""")
    assert _selected(page) == "pu2-tab-inbox"
    page = open_pulse(page, overrides=_bell("#reviews?tab=complaints", 10))
    toast = page.locator(".bx-toast")
    toast.wait_for()
    toast.get_by_role("button", name="Открыть").click()
    page.wait_for_function("() => location.hash.startsWith('#reviews')")
    # пункт меню подсвечивается после перерисовки, а не в момент смены адреса
    page.wait_for_function("(el) => el.classList.contains('active')",
                           arg=_nav(page, "Аудит отзывов").element_handle())
    assert not page.errors, page.errors
    page.context.close()


def test_v2_no_emoji_in_any_section(browser):
    page = open_pulse(browser)
    for k in H.PULSE_TABS_V2:
        page.locator(f"#pu2-tab-{k}").click()
        page.wait_for_timeout(300)
        no_emoji(page)
    assert_page_ok(page)
    page.context.close()


@pytest.mark.parametrize("width", [1208, 948, 390])
def test_v2_dark_theme(browser, width):
    """Тёмная тема — класс html.dark: токены --viz из тёмного блока, фон тёмный, бриф виден."""
    page = open_pulse(browser, width=width, dark=True)
    st = page.evaluate("""() => { const cs = getComputedStyle(document.documentElement);
        // яркость любого цвета CSS (rgb, oklch…) — через холст 1×1
        const cv = document.createElement('canvas'); cv.width = cv.height = 1;
        const cx = cv.getContext('2d');
        const lum = c => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = c; cx.fillRect(0, 0, 1, 1);
          const d = cx.getImageData(0, 0, 1, 1).data; return (d[0] + d[1] + d[2]) / 3; };
        return {dark: document.documentElement.classList.contains('dark'),
                viz: cs.getPropertyValue('--viz').trim(),
                bg: lum(getComputedStyle(document.body).backgroundColor),
                ink: lum(getComputedStyle(document.querySelector('[data-page="pulse"] h1.pu2-brief')).color)}; }""")
    assert st["dark"] and st["viz"].startswith("oklch(80%"), st
    assert st["bg"] < 80 < st["ink"], st
    assert page.locator(f"{PULSE} h1.pu2-brief").is_visible()
    assert_no_hscroll(page)
    _shot(page, f"{width}-dark-brief")
    assert_page_ok(page)
    page.context.close()
