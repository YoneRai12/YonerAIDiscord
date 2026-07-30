from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from yonerai_discord.provider_registry import (
    DEFAULT_CATALOG,
    LogicalCapability,
    ProviderKind,
    ProviderRecommendations,
    QualityTier,
    RecommendationValidationError,
    load_default_recommendations,
)
from yonerai_discord.control_plane import RiskLevel


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (
    ROOT / "src" / "yonerai_discord" / "provider_registry" / "manifests" / "provider-recommendations.rtx5090.v2.json"
)
OLD_MANIFEST = MANIFEST.with_name("provider-recommendations.rtx5090.example.json")
ACTIVE_CATALOG_REVISION = "caa2c2f133cd3013dfe39776e9c1034fcdbd9dacb148ddfedce9dd7bba7e6e79"


def _raw_manifest() -> dict[str, object]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_default_recommendations_cover_24_inactive_probe_required_groups() -> None:
    recommendations = load_default_recommendations()
    expected = {
        (capability, kind)
        for capability in LogicalCapability
        for kind in ProviderKind
        if not (
            (capability is LogicalCapability.WEB_SEARCH and kind is ProviderKind.API)
            or (capability is LogicalCapability.WEB_SEARCH_PAID and kind is ProviderKind.LOCAL)
        )
    }

    assert recommendations.advisory_only is True
    assert recommendations.active is False
    assert {(group.capability, group.kind) for group in recommendations.candidates} == expected
    assert len(recommendations.candidates) == len(expected) == 24
    assert all(not group.active and group.probe_required for group in recommendations.candidates)
    assert all(
        {tier for tier, _recommendation in group.tiers} == set(QualityTier) for group in recommendations.candidates
    )


def test_refresh_20260724_candidates_and_safety_boundaries_are_exact() -> None:
    recommendations = load_default_recommendations()
    local_text = recommendations.group(LogicalCapability.AI_TEXT, ProviderKind.LOCAL)
    local_vision = recommendations.group(LogicalCapability.VISION_UNDERSTANDING, ProviderKind.LOCAL)
    api_text = recommendations.group(LogicalCapability.AI_TEXT, ProviderKind.API)
    api_video = recommendations.group(LogicalCapability.VIDEO_GENERATION, ProviderKind.API)
    local_video = recommendations.group(LogicalCapability.VIDEO_GENERATION, ProviderKind.LOCAL)
    api_embedding = recommendations.group(LogicalCapability.EMBEDDING, ProviderKind.API)
    local_embedding = recommendations.group(LogicalCapability.EMBEDDING, ProviderKind.LOCAL)
    local_rerank = recommendations.group(LogicalCapability.RERANK, ProviderKind.LOCAL)
    browser = recommendations.group(LogicalCapability.ISOLATED_BROWSER, ProviderKind.LOCAL)
    api_image = recommendations.group(LogicalCapability.IMAGE_GENERATION, ProviderKind.API)
    local_image = recommendations.group(LogicalCapability.IMAGE_GENERATION, ProviderKind.LOCAL)
    api_image_edit = recommendations.group(LogicalCapability.IMAGE_EDITING, ProviderKind.API)
    local_image_edit = recommendations.group(LogicalCapability.IMAGE_EDITING, ProviderKind.LOCAL)
    api_music = recommendations.group(LogicalCapability.MUSIC_GENERATION, ProviderKind.API)
    local_music = recommendations.group(LogicalCapability.MUSIC_GENERATION, ProviderKind.LOCAL)
    api_tts = recommendations.group(LogicalCapability.SPEECH_TTS, ProviderKind.API)
    local_tts = recommendations.group(LogicalCapability.SPEECH_TTS, ProviderKind.LOCAL)
    api_stt = recommendations.group(LogicalCapability.SPEECH_STT, ProviderKind.API)
    local_stt = recommendations.group(LogicalCapability.SPEECH_STT, ProviderKind.LOCAL)
    paid_web = recommendations.group(LogicalCapability.WEB_SEARCH_PAID, ProviderKind.API)
    local_web = recommendations.group(LogicalCapability.WEB_SEARCH, ProviderKind.LOCAL)

    assert [api_text.tier(tier).provider_model for tier in QualityTier] == [
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]
    assert [local_text.tier(tier).provider_model for tier in QualityTier] == [
        "Qwen/Qwen3.5-4B",
        "openai/gpt-oss-20b",
        "nvidia/Qwen3.6-27B-NVFP4",
    ]
    assert "harmony_format_required" in local_text.tier(QualityTier.BALANCED).constraints
    assert [local_vision.tier(tier).provider_model for tier in QualityTier] == [
        "Qwen/Qwen3.5-4B",
        "Qwen/Qwen3.5-9B",
        "nvidia/Qwen3.6-27B-NVFP4",
    ]
    assert all(
        tier.license_id == "apache-2.0" and tier.commercial_use == "allowed"
        for group in (local_text, local_vision)
        for _quality, tier in group.tiers
    )
    assert {
        "vllm_nightly_required",
        "modelopt_quantization_required",
        "blackwell_gpu_required",
        "official_validation_gb300_not_rtx5090",
    } <= set(local_text.tier(QualityTier.QUALITY).constraints)
    assert all(
        {
            "vllm_nightly_required",
            "modelopt_quantization_required",
            "blackwell_gpu_required",
            "official_validation_gb300_not_rtx5090",
        }
        <= set(tier.constraints)
        for group in recommendations.candidates
        for _quality, tier in group.tiers
        if tier.provider_model == "nvidia/Qwen3.6-27B-NVFP4"
    )
    assert [api_video.tier(tier).provider_model for tier in QualityTier] == [
        "veo-3.1-lite-generate-preview",
        "veo-3.1-fast-generate-preview",
        "veo-3.1-generate-preview",
    ]
    assert all(
        api_video.tier(tier).source_urls == ("https://ai.google.dev/gemini-api/docs/veo",) for tier in QualityTier
    )
    assert [
        {"resolution_720p", "duration_4_seconds"} <= set(api_video.tier(QualityTier.FAST).constraints),
        {"resolution_720p", "duration_6_seconds"} <= set(api_video.tier(QualityTier.BALANCED).constraints),
        {"resolution_4k", "duration_8_seconds"} <= set(api_video.tier(QualityTier.QUALITY).constraints),
    ] == [True, True, True]
    assert [local_video.tier(tier).provider_model for tier in QualityTier] == [
        "Wan-AI/Wan2.2-TI2V-5B",
        "Wan-AI/Wan2.2-TI2V-5B",
        "Lightricks/LTX-2.3-fp8",
    ]
    assert local_text.tier(QualityTier.QUALITY).source_urls[0] == ("https://huggingface.co/nvidia/Qwen3.6-27B-NVFP4")
    assert api_embedding.tier(QualityTier.FAST).provider_model == "gemini-embedding-2"
    assert api_embedding.tier(QualityTier.FAST).source_urls == (
        "https://ai.google.dev/gemini-api/docs/models/gemini-embedding-2",
    )
    assert "output_dimension_768" in api_embedding.tier(QualityTier.FAST).constraints
    assert all(
        tier.license_id == "apache-2.0" and tier.commercial_use == "allowed"
        for group in (local_embedding, local_rerank)
        for _quality, tier in group.tiers
    )
    assert [api_image.tier(tier).provider_model for tier in QualityTier] == [
        "gemini-3.1-flash-lite-image",
        "gemini-3.1-flash-image",
        "gemini-3-pro-image",
    ]
    assert [local_image.tier(tier).provider_model for tier in QualityTier] == [
        "black-forest-labs/FLUX.2-klein-4B",
        "black-forest-labs/FLUX.2-klein-4B",
        "Qwen/Qwen-Image-2512",
    ]
    assert [local_image_edit.tier(tier).provider_model for tier in QualityTier] == [
        "black-forest-labs/FLUX.2-klein-4B",
        "black-forest-labs/FLUX.2-klein-4B",
        "Qwen/Qwen-Image-Edit-2511",
    ]
    assert all(api_image_edit.tier(tier).provider_model == "gpt-image-2" for tier in QualityTier)
    assert all(
        not group.active
        and group.probe_required
        and all("image_editing" in recommendation.constraints for _tier, recommendation in group.tiers)
        for group in (api_image_edit, local_image_edit)
    )
    assert DEFAULT_CATALOG.capability_policy(LogicalCapability.IMAGE_GENERATION).risk is RiskLevel.HIGH
    assert DEFAULT_CATALOG.capability_policy(LogicalCapability.IMAGE_EDITING).risk is RiskLevel.HIGH
    assert DEFAULT_CATALOG.capability_policy(LogicalCapability.MUSIC_GENERATION).risk is RiskLevel.HIGH
    assert DEFAULT_CATALOG.capability_policy(LogicalCapability.AI_TEXT).risk is RiskLevel.MEDIUM
    assert [api_music.tier(tier).provider_model for tier in QualityTier] == [
        "lyria-3-clip-preview",
        "music_v2",
        "lyria-3-pro-preview",
    ]
    assert "mp3_and_wav_supported_pcm_contract_probe_required" in api_music.tier(QualityTier.BALANCED).constraints
    assert [local_music.tier(tier).provider_model for tier in QualityTier] == [
        "ACE-Step/Ace-Step1.5#acestep-v15-turbo+ACE-Step/acestep-5Hz-lm-0.6B",
        "ACE-Step/acestep-v15-xl-turbo+ACE-Step/Ace-Step1.5#acestep-5Hz-lm-1.7B",
        "ACE-Step/acestep-v15-xl-sft+ACE-Step/acestep-5Hz-lm-4B",
    ]
    assert "generator_subfolder_acestep-v15-turbo" in local_music.tier(QualityTier.FAST).constraints
    assert "language_model_profile_ACE-Step/acestep-5Hz-lm-0.6B" in local_music.tier(QualityTier.FAST).constraints
    assert "language_model_subfolder_acestep-5Hz-lm-1.7B" in local_music.tier(QualityTier.BALANCED).constraints
    assert [api_tts.tier(tier).provider_model for tier in QualityTier] == [
        "eleven_flash_v2_5",
        "gemini-3.1-flash-tts-preview",
        "eleven_v3",
    ]
    assert all("standard_custom_voice_only" in local_tts.tier(tier).constraints for tier in QualityTier)
    assert "voice_clone_profile_Qwen/Qwen3-TTS-12Hz-1.7B-Base_requires_identity_binding_and_audit" in (
        local_tts.tier(QualityTier.QUALITY).constraints
    )
    assert [api_stt.tier(tier).provider_model for tier in QualityTier] == [
        "gpt-4o-mini-transcribe",
        "scribe_v2",
        "gpt-4o-transcribe-diarize",
    ]
    assert [local_stt.tier(tier).provider_model for tier in QualityTier] == [
        "Qwen/Qwen3-ASR-0.6B-hf",
        "Qwen/Qwen3-ASR-1.7B-hf",
        "Qwen/Qwen3-ASR-1.7B-hf",
    ]
    assert all(
        {"transformers_min_version_5_13_0", "dependency_and_model_revision_pin_required"}
        <= set(local_stt.tier(tier).constraints)
        for tier in QualityTier
    )
    for _quality, tier in browser.tiers:
        assert {"planner_only", "isolated_worker_required", "host_pc_shell_filesystem_forbidden"} <= set(
            tier.constraints
        )
    assert all("web_search_tool_separate_from_model_id" in tier.constraints for _quality, tier in paid_web.tiers)
    assert all("searxng_separate_service" in tier.constraints for _quality, tier in local_web.tiers)

    manifest_text = json.dumps(recommendations.to_mapping(), ensure_ascii=False)
    for removed_id in (
        "gemini-3.1-flash-image-preview",
        "gemini-3-pro-image-preview",
        "gemini-2.5-flash-image",
        "gpt-4o-mini-tts",
        "gpt-realtime-whisper",
        "PaddlePaddle/PaddleOCR-VL-1.5",
        "PaddlePaddle/PaddleOCR-VL-1.6",
        "black-forest-labs/FLUX.2-klein-9B",
    ):
        assert removed_id not in manifest_text


@pytest.mark.parametrize(
    "case",
    [
        "unknown-key",
        "active",
        "probe",
        "duplicate",
        "http-url",
        "untrusted-host",
        "wrong-model-source",
    ],
)
def test_recommendation_manifest_rejects_representative_unsafe_shapes(case: str) -> None:
    raw = copy.deepcopy(_raw_manifest())
    candidates = raw["candidates"]
    assert isinstance(candidates, list)
    first = candidates[0]
    assert isinstance(first, dict)

    if case == "unknown-key":
        raw["unexpected"] = True
    elif case == "active":
        raw["active"] = True
    elif case == "probe":
        first["probe_required"] = False
    elif case == "duplicate":
        candidates.append(copy.deepcopy(first))
    elif case == "http-url":
        first["tiers"]["fast"]["source_urls"] = ["http://example.invalid/model"]
    elif case == "untrusted-host":
        first["tiers"]["fast"]["source_urls"] = ["https://example.invalid/not-primary"]
    else:
        local = next(
            candidate
            for candidate in candidates
            if candidate["kind"] == "local" and candidate["capability"] == "ai.text.generate"
        )
        local["tiers"]["fast"]["source_urls"] = [
            "https://huggingface.co/Qwen/not-the-selected-model",
            "https://docs.vllm.ai/en/latest/getting_started/installation/gpu/",
        ]

    with pytest.raises(RecommendationValidationError):
        ProviderRecommendations.from_mapping(raw)


def test_loading_recommendations_does_not_change_active_catalog_revision() -> None:
    assert DEFAULT_CATALOG.content_revision == ACTIVE_CATALOG_REVISION
    load_default_recommendations()
    assert DEFAULT_CATALOG.content_revision == ACTIVE_CATALOG_REVISION
    for capability in LogicalCapability:
        route = DEFAULT_CATALOG.route(capability)
        assert route is not None
        if capability is LogicalCapability.WEB_SEARCH:
            assert all(tier.provider_ids == ("searxng.local",) for tier in route.tiers)
        else:
            assert all(not tier.provider_ids for tier in route.tiers)


def test_recommendation_revision_is_deterministic_roundtrip_and_state_is_frozen() -> None:
    recommendations = load_default_recommendations()
    roundtrip = ProviderRecommendations.from_mapping(recommendations.to_mapping())

    assert roundtrip.to_mapping() == recommendations.to_mapping()
    assert roundtrip.recommendation_revision == recommendations.recommendation_revision
    with pytest.raises(TypeError):
        recommendations.hardware_profile["id"] = "changed"  # type: ignore[index]


def test_v2_manifest_replaces_old_example_and_docs_point_to_v2() -> None:
    payload = MANIFEST.read_bytes()
    assert MANIFEST.is_file()
    assert not OLD_MANIFEST.exists()
    assert not payload.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in payload
    assert payload.endswith(b"\n")
    assert b'"readiness_claim"' not in payload
    assert b'"live_success_claim"' not in payload
    references = "\n".join(
        (
            (ROOT / "docs" / "MODEL_CATALOG_RECOMMENDATIONS_20260722.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "PROVIDER_AND_MODEL_ARCHITECTURE.md").read_text(encoding="utf-8"),
        )
    )
    assert "provider-recommendations.rtx5090.v2.json" in references
    assert "provider-recommendations.rtx5090.example.json" not in references
