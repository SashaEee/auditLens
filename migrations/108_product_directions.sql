-- Направления аудита в фильтре продукта «Аудита отзывов» (04.11.2026): want = 'dir:<код>'
-- — продукты ИЛИ особые срезы направления (src/bank_audit/directions.py). Фильтр продукта
-- стоит во всех запросах вкладки, сигналов, выгрузок и подписок, поэтому направление
-- работает везде сразу. Текст функции генерирует directions.product_fn_sql(), тест
-- сверяет его с этой миграцией.
SET lock_timeout = '3s';
CREATE OR REPLACE FUNCTION al_product_is(product text, slices text[], want text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT product = want
        OR (want = 'Зарплатные проекты' AND 'payroll' = ANY(coalesce(slices, '{}')))
        OR (want = 'УС — банкоматы и терминалы' AND 'us' = ANY(coalesce(slices, '{}')))
        OR (want = 'Выездной сервис — курьеры и выезд' AND 'field' = ANY(coalesce(slices, '{}')))
        OR (want = 'Самозанятые и «Своё дело»' AND 'selfemp' = ANY(coalesce(slices, '{}')))
        OR (want = 'Дети и молодёжь' AND 'youth' = ANY(coalesce(slices, '{}')))
        OR (want = 'Участники СВО' AND 'svo' = ANY(coalesce(slices, '{}')))
        OR (want = 'dir:mortgage' AND (product = ANY(ARRAY['Ипотека'])))
        OR (want = 'dir:metals' AND (product = ANY(ARRAY['Драгметаллы и ОМС'])))
        OR (want = 'dir:field' AND (coalesce(slices, '{}') && ARRAY['field']))
        OR (want = 'dir:youth' AND (coalesce(slices, '{}') && ARRAY['youth']))
        OR (want = 'dir:selfemp' AND (coalesce(slices, '{}') && ARRAY['selfemp']))
        OR (want = 'dir:credit_card' AND (product = ANY(ARRAY['Кредитная карта'])))
        OR (want = 'dir:acquiring' AND (product = ANY(ARRAY['Бизнес: эквайринг'])))
        OR (want = 'dir:atm' AND (coalesce(slices, '{}') && ARRAY['us']))
        OR (want = 'dir:invest' AND (product = ANY(ARRAY['Инвестиции'])))
        OR (want = 'dir:consumer' AND (product = ANY(ARRAY['Потребительский кредит', 'Рассрочка'])))
        OR (want = 'dir:svo' AND (coalesce(slices, '{}') && ARRAY['svo']))
        OR (want = 'dir:payroll' AND (coalesce(slices, '{}') && ARRAY['payroll']))
$$;
