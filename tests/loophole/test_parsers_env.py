from pathlib import Path

from bank_audit.loophole.parsers import env


def test_venv_python_unix(monkeypatch):
    monkeypatch.setattr(env, "sys", type("sys", (), {"platform": "linux"})())
    p = env.venv_python(Path("/tmp/parsers/p1"))
    assert p == Path("/tmp/parsers/p1/venv/bin/python")


def test_venv_python_windows(monkeypatch):
    monkeypatch.setattr(env, "sys", type("sys", (), {"platform": "win32"})())
    p = env.venv_python(Path("C:/parsers/p1"))
    assert p == Path("C:/parsers/p1/venv/Scripts/python.exe")
