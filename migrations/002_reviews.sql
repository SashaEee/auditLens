CREATE TABLE review (
  review_id        BIGSERIAL PRIMARY KEY,
  source           TEXT NOT NULL,              -- 'banki_reviews'|'sravni_reviews'
  source_review_id TEXT NOT NULL,
  source_url       TEXT NOT NULL,
  bank_id          BIGINT REFERENCES bank(bank_id),
  product_category product_category,           -- определяется при наличии маркеров
  posted_at        TIMESTAMPTZ,
  rating           NUMERIC(3,1),
  title            TEXT,
  text             TEXT NOT NULL,
  author_hash      TEXT,                       -- sha256(автор) — без PII
  status           TEXT,                       -- решён/не решён и т.п.
  raw              JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_snapshot_id BIGINT REFERENCES source_snapshot(snapshot_id),
  ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source, source_review_id)
);
CREATE INDEX review_bank_time ON review(bank_id, posted_at DESC);

CREATE TABLE review_sentiment (
  review_id BIGINT PRIMARY KEY REFERENCES review(review_id) ON DELETE CASCADE,
  label     TEXT NOT NULL,                     -- 'neg'|'neu'|'pos'
  score     NUMERIC(5,3) NOT NULL,
  method    TEXT NOT NULL DEFAULT 'rules_v1'
);

CREATE TABLE review_topic (
  topic_id  BIGSERIAL PRIMARY KEY,
  review_id BIGINT NOT NULL REFERENCES review(review_id) ON DELETE CASCADE,
  topic     TEXT NOT NULL,                     -- 'fees','app_bugs','support','rate_change'...
  score     NUMERIC(5,3) NOT NULL DEFAULT 1.0,
  method    TEXT NOT NULL DEFAULT 'rules_v1'
);
CREATE INDEX review_topic_topic ON review_topic(topic);
