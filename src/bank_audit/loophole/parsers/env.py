from __future__ import annotations

import sys
from pathlib import Path


def venv_python(dir_path: Path) -> Path:
    """Возвращает путь к Python-интерпретатору внутри venv."""
    if sys.platform == "win32":
        return dir_path / "venv" / "Scripts" / "python.exe"
    return dir_path / "venv" / "bin" / "python"
