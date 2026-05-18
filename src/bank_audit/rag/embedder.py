"""Embedder: BGE-M3 локально (multilingual, 1024d).

Грузим лениво — модель ~2GB, не нужно при старте сервера. Первый embed
займёт 5-10s (загрузка), последующие ~50ms на CPU.

Альтернатива (если RAM критична): EMBEDDING_MODEL=intfloat/multilingual-e5-small
  — 470MB, 384d. Менять также EMBEDDING_DIM в конфиге и схеме vector(N).

Стратегия:
  • Singleton: одна загруженная модель на процесс
  • Batch encoding: при больших объёмах прогоняем пачками по 32
  • Кэш на 64KB строки в памяти (повторные embed одного текста часто, не страшно)
"""
from __future__ import annotations
import os, hashlib, logging, threading
from typing import Iterable

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))   # BGE-M3 → 1024
MAX_LEN       = int(os.getenv("EMBEDDING_MAX_TOKENS", "512"))

_model = None
_model_lock = threading.Lock()
_text_cache: dict[str, list[float]] = {}
_CACHE_LIMIT = 5000


def _get_model():
    """Lazy load. Только один раз, потокобезопасно."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        log.info("embedder: loading model %s (this takes ~10s on first call)",
                 DEFAULT_MODEL)
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(DEFAULT_MODEL, device="cpu")
        # Установим max_seq_length, чтобы длинные chunks обрезались gracefully
        try:
            _model.max_seq_length = MAX_LEN
        except Exception:
            pass
        log.info("embedder: model loaded, dim=%s", EMBEDDING_DIM)
        return _model


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def embed_one(text: str) -> list[float]:
    """Возвращает embedding для одной строки."""
    if not text:
        return [0.0] * EMBEDDING_DIM
    h = _hash_text(text)
    cached = _text_cache.get(h)
    if cached is not None:
        return cached
    vec = _get_model().encode(text, normalize_embeddings=True).tolist()
    if len(_text_cache) < _CACHE_LIMIT:
        _text_cache[h] = vec
    return vec


def embed_batch(texts: list[str], batch_size: int = 32,
                show_progress: bool = False) -> list[list[float]]:
    """Embeds список текстов. Использует batch_size для скорости.
    Кэш используется на уровне отдельных текстов.
    """
    if not texts:
        return []
    out: list[list[float] | None] = [None] * len(texts)
    miss_idx: list[int] = []
    miss_text: list[str] = []
    for i, t in enumerate(texts):
        if not t:
            out[i] = [0.0] * EMBEDDING_DIM
            continue
        h = _hash_text(t)
        cached = _text_cache.get(h)
        if cached is not None:
            out[i] = cached
        else:
            miss_idx.append(i)
            miss_text.append(t)

    if miss_text:
        model = _get_model()
        encoded = model.encode(
            miss_text, batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=show_progress,
        )
        for j, vec in enumerate(encoded):
            v = vec.tolist()
            i = miss_idx[j]
            out[i] = v
            if len(_text_cache) < _CACHE_LIMIT:
                _text_cache[_hash_text(miss_text[j])] = v
    return out  # type: ignore


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Helper для in-memory ranking. Нормализованные → dot product."""
    return sum(x * y for x, y in zip(a, b))


def is_loaded() -> bool:
    """Проверка для UI/health-check без загрузки модели."""
    return _model is not None
