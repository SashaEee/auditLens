import hashlib, json
from typing import Any

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))

def stable_digest(d: dict[str, Any]) -> str:
    return sha256_text(json.dumps(d, sort_keys=True, ensure_ascii=False, default=str))

def author_hash(author: str | None) -> str | None:
    if not author:
        return None
    return sha256_text(author.strip().lower())[:32]
