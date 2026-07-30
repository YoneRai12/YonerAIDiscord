from __future__ import annotations

from collections.abc import Awaitable, Callable

from yonerai_discord.provider_registry import ArtifactRef


AuthorizationCheck = Callable[[], bool | Awaitable[bool]]
RemoteConsentCheck = Callable[[int], bool | Awaitable[bool]]
AudioArtifactCheck = Callable[[ArtifactRef, str], bool | Awaitable[bool]]
