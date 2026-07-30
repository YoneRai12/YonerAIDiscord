"""Builtin pluginの明示的な登録順と完全性契約。"""

from __future__ import annotations


BUILTIN_PLUGIN_MANIFEST: tuple[str, ...] = (
    "admin_ui",
    "personal_memory",
    "site_publish",
    "media_pipeline",
    "browser_rendering",
    "media_inspection",
    "ai",
    "automod",
    "community",
    "capability_forge",
    "discovery",
    "earthquake",
    "evolution",
    "identity",
    "image_editing",
    "image_generation",
    "jobs",
    "jp_information",
    "nasa_apod",
    "minecraft",
    "moderation",
    "modtools",
    "voice",
    "music",
    "music_generation",
    "operations",
    "scheduling",
    "servertools",
    "speech_synthesis",
    "speech_transcription",
    "utility",
    "video_generation",
    "yonerai",
)


__all__ = ["BUILTIN_PLUGIN_MANIFEST"]
