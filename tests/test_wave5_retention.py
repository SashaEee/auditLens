"""Волна 5 «охват и удержание»."""
from __future__ import annotations

import os
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

T = date(2026, 10, 4)


def _days(*ago):
    return {T - timedelta(days=a) for a in ago}


def test_retention_segments_risk_churn_and_cohorts():
    from bank_audit.web.telemetry import _retention_calc
    daily = {
        "core": _days(*range(0, 20, 2)),            # 10 дней за 30 — ядро
        "reg": _days(1, 5, 9),                     # регулярный
        "once": _days(3),                          # разовый
        "slip": _days(10, 14, 20, 25, 33),          # регулярный, пропал 10 дн. назад
        "gone": _days(40, 45, 50),                 # был 1–2 мес. назад — ушёл
        "newbie": _days(20, 12),                   # пришёл 20 дн. назад, вернулся на 2-й неделе
    }
    first = {"core": T - timedelta(days=80), "reg": T - timedelta(days=90), "once": T - timedelta(days=3),
             "slip": T - timedelta(days=60), "gone": T - timedelta(days=50), "newbie": T - timedelta(days=20)}
    r = _retention_calc(daily, first, T, last_page={"slip": "reviews"}, ai={"slip": 4})
    assert r["segments"] == {"core": 1, "regular": 2, "once": 2, "churned": 1, "new": 2}
    assert [x["username"] for x in r["at_risk"]] == ["slip"]
    assert r["at_risk"][0]["gap_days"] == 10 and r["at_risk"][0]["page"] == "reviews"
    assert [x["username"] for x in r["churned"]] == ["gone"] and r["churned_total"] == 1
    nb = [c for c in r["cohorts"] if c["n"] and c["week"] <= (T - timedelta(days=20)).isoformat()]
    assert any(c["w2"] == 1 and c["w2_ready"] == 1 for c in nb)
    # неделя, которой ещё нет двух недель, — «рано», а не 0 %
    assert all(c["w2_ready"] == 0 for c in r["cohorts"] if c["week"] >= (T - timedelta(days=6)).isoformat())


def test_since_last_visit_first_and_blocks(monkeypatch):
    """«С прошлого визита»: первый визит — ничего; иначе блоки выпуска,
    новостей, всплесков (свои — первыми), тарифов и колокольчика."""
    from datetime import datetime, timezone
    from bank_audit.web import app as A
    prev = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(A, "scalar", lambda sql, p=None: None)
    assert A.since_last_visit("u") == {"first_visit": True}

    def scalar(sql, p=None):
        if "max(created_at)" in sql:
            return prev
        if "EXTRACT(day" in sql:
            return 14
        return 5

    def q(sql, p=None):
        if "daily_digest" in sql:
            return [{"digest_date": datetime(2026, 10, 4).date(), "headline": "Главное"}]
        if "news_item" in sql:
            return [{"title": "Новость", "url": "https://x", "value": 8}]
        if "review_subscription" in sql:
            return [{"bank": "ВТБ", "product": ""}]
        if "signal_journal" in sql:
            return [{"bank": "Альфа-Банк", "product": "", "issue": "fees", "level": "high", "ratio": 2},
                    {"bank": "Сбербанк", "product": "", "issue": "fees", "level": "high", "ratio": 2},
                    {"bank": "ВТБ", "product": "Переводы", "issue": "fees", "level": "mid", "ratio": 1.5}]
        if "change_history" in sql:
            return [{"is_sber": True, "n": 3}, {"is_sber": False, "n": 10}]
        return []
    monkeypatch.setattr(A, "scalar", scalar)
    monkeypatch.setattr(A, "q", q)
    r = A.since_last_visit("u")
    assert r["gap_days"] == 14 and r["issues"]["n"] == 1 and r["news"]["n"] == 5
    # аудиторы Сбера: всплески Сбера и подписки; чужой банк без подписки — нет
    assert r["signals"]["mine"] == 1 and r["signals"]["top"][0]["bank"] == "ВТБ"
    assert [x["bank"] for x in r["signals"]["top"]] == ["ВТБ", "Сбербанк"]
    assert r["tariffs"] == {"sber": 3, "market": 13}


def test_watch_signal_notice_title():
    from bank_audit.web import notices
    assert notices.KIND_GROUP["watch_signal"] == "watch" and "watch" in notices.GROUPS
    assert notices.title_of("watch_signal", {"bank": "Сбербанк", "product": "Переводы",
                                             "label": "Скрытые комиссии", "ratio": 2.4}) == \
        "Сбербанк · Переводы: всплеск жалоб «Скрытые комиссии» ×2,4"


def test_brief_is_the_briefing_not_spam():
    """Письмо — тот же брифинг: шапка с номером, повод дня, пульс, что проверить
    с «почему важно» (разбор — в AuditLens), новости; ссылки через ?go= и
    from=mail. На личную почту — без текстов поводов и новостей."""
    from datetime import datetime, timezone
    from bank_audit.web import mail_templates as T
    d = {"headline": "Жалобы на чарджбэк у Сбера держатся выше нормы", "n_signals": 1, "mine": 1,
         "tariffs": 0, "signals": [{"bank": "Сбербанк", "product": "Переводы и платежи", "issue": "fees",
                                    "label": "Скрытые комиссии", "mine": True}],
         "issue": {"note": "Остальное — в пределах нормы", "n_insights": 2, "risk": 1, "week": 177,
                   "baseline_week": 157.9, "esc": 20.5, "esc_market": 20.0, "sber_changes_7d": 2,
                   "insights": [{"severity": "risk", "title": "Чарджбэк: отказы",
                                 "so_what": "Системный отказ без разбора"}],
                   "news": [{"title": "Новость про мошенников", "source": "rbc.ru"}], "n_news": 6}}
    now = datetime(2026, 10, 6, 8, tzinfo=timezone.utc)
    m = T.render_brief(d, now, "Иванов Иван")
    assert m["subject"].startswith("Брифинг №280 · Жалобы на чарджбэк")
    assert "?go=overview&from=mail" in m["url"] and "#" not in m["url"]
    for part in ("Сводка за 6 октября", "Проверить сегодня", "норма 158", "20,5%",
                 "Чарджбэк: отказы", "Системный отказ без разбора", "Разобрать в AuditLens",
                 "Новость про мошенников", "Скрытые комиссии", "до 11:00"):
        assert part in m["html"], part
    p = T.render_brief(d, now, "", private=True)
    assert "Чарджбэк: отказы" not in p["html"] and "Новость про мошенников" not in p["html"]
    assert "Скрытые комиссии" not in p["html"] and "чарджбэк" not in p["subject"]


def test_brief_only_if_not_visited_and_no_digest(monkeypatch):
    from datetime import datetime, timezone
    from bank_audit.web import mail_delivery as M
    now = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)          # 12:00 МСК, понедельник
    r = {"username": "u", "email": "u@corp.example", "source": "user", "display_name": ""}
    sent = []
    monkeypatch.setattr(M, "deliver", lambda *a, **k: sent.append(a[1]) or "id")
    monkeypatch.setattr(M, "_exec", lambda *a, **k: 1)
    monkeypatch.setattr(M, "brief_data", lambda u, n: {"headline": "Г", "signals": [], "n_signals": 0,
                                                        "mine": 0, "tariffs": 0})
    state = {"visited": True, "digest": False}
    monkeypatch.setattr(M, "_visited_today", lambda u, n: state["visited"])

    def scalar(sql, p=None):
        if "kind = 'digest'" in sql:
            return 1 if state["digest"] else None
        return 7                                                    # слот дня занят нами
    monkeypatch.setattr(M, "_scalar", scalar)
    assert M._brief(r, now) == 0                                    # уже заходил сегодня
    state.update(visited=False, digest=True)
    assert M._brief(r, now) == 0                                    # получил сводку по делам
    state.update(digest=False)
    assert M._brief(r, now) == 1 and sent == ["brief"]


def test_gptr_planner_patch_accepts_new_library_kwargs(monkeypatch):
    """05.10: gpt-researcher 0.16.x начала передавать search_results в
    plan_research — точная сигнатура подмены роняла каждый отчёт."""
    import asyncio
    import sys
    import types
    from bank_audit.research.gptr import planner, runstate

    class RC:
        async def plan_research(self, query, query_domains=None, search_results=None):
            return ["library"]
    mod = types.ModuleType("gpt_researcher.skills.researcher")
    mod.ResearchConductor = RC
    for name in ("gpt_researcher", "gpt_researcher.skills"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "gpt_researcher.skills.researcher", mod)
    monkeypatch.setattr(planner, "plan_to_subqueries", lambda plan, q, attributes=None: (["a", "b"], []))
    runstate.new_run()
    planner.install({}, "вопрос")
    out = asyncio.run(RC().plan_research("q", [], search_results=[{"x": 1}], extra=True))
    assert out == ["a", "b"]


def test_crawl_falls_back_to_archived_bank_pages(monkeypatch):
    """05.10: профилей банков на проде нет — обход берёт ключевые страницы из
    уже собранных страниц официального сайта, по темам, свежие первыми."""
    import types
    from datetime import datetime
    from bank_audit.rag import crawler as C

    class S:
        def execute(self, sql, p=None):
            if "bank_profile" in str(sql):
                return types.SimpleNamespace(first=lambda: None)
            return types.SimpleNamespace(all=lambda: [
                ("https://bank.ru/vklady/a", ["deposits"], datetime(2026, 10, 1)),
                ("https://bank.ru/vklady/b", ["deposits"], datetime(2026, 10, 3)),
                ("https://bank.ru/ipoteka", ["mortgage"], datetime(2026, 9, 1))])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(C.db, "session", lambda: S())
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    read = []
    monkeypatch.setattr(C.indexer, "ingest_document_from_url", lambda url, **k: (
        read.append(url), types.SimpleNamespace(document_id=1, chunks_added=1, doc_type="html",
                                                trust_score=.9, is_new=False, skipped_reason="duplicate"))[1])
    r = C.crawl_one_bank("sberbank", max_urls=8)
    assert r["urls_attempted"] == 2 and read == ["https://bank.ru/vklady/b", "https://bank.ru/ipoteka"]
