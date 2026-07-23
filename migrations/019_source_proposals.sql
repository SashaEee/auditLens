-- 019: предложения источников от аудиторов.
-- Вкладка «Источники» была технической (запуски сборов, капчи). Аудитору нужно
-- другое: какие источники и с каким доверием участвуют в каждом контуре — и
-- возможность предложить свой. Заявки проходят модерацию владельцем.

CREATE TABLE IF NOT EXISTS source_proposal (
    proposal_id  SERIAL PRIMARY KEY,
    -- контур, куда предлагается источник:
    --   ai      — ИИ-аналитик (веб-поиск и отчёты)
    --   digest  — главная страница (утренний брифинг, новости)
    --   reviews — отзывы клиентов
    --   tariffs — тарифы и условия продуктов
    purpose      TEXT NOT NULL,
    url          TEXT NOT NULL,
    domain       TEXT NOT NULL,               -- нормализованный, для дедупа
    title        TEXT,                        -- как называется источник
    reason       TEXT,                        -- зачем он аудиту
    proposed_by  TEXT NOT NULL,               -- username из Authentik
    proposer_name TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    review_note  TEXT,
    reviewed_by  TEXT,
    reviewed_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- один и тот же домен нельзя предлагать в один контур дважды, пока заявка живая
CREATE UNIQUE INDEX IF NOT EXISTS source_proposal_pending_uniq
    ON source_proposal (purpose, domain)
 WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS source_proposal_by_user
    ON source_proposal (proposed_by, created_at DESC);
