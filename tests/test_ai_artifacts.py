from __future__ import annotations

import os

import pytest

from yonerai_discord.modules.ai.artifacts import ArtifactStore, ArtifactStoreError, safe_display_slug


def test_html_artifact_is_atomic_utf8_and_prompt_slug_keeps_japanese(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "data" / "artifacts")
    record = store.save_html("猫カフェの紹介ページを作って", "<!doctype html><title>猫</title>", scope="guild-1")

    assert record.path.read_text(encoding="utf-8") == "<!doctype html><title>猫</title>"
    assert record.path.parent.name == record.artifact_id
    assert "猫カフェ" in record.filename
    assert record.path.is_relative_to(tmp_path / "data" / "artifacts")
    assert len(record.sha256) == 64


@pytest.mark.parametrize("scope", ["../escape", "a/b", "", ".", "guild:1"])
def test_scope_rejects_path_traversal(tmp_path, scope: str) -> None:
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "artifacts").save_html("x", "<html></html>", scope=scope)


def test_size_limit_rejects_before_write(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", max_bytes=8)
    with pytest.raises(ArtifactStoreError, match="size"):
        store.save_html("x", "<html>too long</html>", scope="guild-1")
    assert not (tmp_path / "artifacts").exists()


def test_symlink_root_is_rejected(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "artifacts"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(ArtifactStoreError, match="symlink"):
        ArtifactStore(link).save_html("x", "<html></html>", scope="guild-1")


def test_display_slug_drops_path_characters_and_reserved_names() -> None:
    assert safe_display_slug("../危険:ページ?.html") == "危険-ページ-.html"
    assert safe_display_slug("CON") == "yonerai-web"
