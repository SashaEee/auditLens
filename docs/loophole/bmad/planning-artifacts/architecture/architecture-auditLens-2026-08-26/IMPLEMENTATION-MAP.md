# Карта реализации и ответственность

| Порядок | Инкремент | Результат | Проверка |
| --- | --- | --- | --- |
| 1 | Core и registry | `Agent`, контекст, result, allowlist, manifest validation | unit tests registry и loop |
| 2 | Миграция skills | Шесть изолированных packages и domain adapters | contract tests каждого skill |
| 3 | Chat parity | `graph.py` через новый Agent, shim сохранён | SM-2, SM-4.1, SSE tests |
| 4 | Данные и аудит | migration `agent_audit_log`, classifier guard, `ReportFilter` | migration, security и repository tests |
| 5 | Экспорт | XLSX/PDF и общие фильтры | export tests, 10k boundary, missing browser |
| 6 | UX | tokens, themes, responsive iframe, accessibility | visual, keyboard и обе темы |
| 7 | Cutover | legacy imports removed и shim deleted | full regression, ruff changed files |

## Границы работы

- Core и registry не импортируют `web.py`, static UI или конкретные parser implementations.
- Skill-команда меняет package и контрактные тесты; доменная команда владеет repository, migrations и classifier.
- UI-команда меняет только `src/bank_audit/loophole/static/` и утверждённые DTO.
- Cutover выполняется после общего набора parity-тестов; до него legacy не удаляется.
