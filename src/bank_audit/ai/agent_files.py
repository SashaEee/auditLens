"""Файлы, которые ИИ-помощник отдаёт пользователю: Excel, CSV, Word, PDF…

Агент Hermes работает в своём контейнере, и файл, собранный у него в терминале,
приложению не виден — раньше агент мог только назвать путь (/root/….xlsx),
которого у пользователя нет. Теперь агент отправляет файл командой al-share
(deploy/hermes-al/bin) на POST /agent-files — с ключом AGENT_MCP_KEY и только
локально, как MCP, — и вставляет в ответ метку [[FILE:<id>]]. Обёртка стрима
привязывает файлы из меток к спросившему (claim), интерфейс показывает
карточки «Скачать»; скачать может только тот, кому файл выдан.
Непривязанные файлы живут сутки (их чистит следующая загрузка).
"""
from __future__ import annotations

import os
import re
import secrets
import unicodedata

from sqlalchemy import text

from .. import db

MAX_BYTES = int(float(os.getenv("AGENT_FILE_MAX_MB", "20")) * 1024 * 1024)
MAX_PER_ANSWER = 20
MARKER_RE = re.compile(r"\[\[FILE:([0-9a-f]{32})\]\]")
_ID_RE = re.compile(r"[0-9a-f]{32}")

TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "zip": "application/zip",
}


class FileError(ValueError):
    """Файл не принят: понятная агенту причина."""


def clean_name(name: str | None) -> str:
    """Имя для пользователя: без пути и управляющих символов, до 120 знаков
    (расширение сохраняется)."""
    nm = unicodedata.normalize("NFC", str(name or ""))
    nm = re.split(r"[\\/]", nm)[-1]
    nm = "".join(ch for ch in nm if unicodedata.category(ch)[0] != "C").strip(" .")
    if len(nm) > 120:
        stem, dot, ext = nm.rpartition(".")
        nm = (stem[:120 - len(ext) - 1].rstrip() + "." + ext) if dot and len(ext) <= 5 else nm[:120]
    return nm


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def store(name: str | None, data: bytes) -> dict:
    """Принять файл от агента. Возвращает номер и метку для ответа."""
    nm = clean_name(name)
    ext = ext_of(nm)
    if not nm or ext not in TYPES:
        raise FileError(f"тип «.{ext or '?'}» не поддерживается; можно: {', '.join(sorted(TYPES))}")
    if not data:
        raise FileError("пустой файл")
    if len(data) > MAX_BYTES:
        raise FileError(f"файл больше {MAX_BYTES // (1024 * 1024)} МБ")
    fid = secrets.token_hex(16)
    with db.session() as s:
        s.execute(text("""DELETE FROM agent_file
                           WHERE username IS NULL AND created_at < now() - interval '1 day'"""))
        s.execute(text("""INSERT INTO agent_file (file_id, name, mime, size, data)
                          VALUES (:f, :n, :m, :sz, :d)"""),
                  {"f": fid, "n": nm, "m": TYPES[ext], "sz": len(data), "d": data})
    return {"id": fid, "name": nm, "size": len(data), "mime": TYPES[ext],
            "marker": f"[[FILE:{fid}]]"}


def marker_ids(answer: str) -> list[str]:
    """Номера файлов из меток ответа — по порядку, без повторов."""
    return list(dict.fromkeys(MARKER_RE.findall(answer or "")))[:MAX_PER_ANSWER]


def claim(ids: list, username: str, session_id: int | None = None) -> list[dict]:
    """Привязать файлы из ответа к спросившему. Чужой (уже выданный другому)
    файл не перепривязывается и в ответ не попадает."""
    ids = [i for i in dict.fromkeys(str(x) for x in (ids or [])) if _ID_RE.fullmatch(i)][:MAX_PER_ANSWER]
    if not ids or not username:
        return []
    with db.session() as s:
        rows = s.execute(text("""
            UPDATE agent_file
               SET username = :u, session_id = COALESCE(session_id, :sid),
                   claimed_at = COALESCE(claimed_at, now())
             WHERE file_id = ANY(:ids)
               AND ((username IS NULL AND created_at > now() - interval '1 day') OR username = :u)
         RETURNING file_id, name, size, mime"""),
            {"u": username, "sid": session_id, "ids": ids}).mappings().all()
    by = {r["file_id"]: r for r in rows}
    return [{"id": i, "name": by[i]["name"], "size": int(by[i]["size"]), "mime": by[i]["mime"]}
            for i in ids if i in by]


def get(file_id: str, username: str, is_admin: bool = False) -> dict | None:
    """Файл для скачивания — только тому, кому выдан (и владельцу системы)."""
    if not _ID_RE.fullmatch(file_id or ""):
        return None
    with db.session() as s:
        row = s.execute(text("""SELECT name, mime, data, username FROM agent_file
                                 WHERE file_id = :f"""), {"f": file_id}).mappings().first()
    if not row or not row["username"]:
        return None
    if row["username"] != username and not is_admin:
        return None
    return dict(row)


def replace_markers(answer: str, files: list[dict]) -> str:
    """Метки в сохраняемом тексте → «имя файла» (для PDF, дела, истории);
    метка без файла — убирается."""
    names = {f["id"]: f["name"] for f in files or []}
    return MARKER_RE.sub(lambda m: f"«{names[m.group(1)]}»" if m.group(1) in names else "",
                         answer or "")
