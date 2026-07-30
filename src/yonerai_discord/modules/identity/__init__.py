"""再起動耐性のあるDiscord本人確認と安全な外部callback境界。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yonerai_discord.runtime_readiness import publish_runtime_readiness, withdraw_runtime_readiness

from .callback import IdentityCallbackService
from .discord_adapter import (
    DiscordVerificationRoleGranter,
    UnsafeVerificationRole,
    VerifyGroup,
    validate_verification_role,
)
from .http import GuardResult, IdentityHttpGuard, RequestMetadata, security_headers
from .models import (
    CallbackResult,
    ChallengeBinding,
    ChallengeIntent,
    ChallengePurpose,
    ClaimedChallenge,
    GuildVerificationConfig,
    IdentityFeatures,
    IdentityPolicy,
    IssuedChallenge,
)
from .ports import ChallengeRepository, TurnstileVerifier, VerificationRoleGranter
from .repository import InMemoryChallengeRepository
from .runtime_config import IdentityRuntimeConfig, IdentityRuntimeConfigurationError
from .service import FeatureDisabledError, IdentityService, InvalidIdentityConfiguration
from .sqlite_repository import SQLiteChallengeRepository
from .turnstile import CloudflareTurnstileVerifier, TURNSTILE_SITEVERIFY_URL
from .web_adapter import IdentityWebConfig, IdentityWebConfigurationError, IdentityWebServer


logger = logging.getLogger(__name__)
_RUNTIME_CAPABILITIES = (
    "cap-run-verify-status",
    "cap-run-verify-start",
    "cap-run-verify-configure",
)


class IdentityPlugin:
    def __init__(self) -> None:
        self.repository: SQLiteChallengeRepository | None = None
        self.identity: IdentityService | None = None
        self.callback: IdentityCallbackService | None = None
        self.web: IdentityWebServer | None = None
        self._bot: Any | None = None

    async def start(self, bot: Any) -> None:
        repository = SQLiteChallengeRepository(Path(bot.settings.database_path))
        repository.open()
        web: IdentityWebServer | None = None
        try:
            config = IdentityRuntimeConfig.load(bot.settings)
            identity = self._build_identity(repository, config)
            callback = self._build_callback(bot, repository, identity, config)
            if config.http_enabled:
                if callback is None:
                    raise IdentityRuntimeConfigurationError("identity HTTP requires a valid enabled callback")
                web = IdentityWebServer(
                    callback,
                    IdentityWebConfig(
                        bind_host=config.http_host,
                        bind_port=config.http_port,
                        public_base_url=config.public_base_url,
                        turnstile_site_key=config.turnstile_site_key,
                        captcha_required=bool(config.turnstile_secret),
                    ),
                )
                await web.start()
            bot.tree.add_command(
                VerifyGroup(
                    bot,
                    repository,
                    identity if callback is not None else None,
                    global_enabled=config.enabled,
                )
            )
        except Exception:
            if web is not None:
                await web.stop()
            repository.close()
            raise
        self.repository = repository
        self.identity = identity
        self.callback = callback
        self.web = web
        self._bot = bot
        setattr(bot, "identity_callback_service", callback)
        setattr(bot, "identity_web_server", web)
        publish_runtime_readiness(
            bot,
            {
                "cap-run-verify-status": True,
                "cap-run-verify-start": callback is not None and config.enabled,
                "cap-run-verify-configure": True,
            },
        )

    @staticmethod
    def _build_identity(
        repository: SQLiteChallengeRepository,
        config: IdentityRuntimeConfig,
    ) -> IdentityService | None:
        if not config.enabled or not config.callback_configured:
            return None
        policy = IdentityPolicy(
            public_base_url=config.public_base_url,
            features=IdentityFeatures(member_verification=True),
            captcha_configured=bool(config.turnstile_secret),
            allow_insecure_localhost=config.allow_insecure_localhost,
        )
        try:
            return IdentityService(repository, policy)
        except InvalidIdentityConfiguration:
            logger.error("identity_configuration_rejected")
            return None

    @staticmethod
    def _build_callback(
        bot: Any,
        repository: SQLiteChallengeRepository,
        identity: IdentityService | None,
        config: IdentityRuntimeConfig,
    ) -> IdentityCallbackService | None:
        if identity is None:
            return None
        verifier: TurnstileVerifier | None = None
        if identity.policy.captcha_configured:
            hostname = urlparse(identity.policy.public_base_url).hostname or ""
            try:
                verifier = CloudflareTurnstileVerifier(
                    config.turnstile_secret,
                    expected_hostname=hostname,
                )
            except ValueError:
                logger.error("turnstile_configuration_rejected")
                return None
        try:
            guard = IdentityHttpGuard(identity.policy, verifier)
        except ValueError:
            logger.error("identity_http_guard_rejected")
            return None
        granter = DiscordVerificationRoleGranter(bot, repository)
        return IdentityCallbackService(identity, guard, granter)

    async def stop(self) -> None:
        if self.web is not None:
            await self.web.stop()
        if self._bot is not None:
            withdraw_runtime_readiness(self._bot, _RUNTIME_CAPABILITIES)
            self._bot.tree.remove_command("verify")
            if getattr(self._bot, "identity_callback_service", None) is self.callback:
                delattr(self._bot, "identity_callback_service")
            if getattr(self._bot, "identity_web_server", None) is self.web:
                delattr(self._bot, "identity_web_server")
        if self.repository is not None:
            self.repository.close()
        self.repository = None
        self.identity = None
        self.callback = None
        self.web = None
        self._bot = None


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("identity", IdentityPlugin)


__all__ = [
    "CallbackResult",
    "ChallengeBinding",
    "ChallengeIntent",
    "ChallengePurpose",
    "ChallengeRepository",
    "ClaimedChallenge",
    "CloudflareTurnstileVerifier",
    "DiscordVerificationRoleGranter",
    "FeatureDisabledError",
    "GuardResult",
    "GuildVerificationConfig",
    "IdentityCallbackService",
    "IdentityFeatures",
    "IdentityHttpGuard",
    "IdentityPlugin",
    "IdentityPolicy",
    "IdentityRuntimeConfig",
    "IdentityRuntimeConfigurationError",
    "IdentityWebConfig",
    "IdentityWebConfigurationError",
    "IdentityWebServer",
    "IdentityService",
    "InMemoryChallengeRepository",
    "InvalidIdentityConfiguration",
    "IssuedChallenge",
    "RequestMetadata",
    "SQLiteChallengeRepository",
    "TURNSTILE_SITEVERIFY_URL",
    "TurnstileVerifier",
    "UnsafeVerificationRole",
    "VerificationRoleGranter",
    "VerifyGroup",
    "security_headers",
    "setup",
    "validate_verification_role",
]
