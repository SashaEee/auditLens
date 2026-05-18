from __future__ import annotations
import importlib
from ..config import load_sources
from ..sources.base import SourceAdapter

def load_adapter(source_key: str) -> tuple[type[SourceAdapter], dict]:
    sources = load_sources()
    if source_key not in sources:
        raise KeyError(f"unknown source: {source_key}")
    cfg = sources[source_key]
    module_path, cls_name = cfg["adapter"].split(":")
    cls = getattr(importlib.import_module(module_path), cls_name)
    return cls, cfg
