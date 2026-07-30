from __future__ import annotations

from dataclasses import dataclass

from ...control_plane import RbacLevel


MAX_QUERY_LENGTH = 80
MAX_PAGE = 100
PAGE_SIZE = 6
MAX_INDEXED_COMMANDS = 256
MAX_RESPONSE_LENGTH = 1_900


class DiscoveryInputError(ValueError):
    """ユーザー入力がdiscoveryの制限を超えている。"""


class DiscoveryUnavailableError(RuntimeError):
    """安全な検索indexを構築できないためfail-closedとする。"""


@dataclass(frozen=True, slots=True)
class CommandEntry:
    path: str
    description: str
    module_id: str
    required_level: RbacLevel


@dataclass(frozen=True, slots=True)
class CommandPage:
    entries: tuple[CommandEntry, ...]
    page: int
    total_pages: int
    total_entries: int
    out_of_range: bool = False
