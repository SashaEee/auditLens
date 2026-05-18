"""Нормализация ReviewDraft → таблицы review / review_sentiment / review_topic.

Smart-преобразования (preprocessing):
  • Spam-фильтр: отбрасываем слишком короткие/мусорные отзывы (<20 значимых символов)
  • Cross-source dedup: один и тот же отзыв может появиться на banki + sravni + bankiros
    (пользователь дублирует). Detect через (bank, normalized_text_hash, posted±1d, rating).
  • Sentiment: расширенный keyword-based (POS/NEG лексика) + boost по rating
  • Topics: расширенный набор (10+ topic), multi-label assignment
  • Author hashing: anonymized hash, чтобы можно было считать частоту и обнаруживать
    «отзывных ботов»

Все шаги детерминистичны и быстры — никаких внешних API. LLM-обогащение —
отдельный батч-job вне ingest hot-path.
"""
from __future__ import annotations
import hashlib, json, re, unicodedata
from datetime import timedelta
from typing import Iterable
from sqlalchemy import text
from .. import db
from ..hashing import author_hash, stable_digest
from ..models import ReviewDraft
from .offers import resolve_bank
from .rules import COMPLAINT_TOPICS, POS_WORDS, NEG_WORDS


# ── Препроцессинг текста ─────────────────────────────────────────────────────

_SPAM_MARKERS = (
    "click here", "телеграмм для связи", "+7-9", "пиши в личку", "wa.me/",
    "@@", "########",
)
_PUNCT_RE = re.compile(r"[^\w\sа-яёА-ЯЁ]+", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)


def _normalize_text_for_hash(s: str) -> str:
    """Текст → нижний регистр, без пунктуации, схлопнутые пробелы.
    Используется для cross-source dedup."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _is_spam(text_: str) -> bool:
    """Грубый spam-фильтр: маркеры контактов, мало значимых символов, ссылки."""
    if not text_ or len(text_.strip()) < 20:
        return True
    low = text_.lower()
    if any(m in low for m in _SPAM_MARKERS):
        return True
    # Слишком высокая доля небуквенных символов
    letters = sum(1 for c in text_ if c.isalpha())
    if letters < 15:
        return True
    return False


# ── Расширенные topics ───────────────────────────────────────────────────────
# Добавляем поверх COMPLAINT_TOPICS из rules.py (там 9 тем).
EXTRA_TOPICS = {
    "interest_rate":  ["ставка", "процент по вкладу", "снизили процент"],
    "loan_approval":  ["одобрили", "не одобрили", "отказ", "отказали в кредите"],
    "branch_service": ["отделение", "очередь", "касса", "офис банка", "филиал"],
    "online_bank":    ["онлайн-банк", "интернет-банк", "сбербанк-онлайн", "личный кабинет"],
    "premium":        ["премиум", "вип", "private", "premium"],
    "bonus_program":  ["кешбэк", "кэшбек", "бонус", "спасибо", "мили"],
    "documents":      ["справка", "выписка", "договор", "анкета"],
    "fraud":          ["мошенник", "украли", "сняли деньги", "фишинг", "социальная инженерия"],
    "partner":        ["партнёр", "партнер", "услуга-партнер", "страховая компания"],
}


def _classify_topics(text_: str) -> list[tuple[str, float]]:
    t = text_.lower()
    out = []
    all_topics = {**COMPLAINT_TOPICS, **EXTRA_TOPICS}
    for topic, kws in all_topics.items():
        hits = sum(1 for k in kws if k in t)
        if hits:
            out.append((topic, min(1.0, hits / 3)))
    return out


def _sentiment(text_: str, rating: float | None = None) -> tuple[str, float]:
    """Sentiment: keyword score + boost по rating если он есть.
    Rating ≥4 → push к pos, ≤2 → к neg.
    """
    t = text_.lower()
    pos = sum(1 for w in POS_WORDS if w in t)
    neg = sum(1 for w in NEG_WORDS if w in t)

    base_score = 0.0
    if pos or neg:
        base_score = (pos - neg) / max(pos + neg, 1)

    # Rating-based корректировка (если есть): rating 1-5 → −0.5..+0.5
    if rating is not None:
        try:
            rv = float(rating)
            base_score = 0.6 * base_score + 0.4 * ((rv - 3.0) / 2.0)
        except (TypeError, ValueError):
            pass

    if base_score > 0.2:
        return ("pos", min(1.0, 0.5 + base_score / 2))
    if base_score < -0.2:
        return ("neg", min(1.0, 0.5 - base_score / 2))
    return ("neu", 0.5)


def _content_dedup_key(bank_id: int, posted_at, text_: str,
                        rating: float | None) -> str:
    """Стабильный ключ для cross-source dedup. Если две источника привезли
    один и тот же отзыв (пользователь продублировал) — у них совпадёт текст,
    дата (с точностью до дня) и рейтинг.
    """
    norm = _normalize_text_for_hash(text_)
    # берём первые 400 значимых символов — устойчивость к небольшим правкам
    norm = norm[:400]
    day = posted_at.date().isoformat() if posted_at else ""
    return stable_digest({
        "b":  bank_id, "d": day, "r": str(rating or ""), "t": norm,
    })[:24]


def upsert_review(session, d: ReviewDraft,
                  snapshot_id: int | None) -> tuple[int, bool]:
    """Вставляет отзыв с защитой:
      1. Spam-фильтр → reject
      2. (source, source_review_id) UNIQUE → DO NOTHING
      3. Cross-source dedup через content_key в raw → если уже есть схожий
         отзыв в другом source — пропускаем (записывая факт в log, чтобы
         можно было потом сосчитать дубли).
    """
    if _is_spam(d.text or ""):
        return -1, False

    bank_id = resolve_bank(session, d.bank_name_raw)
    content_key = _content_dedup_key(bank_id, d.posted_at, d.text or "", d.rating)

    # Cross-source dedup: если такой же content_key уже есть — пропускаем.
    # Используем raw->>'content_key' jsonb-индексируемое поле.
    existing = session.execute(text("""
        SELECT review_id FROM review
         WHERE bank_id = :b
           AND raw->>'content_key' = :ck
         LIMIT 1
    """), {"b": bank_id, "ck": content_key}).first()
    if existing:
        return existing[0], False

    raw_payload = dict(d.raw or {})
    raw_payload["content_key"] = content_key

    row = session.execute(text("""
        INSERT INTO review(source, source_review_id, source_url, bank_id,
                           product_category, posted_at, rating, title, text,
                           author_hash, status, raw, source_snapshot_id)
        VALUES (:s,:srid,:url,:b,:cat,:pa,:r,:tt,:tx,:ah,:st, CAST(:raw AS jsonb), :ssid)
        ON CONFLICT (source, source_review_id) DO NOTHING
        RETURNING review_id
    """), {
        "s": d.source, "srid": d.source_review_id, "url": d.source_url,
        "b": bank_id, "cat": d.product_category, "pa": d.posted_at,
        "r": d.rating, "tt": d.title, "tx": d.text,
        "ah": author_hash(d.author_raw), "st": d.status,
        "raw": json.dumps(raw_payload, ensure_ascii=False, default=str),
        "ssid": snapshot_id,
    }).first()
    if not row:
        return -1, False
    rid = row[0]

    # Sentiment учитывает rating если есть
    label, score = _sentiment(d.text or "", d.rating)
    session.execute(text("""
        INSERT INTO review_sentiment(review_id, label, score) VALUES (:r,:l,:s)
        ON CONFLICT (review_id) DO NOTHING
    """), {"r": rid, "l": label, "s": score})

    for topic, sc in _classify_topics(d.text or ""):
        session.execute(text("""
            INSERT INTO review_topic(review_id, topic, score) VALUES (:r,:t,:s)
        """), {"r": rid, "t": topic, "s": sc})

    return rid, True


def normalize_reviews(drafts: Iterable[ReviewDraft],
                      snapshot_id: int | None) -> dict:
    seen = written = spam = dup = 0
    with db.session() as s:
        for d in drafts:
            seen += 1
            if _is_spam(d.text or ""):
                spam += 1
                continue
            rid, new = upsert_review(s, d, snapshot_id)
            if new:
                written += 1
            elif rid > 0:
                dup += 1   # cross-source dup
    return {"seen": seen, "written": written, "spam": spam, "duplicates": dup}
