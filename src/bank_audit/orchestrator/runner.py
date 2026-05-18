"""Runner: умеет (а) сделать ingest источника по target-ам, (б) нормализовать
   сохранённые drafts. Идемпотентно через хеш контента и дедупликацию по уникальным
   ключам в БД."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from sqlalchemy import text
from .. import db
from ..config import Settings
from ..storage.raw_store import RawStore
from ..collectors.http import HttpCollector
from ..collectors.browser import BrowserCollector
from ..normalizer import offers as offers_norm
from ..normalizer import reviews as reviews_norm
from .registry import load_adapter

def _start_run(s, source: str, target: str, openclaw_job: str | None) -> int:
    return s.execute(text("""
        INSERT INTO extraction_run(source, target_name, openclaw_job)
        VALUES (:s,:t,:j) RETURNING run_id
    """), {"s": source, "t": target, "j": openclaw_job}).scalar_one()

def _finish_run(s, run_id: int, status: str, items_seen: int, items_written: int,
                error: str | None = None):
    s.execute(text("""
        UPDATE extraction_run
           SET finished_at=now(), status=:st, items_seen=:se, items_written=:w, error=:e
         WHERE run_id=:r
    """), {"st": status, "se": items_seen, "w": items_written, "e": error, "r": run_id})

def _upsert_source_page(s, source: str, url: str, category: str | None,
                        filter_context: dict) -> int:
    return s.execute(text("""
        INSERT INTO source_page(source, url_norm, category, filter_context)
        VALUES (:s,:u,:c, CAST(:f AS jsonb))
        ON CONFLICT (source, url_norm) DO UPDATE
          SET last_seen=now(), filter_context=EXCLUDED.filter_context
        RETURNING source_page_id
    """), {"s": source, "u": url, "c": category,
           "f": json.dumps(filter_context, ensure_ascii=False, default=str)}).scalar_one()

def _store_snapshot(s, page_id: int, run_id: int, fetched_at, http_status: int,
                    sha256: str, path: str, size: int) -> int | None:
    row = s.execute(text("""
        INSERT INTO source_snapshot(source_page_id, run_id, fetched_at, http_status,
                                    content_sha256, storage_path, bytes)
        VALUES (:p,:r,:f,:hs,:sh,:pa,:b)
        ON CONFLICT (source_page_id, content_sha256) DO NOTHING
        RETURNING snapshot_id
    """), {"p": page_id, "r": run_id, "f": fetched_at, "hs": http_status,
           "sh": sha256, "pa": path, "b": size}).first()
    return row[0] if row else None

def ingest(source_key: str, target_name: str | None = None,
           openclaw_job: str | None = None) -> dict:
    settings = Settings.load()
    db.init(settings)
    cls, cfg = load_adapter(source_key)
    raw_store = RawStore(settings.raw_dir)
    http = HttpCollector(delay_ms=settings.raw["http"]["request_delay_ms"])
    browser = BrowserCollector(
        headless=settings.raw["browser"]["headless"],
        profile_dir=settings.browser_profile,
        nav_timeout_s=settings.raw["browser"]["nav_timeout_s"],
        scroll_pause_ms=settings.raw["browser"]["scroll_pause_ms"],
        max_scrolls=settings.raw["browser"]["max_scrolls"],
    )
    adapter = cls(settings, raw_store, http=http, browser=browser)
    targets = cfg["targets"]
    if target_name:
        targets = [t for t in targets if t["name"] == target_name]
        if not targets:
            raise KeyError(f"target {target_name} not found in {source_key}")

    totals = {"targets": 0, "snapshots_new": 0, "items_seen": 0, "items_written": 0}
    # Inter-target delay для browser collector — снижает шанс капчи
    # (sravni следит за частотой запросов с одного профиля)
    is_browser = cfg.get("collector") == "browser"
    inter_delay_min = float(cfg.get("inter_target_delay_min", 8.0)) if is_browser else 0.0
    inter_delay_max = float(cfg.get("inter_target_delay_max", 25.0)) if is_browser else 0.0

    import random as _r, time as _t
    for i, tgt in enumerate(targets):
        totals["targets"] += 1
        if i > 0 and inter_delay_max > 0:
            wait_s = _r.uniform(inter_delay_min, inter_delay_max)
            _t.sleep(wait_s)
        with db.session() as s:
            run_id = _start_run(s, source_key, tgt["name"], openclaw_job)
        try:
            res = adapter.fetch(tgt)
            with db.session() as s:
                page_id = _upsert_source_page(
                    s, source_key, res.snapshot.url,
                    res.snapshot.category, tgt.get("filter_context", {}),
                )
                snap_id = _store_snapshot(
                    s, page_id, run_id, res.snapshot.fetched_at,
                    res.snapshot.http_status, res.snapshot.content_sha256,
                    res.snapshot.storage_path, res.snapshot.bytes,
                )
            if snap_id is None:
                # контент не изменился -> нормализация не нужна
                with db.session() as s:
                    _finish_run(s, run_id, "ok", 0, 0)
                continue
            totals["snapshots_new"] += 1

            offers = list(adapter.parse_offers(res.html, tgt))
            reviews = list(adapter.parse_reviews(res.html, tgt))

            seen = written = 0
            if offers:
                r = offers_norm.normalize_batch(offers, snap_id, page_id)
                seen += r["seen"]; written += r["written"]
            if reviews:
                r = reviews_norm.normalize_reviews(reviews, snap_id)
                seen += r["seen"]; written += r["written"]

            totals["items_seen"] += seen
            totals["items_written"] += written
            with db.session() as s:
                _finish_run(s, run_id, "ok", seen, written)
        except Exception as e:
            with db.session() as s:
                _finish_run(s, run_id, "failed", 0, 0, error=str(e)[:500])
            # ВАЖНО: НЕ raise — иначе один битый target ломает весь цикл
            # ingest для всех остальных. Записали ошибку — идём дальше.
            continue
    http.close()
    return totals
