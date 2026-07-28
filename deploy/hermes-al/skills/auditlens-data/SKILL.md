# auditlens-data: прямой доступ к БД AuditLens

Полный доступ к данным платформы командой alsql (обёртка psql):
    alsql "SELECT ..."          # или через stdin: echo "SELECT ..." | alsql
Можно всё смотреть, добавлять и править (SELECT/INSERT/UPDATE, \d-метакоманды).
DROP/TRUNCATE/DELETE/ALTER отклоняются — это правило владельца, не пытайся обойти.

## Как работать
1. Не знаешь схему — посмотри сам: `\dt` (таблицы), `\d имя_таблицы` (колонки).
2. Ключевые таблицы: bank (bank_id, name, is_sber), product_offer (офферы:
   category ∈ deposit|credit|mortgage|card_credit|card_debit|auto_loan, is_active),
   product_terms (УСЛОВИЯ И СТАВКИ: rate_pct, valid_to IS NULL = действующие),
   change_history (изменения тарифов: diff jsonb, changed_at),
   daily_digest (digest_date, section, payload jsonb — утренний брифинг),
   v_sber_vs_market (готовое сравнение Сбер против рынка по категориям).
3. Проверенные примеры:
   - топ ставок по вкладам сейчас:
     SELECT b.name, t.rate_pct, o.title FROM product_terms t
     JOIN product_offer o USING (offer_id) JOIN bank b USING (bank_id)
     WHERE o.category='deposit' AND o.is_active AND t.valid_to IS NULL
       AND t.rate_pct IS NOT NULL ORDER BY t.rate_pct DESC LIMIT 10;
   - Сбер vs рынок: SELECT * FROM v_sber_vs_market ORDER BY category;
4. Всегда указывай в ответе, из какой таблицы/представления цифры и за какой период.

## Чего в ЭТОЙ БД НЕТ (не ищи зря)
- Жалоб/отзывов клиентов. Таблица review здесь — легаси-огрызок ~1.6k строк,
  НЕ источник. Настоящие 390 тыс. отзывов — только через скилл reviews-api
  (curl http://127.0.0.1:8000/api/reviews/...). Вопросы про жалобы → сразу туда.
