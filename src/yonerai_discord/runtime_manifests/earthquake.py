from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        "cap-run-earthquake-latest",
        "operations.earthquake",
        "P2PQuake公式APIから最新の地震・EEW情報を表示",
        command="earthquake latest",
        plugin="earthquake",
        risk=RiskLevel.MEDIUM,
    ),
    _cap(
        "cap-run-earthquake-status",
        "operations.earthquake",
        "このサーバーの地震通知設定とfeed状態を表示",
        command="earthquake status",
        plugin="earthquake",
    ),
    _cap(
        "cap-run-earthquake-subscribe",
        "operations.earthquake",
        "管理者がチャンネル別の地震・EEW通知を有効化",
        command="earthquake subscribe",
        plugin="earthquake",
        level=RbacLevel.GUILD_ADMIN,
        risk=RiskLevel.HIGH,
    ),
    _cap(
        "cap-run-earthquake-unsubscribe",
        "operations.earthquake",
        "管理者がこのサーバーの地震・EEW通知を停止",
        command="earthquake unsubscribe",
        plugin="earthquake",
        level=RbacLevel.GUILD_ADMIN,
        risk=RiskLevel.HIGH,
    ),
    _cap(
        "cap-run-earthquake-delivery",
        "operations.earthquake",
        "購読済みチャンネルへ重複排除した地震・EEWを自動通知",
        event="earthquake_feed_delivery",
        plugin="earthquake",
        level=RbacLevel.GUILD_ADMIN,
        risk=RiskLevel.HIGH,
    ),
)
