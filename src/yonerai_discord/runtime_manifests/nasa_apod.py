from __future__ import annotations

from .types import RuntimeCapabilityDefinition, _cap


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        "cap-run-nasa-apod-read",
        "operations.nasa-apod",
        "NASA Astronomy Picture of the Dayを明示取得して表示",
        command="nasa apod",
        plugin="nasa_apod",
        default_enabled=False,
    ),
)
