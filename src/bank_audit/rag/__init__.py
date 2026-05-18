"""RAG (Retrieval-Augmented Generation) layer для Audit Studio.

Модули:
  • embedder    — BGE-M3 локально, многоязычные embeddings 1024d
  • trust       — scoring источников + sponsored-content detection
  • chunker     — нарезка длинного текста на chunk'и с overlap
  • retriever   — semantic search в pgvector с фильтрами доверия
  • cache       — TTL-кэш на rag_cache таблице
  • summarizer  — daily batch для review_summary (горячий слой)
"""
