"""Fail-closed analytics DB skill для опубликованного каталога."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text


class AnalyticsQueryError(ValueError):
    """Запрос не соответствует allowlisted форме аналитики."""


_ALLOWED_VIEW = "loophole_published_catalog_v1"
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"COPY|JOIN|UNION|WITH|RETURNING|INTO)\b",
    re.IGNORECASE,
)
_FUNCTION = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(")
_QUERY = re.compile(
    r"^\s*SELECT\s+[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*\s+"
    rf"FROM\s+{_ALLOWED_VIEW}"
    r"(?:\s+WHERE\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*:[A-Za-z_][A-Za-z0-9_]*)?\s*$",
    re.IGNORECASE,
)


def _validate(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip() or ";" in sql or "--" in sql or "/*" in sql:
        raise AnalyticsQueryError("Допустим только один SELECT к опубликованному каталогу")
    if _FORBIDDEN.search(sql) or _FUNCTION.search(sql) or not _QUERY.fullmatch(sql):
        raise AnalyticsQueryError("Допустим только SELECT к allowlisted published view без функций")
    return sql.strip()


def execute_analytics_query(
    sql: str, params: dict[str, Any], *, session: Any, row_limit: int = 500
) -> dict[str, list]:
    """Выполняет один параметризованный SELECT с жёстким лимитом строк."""
    statement = _validate(sql)
    if row_limit < 1 or row_limit > 500:
        raise AnalyticsQueryError("Допустимый лимит аналитики: 1..500")
    if session.get_bind().dialect.name != "sqlite":
        session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
    rows = session.execute(text(f"{statement} LIMIT {row_limit}"), params).mappings().all()
    columns = list(rows[0].keys()) if rows else [part.strip() for part in statement.split("FROM", 1)[0][6:].split(",")]
    return {"columns": columns, "rows": [[row[column] for column in columns] for row in rows]}
