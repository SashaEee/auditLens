"""Хранилище сборщика ВСП: свои таблицы vsp_*, своя база (VSP_DB_URL).

SQLite — для пилота и локальной работы, PostgreSQL — на сервере; схема одна
(SQLAlchemy Core). Приложение AuditLens эти таблицы не читает и не пишет.

Главные правила:
- отзыв — одна строка на (источник, id в источнике); повторная встреча обновляет
  last_seen_at, изменение текста/оценки/ответа пишет прежнюю версию в историю;
- отзыв, пропавший из ПОЛНОЙ выдачи отделения, получает gone_at (не удаляется);
  из выдачи с потолком (Яндекс) «пропал» — только внутри достижимого окна дат;
- состояние синхронизации по каждому (источник, отделение): курсор для
  продолжения, заявленное и полученное число отзывов, статус полноты, ошибки.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import (Column, DateTime, Float, ForeignKey, Index, Integer,
                        MetaData, String, Table, Text, create_engine, func, select, update)
from sqlalchemy.engine import Engine

from .models import Review, SourceBranch, utcnow

meta = MetaData()

branches = Table(
    "vsp_branch", meta,
    Column("branch_id", Integer, primary_key=True, autoincrement=True),
    Column("region", String(200)),
    Column("city", String(200)),
    Column("name", String(400)),
    Column("address", String(600)),
    Column("address_key", String(300), index=True),   # нормализованный адрес для сопоставления
    Column("lat", Float), Column("lon", Float),
    Column("registry_source", String(40)),            # откуда отделение пришло в реестр
    Column("registry_ref", String(200)),               # id в том источнике
    Column("status", String(20), default="active"),    # active | closed | merged
    Column("created_at", DateTime(timezone=True), default=utcnow),
    Column("updated_at", DateTime(timezone=True), default=utcnow),
)

source_branches = Table(
    "vsp_source_branch", meta,
    Column("source", String(20), primary_key=True),
    Column("source_branch_id", String(120), primary_key=True),
    Column("branch_id", Integer, ForeignKey("vsp_branch.branch_id"), nullable=True, index=True),
    Column("name", String(400)), Column("address", String(600)),
    Column("city", String(200)), Column("region", String(200)),
    Column("lat", Float), Column("lon", Float),
    Column("url", String(600)),
    Column("declared_reviews", Integer), Column("declared_rating", Float),
    Column("source_status", String(20)),               # open | closed | moved | unknown
    Column("availability", String(20), default="ok"),  # ok | not_found | blocked | error
    Column("match_method", String(40)),                # geo+address | manual | registry
    Column("match_score", Float),
    Column("match_status", String(20)),                # auto | confirmed | conflict | none
    Column("last_checked_at", DateTime(timezone=True)),
    Column("raw_json", Text),
)

reviews = Table(
    "vsp_review", meta,
    Column("review_id", String(160), primary_key=True),        # "<source>:<id в источнике>"
    Column("source", String(20), nullable=False),
    Column("source_review_id", String(120), nullable=False),
    Column("source_branch_id", String(120), nullable=False),
    Column("branch_id", Integer, nullable=True),
    Column("rating", Float),
    Column("review_text", Text),
    Column("review_date", DateTime(timezone=True)),
    Column("author_ref", String(40)),
    Column("organization_reply", Text),
    Column("reply_date", DateTime(timezone=True)),
    Column("content_hash", String(40)),
    Column("first_seen_at", DateTime(timezone=True)),
    Column("last_seen_at", DateTime(timezone=True)),
    Column("gone_at", DateTime(timezone=True)),                # пропал из выдачи источника
    Column("collection_method", String(30)),
    Column("source_url", String(600)),
    Column("raw_json", Text),
    Index("vsp_review_branch", "source", "source_branch_id", "review_date"),
)

review_versions = Table(
    "vsp_review_version", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("review_id", String(160), index=True, nullable=False),
    Column("seen_at", DateTime(timezone=True), nullable=False),
    Column("rating", Float), Column("review_text", Text),
    Column("organization_reply", Text), Column("reply_date", DateTime(timezone=True)),
    Column("content_hash", String(40)),
)

sync_state = Table(
    "vsp_sync_state", meta,
    Column("source", String(20), primary_key=True),
    Column("source_branch_id", String(120), primary_key=True),
    Column("status", String(20), default="never"),      # never | running | done | failed | blocked
    Column("completeness", String(20)),                 # complete | window | partial | blocked | unknown
    Column("declared_total", Integer),                  # заявлено источником
    Column("fetched_total", Integer),                   # получено за последний полный проход
    Column("stored_total", Integer),                    # всего в базе (не пропавших)
    Column("pages", Integer),
    Column("cursor", String(400)),                      # где продолжить прерванный проход
    Column("pass_started_at", DateTime(timezone=True)), # начало текущего полного прохода
    Column("sort", String(40)),
    Column("newest_review_date", DateTime(timezone=True)),
    Column("oldest_review_date", DateTime(timezone=True)),
    Column("last_sync_at", DateTime(timezone=True)),
    Column("last_full_at", DateTime(timezone=True)),
    Column("errors", Integer, default=0),
    Column("last_error", Text),
)

sync_runs = Table(
    "vsp_sync_run", meta,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("mode", String(30)), Column("params_json", Text),
    Column("started_at", DateTime(timezone=True)), Column("finished_at", DateTime(timezone=True)),
    Column("status", String(20)), Column("stats_json", Text),
)

sync_errors = Table(
    "vsp_sync_error", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, index=True),
    Column("source", String(20)), Column("source_branch_id", String(120)),
    Column("at", DateTime(timezone=True)), Column("kind", String(40)), Column("message", Text),
)


def _j(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


class Store:
    """Доступ к таблицам сборщика. Методы короткие и транзакционные: прерывание
    между страницами не оставляет полузаписанного состояния."""

    def __init__(self, url: str):
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, future=True)
        meta.create_all(self.engine)
        self.pg = self.engine.dialect.name == "postgresql"

    # ── отделения ───────────────────────────────────────────────────────────
    def upsert_source_branch(self, b: SourceBranch, *, availability: str = "ok") -> None:
        row = {"source": b.source, "source_branch_id": b.source_branch_id, "name": b.name,
               "address": b.address, "city": b.city, "region": b.region, "lat": b.lat, "lon": b.lon,
               "url": b.url, "declared_reviews": b.declared_reviews,
               "declared_rating": b.declared_rating, "source_status": b.status,
               "availability": availability, "last_checked_at": utcnow(), "raw_json": _j(b.raw)}
        with self.engine.begin() as c:
            got = c.execute(select(source_branches.c.source).where(
                source_branches.c.source == b.source,
                source_branches.c.source_branch_id == b.source_branch_id)).first()
            if got:
                keep = {k: v for k, v in row.items() if v is not None or k in ("raw_json",)}
                c.execute(update(source_branches).where(
                    source_branches.c.source == b.source,
                    source_branches.c.source_branch_id == b.source_branch_id).values(**keep))
            else:
                c.execute(source_branches.insert().values(**row, match_status="none"))

    def set_availability(self, source: str, sid: str, availability: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(source_branches).where(
                source_branches.c.source == source, source_branches.c.source_branch_id == sid
            ).values(availability=availability, last_checked_at=utcnow()))

    def source_branch_ids(self, source: str, *, city: str | None = None,
                          region: str | None = None) -> list[str]:
        q = select(source_branches.c.source_branch_id).where(source_branches.c.source == source)
        if city:
            q = q.where(source_branches.c.city == city)
        if region:
            q = q.where(source_branches.c.region == region)
        with self.engine.connect() as c:
            return [r[0] for r in c.execute(q.order_by(source_branches.c.source_branch_id))]

    def branch_of(self, source: str, sid: str) -> int | None:
        with self.engine.connect() as c:
            r = c.execute(select(source_branches.c.branch_id).where(
                source_branches.c.source == source, source_branches.c.source_branch_id == sid)).first()
        return r[0] if r else None

    # ── отзывы ──────────────────────────────────────────────────────────────
    def save_reviews(self, items: Iterable[Review], *, seen_at: datetime | None = None) -> dict:
        """Записать страницу отзывов. Возвращает {new, updated, seen}."""
        seen_at = seen_at or utcnow()
        st = {"new": 0, "updated": 0, "seen": 0}
        items = list(items)
        if not items:
            return st
        ids = [r.review_id for r in items]
        with self.engine.begin() as c:
            old = {r.review_id: r for r in c.execute(select(
                reviews.c.review_id, reviews.c.content_hash, reviews.c.rating, reviews.c.review_text,
                reviews.c.organization_reply, reviews.c.reply_date).where(reviews.c.review_id.in_(ids)))}
            bmap: dict[tuple[str, str], int | None] = {}
            for r in items:
                st["seen"] += 1
                h = r.content_hash()
                key = (r.source, r.source_branch_id)
                if key not in bmap:
                    got = c.execute(select(source_branches.c.branch_id).where(
                        source_branches.c.source == r.source,
                        source_branches.c.source_branch_id == r.source_branch_id)).first()
                    bmap[key] = got[0] if got else None
                o = old.get(r.review_id)
                if o is None:
                    c.execute(reviews.insert().values(
                        review_id=r.review_id, source=r.source, source_review_id=r.source_review_id,
                        source_branch_id=r.source_branch_id, branch_id=bmap[key], rating=r.rating,
                        review_text=r.review_text, review_date=r.review_date, author_ref=r.author_ref,
                        organization_reply=r.organization_reply, reply_date=r.reply_date,
                        content_hash=h, first_seen_at=seen_at, last_seen_at=seen_at, gone_at=None,
                        collection_method=r.collection_method, source_url=r.source_url,
                        raw_json=_j(r.raw)))
                    st["new"] += 1
                    continue
                vals: dict[str, Any] = {"last_seen_at": seen_at, "gone_at": None}
                if o.content_hash != h:
                    c.execute(review_versions.insert().values(
                        review_id=r.review_id, seen_at=seen_at, rating=o.rating,
                        review_text=o.review_text, organization_reply=o.organization_reply,
                        reply_date=o.reply_date, content_hash=o.content_hash))
                    vals.update(rating=r.rating, review_text=r.review_text,
                                organization_reply=r.organization_reply, reply_date=r.reply_date,
                                content_hash=h, raw_json=_j(r.raw))
                    st["updated"] += 1
                c.execute(update(reviews).where(reviews.c.review_id == r.review_id).values(**vals))
        return st

    def known_ids(self, source: str, sid: str, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        if not ids:
            return set()
        with self.engine.connect() as c:
            return {r[0] for r in c.execute(select(reviews.c.review_id).where(
                reviews.c.source == source, reviews.c.source_branch_id == sid,
                reviews.c.review_id.in_(ids)))}

    def mark_gone(self, source: str, sid: str, seen_since: datetime, *,
                  window_from: datetime | None = None) -> int:
        """Отзывы отделения, не встреченные с seen_since, помечаются пропавшими.
        window_from — нижняя граница достижимого окна (для выдачи с потолком):
        старше неё отсутствие ничего не значит."""
        q = update(reviews).where(
            reviews.c.source == source, reviews.c.source_branch_id == sid,
            reviews.c.gone_at.is_(None), reviews.c.last_seen_at < seen_since)
        if window_from is not None:
            q = q.where(reviews.c.review_date >= window_from)
        with self.engine.begin() as c:
            return c.execute(q.values(gone_at=utcnow())).rowcount or 0

    def counts(self, source: str, sid: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(func.count(), func.min(reviews.c.review_date),
                                 func.max(reviews.c.review_date)).where(
                reviews.c.source == source, reviews.c.source_branch_id == sid,
                reviews.c.gone_at.is_(None))).first()
        return {"stored": int(r[0] or 0), "oldest": r[1], "newest": r[2]}

    # ── состояние синхронизации ─────────────────────────────────────────────
    def get_state(self, source: str, sid: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(select(sync_state).where(
                sync_state.c.source == source, sync_state.c.source_branch_id == sid)).mappings().first()
        return dict(r) if r else None

    def put_state(self, source: str, sid: str, **vals: Any) -> None:
        with self.engine.begin() as c:
            got = c.execute(select(sync_state.c.source).where(
                sync_state.c.source == source, sync_state.c.source_branch_id == sid)).first()
            if got:
                c.execute(update(sync_state).where(
                    sync_state.c.source == source, sync_state.c.source_branch_id == sid).values(**vals))
            else:
                c.execute(sync_state.insert().values(source=source, source_branch_id=sid, **vals))

    # ── прогоны и ошибки ────────────────────────────────────────────────────
    def start_run(self, mode: str, params: dict) -> int:
        with self.engine.begin() as c:
            r = c.execute(sync_runs.insert().values(mode=mode, params_json=_j(params),
                                                    started_at=utcnow(), status="running"))
            return int(r.inserted_primary_key[0])

    def finish_run(self, run_id: int, status: str, stats: dict) -> None:
        with self.engine.begin() as c:
            c.execute(update(sync_runs).where(sync_runs.c.run_id == run_id).values(
                finished_at=utcnow(), status=status, stats_json=_j(stats)))

    def log_error(self, run_id: int | None, source: str, sid: str | None, kind: str, msg: str) -> None:
        with self.engine.begin() as c:
            c.execute(sync_errors.insert().values(run_id=run_id, source=source, source_branch_id=sid,
                                                  at=utcnow(), kind=kind, message=msg[:2000]))


def default_store() -> Store:
    from .config import Settings
    return Store(os.getenv("VSP_DB_URL") or Settings().db_url)
