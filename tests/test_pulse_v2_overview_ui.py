"""«Пульс» v2, раздел «Сводка» в браузере (стенд tests/pulse_ui, API — фикстуры).

Что проверяется (CONTRACT §5, §9, макет docs/design/pulse/pulse-v2.html):
- «Требует внимания»: две группы «Сбои · N» и «Ждёт вашего решения · N» из ctx.sig, у
  пункта одна кнопка, кнопки ведут в разделы и якоря (заявки — ссылкой в «Источники», только
  владельцу); пустая очередь — «Всё спокойно…»;
- «Системы»: восемь служб в порядке PU_SYS, строка состояния сегодня, лента за 14 дней из
  m.health (сегодня — из сигналов, без истории — «нет данных»), переход в «Системы»;
- четыре плитки: числа, сравнение с прошлым периодом, мини-графики; ошибки, загрузка и
  «Учёт трат не подключён»; переходы в «Люди», «Качество ИИ», «Итоги»;
- «Аудитория»: столбик на день, нерабочие бледнее, сегодня — штриховка, подписи без
  наложений (и за 30 дней на телефоне), подсказки, ряд статов;
- «Сейчас в инструменте»: онлайн, аватары открывают лист человека, пять последних событий
  в формулировках прежней ленты, ошибки — красным; список онлайн обновляется с «Пульсом»;
- раскладка на 1208 / 948 / 390 px контента в светлой и тёмной теме: нет горизонтальной
  прокрутки, нет эмодзи и ошибок, соседние карточки ряда одной высоты.

Снимки: PULSE_SHOTS=<каталог> python -m pytest tests/test_pulse_v2_overview_ui.py
"""
from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.pulse_ui import harness as H
from tests.pulse_ui.harness import (
    api_calls,
    assert_no_hscroll,
    assert_page_ok,
    no_emoji,
    open_pulse,
)

PULSE = '[data-page="pulse"]'
PANEL = f"{PULSE} #pu2-panel"
QUEUE = f"{PANEL} #pu2-queue"
SYS = f"{PANEL} #pu2-ov-sys"
AUD = f"{PANEL} #pu2-ov-aud"
LIVE = f"{PANEL} #pu2-ov-live"
TILES = f"{PANEL} .pu2-ov-k"
SHOTS = os.getenv("PULSE_SHOTS")
# окно → ширина контента: меню приложения 232 px до 960 px окна
WINDOW = {1208: 1440, 948: 1180, 390: 390}
SYS_NAMES = ["Сайт и сервер", "Ежедневный выпуск", "Площадки отзывов", "Новостной поток",
             "Разметка жалоб", "Поиск по жалобам", "Веб-поиск", "Индексация и база знаний"]


@pytest.fixture(scope="module")
def browser():
    with H.browser_session() as b:
        yield b


def _shot(page, name: str) -> None:
    if SHOTS:
        H.shot(page, os.path.join(SHOTS, name + ".png"), full_page=True)


def _fx(me: str = "owner", days: int = 14, **patch) -> dict:
    """Ответ /api/admin/pulse, как его отдаёт стенд, с поправками полей верхнего уровня."""
    d = H._pulse({"days": str(days)}, me)
    for k, v in patch.items():
        if v is None:
            d.pop(k, None)
        elif isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    return d


def _open(browser, **kw):
    page = open_pulse(browser, **kw)
    page.wait_for_selector(QUEUE)
    return page


def _text(page, sel: str) -> str:
    return re.sub(r"\s+", " ", page.locator(sel).inner_text().replace(" ", " ")).strip()


def _selected(page) -> str:
    return page.locator(f'{PULSE} [role="tab"][aria-selected="true"]').get_attribute("id")


def _back(page):
    page.locator("#pu2-tab-overview").click()
    page.wait_for_selector(QUEUE)


def _wait_calls(page, prefix: str, n: int = 1, timeout: int = 5000) -> list:
    for _ in range(timeout // 50):
        got = api_calls(page, prefix)
        if len(got) >= n:
            return got
        page.wait_for_timeout(50)
    return api_calls(page, prefix)


# ── «Требует внимания» ───────────────────────────────────────────────────────
def test_queue_groups_items_and_actions(browser):
    """Владелец: «Сбои · 3» и «Ждёт вашего решения · 4»; у пункта ровно одна кнопка;
    кнопки ведут в разделы, заявки — ссылкой в «Источники»."""
    page = _open(browser)
    groups = [t.strip() for t in page.locator(f"{QUEUE} h3").all_inner_texts()]
    assert groups == ["Сбои · 3", "Ждёт вашего решения · 4"], groups
    assert "7 пунктов" in _text(page, f"{QUEUE} .pu2-ch-meta")
    items = page.locator(f"{QUEUE} li.pu2-ov-qi")
    assert items.count() == 7
    for i in range(7):
        assert items.nth(i).locator("a, button").count() == 1, items.nth(i).inner_text()
    q = _text(page, QUEUE)
    for s in ("Выпуск вышел без раздела «Тарифы и ставка»", "Отзовик просел: 42 отзыва за неделю при норме 180",
              "2 ошибки сервера сегодня", "3 обращения ждут первого ответа",
              "4 жалобы на ответы ИИ за 14 дней", "2 человека из постоянных перестали заходить",
              "2 заявки на новые источники"):
        assert s in q, s
    # сбои — красные (bad) и жёлтые (warn); красные первыми
    cls = [items.nth(i).get_attribute("class") for i in range(3)]
    assert "bad" in cls[0] and "bad" in cls[1] and "warn" in cls[2], cls
    # заявки на источники — ссылка вне «Пульса»
    link = page.locator(f'{QUEUE} a[href="#sources"]')
    assert link.count() == 1 and link.inner_text().strip() == "Рассмотреть"
    for act, tab in (("Ответить", "inbox"), ("Написать", "people"), ("Разобрать", "quality"),
                     ("Журнал", "systems")):
        page.locator(f"{QUEUE} button.pu2-ov-qb", has_text=act).first.click()
        page.wait_for_function("(t) => location.hash.startsWith('#pulse?tab=' + t)", arg=tab)
        assert _selected(page) == f"pu2-tab-{tab}", act
        _back(page)
    assert_page_ok(page)
    page.context.close()


def test_queue_for_colleague_has_no_proposals(browser):
    page = _open(browser, me="pulse_user")
    groups = [t.strip() for t in page.locator(f"{QUEUE} h3").all_inner_texts()]
    assert groups == ["Сбои · 3", "Ждёт вашего решения · 3"], groups
    assert page.locator(f'{QUEUE} a[href="#sources"]').count() == 0
    assert "заявк" not in _text(page, QUEUE)
    assert_page_ok(page)
    page.context.close()


def _calm() -> dict:
    d = _fx()
    d["today"]["errors"] = 0
    for s in d["digest"]:
        s["status"], s["error"] = "ok", None
    for s in d["review_sources"]["sources"]:
        s["status"], s["last_run_status"] = "норма", "ok"
    d["retention"]["at_risk"] = []
    d["ai_feedback"]["dislikes"] = 0
    d["inbox"].update({"unanswered": 0, "unread": 0})
    d["proposals"]["pending"] = 0
    return d


def test_queue_calm_when_nothing_waits(browser):
    page = _open(browser, overrides={"/api/admin/pulse": _calm()})
    assert page.locator(f"{QUEUE} li").count() == 0
    assert "Всё спокойно: ошибок нет, выпуск собран, площадки и фон работают" in _text(page, QUEUE)
    assert "8 из 8 в норме" in _text(page, SYS)
    assert_page_ok(page)
    page.context.close()


# ── «Системы» ─────────────────────────────────────────────────────────────────
def _cells(page, i: int) -> list[str]:
    return page.locator(f"{SYS} li").nth(i).locator(".pu2-rib i").evaluate_all(
        "els => els.map(e => e.className)")


def test_systems_rows_ribbons_and_states(browser):
    page = _open(browser)
    rows = page.locator(f"{SYS} li")
    assert rows.count() == 8
    names = [rows.nth(i).locator(".pu2-ov-sn").inner_text().strip() for i in range(8)]
    assert names == SYS_NAMES
    assert "5 из 8 в норме · последние 14 дней" in _text(page, f"{SYS} .pu2-ov-sum")
    st = [re.sub(r"\s+", " ", rows.nth(i).locator(".pu2-ov-st").inner_text()).strip() for i in range(8)]
    assert st[0] == "2 ошибки сервера сегодня"
    assert st[1] == "Выпуск вышел без раздела «Тарифы и ставка»"
    assert st[2].startswith("Отзовик просел")
    assert st[3] == "435 материалов за сутки, 4 сильных"
    assert st[4] == "12 жалоб ждут разметки дольше 6 часов"
    assert st[5].startswith("медиана 0,4 с")
    assert st[6] == "Яндекс через шлюз отвечает"
    assert st[7] == "2 воркера, очередь пуста"
    # точки: сегодня по сигналам; подпись — словом
    dots = [rows.nth(i).locator(".pu2-ov-sn .pu2-dot").get_attribute("aria-label") for i in range(8)]
    assert dots[:4] == ["есть сбои", "не работает", "не работает", "в норме"], dots
    # лента: 14 ячеек, последняя обведена и равна состоянию сегодня, прошлые — из m.health
    health = _fx()["health"]
    for i, key in enumerate(("server", "digest", "reviews", "news", "slices", "rsearch", "web", "ingest")):
        c = _cells(page, i)
        assert len(c) == 14 and c[-1].split()[-1] == "t", (key, c)
        past = ["" if x == "o" else x for x in health[key][:13]]
        assert [x.strip() for x in c[:13]] == past, key
    assert _cells(page, 0)[-1].split()[0] == "w" and _cells(page, 1)[-1].split()[0] == "b"
    tip = rows.nth(0).locator(".pu2-rib i").last.get_attribute("data-tip")
    assert tip == "чт, 5 ноября: были сбои", tip
    # строка ведёт в «Системы» с якорем службы, «Подробно» — в раздел
    rows.nth(1).locator("button").click()
    page.wait_for_function("() => location.hash.startsWith('#pulse?tab=systems')")
    _back(page)
    page.locator(f"{SYS} .pu2-lnk").click()
    page.wait_for_function("() => location.hash.startsWith('#pulse?tab=systems')")
    assert_page_ok(page)
    page.context.close()


def test_systems_without_health_history(browser):
    """Нет m.health — прошлые 13 ячеек «нет данных», сегодня — по сигналам."""
    page = _open(browser, overrides={"/api/admin/pulse": _fx(health=None)})
    for i in range(8):
        c = [x.strip() for x in _cells(page, i)]
        assert c[:13] == ["n"] * 13, c
    assert _cells(page, 1)[-1].split()[0] == "b"
    assert_page_ok(page)
    page.context.close()


# ── плитки ───────────────────────────────────────────────────────────────────
def test_kpi_tiles_numbers_and_links(browser):
    page = _open(browser)
    tiles = page.locator(TILES)
    assert tiles.count() == 4
    page.wait_for_selector(f"{TILES} .pu2-bullet")
    page.wait_for_selector(f"{TILES} .pu2-spark >> nth=2")
    t = [re.sub(r"\s+", " ", tiles.nth(i).inner_text().replace(" ", " ")) for i in range(4)]
    assert all(x in t[0] for x in ("Активны за 7 дней", "34", "из 64", "3", "к прошлой неделе")), t[0]
    assert all(x in t[1] for x in ("Вопросы ИИ-помощнику", "312", "за 14 дней", "38",
                                   "к прошлым 14 дням", "61 — отчёты")), t[1]
    assert all(x in t[2] for x in ("Проверочный набор ИИ", "81,5", "из 100", "2,5",
                                   "к прогону 26 октября")), t[2]
    assert all(x in t[3] for x in ("Траты отдела · ноябрь", "41,2", "тыс. ₽",
                                   "из них AuditLens — 14,8 тыс. ₽", "14% лимита", "прогноз 82%",
                                   "300 тыс.")), t[3]
    # сравнение: рост активных и вопросов — зелёным, падение набора — нейтрально
    assert tiles.nth(0).locator(".pu2-delta.pu2-up").count() == 1
    assert tiles.nth(2).locator(".pu2-delta").count() == 1
    assert tiles.nth(2).locator(".pu2-delta.pu2-up").count() == 0
    # мини-графики: 14 столбиков, 5 нерабочих бледнее; прогоны — 8 последних
    bars = tiles.nth(0).locator("rect")
    assert bars.count() == 14
    assert tiles.nth(0).locator("rect.on").count() == 9
    assert "Итоги последних 8 прогонов" in tiles.nth(2).locator("svg.pu2-spark").get_attribute("aria-label")
    assert "/api/admin/agent-eval?engine=hermes" in page.calls
    assert "/api/admin/pulse/costs" in page.calls
    for i, tab in enumerate(("people", "quality", "quality", "results")):
        tiles = page.locator(TILES)
        assert tiles.nth(i).evaluate("e => e.tagName") == "BUTTON"
        tiles.nth(i).click()
        page.wait_for_function("(t) => location.hash.startsWith('#pulse?tab=' + t)", arg=tab)
        _back(page)
    assert_page_ok(page)
    page.context.close()


def test_kpi_states_not_configured_and_errors(browser):
    """Учёт трат не подключён — так и сказано; ошибка трат и прогонов — «Повторить»."""
    page = _open(browser, overrides={"/api/admin/pulse/costs": {"configured": False}})
    cost = page.locator(TILES).nth(3)
    cost.locator(".pu2-kv-na").wait_for()
    assert "Учёт трат не подключён" in cost.inner_text()
    assert cost.evaluate("e => e.tagName") == "DIV"
    page.context.close()

    hits = {"costs": 0, "eval": 0}

    def costs(method, path, query, body):
        hits["costs"] += 1
        return (500, {"detail": "x"}) if hits["costs"] == 1 else H.load_fixture("admin_pulse_costs.json")

    def ev(method, path, query, body):
        hits["eval"] += 1
        return (502, {"detail": "x"}) if hits["eval"] == 1 else H.load_fixture("admin_agent_eval.hermes.json")

    page = _open(browser, overrides={"/api/admin/pulse/costs": costs, "/api/admin/agent-eval": ev})
    tiles = page.locator(TILES)
    tiles.nth(3).get_by_role("button", name="Повторить").wait_for()
    assert "Не удалось получить траты." in tiles.nth(3).inner_text()
    assert "История прогонов не загрузилась." in tiles.nth(2).inner_text()
    assert "81,5" in tiles.nth(2).inner_text(), "оценка набора — из /api/admin/pulse, не пропадает"
    tiles.nth(3).get_by_role("button", name="Повторить").click()
    page.wait_for_function("""() => [...document.querySelectorAll('#pu2-panel .pu2-ov-k')][3]
        .textContent.includes('41,2')""")
    page.locator(TILES).nth(2).get_by_role("button", name="Повторить").click()
    page.wait_for_selector(f"{TILES} >> nth=2 >> .pu2-spark")
    assert page.locator(TILES).nth(3).evaluate("e => e.tagName") == "BUTTON"
    page.errors[:] = [e for e in page.errors if "50" not in e]
    assert_page_ok(page)
    page.context.close()


def test_loading_states(browser):
    """Пока свои запросы раздела в пути — заглушки с role=status, без ошибок."""
    page = _open(browser)
    hold = lambda route: None                         # noqa: E731 — запрос висит до закрытия
    for p in ("**/api/admin/pulse/costs*", "**/api/admin/agent-eval*", "**/api/admin/users*"):
        page.route(p, hold)
    page = open_pulse(page)
    page.wait_for_selector(QUEUE)
    assert page.locator(f'{TILES}[role="status"][aria-label="Загружаем траты"]').count() == 1
    assert page.locator(f'{TILES} [aria-label="Загружаем прогоны"]').count() == 1
    assert page.locator(f'{LIVE} [aria-label="Загружаем, кто онлайн"]').count() == 1
    assert "81,5" in page.locator(TILES).nth(2).inner_text()
    assert_page_ok(page)
    page.context.close()


# ── «Аудитория» ──────────────────────────────────────────────────────────────
def test_audience_chart_and_stats(browser):
    page = _open(browser)
    bars = page.locator(f"{AUD} rect.pu2-ov-bar")
    assert bars.count() == 14
    assert page.locator(f"{AUD} rect.pu2-ov-bar.off").count() == 5
    assert page.locator(f"{AUD} rect.pu2-ov-bar.now").count() == 1
    assert "now" in bars.last.get_attribute("class")
    ax = page.locator(f"{AUD} text.pu2-ov-ax").evaluate_all("els => els.map(e => e.textContent)")
    for s in ("0", "10", "20", "30", "23 окт", "26 окт", "2 нояб", "сегодня", "пик · 31"):
        assert s in ax, (s, ax)
    tips = page.locator(f"{AUD} rect.pu2-ov-hit").evaluate_all("els => els.map(e => e.dataset.tip)")
    # подсказка дня: люди, просмотры (прежняя плитка «Просмотров сегодня»), вопросы ИИ
    assert re.fullmatch(r"чт, 5 ноября, к 10:0\d: 23 человека, 161 просмотр, 41 вопрос ИИ", tips[-1]), tips[-1]
    assert tips[12] == "ср, 4 ноября, нерабочий день: 5 человек, 35 просмотров, 6 вопросов ИИ", tips[12]
    assert tips[1] == "сб, 24 октября: 3 человека, 21 просмотр, 3 вопроса ИИ", tips[1]
    assert "в будни в среднем 27 · 4 ноября — нерабочий день" in _text(page, f"{AUD} .pu2-ch-meta")
    stats = _text(page, f"{AUD} .pu2-ov-stats")
    for s in ("1 791 просмотров разделов", "7 просмотров на человека в день", "3 новых за 14 дней",
              "5 пришли по ссылке из письма за 7 дней"):
        assert s in stats, (s, stats)
    label = page.locator(f"{AUD} svg").get_attribute("aria-label")
    assert label == "Человек в день за 14 дней: в будни 22–31, в нерабочие дни 1–5", label
    # сетка — по настоящему максимуму: при 5 людях в день — 0/2/4
    page.context.close()
    small = _fx()
    for r in small["dau"]:
        r["users"] = min(r["users"], 5)
    page = _open(browser, overrides={"/api/admin/pulse": small})
    ax = page.locator(f"{AUD} text.pu2-ov-ax").evaluate_all("els => els.map(e => e.textContent)")
    assert [x for x in ax if x.isdigit()] == ["0", "2", "4"], ax
    page.context.close()


def _overlaps(page) -> list:
    return page.evaluate("""() => {
      const t = [...document.querySelectorAll('#pu2-ov-aud text.pu2-ov-ax')]
        .map(e => e.getBoundingClientRect()).filter(r => r.width);
      const bad = [];
      for (let i = 0; i < t.length; i++) for (let j = i + 1; j < t.length; j++) {
        const a = t[i], b = t[j];
        if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1)
          bad.push([i, j]); }
      return bad; }""")


@pytest.mark.parametrize("width", [390, 1440])
def test_audience_30_days_labels_do_not_collide(browser, width):
    page = _open(browser, width=width, overrides={"/api/admin/pulse": _fx(days=30)})
    assert page.locator(f"{AUD} rect.pu2-ov-bar").count() == 30
    assert _overlaps(page) == []
    assert "новых за 30 дней" in _text(page, f"{AUD} .pu2-ov-stats")
    assert "за 30 дней" in _text(page, TILES + " >> nth=1")
    vb = page.locator(f"{AUD} svg").get_attribute("viewBox").split()
    assert (int(vb[3]) == 200) is (width == 390), vb       # телефон — своя раскладка графика
    assert_no_hscroll(page)
    assert_page_ok(page)
    page.context.close()


# ── «Сейчас в инструменте» ───────────────────────────────────────────────────
def test_live_online_avatars_and_feed(browser):
    page = _open(browser)
    page.wait_for_selector(f"{LIVE} .pu2-ov-av")
    assert "4 онлайн" in _text(page, f"{LIVE} .pu2-ch-meta")
    assert page.locator(f"{LIVE} .pu2-ch-meta .pu2-dot.ok.pulse").count() == 1
    av = page.locator(f"{LIVE} button.pu2-ov-av")
    names = av.evaluate_all("els => els.map(e => e.dataset.tip)")
    assert names == ["Дмитрий Орлов", "Анна Ковалёва", "Светлана Кузнецова", "Кирилл Громов"], names
    assert any(c.startswith("/api/admin/users?days=30") for c in page.calls)
    rows = page.locator(f"{LIVE} li.pu2-ov-fe")
    assert rows.count() == 5
    txt = [re.sub(r"\s+", " ", rows.nth(i).inner_text()).strip() for i in range(5)]
    assert txt == ["10:02 Анна Ковалёва заказ отчёта", "09:58 Дмитрий Орлов → Аудит отзывов",
                   "09:54 Мария Соколова отправка отчёта коллеге",
                   "09:41 Игорь Лебедев ошибка сервера · Аудит отзывов · 504",
                   "09:33 Елена Смирнова вопрос ИИ-помощнику"], txt
    assert "bad" in rows.nth(3).get_attribute("class")
    assert rows.nth(3).locator(".pu2-ov-what").get_attribute("data-tip") == "Запрос /api/reviews/search"
    assert page.locator(f"{LIVE} li.pu2-ov-fe.bad").count() == 1
    # аватар открывает лист человека
    av.nth(1).click()
    page.locator('.rv-ovl-card[role="dialog"]').wait_for()
    page.wait_for_function("() => location.hash.includes('person=akovaleva')")
    page.keyboard.press("Escape")
    page.locator('.rv-ovl-card[role="dialog"]').wait_for(state="detached")
    # имя в ленте — тоже
    rows.nth(2).locator("button.pu2-ov-who").click()
    page.wait_for_function("() => location.hash.includes('person=msokolova')")
    assert_page_ok(page)
    page.context.close()


def test_live_states_and_refresh(browser):
    """Ошибка списка онлайн — «Повторить»; никого нет — так и сказано; пустая лента —
    «Пока тихо»; список онлайн обновляется вместе с «Пульсом», траты — нет."""
    page = _open(browser, overrides={"/api/admin/users": (500, {"detail": "x"})})
    page.wait_for_selector(f"{LIVE} .pu2-err")
    assert "Не удалось узнать, кто онлайн." in _text(page, LIVE)
    assert page.locator(f"{LIVE} .pu2-err button", has_text="Повторить").count() == 1
    page.context.close()

    quiet = _fx(feed=[])
    quiet["today"]["online"] = 0
    users = H.load_fixture("admin_users.json")
    for u in users["users"]:
        u["online"] = False
    page = _open(browser, overrides={"/api/admin/pulse": quiet, "/api/admin/users": users})
    page.wait_for_selector(f"{LIVE} .pu2-ov-none")
    t = _text(page, LIVE)
    assert "Сейчас никого нет" in t and "Пока тихо" in t and "0 онлайн" in t
    page.context.close()

    page = _open(browser)
    page.wait_for_selector(f"{LIVE} .pu2-ov-av")
    page.calls.clear()
    page.clock.run_for(61_000)
    assert len(_wait_calls(page, "/api/admin/users")) == 1
    assert api_calls(page, "/api/admin/pulse/costs") == []
    assert api_calls(page, "/api/admin/agent-eval") == []
    assert_page_ok(page)
    page.context.close()


# ── раскладка ────────────────────────────────────────────────────────────────
_ROWS_JS = """() => { const rows = {};
  for (const e of document.querySelectorAll('#pu2-panel .pu2-grid > *')) {
    const r = e.getBoundingClientRect(); (rows[Math.round(r.top)] ||= []).push(Math.round(r.height)); }
  return Object.values(rows); }"""


@pytest.mark.parametrize("width", [1208, 948, 390])
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_layout_widths_and_themes(browser, width, dark):
    page = _open(browser, width=WINDOW[width], dark=dark)
    page.wait_for_selector(f"{LIVE} .pu2-ov-av")
    page.wait_for_selector(f"{TILES} .pu2-bullet")
    page.wait_for_timeout(200)
    assert page.evaluate("document.documentElement.classList.contains('dark')") is dark
    assert_no_hscroll(page)
    no_emoji(page)
    assert_page_ok(page)
    rows = page.evaluate(_ROWS_JS)
    # в ряду — карточки одной высоты (сетка тянет, содержимое заполняет без пустот)
    for r in rows:
        assert max(r) - min(r) <= 1, rows
    if width == 1208:
        assert [len(r) for r in rows] == [2, 4, 2], rows
    elif width == 948:
        assert [len(r) for r in rows] == [1, 1, 2, 2, 1, 1], rows
    else:
        assert all(len(r) == 1 for r in rows), rows
    # «Сейчас в инструменте» рядом с графиком не растягивает ряд и не прячет первое событие
    assert page.locator(f"{LIVE} li.pu2-ov-fe:visible").count() >= 1
    _shot(page, f"overview-{width}-{'dark' if dark else 'light'}")
    page.context.close()


def test_long_feed_does_not_stretch_row(browser):
    """Длинные события рядом с «Аудиторией» прячутся целиком, ряд держит высоту графика."""
    d = _fx()
    long = {"ts": "10:01", "username": "akovaleva", "name": "Анна Константиновна Ковалёва-Преображенская",
            "kind": "client_error", "page": "loophole", "status": None, "deep": None}
    d["feed"] = [long] * 5
    page = _open(browser, width=1440, overrides={"/api/admin/pulse": d})
    page.wait_for_timeout(200)
    aud = page.locator(AUD).bounding_box()
    live = page.locator(LIVE).bounding_box()
    assert abs(aud["height"] - live["height"]) <= 1
    vis = page.locator(f"{LIVE} li.pu2-ov-fe:visible")
    assert 1 <= vis.count() < 5
    last = vis.last.bounding_box()
    assert last["y"] + last["height"] <= live["y"] + live["height"], "событие обрезано по середине"
    page.set_viewport_size({"width": 1180, "height": 900})   # во всю ширину — видны все пять
    page.wait_for_timeout(200)
    assert page.locator(f"{LIVE} li.pu2-ov-fe:visible").count() == 5
    assert_page_ok(page)
    page.context.close()
