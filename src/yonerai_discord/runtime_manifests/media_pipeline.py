from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


MEDIA_QR_ENCODE_CAPABILITY_ID = "cap-run-media-qr-encode"
MEDIA_PLACE_ON_CANVAS_CAPABILITY_ID = "cap-run-media-place-on-canvas"
MEDIA_COMPOSE_GRID_CAPABILITY_ID = "cap-run-media-compose-grid"
MEDIA_QUOTE_CARD_CAPABILITY_ID = "cap-run-media-quote-card"
MEDIA_DISCORD_ASSET_INSPECT_CAPABILITY_ID = "cap-run-media-discord-asset-inspect"
MEDIA_PIPELINE_CAPABILITY_IDS = (
    MEDIA_QR_ENCODE_CAPABILITY_ID,
    MEDIA_PLACE_ON_CANVAS_CAPABILITY_ID,
    MEDIA_COMPOSE_GRID_CAPABILITY_ID,
    MEDIA_QUOTE_CARD_CAPABILITY_ID,
    MEDIA_DISCORD_ASSET_INSPECT_CAPABILITY_ID,
)


# Discord command surfaceは持たない。ActionRegistry/Plannerだけが利用するservice-only capability。
CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        MEDIA_QR_ENCODE_CAPABILITY_ID,
        "media.pipeline",
        "制限付きテキストからローカルQR画像artifactを生成",
        plugin="media_pipeline",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.HIGH,
        default_enabled=False,
    ),
    _cap(
        MEDIA_PLACE_ON_CANVAS_CAPABILITY_ID,
        "media.pipeline",
        "同一scopeの画像artifactをローカルcanvasへ配置",
        plugin="media_pipeline",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.HIGH,
        default_enabled=False,
    ),
    _cap(
        MEDIA_COMPOSE_GRID_CAPABILITY_ID,
        "media.pipeline",
        "同一scopeの1〜8画像artifactをローカルgridへ合成",
        plugin="media_pipeline",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.HIGH,
        default_enabled=False,
    ),
    _cap(
        MEDIA_QUOTE_CARD_CAPABILITY_ID,
        "media.pipeline",
        "本文と実行者情報からローカル引用カードartifactを生成",
        plugin="media_pipeline",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.MEDIUM,
        default_enabled=False,
    ),
    _cap(
        MEDIA_DISCORD_ASSET_INSPECT_CAPABILITY_ID,
        "media.pipeline",
        "明示されたDiscord絵文字またはスタンプを読み取り専用で検査",
        plugin="media_pipeline",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.MEDIUM,
        default_enabled=False,
    ),
)


__all__ = [
    "CAPABILITIES",
    "MEDIA_COMPOSE_GRID_CAPABILITY_ID",
    "MEDIA_DISCORD_ASSET_INSPECT_CAPABILITY_ID",
    "MEDIA_PIPELINE_CAPABILITY_IDS",
    "MEDIA_PLACE_ON_CANVAS_CAPABILITY_ID",
    "MEDIA_QR_ENCODE_CAPABILITY_ID",
    "MEDIA_QUOTE_CARD_CAPABILITY_ID",
]
