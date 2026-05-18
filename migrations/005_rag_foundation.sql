-- Migration 005: RAG foundation (knowledge layer for "Audit Studio")
-- Pre-flight: pgvector extension enabled separately.

CREATE EXTENSION IF NOT EXISTS vector;


-- ── Trust layer ───────────────────────────────────────────────────────────────
-- Каноническая таблица доверия источникам. Используется при retrieval —
-- chunk'и фильтруются по trust_score >= порог.
CREATE TABLE IF NOT EXISTS source_trust (
    source_id      SERIAL PRIMARY KEY,
    kind           TEXT NOT NULL,                    -- 'bank_official' | 'regulator' | 'aggregator' | 'forum' | 'blog' | 'sponsored'
    domain         TEXT,                             -- alfabank.ru, sravni.ru, ...
    bank_id        INT REFERENCES bank(bank_id) ON DELETE SET NULL,
    weight         NUMERIC(3,2) NOT NULL DEFAULT 0.5,-- 0.0..1.0 (0=blacklist, 1=официал)
    notes          TEXT,
    UNIQUE(kind, domain)
);

-- Стартовые правила доверия (расширяется при добавлении новых источников)
INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    ('regulator',     'cbr.ru',         1.00, 'Центральный банк РФ — первоисточник'),
    ('regulator',     'fns.gov.ru',     1.00, 'ФНС РФ'),
    ('aggregator',    'sravni.ru',      0.70, 'Sravni.ru — проверенный агрегатор'),
    ('aggregator',    'banki.ru',       0.70, 'Banki.ru — проверенный агрегатор'),
    ('aggregator',    'bankiros.ru',    0.65, 'Bankiros.ru'),
    ('forum',         'irecommend.ru',  0.50, 'Форум отзывов'),
    ('forum',         'otzovik.com',    0.50, 'Форум отзывов')
ON CONFLICT (kind, domain) DO NOTHING;


-- ── Bank profile ──────────────────────────────────────────────────────────────
-- Профиль банка для тёплого слоя: сайт, sitemap, ключевые страницы.
-- key_pages — словарь { topic_slug: url } известных категорий.
CREATE TABLE IF NOT EXISTS bank_profile (
    bank_id           INT PRIMARY KEY REFERENCES bank(bank_id) ON DELETE CASCADE,
    official_url      TEXT,
    sitemap_url       TEXT,
    robots_url        TEXT,
    key_pages         JSONB DEFAULT '{}'::jsonb,    -- {transfers_intl: "...", tariffs_pdf: "...", ...}
    last_crawled_at   TIMESTAMPTZ,
    crawl_status      TEXT,                          -- 'ok'|'partial'|'blocked'|'pending'
    notes             TEXT
);


-- ── Documents (raw + parsed) ──────────────────────────────────────────────────
-- Документ — единица knowledge: HTML-страница, PDF, XLSX, PPT.
-- Хранит чистый текст (для отображения и RAG-extract). Сырьё в workspace/raw.
CREATE TYPE doc_type AS ENUM ('html','pdf','xlsx','pptx','docx','txt','json');

CREATE TABLE IF NOT EXISTS document (
    document_id    SERIAL PRIMARY KEY,
    source_id      INT REFERENCES source_trust(source_id) ON DELETE SET NULL,
    bank_id        INT REFERENCES bank(bank_id) ON DELETE SET NULL,
    url            TEXT NOT NULL,
    doc_type       doc_type NOT NULL,
    title          TEXT,
    headings_path  TEXT,                            -- "Переводы > За рубеж > Лимиты"
    content_text   TEXT,                            -- очищенный текст (для UI/preview)
    raw_path       TEXT,                            -- путь в workspace/raw
    content_sha256 TEXT,                            -- идемпотентность
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_modified  TIMESTAMPTZ,                     -- из HTTP заголовка/sitemap
    trust_score    NUMERIC(3,2),                    -- snapshot weight + content adjustment
    is_sponsored   BOOLEAN DEFAULT FALSE,           -- detected promo/paid material
    bytes          INT,
    UNIQUE(url, content_sha256)                     -- дедуп по контенту
);
CREATE INDEX IF NOT EXISTS document_bank_idx       ON document(bank_id);
CREATE INDEX IF NOT EXISTS document_source_idx     ON document(source_id);
CREATE INDEX IF NOT EXISTS document_trust_idx      ON document(trust_score) WHERE trust_score IS NOT NULL;
CREATE INDEX IF NOT EXISTS document_fetched_idx    ON document(fetched_at DESC);


-- ── Document chunks + vector embeddings ──────────────────────────────────────
-- Документы режутся на chunk'и (~500 токенов) для семантического поиска.
-- Embedding 1024d — соответствует BGE-M3 (нашему дефолтному embedder'у).
CREATE TABLE IF NOT EXISTS document_chunk (
    chunk_id       BIGSERIAL PRIMARY KEY,
    document_id    INT NOT NULL REFERENCES document(document_id) ON DELETE CASCADE,
    idx            INT NOT NULL,                    -- порядковый номер chunk'а в документе
    text           TEXT NOT NULL,
    tokens         INT,
    headings_path  TEXT,                            -- breadcrumb для UI
    embedding      vector(1024),
    UNIQUE(document_id, idx)
);
-- HNSW индекс под cosine distance (масштабируется до миллионов chunk'ов)
CREATE INDEX IF NOT EXISTS chunk_embedding_hnsw
    ON document_chunk USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS chunk_doc_idx ON document_chunk(document_id);


-- ── Bank features (структурированные ответы) ─────────────────────────────────
-- Когда RAG-агент извлёк factual answer (например «лимит SWIFT 10 000$ / день»),
-- он пишет результат сюда. При повторном вопросе сначала читаем отсюда — экономим
-- LLM-токены и latency. Self-enriching база.
CREATE TABLE IF NOT EXISTS bank_feature (
    feature_id       SERIAL PRIMARY KEY,
    bank_id          INT NOT NULL REFERENCES bank(bank_id) ON DELETE CASCADE,
    feature_key      TEXT NOT NULL,                 -- 'swift_limit_per_day', 'mobile_app:android_rating', ...
    feature_value    JSONB NOT NULL,                -- {value: ..., currency: 'RUB', period: 'day', ...}
    confidence       NUMERIC(3,2),                  -- 0..1 — надёжность extraction
    source_url       TEXT,
    source_id        INT REFERENCES source_trust(source_id),
    document_id      INT REFERENCES document(document_id) ON DELETE SET NULL,
    extracted_by     TEXT,                          -- 'rag_agent_v1' | 'manual' | 'crawler'
    extracted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until      TIMESTAMPTZ,                   -- TTL — переэкстрактить после
    UNIQUE(bank_id, feature_key, source_id)
);
CREATE INDEX IF NOT EXISTS bank_feature_lookup_idx ON bank_feature(bank_id, feature_key);


-- ── Review summary (горячий слой) ─────────────────────────────────────────────
-- Предагрегированные темы жалоб/похвал per банк per период. RAG обращается сюда
-- вместо миллионов сырых отзывов.
CREATE TABLE IF NOT EXISTS review_summary (
    summary_id     SERIAL PRIMARY KEY,
    bank_id        INT NOT NULL REFERENCES bank(bank_id) ON DELETE CASCADE,
    period         TEXT NOT NULL,                   -- 'all' | 'q1_2026' | 'last_30d' | ...
    total_reviews  INT NOT NULL,
    avg_rating     NUMERIC(3,2),
    sentiment_pos  INT,                             -- count
    sentiment_neg  INT,
    sentiment_neu  INT,
    top_complaints JSONB,                           -- [{topic, n, sample_quotes:[...]}, ...]
    top_praise     JSONB,
    by_source      JSONB,                           -- {banki: {n, avg}, sravni: {...}, ...}
    generated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(bank_id, period)
);
CREATE INDEX IF NOT EXISTS review_summary_period_idx ON review_summary(period);


-- ── Cache (TTL-таблица для real-time fetch) ──────────────────────────────────
-- Кэшируем дорогие операции: fetch результат, синтез ответа RAG, embed запроса.
CREATE TABLE IF NOT EXISTS rag_cache (
    cache_key      TEXT PRIMARY KEY,                -- digest от запроса+фильтров
    namespace      TEXT NOT NULL,                   -- 'fetch' | 'answer' | 'embed' | 'search'
    value          JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS rag_cache_expires_idx ON rag_cache(expires_at);
CREATE INDEX IF NOT EXISTS rag_cache_namespace_idx ON rag_cache(namespace);


-- ── Удобные view ──────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_document_by_bank AS
SELECT b.slug, b.name AS bank_name, b.is_sber,
       d.document_id, d.url, d.doc_type, d.title,
       d.trust_score, d.is_sponsored, d.fetched_at,
       length(d.content_text) AS chars,
       (SELECT count(*) FROM document_chunk dc WHERE dc.document_id = d.document_id) AS chunks
  FROM document d
  LEFT JOIN bank b USING(bank_id)
 ORDER BY d.fetched_at DESC;

CREATE OR REPLACE VIEW v_bank_knowledge_coverage AS
SELECT b.bank_id, b.slug, b.name,
       count(DISTINCT d.document_id)             AS documents,
       count(DISTINCT dc.chunk_id)               AS chunks,
       count(DISTINCT bf.feature_id)             AS features,
       max(d.fetched_at)                         AS last_doc_fetch,
       max(bf.extracted_at)                      AS last_feature_extract
  FROM bank b
  LEFT JOIN document d            ON d.bank_id = b.bank_id
  LEFT JOIN document_chunk dc     ON dc.document_id = d.document_id
  LEFT JOIN bank_feature bf       ON bf.bank_id = b.bank_id
 GROUP BY b.bank_id, b.slug, b.name
 ORDER BY documents DESC NULLS LAST;
