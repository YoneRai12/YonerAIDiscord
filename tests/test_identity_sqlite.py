from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from yonerai_discord.modules.identity import (
    ChallengeBinding,
    ChallengeIntent,
    ChallengePurpose,
    IdentityFeatures,
    IdentityPolicy,
    IdentityService,
    SQLiteChallengeRepository,
)


NOW = datetime(2026, 7, 21, 6, 0, tzinfo=UTC)


def _service(repository: SQLiteChallengeRepository, *, lease: int = 120) -> IdentityService:
    return IdentityService(
        repository,
        IdentityPolicy(
            public_base_url="https://identity.example.test",
            features=IdentityFeatures(member_verification=True),
            captcha_configured=True,
            claim_lease_seconds=lease,
        ),
    )


def _repository(path: Path) -> SQLiteChallengeRepository:
    repository = SQLiteChallengeRepository(path)
    repository.open()
    return repository


def test_plaintext_token_is_absent_and_challenge_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite3"
    first = _repository(path)
    service = _service(first)
    binding = ChallengeBinding(100, 200, ChallengeIntent.VERIFY_MEMBER)
    issued = service.issue(ChallengePurpose.VERIFICATION, binding, now=NOW)
    assert issued.token not in repr(first.snapshot())
    first.close()

    second = _repository(path)
    claimed = _service(second).claim_for_intent(
        issued.token,
        ChallengePurpose.VERIFICATION,
        ChallengeIntent.VERIFY_MEMBER,
        now=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    assert claimed.binding == binding
    assert issued.token.encode("ascii") not in path.read_bytes()
    second.close()


def test_atomic_claim_across_independent_connections(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite3"
    issuer = _repository(path)
    binding = ChallengeBinding(100, 200, ChallengeIntent.VERIFY_MEMBER)
    issued = _service(issuer).issue(ChallengePurpose.VERIFICATION, binding, now=NOW)
    competitors = [_repository(path) for _ in range(8)]

    def claim(repository: SQLiteChallengeRepository):
        return _service(repository).claim_for_intent(
            issued.token,
            ChallengePurpose.VERIFICATION,
            ChallengeIntent.VERIFY_MEMBER,
            now=NOW,
        )

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(claim, competitors))
        assert sum(result is not None for result in results) == 1
    finally:
        issuer.close()
        for repository in competitors:
            repository.close()


def test_claim_lease_can_be_recovered_but_finalize_is_one_time(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite3"
    repository = _repository(path)
    service = _service(repository, lease=5)
    binding = ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER)
    issued = service.issue(ChallengePurpose.VERIFICATION, binding, now=NOW)
    first = service.claim_for_intent(
        issued.token,
        ChallengePurpose.VERIFICATION,
        ChallengeIntent.VERIFY_MEMBER,
        now=NOW,
    )
    assert first is not None
    assert (
        service.claim_for_intent(
            issued.token,
            ChallengePurpose.VERIFICATION,
            ChallengeIntent.VERIFY_MEMBER,
            now=NOW + timedelta(seconds=4),
        )
        is None
    )
    recovered = service.claim_for_intent(
        issued.token,
        ChallengePurpose.VERIFICATION,
        ChallengeIntent.VERIFY_MEMBER,
        now=NOW + timedelta(seconds=5),
    )
    assert recovered is not None and recovered.claim_id != first.claim_id
    assert not service.finalize(first, now=NOW + timedelta(seconds=6))
    assert service.finalize(recovered, now=NOW + timedelta(seconds=6))
    repository.close()

    reopened = _repository(path)
    assert (
        _service(reopened).claim_for_intent(
            issued.token,
            ChallengePurpose.VERIFICATION,
            ChallengeIntent.VERIFY_MEMBER,
            now=NOW + timedelta(seconds=7),
        )
        is None
    )
    reopened.close()


def test_release_and_latest_url_replacement_are_persistent(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite3"
    repository = _repository(path)
    service = _service(repository)
    binding = ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER)
    old = service.issue(ChallengePurpose.VERIFICATION, binding, now=NOW)
    latest = service.issue(
        ChallengePurpose.VERIFICATION,
        binding,
        now=NOW + timedelta(seconds=1),
    )
    assert (
        service.claim_for_intent(
            old.token,
            ChallengePurpose.VERIFICATION,
            ChallengeIntent.VERIFY_MEMBER,
            now=NOW + timedelta(seconds=2),
        )
        is None
    )
    claimed = service.claim_for_intent(
        latest.token,
        ChallengePurpose.VERIFICATION,
        ChallengeIntent.VERIFY_MEMBER,
        now=NOW + timedelta(seconds=2),
    )
    assert claimed is not None and service.release(claimed)
    repository.close()

    reopened = _repository(path)
    assert (
        _service(reopened).claim_for_intent(
            latest.token,
            ChallengePurpose.VERIFICATION,
            ChallengeIntent.VERIFY_MEMBER,
            now=NOW + timedelta(seconds=3),
        )
        is not None
    )
    reopened.close()


def test_guild_opt_in_configuration_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite3"
    repository = _repository(path)
    saved = repository.configure_guild(10, 20, True, 30, now=NOW)
    assert saved.enabled
    repository.close()

    reopened = _repository(path)
    loaded = reopened.guild_config(10)
    assert loaded is not None
    assert (loaded.guild_id, loaded.verified_role_id, loaded.updated_by) == (10, 20, 30)
    assert loaded.updated_at == NOW
    reopened.close()
