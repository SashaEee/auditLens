-- OpenClaw job + run log: что и когда исполнял оркестратор
CREATE TABLE openclaw_job (
  job_id     TEXT PRIMARY KEY,                 -- ключ из openclaw/jobs/*.yaml
  schedule   TEXT,                             -- cron expr
  agent      TEXT,                             -- collector|normalizer|quality
  enabled    BOOLEAN NOT NULL DEFAULT TRUE,
  meta       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE openclaw_run_log (
  log_id      BIGSERIAL PRIMARY KEY,
  job_id      TEXT NOT NULL REFERENCES openclaw_job(job_id),
  run_id      BIGINT REFERENCES extraction_run(run_id),
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status      TEXT NOT NULL DEFAULT 'running',
  output_path TEXT,                            -- где лежит лог в workspace/logs
  error       TEXT
);
CREATE INDEX openclaw_run_log_job_time ON openclaw_run_log(job_id, started_at DESC);
