-- 106: «слова рядом» как предикат SQL — al_near (rag/bankiru_fts.search_near_sql).
--
-- До 04.11.2026 окно «все слова группы рядом» проверялось в Python: поиск забирал
-- не больше NEAR_CAP (6000) кандидатов по префиксному tsquery вместе с tsv::text и
-- разбирал позиции регуляркой. У частых запросов кандидатов больше — хвост молча
-- отрезался по рангу, и счётчик со страницами считались по обрезанному набору.
-- al_near — ровно та же мера (bankiru_fts.near_window), но внутри базы: набор,
-- точный счёт и страница считаются целиком, без потолка.
--
-- keys — основы слов группы (bankiru_fts.stem_key); span — допустимая длина окна в
-- позициях: max(число основ, слов в запросе) - 1 + разрыв, её считает Python;
-- pmin — с какой длины основа ищется как начало слова (REVIEWS_NEAR_PREFIX_MIN).
-- Ключ совпадает с лексемой отзыва, если равен ей или, когда он не короче pmin,
-- является её началом: «отказ» находит «отказа», а «карт» не находит «картридж».
--
-- Почему plpgsql, а не чистый SQL: окно проверяется скользящим проходом с выходом
-- при первом успехе, а из базы берётся один запрос — позиции совпавших лексем.
-- Замер на 20 тыс. строк: вариант с самосоединением позиций и группировкой
-- медленнее на треть, с вложенными EXISTS — вровень. Для одной и двух основ
-- search_near_sql обходится без функции: хватает самого tsquery (две основы —
-- фразами «a <d> b»), это та же мера, но в разы быстрее.
--
-- NB: знака процента здесь нет намеренно (остаток — mod): миграции бывают
-- накатаны через драйвер, где процент — начало плейсхолдера.
SET lock_timeout = '3s';

CREATE OR REPLACE FUNCTION al_near(tsv tsvector, keys text[], span int, pmin int DEFAULT 5)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    ks   text[] := '{}';
    x    text;
    n    int;
    ev   int[];          -- события «позиция * 256 + номер ключа», по возрастанию
    cnt  int[];
    have int := 0;
    l    int := 1;
    j    int;
BEGIN
    IF tsv IS NULL OR keys IS NULL OR span IS NULL THEN
        RETURN false;
    END IF;
    -- повтор основы — один ключ, как dict.fromkeys в near_window
    FOREACH x IN ARRAY keys LOOP
        IF x <> '' AND NOT x = ANY (ks) THEN
            ks := ks || x;
        END IF;
    END LOOP;
    n := cardinality(ks);
    IF n = 0 THEN
        RETURN false;
    END IF;
    IF n > 255 THEN
        RAISE EXCEPTION USING MESSAGE = 'al_near: больше 255 ключей в группе';
    END IF;
    SELECT array_agg((p * 256 + k.i)::int ORDER BY (p * 256 + k.i)::int) INTO ev
      FROM unnest(ks) WITH ORDINALITY AS k(key, i)
      JOIN unnest(tsv) AS u
        ON u.lexeme = k.key OR (length(k.key) >= pmin AND starts_with(u.lexeme, k.key))
     CROSS JOIN unnest(u.positions) AS p;
    IF ev IS NULL THEN
        RETURN false;
    END IF;
    -- кратчайшее окно, где есть каждый ключ: правый край идёт по событиям, левый
    -- подтягивается, пока все ключи на месте; одно слово — где угодно в тексте
    cnt := array_fill(0, ARRAY[n]);
    FOR r IN 1 .. cardinality(ev) LOOP
        j := mod(ev[r], 256);
        IF cnt[j] = 0 THEN
            have := have + 1;
        END IF;
        cnt[j] := cnt[j] + 1;
        WHILE have = n LOOP
            IF n = 1 OR ev[r] / 256 - ev[l] / 256 <= span THEN
                RETURN true;
            END IF;
            j := mod(ev[l], 256);
            cnt[j] := cnt[j] - 1;
            IF cnt[j] = 0 THEN
                have := have - 1;
            END IF;
            l := l + 1;
        END LOOP;
    END LOOP;
    RETURN false;
END $$;
