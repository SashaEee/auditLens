-- Срезы «УС» (устройства самообслуживания — банкоматы и терминалы) и «Выездной
-- сервис» в списке продуктов «Аудита отзывов» (обращения 08–09.10). Это не продукты,
-- а места, где случилась проблема: канал из LLM-разметки (банкомат / курьер и
-- выезд к клиенту) или проблема «Наличные и банкоматы». Поэтому срез — псевдо-продукт
-- с тем же параметром product, а условие одно на все запросы вкладки:
-- al_product_is(...) вместо i.product = :product. Названия — reviews_dash.PSEUDO_PRODUCTS.

-- каналы из разметки (review_annotate.apply_to_index переносит их вместе с kind/issue)
ALTER TABLE review_index ADD COLUMN IF NOT EXISTS channels text[];

-- SQL-функция без побочных эффектов — планировщик подставляет её тело в запрос
CREATE OR REPLACE FUNCTION al_product_is(product text, channels text[], issue text, want text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT product = want
        OR (want = 'УС — банкоматы и терминалы'
            AND ('atm' = ANY(coalesce(channels, '{}')) OR issue = 'cash_atm'))
        OR (want = 'Выездной сервис — курьеры и выезд'
            AND 'courier' = ANY(coalesce(channels, '{}')))
$$;
