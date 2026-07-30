from __future__ import annotations

from .types import RuntimeCapabilityDefinition, _cap


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    # Utility: read-only情報表示とpureな変換・抽選。
    _cap("cap-run-info-user", "utility.general", "user情報を表示", command="info user"),
    _cap("cap-run-info-server", "utility.general", "server情報を表示", command="info server"),
    _cap("cap-run-info-avatar", "utility.general", "avatar URLを表示", command="info avatar"),
    _cap("cap-run-info-role", "utility.general", "role情報を表示", command="info role"),
    _cap("cap-run-info-channel", "utility.general", "channel情報を表示", command="info channel"),
    _cap("cap-run-info-permissions", "utility.general", "member権限を表示", command="info permissions"),
    _cap("cap-run-tools-timestamp", "utility.general", "Discord timestampへ変換", command="tools timestamp"),
    _cap("cap-run-tools-choose", "utility.general", "候補から安全に抽選", command="tools choose"),
    _cap("cap-run-tools-dice", "utility.general", "dice式を評価", command="tools dice"),
    _cap("cap-run-tools-random", "utility.general", "範囲内の整数を抽選", command="tools random"),
    _cap("cap-run-tools-sha256", "utility.general", "入力のSHA-256を計算", command="tools sha256"),
    _cap("cap-run-tools-snowflake", "utility.general", "Discord Snowflake日時を表示", command="tools snowflake"),
    _cap("cap-run-tools-color", "utility.general", "HEX colorを検証", command="tools color"),
)
