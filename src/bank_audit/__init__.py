# Версия берётся из pyproject.toml (пакет auditlens): раньше здесь стояла
# своя «0.1.0», а в pyproject — «1.0.0», и какая из них настоящая, было не понять.
try:
    from importlib.metadata import version as _version
    __version__ = _version("auditlens")
except Exception:
    __version__ = "0.0.0"
