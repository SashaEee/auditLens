"""Письма-уведомления: шаблоны, ссылки, заголовки и защита от случайной рассылки."""
from __future__ import annotations

from datetime import datetime, timezone

import re

import pytest

from bank_audit.web import mail_templates as T
from bank_audit.web import mailer as M
from bank_audit.web.auth import clean_email

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)      # 12:00 по Москве


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://al.example")


@pytest.mark.parametrize("tpl", list(T.TEMPLATES))
def test_every_template_renders_both_parts_without_external_images(tpl):
    m = T.render(tpl, name="Анна Смирнова", now=NOW)
    assert m["subject"] and m["preheader"] and m["text"].strip()
    # картинка одна — логотип, и он внутри письма (cid:), а не по ссылке
    assert re.findall(r' src="([^"]*)"', m["html"]) == [f"cid:{T.LOGO_CID}"]
    assert 'href="http://' not in m["html"] and "url(" not in m["html"]
    assert m["html"].count("<table") >= 3 and 'lang="ru"' in m["html"]
    assert T.app_base() in m["html"] and T.app_base() in m["text"]
    assert "Настроить уведомления" in m["html"] and "#open?bell=settings" in m["html"]


@pytest.mark.parametrize("tpl", list(T.TEMPLATES))
def test_font_stack_survives_strict_mail_clients(tpl):
    """С -apple-system корпоративный мобильный клиент выбрасывал font-family целиком —
    письмо выходило с засечками. Ни его, ни одинарных кавычек в шрифтах быть не должно."""
    html_ = T.render(tpl, now=NOW)["html"]
    stacks = set(re.findall(r"font-family:([^;\"]+)", html_))
    assert stacks == {T.FONT}
    assert "-apple-system" not in html_ and "BlinkMacSystemFont" not in html_
    assert "'" not in T.FONT and not any(f.strip().startswith("-") for f in T.FONT.split(","))
    assert T.FONT.startswith("Segoe UI,")                    # Outlook на Windows берёт первый


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
    assert "Анна &lt;script&gt;" in m["html"]


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


def test_greeting_uses_first_name_never_the_login():
    assert T.first_name("Анна Смирнова") == "Анна"
    assert T.first_name("Смирнова Анна Павловна") == "Анна"           # как в адресной книге
    assert T.first_name("Орлов Павел Ильич") == "Павел"
    assert T.first_name("Анна-Мария Смирнова") == "Анна-Мария"
    for login in ("ivanov-2127124", "ivanov", "user_1", "", None, "a.ivanov@corp.example.ru"):
        assert T.first_name(login) == ""
    assert "Доброе утро." in T.render("digest", name="ivanov-2127124", now=NOW)["text"]
    w = T.render("welcome", name="Смирнова Анна Павловна")
    assert "Анна, здравствуйте!" in w["html"] and "Смирнова," not in w["html"]
    assert "ivanov" not in T.render("welcome", name="ivanov-2127124")["html"]


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
    plain, rel = msg.iter_parts()
    assert plain.get_content_type() == "text/plain" and rel.get_content_type() == "multipart/related"
    html_part, logo = rel.iter_parts()
    assert html_part.get_content_type() == "text/html" and logo.get_content_type() == "image/png"
    assert logo["Content-ID"] == f"<{T.LOGO_CID}>" and logo.get_content_disposition() == "inline"
    assert logo.get_content().startswith(b"\x89PNG")


def test_logo_is_retina_png_with_transparent_background():
    import io

    from PIL import Image
    png, w, h = T.logo()
    im = Image.open(io.BytesIO(png))
    assert im.mode == "RGBA" and im.size == (w * 3, h * 3) and h == 28 and 100 < w < 140
    assert im.getpixel((0, 0))[3] == 0                      # углы прозрачные
    assert len(png) < 40_000


def test_browser_preview_inlines_the_logo():
    html_ = T.render("event_mention", now=NOW)["html"]
    out = T.for_browser(html_)
    assert "cid:" not in out and 'src="data:image/png;base64,' in out
    assert T.inline_images("<p>без картинок</p>") == []


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


def test_sink_dir_keeps_mail_as_files_for_development(monkeypatch, tmp_path):
    monkeypatch.setenv("MAIL_SINK_DIR", str(tmp_path))
    monkeypatch.delenv("MAIL_PAUSED", raising=False)
    assert M.configured()
    M.send("me@example.org", T.render("verify", now=NOW), consented=True)
    files = list(tmp_path.glob("*.eml"))
    assert len(files) == 1 and b"multipart/related" in files[0].read_bytes()


def test_send_without_settings_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("MAIL_SINK_DIR", raising=False)
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(M.MailError, match="не настроена"):
        M.send("me@example.org", T.render("welcome"))


def test_clean_email_from_login_header():
    assert clean_email(" Ivanov.I@Corp.Example.RU ") == "ivanov.i@corp.example.ru"
    assert clean_email("not-an-email") is None and clean_email(None) is None
    assert clean_email("a@b") is None and clean_email("x y@corp.example.ru") is None


def test_gallery_lists_every_template():
    cards = [{"key": k, "label": v, "mine": False, **T.render(k, now=NOW)} for k, v in T.TEMPLATES.items()]
    page = T.gallery_page(cards, ["me@example.org"], True, "sample")
    for k in T.TEMPLATES:
        assert f'id="{k}"' in page
    assert "рассылка выключена" in page and "Отправить все себе" in page
    assert "cid:" not in page                                # превью видит логотип


# ── своя почта: личный адрес без подробностей, код, кому можно слать ───────────

SECRETS = ("Кредитные карты", "Ипотека: страхование", "Анна Смирнова", "Ирина Котова", "Павел Орлов",
           "посмотрите [3]", "Тариф на дату", "Ставки по вкладам", "Казани", "Розница",
           "считает жалобы")


@pytest.mark.parametrize("tpl", ["event_mention_private", "batch_private", "digest_private"])
def test_private_mail_has_no_case_names_people_or_quotes(tpl):
    m = T.render(tpl, now=NOW)
    for part in ("subject", "preheader", "html", "text"):
        for s in SECRETS:
            assert s not in m[part], (part, s)
    assert T.PRIVATE_NOTE in m["html"] and "№ 12" in m["html"]
    assert "#open?case=12" in m["html"]                       # ссылка ведёт прямо к делу


def test_every_kind_has_a_private_title():
    for kind in T.KIND:
        r = T.redact({"kind": kind, "link": "case:7", "ref": {"case": "Тайное дело", "snippet": "секрет",
                                                              "report": "Тайный отчёт", "team": "Розница"},
                      "actor_name": "Анна Смирнова", "title": "Вас упомянули в деле «Тайное дело»"})
        dump = repr(r)
        assert "Тайн" not in dump and "секрет" not in dump and "Анна" not in dump and "Розница" not in dump
        assert r["title"] and "Новое событие" not in r["title"], kind


def test_verify_mail_carries_code_and_self_confirming_link():
    m = T.render_verify("123456", "a.ivanov@example.org", "Иван Петров")
    assert m["subject"] == "Код подтверждения AuditLens: 123 456"
    assert "123 456" in m["html"] and "a.ivanov@example.org" in m["html"]
    assert "#open?bell=settings&amp;mailcode=123456" in m["html"] and "Иван, здравствуйте!" in m["html"]
    assert "Код подтверждения: 123456" in m["text"]


def test_welcome_warns_private_address():
    assert "без подробностей" in T.render_welcome("Анна Смирнова", private=True)["html"]
    assert "без подробностей" not in T.render_welcome("Анна Смирнова")["html"]


def test_who_may_receive_mail(monkeypatch):
    for k in ("MAIL_ENABLED", "MAIL_PAUSED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MAIL_TEST_TO", "owner@example.org")
    assert M.allowed("owner@example.org") and not M.allowed("x@example.org")
    assert M.allowed("x@example.org", consented=True)          # указал и подтверждает сам
    monkeypatch.setenv("MAIL_PAUSED", "1")
    assert not M.allowed("x@example.org", consented=True) and M.allowed("owner@example.org")


def test_corporate_domains_come_from_env(monkeypatch):
    from bank_audit.web import mail_delivery as MD
    monkeypatch.setenv("MAIL_CORP_DOMAINS", " corp.example.ru , @Bank.Example ")
    assert MD.is_corporate("a@corp.example.ru") and MD.is_corporate("a@mail.bank.example")
    assert not MD.is_corporate("a@example.org") and not MD.is_corporate("a@notcorp.example.ru.evil.org")
    assert not MD.is_corporate("a@xcorp.example.ru") and not MD.is_corporate(None)
    monkeypatch.delenv("MAIL_CORP_DOMAINS")
    assert not MD.is_corporate("a@corp.example.ru")             # без настройки — всё «личное»


def test_closed_contour_address_is_refused_before_any_mail(monkeypatch):
    """Omega внешних писем не принимает: код туда не дойдёт — отказ сразу, без отправки."""
    from bank_audit.web import mail_delivery as MD
    monkeypatch.setenv("MAIL_BLOCKED_DOMAINS", "closed.example.ru")
    monkeypatch.setattr(MD, "address", lambda u: None)
    monkeypatch.setattr(M, "send", lambda *a, **k: pytest.fail("письмо ушло на закрытый адрес"))
    assert MD.is_blocked("a.ivanov@closed.example.ru") and not MD.is_blocked("a@example.org")
    with pytest.raises(MD.MailUserError, match="Omega"):
        MD.start("u1", "A.Ivanov@Closed.Example.RU")
