from __future__ import annotations

from .types import _cap


CAPABILITIES = (
    _cap(
        "cap-run-discovery-help",
        "interaction.discord-surface",
        "利用できるコマンドを検索・一覧表示",
        command="help",
        plugin="discovery",
    ),
)
