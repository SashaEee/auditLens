-- Смысловой поиск на витрине «Рынок».
--
-- Аудиторы писали об одном и том же восемь раз: поиск идёт по формам слов, а
-- не по смыслу. «Детская карта» находит продукт у трёх банков и не находит у
-- четвёртого, потому что там он называется иначе; «выдача дебетовой карты»
-- распадается на отдельные слова. Подстрока и полнотекст остаются — они
-- дополняют друг друга и дают точные попадания, — но к ним добавляется
-- вектор: карточка ищется по названию, банку, категории и условиям.
ALTER TABLE product_offer ADD COLUMN IF NOT EXISTS embedding vector(1024);
ALTER TABLE product_offer ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;

-- Полнотекст по названию и условиям: отдельной колонкой, чтобы выражение не
-- пересчитывалось на каждый запрос и работал индекс.
ALTER TABLE product_offer ADD COLUMN IF NOT EXISTS search_tsv tsvector;

CREATE INDEX IF NOT EXISTS idx_product_offer_tsv
    ON product_offer USING GIN (search_tsv);

-- Список предложений небольшой (тысячи), поэтому точный перебор дешевле
-- приблизительного индекса и не требует обучения списков.
CREATE INDEX IF NOT EXISTS idx_product_offer_embedded
    ON product_offer (embedded_at) WHERE embedding IS NOT NULL;
