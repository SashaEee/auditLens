-- 030: единый индекс отзывов по НЕСКОЛЬКИМ источникам.
--
-- Было. Зеркало (028) строилось под один корпус — внешнюю базу banki.ru, и так и
-- называлось: bankiru_review_fts. Вкладка «Отзывы» читала только её.
--
-- Стало. Источников больше одного: добавился finuslugi.ru, дальше будут другие.
-- Аудитору не нужен отдельный блок на каждую площадку — ему нужна одна лента и
-- один поиск, где источник это просто ещё одна колонка. Поэтому зеркало
-- переименовано в review_index и знает, откуда пришла строка.
--
-- Тексты по-прежнему НЕ дублируются: индекс хранит ссылку и признак хранилища,
-- а тело берётся у владельца — из внешней базы bankiru по её id либо из нашей
-- таблицы review. Так у текста остаётся ровно один владелец.
--
-- NB: знак процента в комментариях миграций не ставить — накатываются через
-- exec_driver_sql, psycopg считает его началом плейсхолдера.

-- Повтор setup на старом volume возможен после частично применённого legacy
-- прогона: тогда новое имя уже есть, а старого нет. ALTER RENAME не имеет
-- собственного IF TARGET NOT EXISTS, поэтому проверяем обе стороны явно.
DO $$
BEGIN
    IF to_regclass('public.review_index') IS NULL
       AND to_regclass('public.bankiru_review_fts') IS NOT NULL THEN
        ALTER TABLE bankiru_review_fts RENAME TO review_index;
    END IF;
    IF to_regclass('public.review_index_state') IS NULL
       AND to_regclass('public.bankiru_fts_state') IS NOT NULL THEN
        ALTER TABLE bankiru_fts_state RENAME TO review_index_state;
    END IF;
END $$;

-- Откуда строка. Значение по умолчанию описывает то, что уже лежит в таблице:
-- на момент миграции там 169 тыс. строк из корпуса banki.ru.
ALTER TABLE review_index ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'bankiru';

-- Оценка. У корпуса banki.ru её нет вовсе (он собран только по 1-2 звёздам, и
-- само его наличие в выборке уже означает негатив), у новых источников есть.
ALTER TABLE review_index ADD COLUMN IF NOT EXISTS rating REAL;

-- Срезы всегда идут «источник + банк + свежесть»
CREATE INDEX IF NOT EXISTS review_index_source_bank_dt
    ON review_index (source, bank, dt DESC);

-- Векторы отзывов, которые считаем МЫ.
--
-- Отдельно от bankiru.review_embeddings, и это не дублирование, а необходимость:
-- сверка показала косинус 0.887 между вектором из внешней базы и вектором того
-- же текста, посчитанным нашим эмбеддером. Модель одна (bge-m3), но рецепты
-- разные — там локальный sentence-transformers, у нас API cloud.ru. Векторы из
-- двух рецептов лежат в БЛИЗКИХ, но не совпадающих пространствах, и складывать
-- их в один ANN-запрос нельзя: расстояния окажутся несопоставимы и выдача
-- начнёт систематически предпочитать один источник другому.
--
-- Поэтому поиск идёт по каждому хранилищу СВОЕЙ ногой, а выдачи сливаются
-- ранговой суммой (RRF). Ранги нечувствительны к разнице масштабов — это и есть
-- причина, по которой RRF здесь подходит, а порог по косинусу не подошёл бы.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        CREATE TABLE IF NOT EXISTS review_embedding (
            url        TEXT PRIMARY KEY,
            embedding  vector(1024) NOT NULL,
            model      TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS review_embedding_hnsw
            ON review_embedding USING hnsw (embedding vector_cosine_ops);
    END IF;
END $$;
