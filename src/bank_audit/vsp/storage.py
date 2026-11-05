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
                        MetaData, String, Table, Text, and_, create_engine, func, or_, select, text, update)
from sqlalchemy.engine import Engine

from .models import Review, SourceBranch, utcnow

meta = MetaData()

branches = Table(
    "vsp_branch", meta,
    Column("branch_id", Integer, primary_key=True, autoincrement=True),
    Column("region", Text),
    Column("city", Text),
    Column("name", Text),
    Column("address", Text),             # у передвижных пунктов ЦБ — до ~4 300 символов
    Column("address_key", String(300), index=True),   # нормализованный адрес для сопоставления
    Column("lat", Float), Column("lon", Float),
    Column("registry_source", String(40)),            # откуда отделение пришло в реестр
    Column("registry_ref", String(200)),               # id в том источнике
    Column("registry_num", String(40), index=True),    # номер подразделения по ЦБ (FILIAL_NUM)
    Column("status", String(20), default="active"),    # active | mobile | remote | cash_center | closed
    Column("created_at", DateTime(timezone=True), default=utcnow),
    Column("updated_at", DateTime(timezone=True), default=utcnow),
)

source_branches = Table(
    "vsp_source_branch", meta,
    Column("source", String(20), primary_key=True),
    Column("source_branch_id", String(120), primary_key=True),
    Column("branch_id", Integer, ForeignKey("vsp_branch.branch_id"), nullable=True, index=True),
    Column("name", Text), Column("address", Text),
    Column("city", Text), Column("region", Text),
    Column("lat", Float), Column("lon", Float),
    Column("url", Text),
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
    Column("source_url", Text),
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
    Column("cursor", Text),                      # где продолжить прерванный проход
    Column("pass_started_at", DateTime(timezone=True)), # начало текущего полного прохода
    Column("sort", String(40)),
    Column("newest_review_date", DateTime(timezone=True)),
    Column("oldest_review_date", DateTime(timezone=True)),
    Column("last_sync_at", DateTime(timezone=True)),
    Column("last_full_at", DateTime(timezone=True)),
    Column("errors", Integer, default=0),
    Column("last_error", Text),
    # расписание: когда проверить «только новые» (по частоте отзывов отделения) и когда
    # полный проход (правки, ответы, пропавшие) — см. sync.schedule_next
    Column("next_due_at", DateTime(timezone=True)),
    Column("next_full_at", DateTime(timezone=True)),
)

kv = Table(
    "vsp_kv", meta,                                     # служебное: прогресс обходов, паузы, частоты
    Column("key", String(200), primary_key=True),
    Column("value", Text),
    Column("updated_at", DateTime(timezone=True)),
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
    return _nul(json.dumps(x, ensure_ascii=False, default=str))


def _nul(s: Any) -> Any:
    """PostgreSQL не хранит символ U+0000 в тексте (SQLite хранит) — вычищаем."""
    if isinstance(s, str) and "\x00" in s:
        return s.replace("\x00", "")
    return s


class Store:
    """Доступ к таблицам сборщика. Методы короткие и транзакционные: прерывание
    между страницами не оставляет полузаписанного состояния."""

    def __init__(self, url: str):
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, future=True, pool_pre_ping=True)
        self.pg = self.engine.dialect.name == "postgresql"
        # своя схема на PostgreSQL (VSP_DB_SCHEMA): в управляемом облачном PostgreSQL
        # создавать объекты в public нельзя даже владельцу базы. Схема — в search_path
        # через параметры подключения (…?options=-csearch_path%3D<схема>), здесь — создание
        schema = os.getenv("VSP_DB_SCHEMA")
        if self.pg and schema:
            if not schema.isidentifier():
                raise ValueError(f"VSP_DB_SCHEMA: недопустимое имя схемы {schema!r}")
            with self.engine.begin() as c:
                c.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        meta.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Своя база модуля: новые столбцы добавляются при запуске (только ADD COLUMN,
        без перестройки таблиц). На PostgreSQL — с lock_timeout, чтобы не ждать чужую блокировку."""
        from sqlalchemy import inspect, text
        insp = inspect(self.engine)
        for t in meta.sorted_tables:
            have = {c["name"] for c in insp.get_columns(t.name)}
            for col in t.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE {t.name} ADD COLUMN {col.name} {col.type.compile(self.engine.dialect)}'
                with self.engine.begin() as c:
                    if self.pg:
                        c.execute(text("SET LOCAL lock_timeout = '3s'"))
                    c.execute(text(ddl))

    # ── отделения ───────────────────────────────────────────────────────────
    def upsert_source_branch(self, b: SourceBranch, *, availability: str = "ok") -> None:
        row = {"source": b.source, "source_branch_id": b.source_branch_id, "name": _nul(b.name),
               "address": _nul(b.address), "city": _nul(b.city), "region": _nul(b.region), "lat": b.lat,
               "lon": b.lon, "url": b.url, "declared_reviews": b.declared_reviews,
               "declared_rating": b.declared_rating, "source_status": b.status,
               "availability": availability, "last_checked_at": utcnow(), "raw_json": _j(b.raw)}
        with self.engine.begin() as c:
            got = c.execute(select(source_branches.c.source, source_branches.c.raw_json).where(
                source_branches.c.source == b.source,
                source_branches.c.source_branch_id == b.source_branch_id)).first()
            if got:
                # служебные поля сливаются: карточка из branch() не затирает данные поиска
                # (номер ЦБ у Яндекса, организация и рубрики у 2ГИС)
                try:
                    old_raw = json.loads(got[1] or "{}")
                except ValueError:
                    old_raw = {}
                # явный None в новом raw — «поля больше нет» (например, Яндекс убрал номер ЦБ):
                # ключ удаляется; отсутствующий ключ — «не знаю», прежнее значение остаётся
                merged = {**old_raw, **(b.raw or {})}
                row["raw_json"] = _j({k: v for k, v in merged.items() if v is not None})
                keep = {k: v for k, v in row.items() if v is not None}
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
        # один и тот же отзыв дважды на странице (замечено у Яндекса на проде) — оставляем последний
        items = list({r.review_id: r for r in items}.values())
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
                    c.execute(self._insert_once(reviews).values(
                        review_id=r.review_id, source=r.source, source_review_id=r.source_review_id,
                        source_branch_id=r.source_branch_id, branch_id=bmap[key], rating=r.rating,
                        review_text=_nul(r.review_text), review_date=r.review_date, author_ref=r.author_ref,
                        organization_reply=_nul(r.organization_reply), reply_date=r.reply_date,
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
                    vals.update(rating=r.rating, review_text=_nul(r.review_text),
                                organization_reply=_nul(r.organization_reply), reply_date=r.reply_date,
                                content_hash=h, raw_json=_j(r.raw))
                    st["updated"] += 1
                c.execute(update(reviews).where(reviews.c.review_id == r.review_id).values(**vals))
        return st

    def _insert_once(self, table):
        """INSERT, который молча пропускает уже существующий ключ (два прохода одновременно
        встретили один отзыв): PostgreSQL — ON CONFLICT DO NOTHING, SQLite — OR IGNORE."""
        if self.pg:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            return pg_insert(table).on_conflict_do_nothing()
        return table.insert().prefix_with("OR IGNORE")

    def known_ids(self, source: str, sid: str, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        if not ids:
            return set()
        with self.engine.connect() as c:
            return {r[0] for r in c.execute(select(reviews.c.review_id).where(
                reviews.c.source == source, reviews.c.source_branch_id == sid,
                reviews.c.review_id.in_(ids)))}

    def gone_candidates(self, source: str, sid: str, seen_since: datetime) -> int:
        """Сколько хранимых отзывов отделения не встречено с seen_since (кандидаты в пропавшие)."""
        with self.engine.connect() as c:
            return int(c.execute(select(func.count()).where(
                reviews.c.source == source, reviews.c.source_branch_id == sid,
                reviews.c.gone_at.is_(None), reviews.c.last_seen_at < seen_since)).scalar() or 0)

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

    def count_seen(self, source: str, sid: str, since: datetime) -> int:
        """Сколько РАЗНЫХ отзывов отделения встречено с момента since (за проход).
        Выдача по релевантности бывает с повторами между страницами — счёт строк страниц
        завысил бы полноту."""
        with self.engine.connect() as c:
            return int(c.execute(select(func.count()).where(
                reviews.c.source == source, reviews.c.source_branch_id == sid,
                reviews.c.last_seen_at >= since)).scalar() or 0)

    def recent_count(self, source: str, sid: str, since: datetime) -> int:
        """Отзывов отделения с датой не раньше since — для оценки частоты новых."""
        with self.engine.connect() as c:
            return int(c.execute(select(func.count()).where(
                reviews.c.source == source, reviews.c.source_branch_id == sid,
                reviews.c.review_date >= since)).scalar() or 0)

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

    def due(self, source: str, now: datetime, *, limit: int = 200) -> dict[str, list[str]]:
        """Кому пора: {"full": [...], "new": [...]}.
        full — ни разу не пройдено целиком, прерванный проход, или подошёл next_full_at;
        new  — успешно синхронизировано и подошёл next_due_at.
        Карточки, которых нет в источнике (not_found) и закрытые, — только по next_full_at."""
        sb, ss = source_branches, sync_state
        j = sb.outerjoin(ss, and_(ss.c.source == sb.c.source, ss.c.source_branch_id == sb.c.source_branch_id))
        alive = and_(or_(sb.c.availability.is_(None), sb.c.availability != "not_found"),
                     or_(sb.c.source_status.is_(None), sb.c.source_status != "closed"))
        st = func.coalesce(ss.c.status, "never")
        due_at = or_(ss.c.next_due_at.is_(None), ss.c.next_due_at <= now)
        full_q = (select(sb.c.source_branch_id).select_from(j).where(
            sb.c.source == source,
            or_(ss.c.source.is_(None),                     # ни разу не трогали (в т. ч. закрытые — история нужна)
                and_(alive, st == "running"),              # прерванный проход — продолжить
                # полного прохода ещё не было: после любой попытки (сбой, предохранитель) —
                # не раньше срока next_due_at, иначе вечный круг на битой карточке
                and_(alive, ss.c.last_full_at.is_(None), st != "running", due_at),
                ss.c.next_full_at <= now))
            # сначала прерванные, затем никогда не пройденные, затем по сроку
            .order_by(st != "running", ss.c.next_full_at.isnot(None), ss.c.next_full_at,
                      sb.c.source_branch_id).limit(limit))
        new_q = (select(ss.c.source_branch_id).select_from(j).where(
            sb.c.source == source, alive, ss.c.status.in_(("done", "failed", "blocked")),
            ss.c.last_full_at.isnot(None), due_at,
            or_(ss.c.next_full_at.is_(None), ss.c.next_full_at > now))
            .order_by(ss.c.next_due_at).limit(limit))
        with self.engine.connect() as c:
            return {"full": [r[0] for r in c.execute(full_q)], "new": [r[0] for r in c.execute(new_q)]}

    # ── служебное key-value ─────────────────────────────────────────────────
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self.engine.connect() as c:
            r = c.execute(select(kv.c.value).where(kv.c.key == key)).first()
        if not r or r[0] is None:
            return default
        try:
            return json.loads(r[0])
        except ValueError:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        with self.engine.begin() as c:
            got = c.execute(select(kv.c.key).where(kv.c.key == key)).first()
            vals = {"value": _j(value), "updated_at": utcnow()}
            if got:
                c.execute(update(kv).where(kv.c.key == key).values(**vals))
            else:
                c.execute(kv.insert().values(key=key, **vals))

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
