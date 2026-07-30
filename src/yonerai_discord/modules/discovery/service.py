from __future__ import annotations

from collections.abc import Callable, Mapping, Set
from math import ceil

from ...control_plane import ActorContext, PolicyEngine, RbacLevel, Registry
from .domain import (
    MAX_INDEXED_COMMANDS,
    MAX_PAGE,
    MAX_QUERY_LENGTH,
    MAX_RESPONSE_LENGTH,
    PAGE_SIZE,
    CommandEntry,
    CommandPage,
    DiscoveryInputError,
    DiscoveryUnavailableError,
)


_CORE_COMMAND_ROOTS = frozenset({"system"})


class DiscoveryService:
    """実効policyと現在のsurfaceから安全なコマンドindexを構築する。"""

    def __init__(
        self,
        registry: Registry,
        *,
        command_capabilities: Mapping[str, str],
        command_plugins: Mapping[str, str],
        command_floors: Mapping[str, RbacLevel] | None = None,
        plugin_is_running: Callable[[str], bool],
    ) -> None:
        if not isinstance(registry, Registry):
            raise TypeError("registry must be Registry")
        try:
            capability_snapshot = dict(command_capabilities)
            plugin_snapshot = dict(command_plugins)
            floor_snapshot = dict(command_floors or {})
        except Exception as exc:
            raise DiscoveryUnavailableError("command inventory cannot be read safely") from exc
        if len(capability_snapshot) > MAX_INDEXED_COMMANDS:
            raise DiscoveryUnavailableError("command inventory exceeds the safe maximum")
        self._registry = registry
        self._command_capabilities = capability_snapshot
        self._command_plugins = plugin_snapshot
        self._command_floors = floor_snapshot
        self._plugin_is_running = plugin_is_running
        self._policy = PolicyEngine(registry)

    def search(
        self,
        actor: ActorContext,
        *,
        live_command_paths: Set[str],
        query: str | None = None,
        page: int = 1,
    ) -> CommandPage:
        if not isinstance(actor, ActorContext):
            raise TypeError("actor must be ActorContext")
        normalized_query = _normalize_query(query)
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= MAX_PAGE:
            raise DiscoveryInputError(f"page must be between 1 and {MAX_PAGE}")
        if actor.guild_id is None:
            return CommandPage((), page, 1, 0, out_of_range=page > 1)
        if len(live_command_paths) > MAX_INDEXED_COMMANDS:
            raise DiscoveryUnavailableError("live command inventory exceeds the safe maximum")

        try:
            live = frozenset(_normalize_path(path) for path in live_command_paths)
        except (TypeError, ValueError) as exc:
            raise DiscoveryUnavailableError("live command inventory is invalid") from exc

        entries: list[CommandEntry] = []
        for raw_path, capability_id in sorted(self._command_capabilities.items()):
            try:
                path = _normalize_path(raw_path)
                if path not in live or not self._surface_plugin_is_running(path):
                    continue
                capability = self._registry.capability(capability_id)
                decision = self._policy.evaluate(capability_id, actor)
                if not decision.allowed or decision.required_level is None:
                    continue
                surface_floor = RbacLevel.parse(self._command_floors.get(path, RbacLevel.EVERYONE))
                required_level = max(decision.required_level, surface_floor)
                if actor.level < required_level:
                    continue
                entry = CommandEntry(
                    path=path,
                    description=(capability.name or "説明なし")[:256],
                    module_id=capability.module_id,
                    required_level=required_level,
                )
            except (KeyError, TypeError, ValueError):
                # mapping、Registry、plugin状態の不整合は個別に表示しない。
                continue
            if normalized_query and normalized_query not in _search_text(entry):
                continue
            entries.append(entry)

        total_entries = len(entries)
        total_pages = max(1, ceil(total_entries / PAGE_SIZE))
        if page > total_pages:
            return CommandPage((), page, total_pages, total_entries, out_of_range=True)
        start = (page - 1) * PAGE_SIZE
        return CommandPage(
            tuple(entries[start : start + PAGE_SIZE]),
            page,
            total_pages,
            total_entries,
        )

    def _surface_plugin_is_running(self, path: str) -> bool:
        root = path.split(" ", 1)[0]
        if root in _CORE_COMMAND_ROOTS:
            return True
        plugin_name = self._command_plugins.get(root)
        if plugin_name is None:
            return False
        try:
            return self._plugin_is_running(plugin_name) is True
        except Exception:
            return False


def render_command_page(result: CommandPage) -> str:
    if result.out_of_range:
        return f"ページ範囲外です。1～{result.total_pages}を指定してください。"
    if not result.entries:
        return "現在利用できるコマンドは見つかりません。"

    lines = [f"中央policy上利用できるコマンド {result.page}/{result.total_pages}ページ （{result.total_entries}件）"]
    for entry in result.entries:
        path = _display_text(entry.path, 100)
        description = _display_text(entry.description, 72)
        module_id = _display_text(entry.module_id, 64)
        level = entry.required_level.name.lower()
        lines.append(f"`/{path}` — {description} ｜ module `{module_id}` ｜ RBAC `{level}`")
    if result.page < result.total_pages:
        lines.append(f"続き: `/help page:{result.page + 1}`")
    lines.append("※対象・チャンネル・Bot権限は実行時に再確認されます。")
    rendered = "\n".join(lines)
    if len(rendered) >= MAX_RESPONSE_LENGTH:
        raise DiscoveryUnavailableError("rendered response exceeds the safe maximum")
    return rendered


def _normalize_query(query: str | None) -> str:
    if query is None:
        return ""
    if not isinstance(query, str):
        raise DiscoveryInputError("query must be a string")
    stripped = query.strip()
    if len(stripped) > MAX_QUERY_LENGTH:
        raise DiscoveryInputError(f"query must not exceed {MAX_QUERY_LENGTH} characters")
    if any(ord(character) < 32 for character in stripped):
        raise DiscoveryInputError("query contains a control character")
    return stripped.casefold()


def _normalize_path(path: str) -> str:
    if not isinstance(path, str):
        raise TypeError("command path must be a string")
    normalized = " ".join(path.strip().lower().split())
    if not normalized or len(normalized) > 100:
        raise ValueError("invalid command path")
    return normalized


def _search_text(entry: CommandEntry) -> str:
    return "\n".join(
        (
            entry.path,
            entry.description,
            entry.module_id,
            entry.required_level.name,
        )
    ).casefold()


def _display_text(value: str, maximum: int) -> str:
    safe = " ".join(str(value).replace("`", "ˋ").replace("@", "＠").split())
    return safe[:maximum] or "-"
