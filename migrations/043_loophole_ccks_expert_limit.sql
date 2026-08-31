-- 043: preflight лимита активных экспертов ЦК КС (story 1.5).
--
-- Greenplum 6 не поддерживает пользовательские triggers. Межпроцессная
-- сериализация назначения выполняется server-side через advisory lock в
-- authorization.grant_ccks_expert; миграция только fail-closed проверяет
-- уже нарушенные данные.
DO $$
BEGIN
    IF (
        SELECT COUNT(*) > 5
          FROM loophole_role_assignment
         WHERE role = 'ccks_expert' AND status = 'active'
    ) THEN
        RAISE EXCEPTION
            'Нельзя применить migration 043: активных экспертов ЦК КС больше 5';
    END IF;
END;
$$;