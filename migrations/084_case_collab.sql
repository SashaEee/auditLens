-- Аудит-дело: совместная работа (этап 3).
--
-- 1. Статус дела: «Сбор материалов → В работе → Завершено» и архив. Меняет
--    владелец. Архивное дело только читается: в него не добавляют и не пишут.
-- 2. Лента сообщений: обсуждение дела (item_id IS NULL) и комментарии к
--    материалу (item_id) — с авторами, @упоминаниями (mentions — логины),
--    ссылками на материалы [N] (refs: номер в тексте → item_id, номера
--    сдвигаются при удалении материалов, item_id — нет) и ответами (reply_to).
--    Пишут все участники, включая «только смотрит» (решение владельца
--    инструмента 03.10). Прежний единственный комментарий к материалу,
--    который любой участник молча переписывал, становится первым сообщением.
-- 3. История дела: кто что добавил, убрал, переименовал, кого пригласил, когда
--    сменился статус и запустили разбор. Раньше не было видно ничего.
-- 4. Версии разбора ИИ: «кто и когда» и прошлые версии — новый разбор больше
--    не затирает старый без следа.
-- 5. Личное в деле: «не следить» (без уведомлений о материалах, сообщениях и
--    статусе; упоминания и ответы приходят всё равно) и «обсуждение прочитано до».
--
-- Только CREATE/ADD ... IF NOT EXISTS и идемпотентные переносы: повтор безопасен.

ALTER TABLE audit_case ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'collect';
ALTER TABLE audit_case ADD COLUMN IF NOT EXISTS status_at TIMESTAMPTZ;
ALTER TABLE audit_case ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
DO $$ BEGIN
    ALTER TABLE audit_case ADD CONSTRAINT audit_case_status_chk
        CHECK (status IN ('collect', 'work', 'done'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS audit_case_msg (
    msg_id     BIGSERIAL PRIMARY KEY,
    case_id    BIGINT NOT NULL REFERENCES audit_case (case_id) ON DELETE CASCADE,
    item_id    BIGINT REFERENCES audit_case_item (item_id) ON DELETE CASCADE,
    username   TEXT NOT NULL,
    body       TEXT NOT NULL,
    mentions   TEXT[] NOT NULL DEFAULT '{}',
    refs       JSONB NOT NULL DEFAULT '{}'::jsonb,
    reply_to   BIGINT REFERENCES audit_case_msg (msg_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    edited_at  TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_audit_case_msg_case ON audit_case_msg (case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_case_msg_item ON audit_case_msg (item_id) WHERE item_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS audit_case_event (
    event_id   BIGSERIAL PRIMARY KEY,
    case_id    BIGINT NOT NULL REFERENCES audit_case (case_id) ON DELETE CASCADE,
    username   TEXT,
    kind       TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_case_event_case ON audit_case_event (case_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_case_analysis (
    analysis_id BIGSERIAL PRIMARY KEY,
    case_id     BIGINT NOT NULL REFERENCES audit_case (case_id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    n_items     INT,
    username    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_case_analysis_case ON audit_case_analysis (case_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_case_pref (
    case_id      BIGINT NOT NULL REFERENCES audit_case (case_id) ON DELETE CASCADE,
    username     TEXT NOT NULL,
    muted        BOOLEAN NOT NULL DEFAULT false,
    talk_seen_at TIMESTAMPTZ,
    PRIMARY KEY (case_id, username)
);

-- прежний комментарий к материалу → первое сообщение его ленты
INSERT INTO audit_case_msg (case_id, item_id, username, body, created_at)
SELECT i.case_id, i.item_id, COALESCE(i.added_by, c.username), i.note, i.added_at
  FROM audit_case_item i JOIN audit_case c ON c.case_id = i.case_id
 WHERE COALESCE(btrim(i.note), '') <> ''
   AND NOT EXISTS (SELECT 1 FROM audit_case_msg m WHERE m.item_id = i.item_id);

-- текущий разбор → первая версия (автор неизвестен)
INSERT INTO audit_case_analysis (case_id, body, n_items, username, created_at)
SELECT c.case_id, c.analysis, c.analysis_items, NULL, COALESCE(c.analysis_at, c.updated_at)
  FROM audit_case c
 WHERE COALESCE(c.analysis, '') <> ''
   AND NOT EXISTS (SELECT 1 FROM audit_case_analysis a WHERE a.case_id = c.case_id);

-- история задним числом — из того, что известно: создание, приобщения, участники
INSERT INTO audit_case_event (case_id, username, kind, payload, created_at)
SELECT x.case_id, x.username, x.kind, x.payload, x.at FROM (
    SELECT c.case_id, c.username, 'created' AS kind,
           jsonb_build_object('title', c.title) AS payload, c.created_at AS at
      FROM audit_case c
    UNION ALL
    SELECT i.case_id, COALESCE(i.added_by, c.username), 'items_added',
           jsonb_build_object('n', 1, 'titles', jsonb_build_array(left(COALESCE(i.title, i.url, ''), 120))),
           i.added_at
      FROM audit_case_item i JOIN audit_case c ON c.case_id = i.case_id
    UNION ALL
    SELECT m.case_id, m.added_by, 'member_added',
           jsonb_build_object('member', m.username, 'role', m.role), m.added_at
      FROM audit_case_member m) x
 WHERE NOT EXISTS (SELECT 1 FROM audit_case_event e WHERE e.case_id = x.case_id);

-- всё, что уже лежит в делах (в том числе перенесённые комментарии), — прочитано:
-- иначе у каждого участника вспыхнуло бы «новые сообщения» на старом
INSERT INTO audit_case_pref (case_id, username, talk_seen_at)
SELECT p.case_id, p.username, now() FROM (
    SELECT case_id, username FROM audit_case
    UNION SELECT case_id, username FROM audit_case_member) p
ON CONFLICT (case_id, username) DO NOTHING;
