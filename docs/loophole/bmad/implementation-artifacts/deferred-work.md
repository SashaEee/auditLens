- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-loophole-refresh-button.md`
  summary: Глобальная кнопка ⟳ топбара остаётся no-op на всех страницах, кроме «Лазеек» — оживить (ремаунт по тику / refetch данных) при появлении потребности.
  evidence: Пользователь явно сузил скоуп фикса до страницы «Лазейки» («правь только участок для модуля лазеек»); ревью 2026-08-26 подтвердило, что `onClick` на прочих страницах вычисляется в `false` и контрол вводит в заблуждение (title «Обновить страницу»).

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Интегрировать реальный AI-чат с `ResearchCaseService`: создание research/revision/source/candidate вместо прямой записи только в legacy `loophole_record`.
  evidence: Независимые blind/verification-gap ревью 2026-08-30 подтвердили, что `start_research`, `collect_research` и `classify_candidates` не вызываются из chat runtime; frozen boundary текущей спеки требует Ask First для snapshot-публикации.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Связать candidate submission, неизменяемые verification snapshots, экспертную очередь, решение и идемпотентную публикацию в один production lifecycle.
  evidence: `/research/candidates/{id}/submit` создаёт snapshot, но legacy `/queue` его не читает, UI отправляет решения через `/records/verdict`, а `publish_decision()` не имеет production caller; это отдельная Story 2, запрещённая текущей UI-спекой без согласования.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Провести отдельный RBAC-аудит legacy `/records`, `/records/{id}/content`, `/records/verdict` и старых экспортов, чтобы опубликованный каталог нельзя было обойти и менять обычному участнику.
  evidence: Blind review 2026-08-30 обнаружил чтение записей любых статусов и возможность менять вердикт через legacy API; существующие manual-mark контракты разрешают это поведение, поэтому изменение прав выходит за утверждённую адаптацию макета.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Завершить доменный контракт публикации: перенос URL, банка, доказательств и их ревизий, дат, модели и confidence; исключить опубликованный объект из legacy-очереди.
  evidence: Review 2026-08-30 нашёл пустой URL, подмену raw_text описанием, отсутствие evidence/published_at и повторное попадание записи с `verdict_model=NULL` в `list_verification_queue()`; это относится к snapshot-публикации вне текущего scope.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Исправить PostgreSQL/SQLite расхождения при чтении JSONB во всех research, scheduled analytics, Telegram targets и checkpoint путях.
  evidence: Blind review 2026-08-30 выявил `json.loads()` над уже декодированными psycopg dict/list и невалидный `str(dict)` checkpoint; offline SQLite-набор эти production ошибки не воспроизводит.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Усилить конкурентную идемпотентность submit/decision/publication и неизменяемость первой классификации кандидата.
  evidence: Review 2026-08-30 зафиксировал SELECT-before-INSERT гонки, необработанные unique conflicts, непроверенный command_key, повторную перезапись model_classified_at и незаполненный `candidate.ccks_decision`; это отдельный lifecycle-проект.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Ввести строгую схему LLM/Telegram payload для boolean и диапазонов вместо `bool(value)`.
  evidence: Строковое `"false"` сейчас становится `True` в research collect и Telegram parser; исправление затрагивает контракты внешних payload, не UI-макет.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Исправить Windows init-db: `ensure_vector.sql` и `analytics/views.sql` должны выполняться повторно, а определение `v_sber_vs_market` — не откатываться на старый источник.
  evidence: Blind/verification-gap review 2026-08-30 подтвердил, что общий `apply_sql()` журналирует repeatable SQL, в отличие от `docker/entrypoint.sh`, а поздний `views.sql` перезаписывает изменение миграции 017.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Провести отдельное укрепление аналитики: lowercase empty columns, фактическое использование readonly-роли, атомарный multi-worker claim, изоляция ошибок задач и корректная агрегация named query.
  evidence: Blind/verification-gap review 2026-08-30 обнаружил регистрозависимый `split("FROM")`, выполнение application-сессией, process-local lock, остановку всего run_due одной ошибкой и неагрегированный `published_cases_by_bank`; эти файлы не входят в Code Map текущей спеки.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Довести отчётные CSV/XLSX/PDF экспорты по фильтрам: две даты, отсутствие Trust, явный лимит без молчаливого усечения, streaming/лимиты памяти, formula injection и безопасный HTML/PDF без сетевых side effects.
  evidence: Review 2026-08-30 выявил расхождения в `/export/csv`, XLSX и PDF; текущая спека меняет только мгновенный выборочный `POST /export`, а отчётные маршруты требуют отдельного согласованного контракта.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Добавить `loophole/static/*` и `loophole/chat/prompt/*.md` в wheel package-data и проверить собранный wheel.
  evidence: Blind review 2026-08-30 отметил, что editable/Docker путь работает, но `pyproject.toml` перечисляет только общую web-статику и analytics SQL; packaging не входит в текущий визуальный scope.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Завершить production Telegram worker deployment: реальный entrypoint/loop/transport, корректные env-имена секретов и фактический TLS verify-full в драйвере.
  evidence: Review 2026-08-30 нашёл несуществующий `auditlens-telegram-worker`, ключи `database-url`/`telegram-session`, не создающие ожидаемые env, и неиспользуемые `DATABASE_SSLMODE`/`DATABASE_CA_FILE`; Telegram lifecycle явно Ask First.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Усилить DB-perimeter Telegram worker: fencing владельца/цели, атомарный attempt id, сквозную дедупликацию accepted/quarantine, серверную валидацию sanitization и работоспособную retention-role.
  evidence: Blind review 2026-08-30 выявил гонки lease/attempt, раздельные unique indexes, доверие произвольному sanitized JSONB и trigger по `session_user` для NOLOGIN-роли; исправление требует отдельной threat-model спеки.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Сделать PostgreSQL/staging evidence обязательным release gate для lifecycle, Telegram SECURITY DEFINER-функций, ролей, триггеров и конкурентных сценариев.
  evidence: Verification-gap review 2026-08-30 подтвердил, что behavioral тесты идут через SQLite, production adapter проверяется текстом, `UNVERIFIED` считается допустимым и подписанного staging evidence/gate нет.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-adapt-loophole-ui-to-final-mockup.md`
  summary: Покрыть FastAPI lifespan caller-path для scheduled analytics, чтобы env-флаг гарантированно создавал background task и выполнял due-контракт.
  evidence: Verification-gap review 2026-08-30 нашёл только прямые service-тесты `run_due()`; удаление запуска `scheduled_analytics_loop()` из lifespan не ломает текущий набор.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Согласовать статус незавершённой работы со списком deferred-work, чтобы `STATUS_RESTART.md` не объявлял модуль завершённым при открытых долгах.
  evidence: Blind review 2026-08-31 обнаружил противоречие между заявлением об отсутствии незавершённых историй и существующим реестром отложенных задач.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Довести новый lifecycle research cases до единого production-потока и исключить параллельное использование legacy graph/queue API.
  evidence: Blind review 2026-08-31 нашёл прямое сохранение legacy-графа, старую очередь и membership-only API, которые обходят snapshots, case decisions и новый RBAC-контракт.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Устранить sparse-публикации и неоднозначное повторное попадание опубликованной записи в legacy очередь.
  evidence: Blind review 2026-08-31 показал, что публикация создаёт неполную запись и не обеспечивает единый терминальный статус между новым и старым контурами.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Нормализовать чтение JSONB, строгий разбор boolean и конкурентную идемпотентность команд research cases.
  evidence: Blind review 2026-08-31 выявил `json.loads` для уже декодированных значений, истинность строки `false`, select-before-insert гонки и неоднозначный поиск решения по command key.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Исправить контракты аналитических запросов и обеспечить атомарный DB-claim для scheduled analytics с изоляцией ошибок отдельных заданий.
  evidence: Blind review 2026-08-31 нашёл хрупкий lower-case разбор колонок, несоответствие группировки тексту запроса, process-local lock и остановку batch при одной ошибке.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Усилить безопасность экспортов: CSV formula sanitization, потоковую обработку больших выборок и экранирование HTML при PDF-рендеринге без внешних шрифтов.
  evidence: Blind review 2026-08-31 обнаружил разные контракты безопасного CSV между путями экспорта, сборку результата в памяти и неэкранированные значения в PDF HTML.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Восстановить безопасную доставку миграций и repeatable SQL без перезаписи уже выпущенных файлов и без конфликтующего применения views.sql.
  evidence: Blind review 2026-08-31 выявил журналирование repeatable-скриптов в setup.ps1, перекрытие исправления миграции 017 аналитической view и изменения старых миграций, которые существующие БД повторно не применят.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Включить статику и prompts модуля «Лазейки» в package data и описать работоспособный deployment entrypoint/env-контракт Telegram worker.
  evidence: Blind review 2026-08-31 нашёл неполный package-data контракт и конфигурацию worker, не создающую ожидаемые CLI/env параметры.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Закрыть DB-perimeter Telegram worker: права reaper, fencing владельца, атомарные attempt id, сквозную дедупликацию и серверную проверку sanitization.
  evidence: Blind review 2026-08-31 выявил расхождения ролей/триггеров, гонки lease и journal, раздельную уникальность accepted/quarantine и доверие входному sanitized JSONB.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-remove-telegram-target-status-card.md`
  summary: Сделать perimeter proof Telegram-пути подписанным, проверяемым и блокирующим выпуск при устаревших или отсутствующих evidence.
  evidence: Blind review 2026-08-31 обнаружил, что текстовый proof-файл можно заменить или оставить устаревшим без автоматической проверки происхождения и актуальности.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-fix-agent-clarification-loop-and-latency.md`
  summary: Спроектировать идемпотентный обмен clarification/execution token, устойчивый к потере HTTP-ответа, разрыву SSE и повторной доставке.
  evidence: Ревью 2026-08-31 подтвердило, что одноразовые in-memory token поглощаются до подтверждённого клиентом запуска; полноценное исправление меняет token-хранилище и по frozen-границе требует отдельного согласования.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-fix-agent-clarification-loop-and-latency.md`
  summary: Ввести серверную схему и привязку clarification-ответа к фактически выданному вопросу, типу и допустимым options.
  evidence: Ревью 2026-08-31 показало, что route принимает нетипизированный `list[dict]`, произвольные значения и неограниченные поля; строгая привязка требует расширения server-side challenge state.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-fix-agent-clarification-loop-and-latency.md`
  summary: Сделать сохранение находок в async graph неблокирующим и не выдавать несохранённые pending findings как persisted records.
  evidence: Blind review 2026-08-31 выявил синхронный fetch/save в async-path, обрыв SSE при ошибке repository и fallback на `hook.records`, когда persistence вернул пустой результат.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-fix-agent-clarification-loop-and-latency.md`
  summary: Унифицировать безопасность и лимиты CSV/XLSX/PDF-экспорта опубликованного каталога.
  evidence: Blind review 2026-08-31 обнаружил spreadsheet-injection в filtered CSV/XLSX, лишнюю загрузку full content для XLSX и молчаливое обрезание PDF на 10000 строк; эти пути не относятся к исправлению чата.

- source_spec: `docs/loophole/bmad/implementation-artifacts/spec-fix-agent-clarification-loop-and-latency.md`
  summary: Добавить route-level интеграционный тест фильтров `/catalog` на published, draft и `is_loophole=false`.
  evidence: Verification-gap review 2026-08-31 подтвердило, что текущие тесты проверяют repository и статическую строку JSX, но не фактическую комбинацию route-фильтров.

