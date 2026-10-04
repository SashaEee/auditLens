"""Волна 1 аудита 03.10: «Для вас» без повторов, полный текст новостей,
ручной сбор только владельцу, честные подписи находок «Аудита уязвимостей»
и заглушки сорванного отчёта. БД не нужна."""
from __future__ import annotations

import os
import types

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


def test_for_you_skips_news_that_the_issue_marked_as_repeats():
    """ОБЗ-05: первая плитка «Для вас» 03.10 была повтором, который выпуск отсеял."""
    from bank_audit.digest import personal as P
    pool = [{"url": "https://ria.ru/old", "title": "С 1 октября пополнение через чужие банкоматы по СБП",
             "source": "ria", "tri": 7, "snippet": "Сбербанк"},
            {"url": "https://rbc.ru/new", "title": "Сбербанк изменил условия кредитных карт",
             "source": "rbc", "tri": 7, "snippet": "Сбербанк"}]
    sections = {"news": {"payload": {"pool": pool, "groups": [],
                                     "repeats": [{"url": "https://ria.ru/old", "title": "…"}]}}}
    urls = [t.get("url") for t in P._news_tiles(sections, {}, [])]
    assert "https://ria.ru/old" not in urls and "https://rbc.ru/new" in urls


def test_news_bodies_read_full_length_and_only_tried_rows_are_marked(monkeypatch):
    """ОБЗ-03: читали 2000 знаков вместо 6000, а 28 из 40 помечали «полный текст» без скачивания."""
    from bank_audit.digest import newsflow as NF
    from bank_audit.digest import writer as W
    rows = [(i, f"https://site.ru/{i}") for i in range(40)]
    seen = {}

    class S:
        def __init__(self):
            self.updates = []

        def execute(self, sql, p=None):
            if "SELECT" in str(sql):
                return types.SimpleNamespace(all=lambda: rows)
            self.updates.append(p)
            return types.SimpleNamespace(rowcount=len(p))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    sess = S()
    monkeypatch.setattr(NF.db, "session", lambda: sess)

    def fake_bodies(urls, max_chars=None, max_n=None):
        seen.update(max_chars=max_chars, max_n=max_n)
        return {u: "т" * 7000 for u in urls[:30]}          # 10 ссылок не пробовали
    monkeypatch.setattr(W, "_news_bodies", fake_bodies)
    assert NF.fetch_bodies(40) == 30
    assert seen == {"max_chars": NF.BODY_CHARS, "max_n": 40}
    marked = sess.updates[0]
    assert len(marked) == 30 and all(len(p["b"]) == NF.BODY_CHARS for p in marked)


def _user(name):
    from bank_audit.web.auth import CurrentUser
    return CurrentUser(username=name, name=name, authenticated=True)


def test_manual_ingest_is_owner_only_and_never_runs_disabled_sources(monkeypatch):
    """ДАН-06: любой аудитор мог запустить выключенный сборщик, который пишет чужие отзывы."""
    from fastapi import BackgroundTasks, HTTPException

    from bank_audit import config
    from bank_audit.web import app as A
    monkeypatch.setattr(config, "load_sources", lambda: {
        "banki_reviews": {"enabled": False}, "sravni_api": {"enabled": True}})
    monkeypatch.setattr(A.telemetry, "is_admin", lambda u: u == "owner")
    req = lambda s: A.IngestRequest(source=s)  # noqa: E731
    with pytest.raises(HTTPException) as e:
        A.ingest_run(req("sravni_api"), BackgroundTasks(), user=_user("auditor"))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        A.ingest_run_all(BackgroundTasks(), user=_user("auditor"))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        A.ingest_run(req("banki_reviews"), BackgroundTasks(), user=_user("owner"))
    assert e.value.status_code == 409
    bt = BackgroundTasks()
    assert A.ingest_run(req("sravni_api"), bt, user=_user("owner"))["status"] == "started"
    assert len(bt.tasks) == 1


def test_loophole_findings_are_labelled_as_unverified(monkeypatch):
    """УЯЗ-05: каждая запись шла в отчёт как «Схема: …», хотя проверено 0 из 291."""
    from bank_audit.research.gptr import own_data as OD
    recs = [{"record_id": 7, "about_bank": True, "title": "Пост", "headline": "Кэшбэк за переводы себе",
             "summary": "Переводы между своими счетами дают кэшбэк", "type": "уязвимость",
             "model_doubt": None, "expert_checked": False, "why_loophole": "условия не запрещают",
             "source": "vk.com", "url": None},
            {"record_id": 8, "about_bank": True, "title": "Схема", "headline": "Обнал через СБП",
             "type": "мошенническая схема", "expert_checked": True, "model_doubt": "реклама?",
             "source": "t.me", "url": "https://t.me/x/1"}]
    monkeypatch.setattr(OD, "_j", lambda tool, **kw: {"records": recs, "stats": {}})
    od = OD.OwnData()
    plan = types.SimpleNamespace(subjects=["sberbank"], subject_labels={"sberbank": "Сбербанк"},
                                 anchor="sberbank", product="карты")
    OD.collect_loopholes(od, plan, "кэшбэк")
    p7 = od.pages["#loophole?record=7"]
    assert "Возможная уязвимость (оценка модели, экспертом не проверена): Кэшбэк за переводы себе" in p7
    assert "Схема:" not in p7 and "Суть: Переводы между своими счетами" in p7
    p8 = od.pages["https://t.me/x/1"]
    assert "Мошенническая схема (проверено экспертом ЦК КС)" in p8 and "Модель сомневается: реклама?" in p8
    assert "не зафиксированные схемы" in od.pages["#loophole"]


def test_failed_run_stubs_are_recognised():
    """ИИ-09: заглушки сбоя сохранялись отчётами с текстом исключения."""
    from bank_audit.research.gptr import stream as ST
    for stub in (ST.FAIL_PLAN, ST.FAIL_COLLECT, ST.FAIL_REPORT, "⚠ **Не удалось построить план:** module"):
        assert f"{stub} Модель не ответила".startswith(ST.FAIL_PREFIXES)
    assert not "## Вывод\nСбер отстаёт".startswith(ST.FAIL_PREFIXES)
