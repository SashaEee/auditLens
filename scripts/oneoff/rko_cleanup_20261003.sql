-- Разовая чистка РКО (аудит 03.10, ПЛТ-02). Старый ключ тарифа (rko_…) склеивал
-- разные тарифы банка под общим алиасом sravni, и каждую ночь в историю
-- писались «изменения» вида 9900 → 690 → 1990 → 3500 → 9900. История таких
-- офферов — смесь разных тарифов, отличить в ней настоящее изменение цены
-- нельзя. Шаг 1 копирует её в резервные таблицы и удаляет из журнала; версии
-- условий (product_terms) не трогаются. Шаг 2 — после принудительного сбора
-- sravni_rko с новым ключом (rko2_…): гасит старые карточки, только если новых
-- пришло достаточно. Запуск: psql -v ON_ERROR_STOP=1 -f … (вне окна сбора 05:00),
-- шаг 1 — только ПОСЛЕ выкладки кода с ключом rko2_ (иначе ночной сбор старым
-- кодом снова напишет «пилу» под старыми ключами).
--
-- Откат шага 1: INSERT INTO change_history SELECT * FROM bak20261003_rko_changes
--               ON CONFLICT DO NOTHING;
-- Откат шага 2: UPDATE product_offer o SET is_active = b.is_active
--               FROM bak20261003_rko_offer b WHERE b.offer_id = o.offer_id;

\echo == шаг 1: резервная копия и удаление истории старых ключей
SET lock_timeout = '10s';
BEGIN;
LOCK TABLE change_history IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE IF NOT EXISTS bak20261003_rko_offer AS
    SELECT o.* FROM product_offer o WHERE o.category = 'rko' LIMIT 0;
INSERT INTO bak20261003_rko_offer
    SELECT o.* FROM product_offer o WHERE o.category = 'rko'
       AND NOT EXISTS (SELECT 1 FROM bak20261003_rko_offer b WHERE b.offer_id = o.offer_id);
-- копия дописывается при каждом запуске: повтор шага 1 не удаляет то, чего
-- в копии нет
CREATE TABLE IF NOT EXISTS bak20261003_rko_changes AS
    SELECT ch.* FROM change_history ch LIMIT 0;
INSERT INTO bak20261003_rko_changes
    SELECT ch.* FROM change_history ch JOIN product_offer o USING (offer_id)
     WHERE o.category = 'rko' AND left(o.external_id, 4) = 'rko_'
       AND NOT EXISTS (SELECT 1 FROM bak20261003_rko_changes b WHERE b.change_id = ch.change_id);
SELECT count(*) AS backed_up_changes FROM bak20261003_rko_changes;
DELETE FROM change_history ch USING product_offer o
 WHERE ch.offer_id = o.offer_id AND o.category = 'rko' AND left(o.external_id, 4) = 'rko_';
COMMIT;
