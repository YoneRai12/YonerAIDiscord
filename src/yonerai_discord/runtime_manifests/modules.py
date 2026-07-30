from __future__ import annotations

from .types import RuntimeModuleDefinition


MODULES: tuple[RuntimeModuleDefinition, ...] = (
    RuntimeModuleDefinition(
        "intelligence.personal-memory",
        dependencies=("intelligence.memory",),
    ),
    RuntimeModuleDefinition("intelligence.capability-forge", default_enabled=False),
    RuntimeModuleDefinition(
        "operations.earthquake",
        dependencies=("operations.scheduling-notification",),
    ),
    RuntimeModuleDefinition("operations.nasa-apod", default_enabled=False),
    RuntimeModuleDefinition("operations.public-information"),
    RuntimeModuleDefinition("media.audio-core"),
    RuntimeModuleDefinition("media.image-editing", default_enabled=False),
    RuntimeModuleDefinition("media.image-generation", default_enabled=False),
    RuntimeModuleDefinition("media.pipeline", default_enabled=False),
    RuntimeModuleDefinition("media.url-inspection", default_enabled=False),
    RuntimeModuleDefinition("media.music-generation", default_enabled=False),
    RuntimeModuleDefinition("media.speech-synthesis", default_enabled=False),
    RuntimeModuleDefinition("media.speech-transcription", default_enabled=False),
    RuntimeModuleDefinition("media.video-generation", default_enabled=False),
    RuntimeModuleDefinition("publishing.site-host"),
    RuntimeModuleDefinition("security.admin-ui", default_enabled=False),
    RuntimeModuleDefinition("web.browser-rendering", default_enabled=False),
)
