from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / "infra" / "search-sandbox"
SEARXNG_INDEX_DIGEST = "sha256:d0aaeb14880e6e92bde1518fcc7261e995783367d63d95203383607bef9c6516"
SEARXNG_FROM = f"FROM docker.io/searxng/searxng@{SEARXNG_INDEX_DIGEST}"


def _read(name: str) -> str:
    return (ASSET_ROOT / name).read_text(encoding="utf-8")


def test_images_and_gateway_runtime_are_version_fixed() -> None:
    searxng = _read("Dockerfile.searxng")
    gateway = _read("Dockerfile.gateway")
    gateway_lock = _read("gateway-requirements.lock")

    assert searxng.startswith(f"{SEARXNG_FROM}\n")
    assert gateway.startswith(f"{SEARXNG_FROM}\n")
    assert ":latest" not in searxng.casefold()
    assert ":latest" not in gateway.casefold()
    assert "COPY --chown=searxng:searxng settings.yml /usr/local/searxng/settings.template.yml" in searxng
    assert "import yonerai_discord.search_fabric.server" in gateway
    assert "--only-binary=:all:" in gateway
    assert "aiohttp==3.14.1" in gateway_lock
    assert all("==" in line for line in gateway_lock.splitlines() if line)
    assert (
        'ENTRYPOINT ["/usr/local/searxng/.venv/bin/python", "-m", "yonerai_discord.search_fabric.server"]'
    ) in gateway
    assert ('CMD ["--bind", "0.0.0.0", "--port", "8787", "--searxng-origin", "http://searxng:8080"]') in gateway


def test_gateway_build_context_excludes_repo_secrets_and_host_state() -> None:
    patterns = _read("Dockerfile.gateway.dockerignore").splitlines()

    assert patterns[0] == "**"
    assert patterns == [
        "**",
        "!src/",
        "!src/yonerai_discord/",
        "!src/yonerai_discord/**",
        "!infra/",
        "!infra/search-sandbox/",
        "!infra/search-sandbox/gateway-requirements.lock",
    ]
    assert not any(".env" in line or ".git" in line or "test" in line for line in patterns[1:])


def test_compose_exposes_only_gateway_on_host_loopback() -> None:
    compose = _read("compose.yaml")

    assert compose.count("\n  searxng:\n") == 1
    assert compose.count("\n  gateway:\n") == 1
    assert '      - "8080"\n' in compose
    assert compose.count('      - "127.0.0.1:8787:8787"\n') == 1
    assert compose.count("      - private_gateway\n") == 2
    assert compose.count("      - search_egress\n") == 1
    assert "  private_gateway:\n    driver: bridge\n    internal: true\n" in compose
    assert "  search_egress:\n    driver: bridge\n" in compose
    assert "      searxng:\n        condition: service_healthy\n        restart: false\n" in compose


def test_compose_has_no_host_mount_secret_or_host_environment_input() -> None:
    compose = _read("compose.yaml")

    for forbidden in ("\nvolumes:", "\nsecrets:", "\nconfigs:", "\n    environment:", "\n    env_file:"):
        assert forbidden not in compose
    for exact, count in (
        ('    restart: "no"\n', 2),
        ("    init: true\n", 2),
        ("    read_only: true\n", 2),
        ('    user: "searxng:searxng"\n', 2),
        ("      - ALL\n", 2),
        ("      - no-new-privileges:true\n", 2),
        ("    stop_grace_period: 10s\n", 2),
        ("        - CMD\n", 2),
        ("      driver: local\n", 2),
        ("        max-size: 1m\n", 2),
        ('        max-file: "1"\n', 2),
    ):
        assert compose.count(exact) == count
    assert compose.count("    pids_limit: ") == 2
    assert compose.count("    mem_limit: ") == 2
    assert compose.count("    cpus: ") == 2
    assert compose.count("    tmpfs:\n") == 2


def test_searxng_settings_are_json_only_and_engine_allowlisted() -> None:
    settings = _read("settings.yml")

    for expected in (
        "    keep_only:\n      - duckduckgo\n      - wikipedia\n",
        "  formats:\n    - json\n",
        "  safe_search: 2\n",
        '  autocomplete: ""\n',
        '  favicon_resolver: ""\n',
        "  max_page: 1\n",
        '  bind_address: "0.0.0.0"\n',
        "  port: 8080\n",
        '  secret_key: "ultrasecretkey"\n',
        "  limiter: false\n",
        "  public_instance: false\n",
        "  image_proxy: false\n",
        '  method: "POST"\n',
        "    X-Content-Type-Options: nosniff\n",
        '    X-Robots-Tag: "noindex, nofollow"\n',
        "    Referrer-Policy: no-referrer\n",
        "  debug: false\n",
        "  request_timeout: 3.0\n",
        "  max_request_timeout: 5.0\n",
    ):
        assert expected in settings


def test_readme_records_pin_limiter_license_and_unverified_boundary() -> None:
    readme = _read("README.md")

    for expected in (
        SEARXNG_INDEX_DIGEST,
        "sha256:fa1b0523e5a66c374fc04f4471f7ab54a718f33f864e8409e0db0133041eab3a",
        "2026.7.26-b060c780d",
        "AGPL-3.0-or-later",
        "https://docs.searxng.org/dev/search_api.html",
        "https://docs.searxng.org/admin/settings/settings.html",
        "https://docs.searxng.org/admin/settings/settings_search.html",
        "https://docs.searxng.org/admin/settings/settings_server.html",
        "https://docs.searxng.org/admin/searx.limiter.html",
        "https://docs.searxng.org/admin/installation-docker.html",
        "https://github.com/searxng/searxng/blob/master/LICENSE",
        "live readinessは未検証",
        "Valkey",
        "retentionは0",
    ):
        assert expected in readme
    assert "latest`ではなく" in readme
    assert "public ingressへ" in readme
    assert "変える場合、このtemplateは使用不可" in readme


def test_assets_are_strict_utf8_lf_without_bom() -> None:
    expected = {
        "README.md",
        "compose.yaml",
        "Dockerfile.gateway",
        "Dockerfile.gateway.dockerignore",
        "Dockerfile.searxng",
        "gateway-requirements.lock",
        "LICENSES.md",
        "settings.yml",
        "VERSION.lock",
    }
    assert {path.name for path in ASSET_ROOT.iterdir() if path.is_file()} == expected

    for path in (*ASSET_ROOT.iterdir(), Path(__file__)):
        if not path.is_file():
            continue
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" not in data
        assert data.endswith(b"\n")
        data.decode("utf-8", errors="strict")
