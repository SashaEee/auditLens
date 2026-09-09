---
title: 'Скрытие вкладки добавления парсера'
type: 'bugfix'
created: '2026-09-07'
status: 'done'
route: 'one-shot'
---

# Скрытие вкладки добавления парсера

## Intent

**Problem:** В модуле «Лазейки» вкладка «Добавить источник» была доступна всем УЗ и ролям, хотя её требуется скрыть глобально.

**Approach:** Базовый серверный список UI-контекстов больше не выдаёт `sources`; интерфейс возвращается в «Общую базу», если после обновления контекстов ранее выбранная вкладка стала недоступной. API заявок и dormant-панель не изменяются.

## Suggested Review Order

**Контракт видимости**

- Серверная выдача исключает вкладку для базового доступа и всех ролевых расширений.
  [`authorization.py:165`](../../../../src/bank_audit/loophole/authorization.py#L165)

- Повторная загрузка контекстов не сохраняет исчезнувшую активную вкладку.
  [`loophole.jsx:269`](../../../../src/bank_audit/loophole/static/loophole.jsx#L269)

**Регрессии**

- Тест фиксирует fallback при изменении списка разрешённых UI-контекстов.
  [`test_accessible_states_feedback.py:407`](../../../../tests/loophole/test_accessible_states_feedback.py#L407)

- Роль администратора явно подтверждает отсутствие вкладки источников.
  [`test_admin_roles_audit.py:139`](../../../../tests/loophole/test_admin_roles_audit.py#L139)

- Browser-фикстуры разделяют реальную навигацию и изолированную dormant-панель.
  [`test_final_layout_runtime.py:27`](../../../../tests/loophole/test_final_layout_runtime.py#L27)

- Runtime-регрессия проверяет отсутствие вкладки и сохранение фокуса на доступной.
  [`test_focus_restore_runtime.py:53`](../../../../tests/loophole/test_focus_restore_runtime.py#L53)
