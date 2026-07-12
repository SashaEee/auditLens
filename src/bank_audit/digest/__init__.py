"""Ежедневный дайджест вкладки «Обзор» — утренний брифинг аудитора.

Принцип: один выпуск в день на всех (~50 юзеров читают один кэш из Postgres).
Числа — детерминированные SQL-агрегаты; LLM только формулирует (3 вызова/день).
Секции независимы: падение одной = деградация секции, не страницы.

Модули:
  store.py      — daily_digest/digest_run CRUD + advisory lock (stampede)
  aggregator.py — SQL-секции (0 токенов): reviews_pulse, tariff_moves, quality_ops
  news.py       — сбор новостей: RSS ЦБ/banki.ru/frankmedia + t.me/s/-превью +
                  SearXNG; дедуп; окно 48 ч; ключевая ставка (SOAP ЦБ)
  writer.py     — LLM-секции: reviews_brief, news, headline (+insights)
  pipeline.py   — run_daily: реестр секций, per-section timeout + copy_forward
  scheduler.py  — фоновые циклы: дайджест 07:00 МСК + автосбор тарифов 06:00
"""
