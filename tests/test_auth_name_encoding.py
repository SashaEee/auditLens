"""Имя из заголовка входа: кириллица приходит мохибейком (latin-1), восстанавливаем.

08.10: «Алексей Глухих» хранился мохибейком — strip() до раскодирования срезал
хвостовой U+0085 (второй байт буквы «х»), и UTF-8 не собирался.
"""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.web import auth  # noqa: E402


def as_header(name: str) -> str:
    """Как Starlette отдаёт UTF-8-заголовок: байты, прочитанные как latin-1."""
    return name.encode("utf-8").decode("latin-1")


@pytest.mark.parametrize("name", ["Алексей Глухих", "Мария Черных", "Иван Седых",
                                  "Анна Петрова", "ПЁТР ЖАР", "Ivan Ivanov"])
def test_name_from_header_survives_trailing_nel(name):
    # «х» → «Ñ»+U+0085 (NEL), «Р» → «Ð»+U+00A0 (NBSP): оба str.strip() считает пробелом
    u = auth.get_current_user(x_authentik_username="user-1", x_authentik_name=as_header(" " + name + " "))
    assert u.name == name
