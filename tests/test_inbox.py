"""«Обратная связь»: проверка ввода и запись обращения (без БД)."""
from __future__ import annotations

import base64

import pytest

from bank_audit.web import inbox as I

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 20


def test_sniff_image_by_signature():
    assert I.sniff_image(PNG) == "image/png"
    assert I.sniff_image(JPG) == "image/jpeg"
    assert I.sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    # SVG — документ со скриптами, а не картинка
    assert I.sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None


def test_decode_image_accepts_data_url_and_rejects_junk():
    data, mime = I.decode_image("data:image/png;base64," + base64.b64encode(PNG).decode())
    assert data == PNG and mime == "image/png"
    with pytest.raises(I.TicketError):
        I.decode_image("не base64 !!!")
    with pytest.raises(I.TicketError):
        I.decode_image(base64.b64encode(b"<svg/>").decode())
    with pytest.raises(I.TicketError):
        I.decode_image(base64.b64encode(PNG + b"\x00" * I.MAX_FILE).decode())


def test_clean_context_is_flat_and_bounded():
    ctx = I.clean_context({"url": "#reviews?tab=complaints", "n": 3, "errors": ["a"] * 20,
                           "nested": {"x": 1}, "long": "x" * 5000})
    assert ctx["url"] == "#reviews?tab=complaints" and ctx["n"] == 3
    assert len(ctx["errors"]) == 8 and "nested" not in ctx and len(ctx["long"]) == 500
    assert I.clean_context("строка") == {}


def test_clean_body():
    with pytest.raises(I.TicketError):
        I.clean_body("  ")
    assert I.clean_body("  Ошибка в выпуске  ") == "Ошибка в выпуске"
    assert len(I.clean_body("x" * 9000)) == I.MAX_BODY


class _S:
    def __init__(self, recent=0):
        self.calls, self.recent = [], recent

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        sql = str(sql)

        class R:
            def __init__(self, v):
                self.v = v

            def scalar_one(self):
                return self.v

        if "count(*)" in sql:
            return R(self.recent)
        if "RETURNING ticket_id" in sql:
            return R(17)
        return R(None)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_create_writes_ticket_and_files(monkeypatch):
    s = _S()
    monkeypatch.setattr(I.db, "session", lambda: s)
    res = I.create("u1", "numbers", "reviews", "Аудит отзывов › Жалобы", "Числа не сходятся",
                   {"url": "#reviews"}, [{"data": base64.b64encode(PNG).decode(), "w": 10, "h": 5}])
    assert res == {"ticket_id": 17}
    ins = [c for c in s.calls if "INSERT INTO user_ticket " in c[0]][0][1]
    assert ins["k"] == "numbers" and ins["s"] == "reviews" and "#reviews" in ins["c"]
    f = [c for c in s.calls if "INSERT INTO user_ticket_file" in c[0]][0][1]
    assert f["m"] == "image/png" and f["d"] == PNG and f["w"] == 10


def test_create_unknown_kind_becomes_other_and_rate_limit(monkeypatch):
    s = _S()
    monkeypatch.setattr(I.db, "session", lambda: s)
    I.create("u1", "hack", None, None, "Просто мысль")
    ins = [c for c in s.calls if "INSERT INTO user_ticket " in c[0]][0][1]
    assert ins["k"] == "other"
    monkeypatch.setattr(I.db, "session", lambda: _S(recent=I.PER_HOUR))
    with pytest.raises(I.TicketError):
        I.create("u1", "idea", None, None, "Ещё одна мысль")
