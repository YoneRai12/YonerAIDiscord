from __future__ import annotations

import json
import logging
from pathlib import Path

from yonerai_discord.logging import JsonFormatter, configure_logging, redact


def test_redact_handles_nested_sensitive_values() -> None:
    value = redact({"discord_token": "do-not-log", "nested": {"api_key": "also-secret"}, "safe": 42})
    assert value == {"discord_token": "[REDACTED]", "nested": {"api_key": "[REDACTED]"}, "safe": 42}


def test_json_formatter_outputs_structured_log_and_redacts_extras() -> None:
    record = logging.LogRecord("suite", logging.INFO, __file__, 1, "ready %s", ("now",), None)
    record.plugin = "verification"
    record.authorization = "Bot secret-value"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["message"] == "ready now"
    assert payload["plugin"] == "verification"
    assert payload["authorization"] == "[REDACTED]"


def test_configure_logging_writes_utf8_rotating_json_without_secret(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "bot.jsonl"
    configure_logging(logging.INFO, log_path=path, max_bytes=1_048_576, backup_count=2)
    root = logging.getLogger()
    try:
        logger = logging.getLogger("suite.test")
        logger.info("日本語 ready", extra={"api_key": "never-write-this"})
        for handler in root.handlers:
            handler.flush()

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["message"] == "日本語 ready"
        assert payload["api_key"] == "[REDACTED]"
        assert "never-write-this" not in path.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers.clear()
