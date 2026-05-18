"""Raw-store: атомарная запись HTML/JSON-снапшотов на ФС с manifest.json.
   Структура: workspace/raw/<source>/<target>/<YYYY>/<MM>/<DD>/<sha256>.<ext>"""
from __future__ import annotations
import json, os, tempfile
from datetime import datetime, timezone
from pathlib import Path
from ..hashing import sha256_bytes

class RawStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, source: str, target: str, content: bytes,
              ext: str = "html", meta: dict | None = None) -> tuple[str, str, int]:
        digest = sha256_bytes(content)
        now = datetime.now(timezone.utc)
        sub = self.root / source / target / f"{now:%Y/%m/%d}"
        sub.mkdir(parents=True, exist_ok=True)
        path = sub / f"{digest}.{ext}"
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(content)
            os.replace(tmp, path)
        if meta is not None:
            (sub / f"{digest}.meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        return str(path.relative_to(self.root)), digest, len(content)
