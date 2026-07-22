# auditlens-data: прямой доступ к БД AuditLens

Полный доступ к данным платформы командой alsql (обёртка psql):
    alsql "SELECT ..."          # или через stdin: echo "SELECT ..." | alsql
Можно всё смотреть, добавлять и править (SELECT/INSERT/UPDATE, \\d-метакоманды).
DROP/TRUNCATE/DELETE/ALTER отклоняются — это правило владельца, не пытайся обойти.

## Как работать
1. Не знаешь схему — посмотри сам: `\dt` (таблицы), `\d имя_таблицы` (колонки), и запомни
   выводы в память — в следующий раз не придётся.
2. Ключевые таблицы: bank (bank_id, name, is_sber), product_offer (офферы банков:
   category ∈ deposit|credit|mortgage|card_credit|card_debit|auto_loan, rate_pct, is_current),
   change_history (изменения тарифов), daily_digest (digest_date, section, payload jsonb —
   утренний брифинг), v_sber_vs_market (представление Сбер против рынка).
3. Проверенные примеры:
   - топ ставок по вкладам сейчас:
     SELECT b.name, o.rate_pct, o.title FROM product_offer o JOIN bank b USING (bank_id)
     WHERE o.category='deposit' AND o.is_current AND o.rate_pct IS NOT NULL
     ORDER BY o.rate_pct DESC LIMIT 10;
   - Сбер vs рынок: SELECT * FROM v_sber_vs_market ORDER BY category;
4. Всегда указывай в ответе, из какой таблицы/представления цифры и за какой период.
