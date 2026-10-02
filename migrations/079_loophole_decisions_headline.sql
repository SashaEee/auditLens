-- «Уязвимости»: журнал решений экспертов и заголовок находки.
--
-- loophole_record_decision — каждое решение эксперта по записи (очередь, смена
-- вердикта в базе, применение к копиям): кто, когда, что было и что стало,
-- комментарий. Раньше решение писалось только в общий журнал действий, и
-- карточка записи не могла показать, кто решил.
--
-- headline / summary_doubt / bank_inferred заполняет тот же вызов модели, что
-- составляет суть (только для уязвимостей и схем): короткий заголовок находки
-- вместо названия ветки форума, сомнение модели («похоже на рекламу») для
-- эксперта и признак, что банк определён моделью по тексту, а не сборщиком.
-- Только ADD COLUMN IF NOT EXISTS / CREATE ... IF NOT EXISTS: повтор безопасен.
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS headline TEXT;
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS summary_doubt TEXT;
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS bank_inferred BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS loophole_record_decision (
    decision_id  BIGSERIAL PRIMARY KEY,
    record_id    BIGINT NOT NULL,
    decided_by   TEXT NOT NULL,
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    previous     TEXT,
    decision     TEXT NOT NULL,
    comment      TEXT,
    source       TEXT
);
CREATE INDEX IF NOT EXISTS idx_loophole_record_decision_record
    ON loophole_record_decision (record_id, decided_at);

-- Прошлые решения (до журнала) восстанавливаются из общего журнала действий:
-- кто, когда, какой тип и комментарий; «было» там не сохранялось. Источник
-- 'journal' отличает их от решений, записанных сразу. Повтор ничего не дублирует.
INSERT INTO loophole_record_decision (record_id, decided_by, decided_at, previous, decision, comment, source)
SELECT ids.record_id::BIGINT, log.user_id, COALESCE(log.created_at, now()), NULL,
       COALESCE(log.detail->>'classification',
                CASE WHEN (log.detail->>'is_loophole')::BOOLEAN THEN 'vulnerability' ELSE 'not_confirmed' END),
       NULLIF(log.detail->>'comment', ''), 'journal'
FROM loophole_action_log AS log
CROSS JOIN LATERAL jsonb_array_elements_text(log.detail->'ids') AS ids(record_id)
WHERE log.action = 'mark_verdict' AND log.user_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM loophole_record_decision AS d
      WHERE d.record_id = ids.record_id::BIGINT AND d.decided_at = log.created_at
        AND d.decided_by = log.user_id
  );
