from bank_audit.loophole.chat.nanobot_agent import (
    build_nanobot_config,
    build_prompt,
    create_nanobot,
    load_system_prompt,
)


def _transport_uses_proxy(client):
    """Возвращает True, если httpx transport содержит proxy-пул."""
    transport = client._transport
    return "Proxy" in type(transport).__name__ or hasattr(transport, "_pool") and (
        "Proxy" in type(transport._pool).__name__
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


def test_create_nanobot_ignores_proxy_env(monkeypatch):
    """Фактический SDK nanobot не должен строить proxy transport из env."""
    import asyncio
    from pathlib import Path

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    bot, config_path = create_nanobot()
    http_client = None
    try:
        provider = bot._loop.provider
        asyncio.run(provider._ensure_client())
        http_client = provider._client._client
        assert not _transport_uses_proxy(http_client)
    finally:
        asyncio.run(bot.aclose())
        Path(config_path).unlink(missing_ok=True)
    assert http_client is not None and http_client.is_closed


def _find_type_arrays(node, path=""):
    """Рекурсивно ищет ``type``-массивы в JSON Schema (Gemini их отклоняет)."""
    bad = []
    if isinstance(node, dict):
        if isinstance(node.get("type"), list):
            bad.append((path, node["type"]))
        for key, value in node.items():
            bad.extend(_find_type_arrays(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            bad.extend(_find_type_arrays(value, f"{path}[{i}]"))
    return bad


def test_create_nanobot_tool_schemas_gemini_compatible():
    """Gemini отклоняет ``type`` как массив (['string','null']):
    proto-поле не repeating. Все схемы tools должны быть схлопнуты."""
    from bank_audit.loophole.chat.tools_nanobot import NANOBOT_HEAL_TOOLS

    bot, config_path = create_nanobot(extra_tools=NANOBOT_HEAL_TOOLS)
    try:
        bad = []
        for definition in bot._loop.tools.get_definitions():
            name = (definition.get("function") or {}).get("name")
            for path, types in _find_type_arrays(definition):
                bad.append(f"{name}{path}: {types}")
        assert not bad, f"type-массивы в схемах tools: {bad}"
    finally:
        import asyncio
        from pathlib import Path

        asyncio.run(bot.aclose())
        Path(config_path).unlink(missing_ok=True)


def test_create_nanobot_respects_custom_model():
    bot, config_path = create_nanobot(model="gpt-4o")
    try:
        assert bot._loop.model == "gpt-4o"
    finally:
        from pathlib import Path

        Path(config_path).unlink(missing_ok=True)
