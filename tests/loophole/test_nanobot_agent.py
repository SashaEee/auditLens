from bank_audit.loophole.chat.nanobot_agent import (
    build_nanobot_config,
    build_prompt,
    create_nanobot,
    load_system_prompt,
)


def test_load_system_prompt_contains_tools():
    prompt = load_system_prompt()
    assert "audit_web_search" in prompt
    assert "audit_db_query" in prompt
    assert "loophole_record" in prompt


def test_build_nanobot_config_uses_env():
    cfg = build_nanobot_config()
    assert cfg["agents"]["defaults"]["maxToolIterations"] >= 1
    assert cfg["tools"]["web"]["enable"] is False


def test_build_prompt_includes_history():
    prompt = build_prompt("вопрос", [{"role": "user", "content": "привет"}])
    assert "привет" in prompt
    assert "вопрос" in prompt


def test_create_nanobot_registers_custom_tools():
    bot, config_path = create_nanobot()
    try:
        names = bot._loop.tools.tool_names
        assert "audit_web_search" in names
        assert "audit_db_query" in names
        assert "audit_table_load" in names
    finally:
        from pathlib import Path

        Path(config_path).unlink(missing_ok=True)


def test_create_nanobot_respects_custom_model():
    bot, config_path = create_nanobot(model="gpt-4o")
    try:
        assert bot._loop.model == "gpt-4o"
    finally:
        from pathlib import Path

        Path(config_path).unlink(missing_ok=True)
