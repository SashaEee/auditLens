-- 035: дедуп витрины «Рынок» — банки-двойники и повторы одного продукта.
-- Аудит 11.08.2026 показал: 1378 лишних строк офферов (1177 из них во вкладах),
-- один банк живёт в справочнике под двумя записями («ВБРР» и «Банк ВБРР»,
-- «АГОРА» и «Банк «АГОРА»»), и атлас считает его двумя точками рынка — это
-- искажает и медиану, и знаменатель ранга.
-- NB: одиночного знака процента в файле быть не должно нигде, включая
-- комментарии: миграции идут через exec_driver_sql, который принимает его за
-- начало placeholder. В LIKE-шаблонах знак удваиваем.

-- ── 1. Слияние банков-двойников ──────────────────────────────────────────────
-- Каноническим считаем банк с осмысленным слагом (не unknown), при равенстве —
-- с наибольшим числом офферов; остальные записи переносим на него.
CREATE OR REPLACE FUNCTION _norm_bank_name(s TEXT) RETURNS TEXT AS $$
  SELECT lower(regexp_replace(
           regexp_replace(
             regexp_replace(coalesce(s, ''), '[«»""''()]', ' ', 'g'),
             '^(пао|оао|ао|зао|ооо|кб|акб)\s+|^банк\s+|\s+банк$', '', 'gi'),
           '[^[:alnum:]]', '', 'g'));
$$ LANGUAGE sql IMMUTABLE;

WITH grp AS (
    SELECT _norm_bank_name(name) AS k, bank_id, slug,
           (SELECT count(*) FROM product_offer o WHERE o.bank_id = b.bank_id) AS n_offers
      FROM bank b
     WHERE _norm_bank_name(name) <> ''
), ranked AS (
    SELECT k, bank_id,
           row_number() OVER (PARTITION BY k
                              ORDER BY (slug NOT LIKE 'unknown!_%%' ESCAPE '!') DESC,
                                       n_offers DESC, bank_id) AS rn
      FROM grp
), pairs AS (
    SELECT r.bank_id AS dup_id, c.bank_id AS keep_id
      FROM ranked r JOIN ranked c ON c.k = r.k AND c.rn = 1
     WHERE r.rn > 1
)
UPDATE product_offer o SET bank_id = p.keep_id
  FROM pairs p WHERE o.bank_id = p.dup_id
    -- у канонического банка уже может быть такой же оффер: тогда дубль
    -- останется на месте и его схлопнет шаг 2 (перенос упёрся бы в уникальный
    -- ключ bank_id+category+external_id)
    AND NOT EXISTS (SELECT 1 FROM product_offer x
                     WHERE x.bank_id = p.keep_id AND x.category = o.category
                       AND x.external_id = o.external_id);

-- ── 2. Схлопывание повторов одного продукта ─────────────────────────────────
-- Один вклад собирается семью таргетами (регионы и суммы), и каждый срез даёт
-- свой external_id. Для сравнения это один и тот же продукт: оставляем самую
-- свежую версию, прочие гасим (история версий у них сохраняется).
WITH live AS (
    SELECT o.offer_id, o.bank_id, o.category,
           lower(regexp_replace(coalesce(o.title, ''), '[^[:alnum:]]', '', 'g')) AS k,
           t.valid_from
      FROM product_offer o
      JOIN product_terms t ON t.offer_id = o.offer_id AND t.valid_to IS NULL
     WHERE o.is_active
), ranked AS (
    SELECT offer_id,
           row_number() OVER (PARTITION BY bank_id, category, k
                              ORDER BY valid_from DESC, offer_id DESC) AS rn
      FROM live WHERE k <> ''
)
UPDATE product_offer o SET is_active = false
  FROM ranked r WHERE o.offer_id = r.offer_id AND r.rn > 1;
