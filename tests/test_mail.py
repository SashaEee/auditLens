"""Письма-уведомления: шаблоны, ссылки, заголовки и защита от случайной рассылки."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bank_audit.web import mail_templates as T
from bank_audit.web import mailer as M
from bank_audit.web.auth import clean_email

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)      # 12:00 по Москве


@pytest.mark.parametrize("tpl", list(T.TEMPLATES))
def test_every_template_renders_both_parts_without_external_images(tpl):
    m = T.render(tpl, name="Анна Смирнова", now=NOW)
    assert m["subject"] and m["preheader"] and m["text"].strip()
    assert "<img" not in m["html"] and " src=" not in m["html"] and 'href="http://' not in m["html"]
    assert m["html"].count("<table") >= 3 and 'lang="ru"' in m["html"]
    assert T.app_base() in m["html"] and T.app_base() in m["text"]
    assert "Настроить уведомления" in m["html"] and "#open?bell=settings" in m["html"]


def test_links_go_straight_to_the_object(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://al.example/")
    assert T.link_for({"link": "case:12"}) == "https://al.example/#open?case=12"
    assert T.link_for({"link": "case:12:talk:55"}) == "https://al.example/#open?case=12&tab=talk&msg=55"
    assert T.link_for({"link": "report:45"}) == "https://al.example/#ai?report=45"
    assert T.link_for({"link": "inbox:7"}) == "https://al.example/#open?inbox=7"
    assert T.link_for({"link": None}) == "https://al.example/#open?bell=1"


def test_user_text_is_escaped_and_mentions_highlighted():
    n = {"kind": "case_mention", "title": "Вас упомянули в деле «<b>X</b>»", "actor_name": "Анна <script>",
         "link": "case:1:talk:2", "updated_at": NOW.isoformat(),
         "ref": {"case": "<b>X</b>", "snippet": "@Павел Орлов <img src=x onerror=alert(1)> гляньте"}}
    m = T.render_event(n, NOW)
    assert "<script>" not in m["html"] and "<img src=x" not in m["html"]
    assert "&lt;img src=x" in m["html"] and "font-weight:600\">@Павел Орлов</span>" in m["html"]


def test_when_and_short_titles():
    assert T.when("2026-10-03T08:40:00+00:00", NOW) == "сегодня в 11:40"
    assert T.when("2026-10-02T15:05:00+00:00", NOW) == "вчера в 18:05"
    assert T.when("2026-09-28T06:10:00+00:00", NOW) == "28 сен в 09:10"
    c = {"case": "Карты"}
    assert T.short_title({"title": "В деле «Карты» 4 новых материала", "ref": c}) == "4 новых материала"
    assert T.short_title({"title": "Вас упомянули в деле «Карты»", "ref": c}) == "Вас упомянули"
    assert T.short_title({"title": "Статус дела «Карты»: В работе", "ref": c}) == "Статус: В работе"
    assert T.short_title({"title": "Без дела", "ref": {}}) == "Без дела"


def test_digest_groups_by_case_and_counts():
    m = T.render("digest", name="Анна Смирнова", now=NOW)
    assert m["subject"] == "Сводка AuditLens за 3 октября: 8 событий в 2 делах"
    assert "Анна, доброе утро." in m["text"]
    assert "5 сообщений · 4 материала" in m["text"] and "  — 4 новых материала" in m["text"]
    assert "Отчёты и обращения" in m["text"]


def test_batch_of_one_is_a_plain_event():
    one = T.samples(NOW)["event_report"]
    assert T.render_batch(one, NOW)["subject"] == T.render_event(one[0], NOW)["subject"]
    many = T.render("batch", now=NOW)
    assert many["subject"].endswith("и ещё 2 события")


def test_headers_suppress_autoreplies_and_thread_by_case(monkeypatch):
    monkeypatch.setenv("SMTP_FROM", "bot@agents.example.org")
    msg = M.build("me@example.org", T.render("event_mention", now=NOW), bulk=True)
    assert msg["From"] == "AuditLens <bot@agents.example.org>"
    assert msg["Auto-Submitted"] == "auto-generated" and msg["X-Auto-Response-Suppress"] == "All"
    assert msg["Precedence"] == "bulk" and msg["References"] == "<auditlens-case-12@agents.example.org>"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]


def test_nobody_but_test_address_gets_mail_until_enabled(monkeypatch):
    monkeypatch.setenv("MAIL_TEST_TO", " Me@Example.org , second@example.org")
    monkeypatch.delenv("MAIL_ENABLED", raising=False)
    assert M.allowed("me@example.org") and M.allowed("SECOND@example.org")
    assert not M.allowed("colleague@example.org")
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    with pytest.raises(M.MailError, match="только на тестовые"):
        M.send("colleague@example.org", T.render("welcome"))
    monkeypatch.setenv("MAIL_ENABLED", "1")
    assert M.allowed("colleague@example.org")


def test_send_without_settings_is_a_clear_error(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(M.MailError, match="не настроена"):
        M.send("me@example.org", T.render("welcome"))


def test_clean_email_from_authentik_header():
    assert clean_email(" Ivanov.I@Sberbank.RU ") == "ivanov.i@sberbank.ru"
    assert clean_email("not-an-email") is None and clean_email(None) is None
    assert clean_email("a@b") is None and clean_email("x y@sberbank.ru") is None


def test_gallery_lists_every_template():
    cards = [{"key": k, "label": v, "mine": False, **T.render(k, now=NOW)} for k, v in T.TEMPLATES.items()]
    page = T.gallery_page(cards, ["me@example.org"], True, "sample")
    for k in T.TEMPLATES:
        assert f'id="{k}"' in page
    assert "рассылка сотрудникам выключена" in page and "Отправить все себе" in page
