---
stepsCompleted: ['step-01-load-context', 'step-02-discover-tests', 'step-03-map-criteria', 'step-04-analyze-gaps', 'step-05-gate-decision']
lastStep: 'step-05-gate-decision'
lastSaved: '2026-08-31'
coverageBasis: 'acceptance_criteria'
oracleConfidence: 'high'
oracleResolutionMode: 'formal_requirements'
oracleSources:
  - 'docs/loophole/bmad/implementation-artifacts/spec-*.md'
externalPointerStatus: 'not_used'
collectionStatus: 'COLLECTED'
sourceSha: '0e17177799d123ebc01995bc743cdffbfe8357ed'
tempCoverageMatrixPath: 'docs/loophole/bmad/test-artifacts/traceability/phase-1-coverage-20260831.json'
---

# Матрица требований и тестов модуля «Лазейки»

## Основание проверки

Источник требований — 28 спецификаций `spec-*.md` из implementation-artifacts. Для этого отчёта
история считается реализованной только при наличии связанного набора тестов, который проходит в
текущем рабочем дереве. Статус в frontmatter, старый лог или наличие исходного кода сами по себе
не считаются доказательством.

## Граница отчёта

- Включены: спецификации 1.1–6.5, заявка на разработку парсера, перенос предварительных
  источников и читаемый отчёт AI-исследования.
- Не включены: комментарии в production-коде, коммиты и изменение поведения приложения.
- Дополнительное доказательство: короткая browser-проверка только для уже доступной UI-поверхности;
  она не заменяет тестовый результат.

## Текущий этап

Сопоставление каждого критерия завершено. Статусы ниже опираются на свежий полный pytest-прогон
и адресное чтение тестов/кода; следующая фаза отдельно проанализирует пробелы и примет gate-решение.

## Обнаруженные тесты и свежий результат

- Найдено 70 файлов `test_*.py` модуля `tests/loophole`.
- Не использовался манифест ручной live-верификации: статус `COLLECTED` опирается на исполнимый
  pytest-набор.
- Свежий прогон: `.venv\\Scripts\\python.exe -m pytest tests\\loophole -q -p no:cacheprovider`
  с изолированным `--basetemp`.
- Результат: **722 passed, 1 skipped, 21 warnings** за 2 минуты 2 секунды.

### Инвентарь связанного покрытия

| Группа требований | Основные тесты |
|---|---|
| Рабочие контексты и UI (1.1–1.5) | `test_workspace.py`, `test_authorization.py`, `test_iframe_shell_theme.py`, `test_adaptive_context_routes.py`, `test_accessible_states_feedback.py`, `test_admin_roles_audit.py`, `test_final_layout_*.py` |
| Агент исследования (2.1–2.5) | `test_story_2_1_*.py`, `test_story_2_2_research_cases.py`, `test_story_2_3_config.py`, `test_story_2_4_research_progress.py`, `test_story_2_5_submission_route.py` |
| Жизненный цикл кейсов (3.1–3.3) | `test_db_schema_048.py`–`test_db_schema_051.py`, `test_manual_mark.py`, `test_story_2_5_submission_route.py` |
| Каталог, экспорт и аналитика (4.1–4.4) | `test_story_4_1_published_catalog.py`–`test_story_4_4_scheduled_analytics.py`, `test_db_schema_052.py`, `test_db_schema_053.py` |
| Обычные парсеры (5.1–5.3) | `test_parsers_*.py`, `test_parsers_web.py` |
| Telegram (6.1–6.5) | `test_story_6_1_target_registry.py`–`test_story_6_5_telegram_perimeter.py` |
| Отчёт исследования | `test_research_report_export.py`, `test_story_2_1_routes.py` |

### Проверка эвристик покрытия

- Авторизация, ownership и отрицательные сценарии присутствуют в тестах workspace, route, доступа,
  публикации и Telegram.
- Ошибки источников, валидация и fallback-пути присутствуют в историях 2.x, 4.x, parser- и
  Telegram-наборах.
- UI-journey и доступные состояния проверяются статическими и browser-runtime тестами модуля.
- Для историй 3.x, заявки на разработку парсера и переноса предварительных источников требуется
  адресно подтвердить каждый критерий приёмки на следующем шаге, а не выводить готовность только
  из имён файлов.

## Детальная трассировка критериев

**Легенда:** `FULL` — все критерии истории имеют свежую исполнимую проверку; `PARTIAL` — часть
поведения проверена, но хотя бы один критерий не имеет достаточного доказательства; `NONE` —
нет реализации/исполняемых тестов; `SUPERSEDED` — более позднее принятое требование пользователя
намеренно заменило исходный критерий. Уровни: `API` — маршрут/сервис, `UNIT` — изолированная
логика или DDL-контракт, `BROWSER` — browser-runtime.

### 1. Рабочие контексты и интерфейс

| Критерий | Свежая тестовая трасса | Уровень | Покрытие |
|---|---|---|---|
| 1.1-A — member видит только общую базу и исследование | `test_authorization.py::test_contexts_member_gets_catalog_and_research` | API | FULL |
| 1.1-B — эксперт дополнительно видит очередь | `test_authorization.py::test_contexts_expert_also_gets_queue` | API | FULL |
| 1.1-C — прямой доступ без роли не раскрывает очередь | `test_authorization.py::test_queue_member_without_role_403_no_protected_data`, `::test_queue_deny_writes_redacted_audit` | API | FULL |
| 1.1-D/E — отзыв роли и недостоверная identity fail-closed до workspace | `test_authorization.py::test_revoked_role_denies_next_request`, `::test_contexts_without_principal_401`, `::test_contexts_authenticated_without_membership_403`, `::test_workspace_not_created_before_authorization` | API | FULL |
| 1.2-A — только self-hosted vendor и без CDN/hex-палитр | `test_iframe_shell_theme.py::test_vendor_scripts_selfhosted`, `::test_vendor_fonts_selfhosted`, `::test_no_external_cdn`, `::test_palette_blocks_have_no_hex` | UNIT | FULL |
| 1.2-B — theme sync от parent и fallback при прямом открытии | `test_iframe_shell_theme.py::test_theme_sync_mutation_observer_on_parent`, `::test_theme_sync_prefers_color_scheme_fallback` | UNIT | FULL |
| 1.2-C — токены и контраст всех поверхностей | `test_iframe_shell_theme.py::test_text_contrast_floor_4_5`, `::test_component_rules_use_centralized_color_tokens`, `test_final_layout_runtime.py::test_chat_panel_follows_theme_tokens_without_gradient_or_slash_copy` | UNIT+BROWSER | FULL |
| 1.3-A — раздельные маршруты и scoped surface | `test_adaptive_context_routes.py::test_view_state_supports_three_routes`, `::test_open_context_maps_route_directly`, `::test_research_surface_without_catalog_data` | UNIT | FULL |
| 1.3-B — действия переносятся, у root нет горизонтальной прокрутки | `test_adaptive_context_routes.py::test_header_wraps_actions_to_second_line`, `::test_root_has_no_overflow_hidden`, `test_final_layout_runtime.py::test_breakpoints_have_no_root_overflow_or_clipped_persistent_controls` | UNIT+BROWSER | FULL |
| 1.3-C — второстепенные колонки скрываются, URL остаётся в details | `test_adaptive_context_routes.py::test_secondary_columns_hidden_by_priority`, `::test_url_available_in_row_details`, `::test_table_container_is_the_only_horizontal_scroller` | UNIT | FULL |
| 1.4-A — loading/empty/error различаются и дают правильное действие | `test_accessible_states_feedback.py::test_catalog_loading_surface`, `::test_catalog_empty_state_has_reset_action`, `::test_catalog_error_state_not_masked_as_empty`, `::test_queue_error_state_has_retry` | UNIT | FULL |
| 1.4-B — typed toast и подтверждение destructive action без browser dialogs | `test_accessible_states_feedback.py::test_single_typed_toast`, `::test_toast_variants_in_css`, `::test_delete_parser_requires_modal_confirmation`, `::test_no_alert_or_confirm_calls` | UNIT | FULL |
| 1.4-C — trap, Escape, return focus, семантика и размер целей | `test_accessible_states_feedback.py::test_focus_layer_used_by_all_layers`, `::test_modals_dialog_semantics`, `::test_focus_visible_ring_for_interactive_controls`, `::test_targets_at_least_28px`, `::test_focus_trap_cycles_from_programmatic_title_on_shift_tab` | UNIT | FULL |
| 1.5 — исходная карточка Telegram-целей в admin route | Заменено `spec-remove-telegram-target-status-card.md` по прямой пользовательской аннотации; удаление проверено `test_admin_roles_audit.py::test_admin_sections_and_states` и отдельной browser-проверкой | SUPERSEDED | SUPERSEDED |
| 1.5 — назначение/отзыв ЦК КС, лимит 5 и redacted audit | `test_admin_roles_audit.py::test_grant_role_assigns_queue_access`, `::test_grant_role_limit_five_active_experts`, `::test_successful_revoke_and_admin_audit_use_request_session`, `::test_admin_audit_summary_is_redacted` | API | FULL (с учётом замены карточки) |

### 2. AI-исследование

| Критерий | Свежая тестовая трасса | Уровень | Покрытие |
|---|---|---|---|
| 2.1-A — allowlisted AgentFactory/SkillRegistry и configurable 20 итераций | `test_story_2_1_agent.py::test_skill_registry_uses_only_allowlisted_read_only_skills`, `::test_agent_factory_creates_isolated_agent_with_registry_tools`, `::test_nanobot_iteration_limit_is_default_twenty_and_configurable` | UNIT | FULL |
| 2.1-B — clarification ожидает ответ без расхода итерации | `test_story_2_1_agent.py::test_clarification_wait_does_not_consume_iteration`, `::test_client_skip_clarify_cannot_start_agent_for_ambiguous_query`, `test_story_2_1_routes.py::test_clarify_answer_uses_real_builder_token_and_history` | UNIT+API | FULL |
| 2.1-C — limit/noncritical failure возвращают понятный partial result | `test_story_2_1_agent.py::test_agent_marks_nanobot_iteration_limit_as_partial`, `::test_noncritical_skill_failure_returns_partial_answer`, `::test_stream_failure_emits_safe_partial_terminal_event_and_audit` | UNIT | FULL |
| 2.1-D — redacted audit и безопасный список tool names | `test_story_2_1_agent.py::test_agent_audit_is_redacted_and_contains_run_metadata`, `::test_sse_tool_events_never_expose_arguments_or_result_payload`, `::test_ui_shows_tool_names_without_technical_payloads`, `test_story_2_1_routes.py::test_chat_and_clarify_action_audit_redacts_sensitive_text` | UNIT+API | FULL |
| 2.2-A — query, source и extracted text сохраняются со ссылкой | `test_story_2_2_research_cases.py::test_research_case_service_keeps_candidate_and_source_outside_catalog` | API | FULL |
| 2.2-B — CaseContract остаётся в текущем research | `test_story_2_2_research_cases.py::test_research_case_service_keeps_candidate_and_source_outside_catalog` | API | FULL |
| 2.2-C — source/fetch/extract error оставляет limitation и продолжает run | `test_story_2_2_research_cases.py::test_unavailable_source_keeps_limitation_and_rejects_candidate`, `::test_collect_research_continues_after_unavailable_source`, `::test_collect_research_keeps_running_when_fetch_raises`, `::test_collect_research_keeps_limitation_when_extraction_raises` | API | FULL |
| 2.2-D — непроверенный кандидат не попадает в общую базу, связь с источником доступна | `test_story_2_2_research_cases.py::test_research_case_service_keeps_candidate_and_source_outside_catalog` | API | FULL |
| 2.3-A — model verdict, confidence/reason, модель и batch config | `test_story_2_2_research_cases.py::test_classification_stores_model_verdict_without_overwriting_manual_case`, `test_story_2_3_config.py::test_research_classifier_batch_size_comes_from_config` | API+UNIT | FULL |
| 2.3-B — классификация после извлечения и профильный prompt | `test_story_2_2_research_cases.py::test_classification_stores_model_verdict_without_overwriting_manual_case` | API | FULL |
| 2.3-C — ручной/ЦК verdict не изменяется | `test_story_2_2_research_cases.py::test_classification_stores_model_verdict_without_overwriting_manual_case` | API | FULL |
| 2.3-D — частичная ошибка не стирает успешные результаты | `test_story_2_2_research_cases.py::test_classification_reports_partial_failure_and_keeps_success` | API | FULL |
| 2.4-A — локализованный статус до 15 секунд и русские фазы | `test_story_2_4_research_progress.py::test_first_localized_research_status_arrives_before_fifteen_seconds`, `test_adaptive_context_routes.py::test_pipeline_phase_labels_are_russian` | API+UNIT | FULL |
| 2.4-B/C — sidebar/off-canvas, overlay, Escape и focus restore | `test_adaptive_context_routes.py::test_chat_default_closed_below_1100px`, `::test_chat_offcanvas_below_1100px`, `::test_escape_closes_chat_panel`, `::test_only_compact_chat_is_modal_and_focus_trapped` | UNIT | FULL |
| 2.4-D — reopen сохраняет сообщения/draft/progress и не отменяет run | `test_story_2_4_research_progress.py::test_research_panel_uses_localized_phases_and_keeps_chat_state_outside_panel`, `test_adaptive_context_routes.py::test_compact_chat_survives_resize_inside_compact_viewport` | UNIT | FULL |
| 2.5-A — выбор показывает summary/source и допускает active evidence | `test_story_2_2_research_cases.py::test_submit_selected_case_creates_idempotent_immutable_snapshot` | API | FULL |
| 2.5-B/C — immutable submitted snapshot, idempotency и waiting status | `test_story_2_2_research_cases.py::test_submit_selected_case_creates_idempotent_immutable_snapshot`, `test_story_2_5_submission_route.py::test_submit_route_requires_workspace_owner_and_returns_waiting_status` | API | FULL |
| 2.5-D — revoked evidence отклоняется без disclosure | `test_story_2_2_research_cases.py::test_submit_rejects_revoked_evidence_without_metadata_disclosure` | API | FULL |

### 3. Верификация, публикация и lifecycle

| Критерий | Свежая тестовая трасса | Уровень | Покрытие |
|---|---|---|---|
| 3.1-A — queue использует submitted snapshot и закреплённое evidence | `test_story_2_2_research_cases.py::test_submit_selected_case_creates_idempotent_immutable_snapshot` | API | FULL |
| 3.1-B — одно append-only решение с comment/expert/time/run_id | `test_story_2_2_research_cases.py::test_ccks_decision_is_append_only_and_idempotent_for_submitted_snapshot`, `test_db_schema_049.py::test_migration_049_has_allowed_decisions_and_postgres_append_only_guard` | API+UNIT | PARTIAL: нет отдельного RED/GREEN для пустого comment и route-level audit полей |
| 3.1-C — конкурентный второй эксперт получает зафиксированный итог | `test_story_2_2_research_cases.py::test_ccks_decision_is_append_only_and_idempotent_for_submitted_snapshot` | API | PARTIAL: проверена последовательная идемпотентность, не конкурентный route-сценарий |
| 3.1-D — не-ЦК не читает/не принимает решение | `test_authorization.py::test_queue_member_without_role_403_no_protected_data` | API | PARTIAL: нет прямого теста `POST /verification/snapshots/{id}/decision` без роли |
| 3.2-A/B — положительное решение публикуется единожды по command key | `test_story_2_2_research_cases.py::test_positive_decision_publishes_catalog_case_once_and_negative_never_publishes`, `test_db_schema_050.py::test_migration_050_creates_one_mapping_per_command_key` | API+UNIT | PARTIAL: success/idempotency есть, нет restart/failure transition |
| 3.2-C — failed publishing показывает states и допускает retry только из error | Нет отдельного теста | — | NONE |
| 3.2-D — `not_confirmed` не публикуется и фиксирует отсутствие публикации | `test_story_2_2_research_cases.py::test_positive_decision_publishes_catalog_case_once_and_negative_never_publishes` | API | PARTIAL: отсутствие catalog record есть, отдельного audit assertion нет |
| 3.3-A/B — numbered идемпотентные migrations и unique lifecycle keys | `test_db_schema_048.py::test_migration_048_keeps_submitted_snapshot_greenplum_safe`, `test_db_schema_049.py::test_migration_049_has_allowed_decisions_and_postgres_append_only_guard`, `test_db_schema_050.py::test_migration_050_creates_one_mapping_per_command_key`, `test_db_schema_051.py::test_migration_051_guarantees_one_lifecycle_result_per_business_key` | UNIT | PARTIAL: DDL contracts проверены текстом, но не применением к PostgreSQL |
| 3.3-C — PostgreSQL lifecycle acceptance / честный UNVERIFIED без staging | `test_db_schema_051.py::test_lifecycle_postgres_verifier_is_honest_without_staging` | UNIT | PARTIAL: verifier честен, staging evidence отсутствует |

### 4. Каталог, экспорт и аналитика

| Критерий | Свежая тестовая трасса | Уровень | Покрытие |
|---|---|---|---|
| 4.1 — исходное правило «в общей базе только published» | `test_story_4_1_published_catalog.py::test_published_catalog_excludes_research_and_pending_cases`, `::test_catalog_ui_uses_published_endpoint_and_debounces_text_search` | API+UNIT | SUPERSEDED: `spec-preliminary-research-source-import.md` позже требует показывать preliminary records всем участникам; новая модель ещё не реализована |
| 4.1 — filter/reset/debounce, keyboard sort, selection/details | `test_story_4_1_published_catalog.py::test_catalog_ui_uses_published_endpoint_and_debounces_text_search`, `test_accessible_states_feedback.py::test_table_sort_keyboard_accessible`, `::test_record_details_use_native_buttons_not_interactive_rows` | UNIT | FULL для не противоречащей части |
| 4.2-A — CSV/XLSX/PDF используют published filter без неполного файла | `test_story_4_2_filtered_export.py::test_filtered_export_uses_published_catalog_only`, `::test_xlsx_export_rejects_more_than_ten_thousand_without_partial_file`, `::test_pdf_export_returns_structured_error_when_renderer_is_unavailable` | API | FULL для export каталога |
| 4.2-B — export AI-исследования содержит кейсы, источники и исходные параметры | `test_research_report_export.py::test_report_docx_contains_query_result_and_only_snapshot_evidence` | API | PARTIAL: текущий сервис сохраняет пустой `evidence_snapshot`, кейсы/URL в документ не доказаны |
| 4.2-C/D — limit и typed PDF fallback | `test_story_4_2_filtered_export.py::test_xlsx_export_rejects_more_than_ten_thousand_without_partial_file`, `::test_pdf_export_returns_structured_error_when_renderer_is_unavailable` | API | FULL |
| 4.3-A/B — только один параметризованный SELECT на allowlisted view; unsafe SQL блокируется до DB | `test_story_4_3_safe_analytics.py::test_analytics_allows_single_select_on_published_view_and_returns_json_table`, `::test_analytics_blocks_unsafe_sql_before_db_access`, `test_story_2_1_agent.py::test_db_query_rejects_limit_over_500_before_database` | API+UNIT | PARTIAL: role `loophole_readonly` и PostgreSQL timeout не подтверждены интеграционно |
| 4.3-C — JSON table, timeout и hard row limit | `test_story_4_3_safe_analytics.py::test_analytics_allows_single_select_on_published_view_and_returns_json_table`, `test_story_2_1_agent.py::test_db_query_rejects_limit_over_500_before_database` | API+UNIT | PARTIAL: timeout не имеет отдельного исполнимого теста |
| 4.4-A — named schedule contract хранит id/version без raw SQL | `test_story_4_4_scheduled_analytics.py::test_schedule_contract_stores_named_query_not_raw_sql_and_keeps_result_private`, `::test_schedule_api_contract_rejects_raw_sql_field` | API | FULL |
| 4.4-B — scheduler skip при revoked membership с одним audit event | `test_story_4_4_scheduled_analytics.py::test_schedule_skips_once_without_query_when_recipient_membership_revoked` | API | PARTIAL: owner membership, capabilities и expiry не имеют отдельных тестов |
| 4.4-C — result private, ACL и TTL ≤24h | `test_story_4_4_scheduled_analytics.py::test_schedule_contract_stores_named_query_not_raw_sql_and_keeps_result_private`, `::test_schedule_result_acl_rejects_another_member` | API | FULL |

### 5. Обычные веб-парсеры

| Критерий | Свежая тестовая трасса | Уровень | Покрытие |
|---|---|---|---|
| 5.1 — форма сразу создаёт и валидирует parser | `test_parsers_generator.py::test_generate_saves_to_catalog`, `::test_validation_failure_keeps_parser_and_validation_run` покрывают старое поведение | API | SUPERSEDED: новая `spec-register-parser-development-request.md` запрещает это поведение и требует только pending-заявку |
| 5.2-A — manual run создаёт отдельный run/status/log и показывает состояние | `test_parsers_runner.py::test_wait_success_creates_run_record`, `::test_wait_error_on_nonzero_rc`, `test_parsers_web.py::test_manual_run_returns_run_id`, `test_final_layout_runtime.py::test_web_parser_lifecycle_is_inline_on_sources_tab` | API+BROWSER | FULL |
| 5.2-B — ошибка run безопасна, retry не меняет parser/other runs | `test_parsers_runner.py::test_wait_pump_exception_finalizes_error`, `::test_stop_finishes_run`, `test_parsers_web.py::test_manual_run_rejects_parser_without_successful_validation`, `test_final_layout_runtime.py::test_parser_log_disconnect_closes_stream_and_shows_inline_error` | API+BROWSER | FULL |
| 5.3-A — enable/disable schedule по parser, запрет невалидного и disabled не стартует | `test_parsers_web.py::test_patch_schedule_valid`, `::test_patch_schedule_rejects_parser_with_failed_validation`, `::test_patch_clear_cron`, `test_parsers_scheduler.py::test_tick_skips_future_and_disabled` | API | PARTIAL: нет отдельной audit-проверки toggle |
| 5.3-B — scheduled run виден как manual и не создаёт Telegram client/session/target | `test_parsers_scheduler.py::test_tick_runs_due_parser`, `::test_tick_skips_already_running`, `test_parsers_repository.py::test_list_auto_parsers_excludes_failed_validation` | API | PARTIAL: нет прямого отрицательного теста отсутствия Telegram side effects |

### 6. Telegram-контур

| Критерий | Свежая тестовая трасса | Уровень | Покрытие |
|---|---|---|---|
| 6.1-A — normalise t.me/@handle/invite и не создавать дубликат | `test_story_6_1_target_registry.py::test_register_normalizes_supported_addresses`, `::test_repeat_registration_returns_existing_target_without_duplicate`, `::test_register_rejects_unsupported_target_fail_closed` | API | FULL |
| 6.1-B — registration/access/collection разделены, registry не управляет worker | `test_story_6_1_target_registry.py::test_register_normalizes_supported_addresses` | API | FULL |
| 6.2-A — canonical target-only subscription, audit и fail-closed denial | `test_story_6_2_target_access.py::test_grant_subscription_changes_only_canonical_target_and_audits`, `::test_grant_rejects_redirect_or_foreign_workspace_fail_closed`, `::test_grant_without_capability_is_denied_fail_closed`, `::test_revoke_increments_grant_version_and_keeps_subscription_history` | API | FULL |
| 6.2-B — deactivate fences active lease, checkpoint сохраняется, reactivation continues | `test_story_6_2_target_access.py::test_deactivation_fences_active_lease_without_deleting_checkpoint`, `::test_reactivation_starts_new_fenced_lease_and_preserves_checkpoint` | API | FULL |
| 6.3-A — initial sync хранит sanitised ingress и не создаёт research/candidate/catalog | `test_story_6_3_telegram_ingestion.py::test_first_sync_keeps_available_history_as_sanitized_ingress_and_checkpoint` | API | FULL |
| 6.3-B — incremental sync, late comment и identity/version dedup | `test_story_6_3_telegram_ingestion.py::test_incremental_sync_accepts_late_comment_and_deduplicates_identity_version` | API | FULL |
| 6.3-C — uncertain body/attachment metadata-only quarantine без raw persistence | `test_story_6_3_telegram_ingestion.py::test_uncertain_content_is_metadata_only_quarantine_without_raw_body_or_attachments`, `::test_untrusted_value_in_safe_metadata_key_is_quarantined_without_raw_text` | API | FULL |
| 6.4-A — global/target lease и fencing блокируют stale write | `test_story_6_4_telegram_worker.py::test_stale_worker_cannot_write_batch_or_checkpoint_after_lease_is_replaced` | API | FULL |
| 6.4-B — reaper terminalizes один раз, новый owner продолжает checkpoint без duplicate | `test_story_6_4_telegram_worker.py::test_reaper_terminalizes_expired_attempt_once_and_new_owner_resumes_checkpoint` | API | FULL |
| 6.4-C — 24h SLO и safe journal без body | `test_story_6_4_telegram_worker.py::test_slo_uses_24h_attempt_journal_and_never_serializes_message_body` | API | FULL |
| 6.5-A/B — least privilege DCL, controlled functions, no listener и restricted deployment | `test_story_6_5_telegram_perimeter.py::test_migration_057_encodes_least_privilege_and_controlled_functions`, `::test_deployment_contract_has_no_listener_and_restricts_egress`, `::test_production_worker_uses_only_controlled_db_functions` | UNIT | PARTIAL: contracts проверены без production-like runtime |
| 6.5-C — external staging evidence required и отсутствие честно UNVERIFIED | `test_story_6_5_telegram_perimeter.py::test_perimeter_verifier_is_honest_without_external_evidence`, `::test_perimeter_verifier_accepts_only_complete_external_evidence`, `::test_perimeter_verifier_rejects_incomplete_external_evidence` | UNIT | PARTIAL: внешнее evidence для текущего окружения отсутствует |

### Поздние пользовательские спецификации

| Спецификация / критерий | Свежая тестовая трасса | Уровень | Покрытие |
|---|---|---|---|
| Перенос найденных research-source в общую базу как `Предварительно`, idempotency, badge/probability/provenance, фильтры ЦК и ownership | Нет соответствующего route/service/migration/test: поиск по `source_proposal`, `preliminary`, `research import` не нашёл реализации в loophole module | — | NONE |
| Заявка на разработку parser вместо автогенерации: `source_proposal(purpose='loophole_parser')`, 201/409/422/audit/read-only catalog и удаление Telegram-note | Нет соответствующего route/service/test в `src/bank_audit/loophole`; текущая форма всё ещё использует `ParserCreateRequest` и generator/validation contract | — | NONE |
| Читаемый Markdown-результат | `test_final_layout_runtime.py::test_research_result_renders_safe_markdown_and_exposes_snapshot_downloads`, `test_research_report_export.py::test_report_renderer_escapes_untrusted_text_and_marks_missing_evidence` | BROWSER+UNIT | FULL |
| PDF/Word только из immutable report, ownership и typed PDF fallback | `test_research_report_export.py::test_report_result_is_bound_to_current_workspace_run_and_has_no_evidence`, `::test_report_docx_contains_query_result_and_only_snapshot_evidence`, `::test_report_export_denies_foreign_workspace`, `::test_report_pdf_failure_is_typed_and_word_remains_available` | API | PARTIAL: `save_report_result()` всегда записывает `evidence_snapshot='[]'`; содержимое проверенных источников/URL не сохраняется и не доказано экспортом |

## Результат шага 3

- `FULL`: 14 из 28 активных спецификаций — 1.1–1.4, 2.1–2.5, 5.2, 6.1–6.4.
- `PARTIAL`: 9 — 3.1–3.3, 4.2–4.4, 5.3, 6.5 и export-часть отчёта исследования.
- `NONE`: 2 — перенос предварительных research-source и заявка на разработку parser.
- `SUPERSEDED`: 3 — 1.5 (Telegram-card), 4.1 (published-only catalog) и 5.1 (автогенерация parser),
  потому что их исходные формулировки заменены более поздними пользовательскими требованиями.

Полный `pytest tests/loophole` остаётся зелёным: **722 passed, 1 skipped, 21 warnings**. Единственный
skip — явный optional PostgreSQL staging-check migration 044; он не учитывался как доказательство
готовности внешнего окружения.

## Анализ пробелов и live-проверка

Подробная машинно-читаемая матрица Phase 1: `phase-1-coverage-20260831.json`.

### Приоритизированные пробелы

1. **P0 — нет реализации:** перенос новых research-source в общую базу как «Предварительно» и
   форма pending-заявки на разработку parser. Это два прямых поздних требования пользователя,
   поэтому старые истории 4.1 и 5.1 не могут считаться заменой реализации.
2. **P1 — неполное доказательство lifecycle:** route-level denial/concurrency решения ЦК КС,
   failed-publication/retry, PostgreSQL role/timeout, owner/capability/expiry расписания и
   parser-schedule audit/Telegram-isolation.
3. **P1 — экспорт исследования:** safe Markdown и PDF/DOCX route проверены, но
   `save_report_result()` всегда создаёт пустой `evidence_snapshot`; экспорт не может
   подтвердить реальные проверенные URL/источники.
4. **Внешний gate:** lifecycle PostgreSQL и Telegram perimeter честно возвращают `UNVERIFIED`
   без staging/production-like evidence. Это не тестовый дефект и не исправляется пересозданием
   локального Docker-контейнера.

### Ручная Browser-проверка

- На исходном экране «Новое AI-исследование» видны отдельные карточки параметров, прогресса и
  доказательств; console errors — 0.
- Открытие и закрытие «Аналитика лазеек» работает, после закрытия фокус возвращается на
  «Открыть чат»; это подтверждает ключевую часть 2.4 на реальной UI-поверхности.
- Вкладка «Добавить источник» всё ещё отображает «Создать и проверить» и карточку
  «Подключение Telegram». Это непосредственное browser-доказательство статуса `NONE` для
  `spec-register-parser-development-request.md`, а не результат устаревшего теста.
- Проверить в in-app Browser готовый Markdown-результат и реальные PDF/Word download без
  создания нового исследования нельзя: в доступном workspace нет завершённого report. Эту
  часть не засчитываю как live-подтверждение; есть только browser-runtime regression-тест.

## Решение quality gate: FAIL

Автоматическое правило применимо: `collection_status=COLLECTED`, формальный oracle имеет высокую
уверенность, а полный pytest-набор реально выполнен. Gate не пройден, потому что:

- P0: **5 из 11 (45%)**, при обязательных 100%;
- P1: **9 из 17 (53%)**, при минимуме 80%;
- всего: **14 из 28 (50%)**, при минимуме 80%;
- открыты две P0-спецификации без реализации: перенос preliminary research-source и pending-заявка
  на разработку parser.

Это не означает, что зелёные 14 спецификаций сломаны: их автоматические тесты прошли. Это означает,
что весь набор спецификаций нельзя честно обозначить как завершённый или release-ready.

Машиночитаемые результаты:

- `e2e-trace-summary.json` — сводка покрытия и выполненного полного прогона;
- `gate-decision.json` — краткий детерминированный сигнал FAIL;
- `phase-1-coverage-20260831.json` — список требований, тестов и конкретных пробелов.
