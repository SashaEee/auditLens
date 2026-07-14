import pytest

from bank_audit.loophole.chat.hooks import AuditHook


@pytest.mark.asyncio
async def test_audit_hook_collects_stream():
    hook = AuditHook()
    assert hook.wants_streaming() is True

    class Ctx:
        pass

    await hook.on_stream(Ctx(), "hello")
    await hook.on_stream(Ctx(), " world")
    assert hook.final_answer == "hello world"


def test_audit_hook_finalize_masks_pii():
    hook = AuditHook()
    raw = "Контакт Иванова Ивана Ивановича +7 999 123 45 67"
    masked = hook.finalize_content(None, raw)
    assert "+7 999 123 45 67" not in masked
    assert "[NAME_" in masked  # имя замаскировано
    assert "[PHONE_" in masked  # телефон замаскирован


@pytest.mark.asyncio
async def test_audit_hook_records_tools():
    hook = AuditHook()

    class ToolCall:
        name = "audit_db_query"

    class Ctx:
        tool_calls = [ToolCall()]
        tool_events = []
        usage = {}

    await hook.after_iteration(Ctx())
    assert "audit_db_query" in hook.tools_used
