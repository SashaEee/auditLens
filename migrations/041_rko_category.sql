-- 041: расчётно-кассовое обслуживание как отдельная полка витрины.
--
-- Значение enum добавляем guarded-блоком: ALTER TYPE ... ADD VALUE нельзя
-- выполнять внутри транзакции повторно, а миграции у нас идут пачкой.
-- NB: одиночного знака процента в файле быть не должно (exec_driver_sql).

DO $$ BEGIN
  ALTER TYPE product_category ADD VALUE IF NOT EXISTS 'rko';
EXCEPTION WHEN others THEN NULL;
END $$;
