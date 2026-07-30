from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from html import escape
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from aiohttp import web

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.deployment_current_truth import M10CurrentTruthV1, build_m10_current_truth

from .config import AdminUiConfig
from .projection import AdminUiProjection, GuildAuditSummarySource, build_admin_ui_projection


ADMIN_UI_CAPABILITY_ID = "cap-run-admin-ui-read"
_GUILD_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,18}$")
_MAX_DISCORD_ID = 2**63 - 1
_SECURITY_HEADERS: Mapping[str, str] = MappingProxyType(
    {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
)
_ERROR_MESSAGES = MappingProxyType(
    {
        401: "認証を確認できませんでした。",
        403: "このguildの管理情報を表示する権限がありません。",
        404: "指定された管理画面は利用できません。",
        405: "この管理画面はGETだけを受け付けます。",
        503: "管理画面は現在利用できません。",
    }
)
_PROTOCOL_LOGGER = logging.Logger("yonerai_discord.admin_ui.protocol")
_PROTOCOL_LOGGER.addHandler(logging.NullHandler())
_PROTOCOL_LOGGER.propagate = False


class AdminUiAuthenticator(Protocol):
    """OAuth/session実装が後から注入する、user IDだけの最小境界。"""

    async def current_user_id(self, request: object) -> int | None: ...


class AdminUiServer(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AdminUiHttpResponse:
    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=lambda: _SECURITY_HEADERS)


class AdminUiRequestHandler:
    def __init__(
        self,
        *,
        bot: Any,
        authenticator: AdminUiAuthenticator | None,
        audit_source: GuildAuditSummarySource,
    ) -> None:
        self._bot = bot
        self._authenticator = authenticator
        self._audit_source = audit_source

    async def handle(
        self,
        request: object,
        *,
        method: str,
        guild_id_text: str,
    ) -> AdminUiHttpResponse:
        if method != "GET":
            return _error_response(405)
        guild_id = _parse_guild_id(guild_id_text)
        if guild_id is None:
            return _error_response(404)
        if self._authenticator is None:
            return _error_response(503)
        try:
            user_id = await self._current_user_id(request)
            if user_id is None:
                return _error_response(401)
            if not await self._authorize_user(guild_id, user_id):
                return _error_response(403)
            registry = getattr(self._bot, "capability_registry", None)
            if registry is None:
                return _error_response(503)
            deployment_truth, deployment_identity = _deployment_current_truth(self._bot)
            projection = await asyncio.to_thread(
                build_admin_ui_projection,
                guild_id=guild_id,
                registry=registry,
                audit_source=self._audit_source,
                deployment_truth=deployment_truth,
            )
            final_user_id = await self._current_user_id(request)
            if final_user_id != user_id:
                return _error_response(403)
            if not await self._authorize_user(guild_id, user_id):
                return _error_response(403)
            _final_truth, final_identity = _deployment_current_truth(self._bot)
            if final_identity is not deployment_identity:
                return _error_response(503)
            return AdminUiHttpResponse(200, render_admin_ui(projection))
        except asyncio.CancelledError:
            raise
        except Exception:
            return _error_response(503)

    async def _authorize_user(
        self,
        guild_id: int,
        user_id: int,
    ) -> bool:
        get_guild = getattr(self._bot, "get_guild", None)
        guild = get_guild(guild_id) if callable(get_guild) else None
        if guild is None or getattr(guild, "id", None) != guild_id:
            return False
        fetch_member = getattr(guild, "fetch_member", None)
        guard = getattr(self._bot, "capability_guard", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        currently_allowed = getattr(guard, "currently_allowed", None)
        if not callable(fetch_member) or not callable(evaluate) or not callable(currently_allowed):
            return False
        try:
            member = await fetch_member(user_id)
            if getattr(member, "id", None) != user_id:
                return False
            decision = await evaluate(ADMIN_UI_CAPABILITY_ID, guild=guild, member=member)
            actor_level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
            if getattr(decision, "allowed", False) is not True or actor_level < RbacLevel.GUILD_ADMIN:
                return False
            current_result = currently_allowed(
                ADMIN_UI_CAPABILITY_ID,
                guild_id=guild_id,
                user_id=user_id,
                actor_level=actor_level,
                floor=RbacLevel.GUILD_ADMIN,
            )
            if current_result is not True:
                return False
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True

    async def _current_user_id(self, request: object) -> int | None:
        authenticator = self._authenticator
        current = getattr(authenticator, "current_user_id", None)
        if not callable(current):
            return None
        try:
            value = await current(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_DISCORD_ID:
            return None
        return value


class AdminUiWebServer:
    """loopback GET-only adapter。proxy headerとaccess logを使用しない。"""

    def __init__(self, handler: AdminUiRequestHandler, config: AdminUiConfig) -> None:
        self._handler = handler
        self.config = config
        self._runner: web.AppRunner | None = None

    def create_application(self) -> web.Application:
        application = web.Application(
            client_max_size=1_024,
            middlewares=[self._security_middleware],
        )
        application.router.add_get(
            "/admin/guild/{guild_id}",
            self._get_guild,
            allow_head=False,
        )
        return application

    async def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("admin UI server is already running")
        runner = web.AppRunner(
            self.create_application(),
            access_log=None,
            handle_signals=False,
            logger=_PROTOCOL_LOGGER,
        )
        self._runner = runner
        try:
            await runner.setup()
            site = web.TCPSite(
                runner,
                host=self.config.bind_host,
                port=self.config.bind_port,
                shutdown_timeout=5.0,
            )
            await site.start()
        except BaseException:
            try:
                await runner.cleanup()
            except BaseException:
                pass
            else:
                self._runner = None
            raise

    async def stop(self) -> None:
        runner = self._runner
        if runner is not None:
            await runner.cleanup()
            self._runner = None

    @web.middleware
    async def _security_middleware(self, request: web.Request, handler: Any) -> web.Response:
        if request.method != "GET":
            return _to_web_response(_error_response(405))
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = _error_response(exc.status if exc.status in _ERROR_MESSAGES else 404)
        except asyncio.CancelledError:
            raise
        except Exception:
            response = _error_response(503)
        if isinstance(response, web.Response):
            for key, value in _SECURITY_HEADERS.items():
                response.headers[key] = value
            return response
        return _to_web_response(response)

    async def _get_guild(self, request: web.Request) -> web.Response:
        response = await self._handler.handle(
            request,
            method=request.method,
            guild_id_text=request.match_info.get("guild_id", ""),
        )
        return _to_web_response(response)


def render_admin_ui(projection: AdminUiProjection) -> str:
    modules = "".join(
        "<tr><td>"
        + escape(row.module_id)
        + "</td><td>"
        + ("ON" if row.configured_enabled else "OFF")
        + "</td><td>"
        + escape(row.status_code)
        + "</td></tr>"
        for row in projection.modules
    )
    capabilities = "".join(
        "<tr><td>"
        + escape(row.capability_id)
        + "</td><td>"
        + escape(row.module_id)
        + "</td><td>"
        + escape(row.name)
        + "</td><td>"
        + escape(row.required_level)
        + "</td><td>"
        + escape(row.status_code)
        + "</td><td>"
        + escape(row.runtime_readiness)
        + "</td></tr>"
        for row in projection.capabilities
    )
    audit = "".join(
        "<tr><td>"
        + str(row.id)
        + "</td><td>"
        + escape(row.event)
        + "</td><td>"
        + escape(row.plugin or "-")
        + "</td><td>"
        + ("-" if row.actor_id is None else str(row.actor_id))
        + "</td><td>"
        + escape(row.created_at)
        + "</td></tr>"
        for row in projection.audit
    )
    deployment = projection.deployment
    sources = "".join(
        "<tr><td>"
        + escape(row.name)
        + "</td><td>"
        + ("yes" if row.configured else "no")
        + "</td><td>"
        + ("yes" if row.ready else "no")
        + "</td><td>"
        + escape(row.live_success)
        + "</td><td>"
        + escape(row.blocker or "-")
        + "</td></tr>"
        for row in deployment.sources
    )
    deployment_summary = (
        "<dl>"
        f"<dt>schema</dt><dd>{escape(deployment.schema_version)}</dd>"
        f"<dt>selection configured</dt><dd>{'yes' if deployment.selection_configured else 'no'}</dd>"
        f"<dt>selected topology</dt><dd>{escape(deployment.selected_topology or '-')}</dd>"
        f"<dt>effective topology</dt><dd>{escape(deployment.effective_topology)}</dd>"
        f"<dt>hosting profile</dt><dd>{escape(deployment.hosting_profile or '-')}</dd>"
        f"<dt>packaging</dt><dd>{escape(deployment.packaging or '-')}</dd>"
        f"<dt>available ports</dt><dd>{escape(', '.join(deployment.available_ports) or '-')}</dd>"
        f"<dt>missing ports</dt><dd>{escape(', '.join(deployment.missing_ports) or '-')}</dd>"
        f"<dt>exact blockers</dt><dd>{escape(', '.join(deployment.blockers) or '-')}</dd>"
        "</dl>"
    )
    return (
        "<!doctype html><html lang=ja><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>YonerAI 管理状態</title>"
        "<style>body{font-family:sans-serif;margin:2rem;max-width:100rem}"
        "table{border-collapse:collapse;width:100%;margin-bottom:2rem}"
        "th,td{border:1px solid #bbb;padding:.35rem;text-align:left}</style>"
        "</head><body><h1>YonerAI 管理状態</h1>"
        f"<p>選択guild: {projection.guild_id}</p>"
        "<p><strong>注意:</strong> この静的projectionは実行権限、runtime readiness、live成功、"
        "外部接続成功を保証しません。</p>"
        "<h2>Modules</h2><table><thead><tr><th>ID</th><th>設定</th><th>状態</th></tr></thead>"
        f"<tbody>{modules}</tbody></table>"
        "<h2>Capabilities</h2><table><thead><tr><th>ID</th><th>Module</th><th>名前</th>"
        "<th>RBAC</th><th>状態</th><th>readiness</th></tr></thead>"
        f"<tbody>{capabilities}</tbody></table>"
        "<h2>Deployment current truth</h2>"
        "<p><strong>注意:</strong> この表示はreadinessやlive成功を推測しません。"
        "未設定・missing port・blockerはそのまま表示します。</p>"
        f"{deployment_summary}"
        "<table><thead><tr><th>Source</th><th>configured</th><th>ready</th>"
        "<th>live success</th><th>blocker</th></tr></thead>"
        f"<tbody>{sources}</tbody></table>"
        "<h2>Redacted audit</h2><table><thead><tr><th>ID</th><th>Event</th><th>Plugin</th>"
        f"<th>Actor</th><th>Created</th></tr></thead><tbody>{audit}</tbody></table>"
        "</body></html>"
    )


def _parse_guild_id(value: object) -> int | None:
    if not isinstance(value, str) or not _GUILD_ID_PATTERN.fullmatch(value):
        return None
    parsed = int(value)
    return parsed if parsed <= _MAX_DISCORD_ID else None


def _deployment_current_truth(bot: object) -> tuple[M10CurrentTruthV1, M10CurrentTruthV1 | None]:
    current = getattr(bot, "deployment_current_truth_current", None)
    if current is not None:
        if not callable(current):
            return build_m10_current_truth(), None
        try:
            refreshed = current()
        except Exception:
            return build_m10_current_truth(), None
        if type(refreshed) is M10CurrentTruthV1 and getattr(bot, "deployment_current_truth", None) is refreshed:
            return refreshed, refreshed
        return build_m10_current_truth(), None
    value = getattr(bot, "deployment_current_truth", None)
    if type(value) is M10CurrentTruthV1:
        return value, value
    return build_m10_current_truth(), None


def _error_response(status: int) -> AdminUiHttpResponse:
    message = _ERROR_MESSAGES.get(status, _ERROR_MESSAGES[503])
    body = (
        "<!doctype html><html lang=ja><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>YonerAI 管理状態</title></head><body><p>" + escape(message) + "</p></body></html>"
    )
    return AdminUiHttpResponse(status, body)


def _to_web_response(response: AdminUiHttpResponse) -> web.Response:
    return web.Response(
        text=response.body,
        status=response.status,
        content_type="text/html",
        charset="utf-8",
        headers=dict(response.headers),
    )


__all__ = [
    "ADMIN_UI_CAPABILITY_ID",
    "AdminUiAuthenticator",
    "AdminUiHttpResponse",
    "AdminUiRequestHandler",
    "AdminUiServer",
    "AdminUiWebServer",
    "render_admin_ui",
]
