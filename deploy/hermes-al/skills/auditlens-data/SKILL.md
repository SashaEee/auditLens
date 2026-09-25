---
name: auditlens-data
description: "Use when ready AuditLens tools are not enough and you need SQL over the AuditLens database (tariff history, key rate, documents, news, «Уязвимости» records) — schema, traps, alsql."
version: 2.0.0
author: AuditLens
license: proprietary
metadata:
  hermes:
    tags: [auditlens, sql, database, schema, loopholes]
    related_skills: [auditlens-complaints, auditlens-market, auditlens-research]
---

# База AuditLens: схема и ловушки

## Overview
Сначала — готовые инструменты `mcp_auditlens_*`: они считают так же, как
вкладки. SQL — когда вопрос нестандартный: история изменения тарифа,
ключевая ставка по датам, выборка по базе знаний, записи «Уязвимостей».

Два пути:
- `mcp_auditlens_sql(query)` — SELECT, транзакция только для чтения, JSON до
  200 строк. Предпочтительно: без экранирования и разбора вывода.
- `alsql` в терминале — psql-обёртка, можно SELECT/INSERT/UPDATE (DELETE, DROP,
  ALTER, TRUNCATE запрещены владельцем). Вызов: `echo "SQL" | /root/.hermes/bin/alsql`
  (флаг `-c` не работает). Вывод: заголовок, разделитель, строки.

Схема `auditlens` (search_path уже настроен).

## When to Use
- Динамика условий продукта во времени, «когда Сбер менял ставку».
- Ключевая ставка ЦБ на дату, спред к ней.
- Статистика по разделу «Уязвимости» (лазейки, схемы, найденные агентом).
- Don't use for: числа жалоб (только инструменты жалоб), ранг на рынке
  (`market_position`).

## Таблицы
| Таблица | Что | Ключевые поля |
|---|---|---|
| `bank` | банки | bank_id, slug, name, is_sber, region, assets_rank |
| `product_offer` | продукт банка | offer_id, bank_id, category (enum), title, url, is_active, segment |
| `product_terms` | условия (SCD2) | offer_id, valid_from, **valid_to IS NULL = действуют**, rate_pct, rate_kind, psk_min/psk_max, amount_*, term_months_*, fee_open, fee_service, grace_days, cashback_pct, conditions, raw |
| `change_history` | изменения условий | offer_id, changed_at, diff (jsonb: было/стало) |
| `v_market_rub_offer` | витрина «Рынка» (рублёвые, действующие) | bank_name, is_sber, category, title, rate_pct, psk_min, term_bucket, segment, url |
| `v_sber_vs_market` | Сбер против рынка по категориям | sber_max, sber_min, market_median, sber_vs_median_pp |
| `cbr_key_rate` | ключевая ставка | rate_date, rate |
| `document`, `document_chunk` | база знаний | document_id, bank_id, url, title, doc_type, content_text, fetched_at; chunk: idx, text |
| `news_item` | лента новостей | ts, source, title, body, value (важность 0–10), s2 (jsonb: summary, sber, idea, request) |
| `daily_digest` | выпуски «Обзора» | digest_date, section (headline, news, reviews_pulse, reviews_brief, tariff_moves, update), payload jsonb |
| `loophole_record` | записи «Уязвимостей» | record_id, title, snippet, raw_text, url, domain, bank_slug, keyword, is_loophole, verdict_confidence, verdict_reason, classification, status, collected_at, published_at |

category (enum): deposit, savings_account, credit, mortgage, card_credit,
card_debit, auto_loan, rko, microloan, refinance, business_loan, acquiring,
insurance_*, invest_*, … Сбер: `bank.is_sber` или `slug='sberbank'`.

## «Уязвимости» (лазейки)
Записи собирает и классифицирует агент модуля «Уязвимости»:
`is_loophole = true` — модель признала описание лазейкой/схемой,
`verdict_confidence` и `verdict_reason` — её уверенность и обоснование,
`status='preliminary'` — ещё не проверено человеком (сейчас так у всех).
Период — по `collected_at`; Сбер — `bank_slug='sberbank'` (у многих bank_slug
пуст, банк виден только в тексте). В ответе всегда оговаривай, что оценки
предварительные, и давай ссылку на страницу [Уязвимости](#loophole).

## Common Pitfalls
1. Таблицы `review`, `review_topic*`, `review_sentiment` — старая разметка,
   числа не совпадут с «Отзывами». Жалобы — только инструментами.
2. Без `valid_to IS NULL` получишь всю историю условий вместо действующих.
3. Категории — только значениями enum (`card_credit`, не `credit_card`).
4. `rate_pct` у кредитов — рекламная ставка «от»; сравнение — по `psk_min`.
5. Пустой результат ≠ «у банка нет продукта»: витрина собирает не всё.

## Verification Checklist
- [ ] Фильтр по действующим условиям и активным продуктам, где нужно.
- [ ] Число в ответе — из результата запроса, с периодом.
- [ ] Для «Уязвимостей» — оговорка «предварительные оценки модели».
