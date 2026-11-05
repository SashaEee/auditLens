"""Загрузка отзывов для расчёта сигналов: лёгкие записи в памяти.

Одна запись — один отзыв со всем, что нужно признакам: точка обслуживания (ВСП или
карточка без привязки), ГОСБ и субъект, оценка, время в UTC и местное, автор (хэш),
подтверждение операцией, сколько всего отзывов у автора на площадке, отметка площадки
о скрытии, нормализованный текст (для шаблонов).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..storage import Store, branches, reviews, source_branches

UTC = timezone.utc
_WORD = re.compile(r"[a-zа-я0-9]+")


def norm_text(s: str | None, limit: int = 400) -> str:
    """Нижний регистр, ё→е, только слова — для сравнения текстов между собой."""
    return " ".join(_WORD.findall((s or "").lower().replace("ё", "е")[:limit * 2]))[:limit]


def as_utc(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


@dataclass(slots=True)
class Rec:
    rid: str
    source: str
    point: str                   # "b<branch_id>" или "<источник>:<id карточки>"
    branch_id: int | None
    gosb: str | None
    subject: str | None
    rating: int | None
    at: datetime                 # UTC
    local: datetime              # местное время ВСП (часовой пояс субъекта; иначе МСК)
    author: str | None
    provider: str | None         # 2gis | flamp | … (у Яндекса None)
    confirmed: bool              # «отзыв подтверждён» операцией (2ГИС: trust_factors)
    author_total: int | None     # всего отзывов автора на площадке
    hidden: str | None           # причина скрытия/пометки площадкой
    text: str                    # нормализованный текст
    words: int
    gone: bool
    replied: bool
    extra: dict = field(default_factory=dict)

    @property
    def u5(self) -> bool:
        """Пятёрка не по официальному каналу (не подтверждена операцией)."""
        return self.rating == 5 and not self.confirmed

    @property
    def footfall(self) -> bool:
        """Отзыв, отражающий поток клиентов, а не кампанию: 1–4★ или подтверждённая пятёрка."""
        return self.rating is not None and (self.rating < 5 or self.confirmed)


def point_of(branch_id: int | None, source: str, sbid: str) -> str:
    return f"b{branch_id}" if branch_id else f"{source}:{sbid}"


def load(store: Store, *, sources: tuple[str, ...] = ("2gis", "yandex")) -> tuple[list[Rec], dict]:
    """Все отзывы источников (вместе с пропавшими из выдачи) + справочник точек."""
    with store.engine.connect() as c:
        br = {r.branch_id: r for r in c.execute(select(
            branches.c.branch_id, branches.c.gosb, branches.c.subject, branches.c.utc_offset, branches.c.name,
            branches.c.city, branches.c.address, branches.c.status, branches.c.formats, branches.c.lat,
            branches.c.lon, branches.c.open_date))}
        cards = {(r.source, r.source_branch_id): r for r in c.execute(select(
            source_branches.c.source, source_branches.c.source_branch_id, source_branches.c.branch_id,
            source_branches.c.name, source_branches.c.address, source_branches.c.city, source_branches.c.lat,
            source_branches.c.lon, source_branches.c.declared_reviews, source_branches.c.declared_rating,
            source_branches.c.raw_json))}
        rows = c.execute(select(
            reviews.c.review_id, reviews.c.source, reviews.c.source_branch_id, reviews.c.branch_id,
            reviews.c.rating, reviews.c.review_date, reviews.c.author_ref, reviews.c.review_text,
            reviews.c.gone_at, reviews.c.organization_reply, reviews.c.raw_json,
        ).where(reviews.c.source.in_(sources), reviews.c.review_date.isnot(None)))
        out: list[Rec] = []
        for r in rows:
            bid = r.branch_id
            if bid is None:
                card = cards.get((r.source, r.source_branch_id))
                bid = card.branch_id if card else None
            b = br.get(bid) if bid else None
            try:
                raw = json.loads(r.raw_json or "{}") or {}
            except ValueError:
                raw = {}
            at = as_utc(r.review_date)
            off = b.utc_offset if b is not None and b.utc_offset is not None else 3
            txt = norm_text(r.review_text)
            out.append(Rec(
                rid=r.review_id, source=r.source, point=point_of(bid, r.source, r.source_branch_id),
                branch_id=bid, gosb=b.gosb if b is not None else None,
                subject=b.subject if b is not None else None,
                rating=int(r.rating) if r.rating is not None else None,
                at=at, local=at + timedelta(hours=off), author=r.author_ref,
                provider=raw.get("provider"), confirmed=bool(raw.get("confirmed_by")),
                author_total=raw.get("author_reviews_count"),
                hidden=(raw.get("hiding_reason") or None), text=txt, words=txt.count(" ") + 1 if txt else 0,
                gone=r.gone_at is not None, replied=bool(r.organization_reply),
                extra={"raw_text": r.review_text} if r.review_text else {}))
    return out, {"branches": br, "cards": cards}
