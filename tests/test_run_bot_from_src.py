from __future__ import annotations

import os

from scripts import run_bot_from_src


def test_explicit_env_file_is_loaded_without_overriding_process_values(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BOOTSTRAP_NEW=value\nBOOTSTRAP_KEEP=file\n", encoding="utf-8")
    monkeypatch.setenv("YONERAI_ENV_FILE", str(env_file))
    monkeypatch.setenv("BOOTSTRAP_KEEP", "process")
    monkeypatch.delenv("BOOTSTRAP_NEW", raising=False)

    run_bot_from_src._load_explicit_env()

    assert os.environ["BOOTSTRAP_NEW"] == "value"
    assert os.environ["BOOTSTRAP_KEEP"] == "process"
