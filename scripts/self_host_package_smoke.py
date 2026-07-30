"""Self-host wheelのbuild/install/importだけをofflineで検証する明示CLI。

このsmokeはdependency、runtime設定、外部dependency、Discord接続、live readinessを
証明しない。引数、設定ファイル、.env、secret、運用DB、networkは使用しない。
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_IS_WINDOWS = os.name == "nt"
_DISTRIBUTION_NAME = "yonerai-discord-suite"
_SCHEMA_VERSION = "yonerai.discord.self-host-package-smoke.v1"
_ERROR = "self_host_package_smoke_failed"
_MAX_WHEEL_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 4096
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_MAX_METADATA_BYTES = 64 * 1024
_MAX_PYVENV_CFG_BYTES = 16 * 1024
_BUILD_TIMEOUT_SECONDS = 180.0
_VENV_TIMEOUT_SECONDS = 90.0
_INSTALL_TIMEOUT_SECONDS = 90.0
_IMPORT_TIMEOUT_SECONDS = 30.0
_IMPORT_CHECK = (
    "from importlib import metadata\n"
    "import yonerai_discord\n"
    f"installed = metadata.version({_DISTRIBUTION_NAME!r})\n"
    "declared = getattr(yonerai_discord, '__version__', None)\n"
    "raise SystemExit(0 if isinstance(declared, str) and declared == installed else 1)\n"
)

_Runner = Callable[[Sequence[str], Path, Mapping[str, str], float], int]


class _SmokeFailure(Exception):
    """公開しない内部失敗。"""


@dataclass(frozen=True, slots=True)
class PackageSmokeResult:
    success: bool
    error: str | None
    schema_version: str = _SCHEMA_VERSION

    def to_dict(self) -> dict[str, bool | str | None]:
        return {
            "error": self.error,
            "schema_version": self.schema_version,
            "success": self.success,
        }


def _failure() -> PackageSmokeResult:
    return PackageSmokeResult(success=False, error=_ERROR)


def _default_runner(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> int:
    completed = subprocess.run(
        tuple(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout_seconds,
        check=False,
        shell=False,
    )
    return completed.returncode


def _child_environment(temp_root: Path) -> dict[str, str]:
    environment = {
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "TEMP": str(temp_root),
        "TMP": str(temp_root),
    }
    if _IS_WINDOWS:
        for name in ("SystemRoot", "WINDIR"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    return environment


def _require_success(return_code: int) -> None:
    if return_code != 0:
        raise _SmokeFailure


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _safe_archive_name(name: str) -> bool:
    if not name or len(name) > 1024 or "\x00" in name or "\\" in name:
        return False
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False
    trimmed = name[:-1] if name.endswith("/") else name
    if not trimmed or "//" in name:
        return False
    return all(part not in {"", ".", ".."} for part in trimmed.split("/"))


def _read_member_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
) -> bytes:
    if info.file_size < 0 or info.file_size > limit:
        raise _SmokeFailure
    with archive.open(info, "r") as source:
        value = source.read(limit + 1)
    if len(value) > limit or len(value) != info.file_size:
        raise _SmokeFailure
    return value


def _validate_wheel(wheel_directory: Path) -> Path:
    wheels = tuple(wheel_directory.glob("*.whl"))
    if len(wheels) != 1:
        raise _SmokeFailure
    wheel = wheels[0]
    wheel_stat = wheel.lstat()
    if wheel.is_symlink() or not stat.S_ISREG(wheel_stat.st_mode):
        raise _SmokeFailure
    if wheel_stat.st_size < 1 or wheel_stat.st_size > _MAX_WHEEL_BYTES:
        raise _SmokeFailure

    with zipfile.ZipFile(wheel, "r") as archive:
        entries = archive.infolist()
        if not entries or len(entries) > _MAX_ARCHIVE_ENTRIES:
            raise _SmokeFailure
        names: set[str] = set()
        total_size = 0
        for info in entries:
            if not _safe_archive_name(info.filename) or info.filename in names:
                raise _SmokeFailure
            names.add(info.filename)
            if info.file_size < 0:
                raise _SmokeFailure
            total_size += info.file_size
            if total_size > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise _SmokeFailure
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                raise _SmokeFailure
            if info.flag_bits & 0x1:
                raise _SmokeFailure

        if "yonerai_discord/__init__.py" not in names:
            raise _SmokeFailure
        metadata_entries = [
            info for info in entries if info.filename.count("/") == 1 and info.filename.endswith(".dist-info/METADATA")
        ]
        if len(metadata_entries) != 1:
            raise _SmokeFailure
        metadata_info = metadata_entries[0]
        dist_info = metadata_info.filename.rsplit("/", 1)[0]
        if f"{dist_info}/WHEEL" not in names or f"{dist_info}/RECORD" not in names:
            raise _SmokeFailure
        metadata_document = BytesParser().parsebytes(_read_member_bounded(archive, metadata_info, _MAX_METADATA_BYTES))
        names_found = metadata_document.get_all("Name", [])
        versions_found = metadata_document.get_all("Version", [])
        if len(names_found) != 1 or len(versions_found) != 1:
            raise _SmokeFailure
        if _normalized_distribution_name(names_found[0]) != _DISTRIBUTION_NAME:
            raise _SmokeFailure
        version = versions_found[0]
        if (
            not version
            or len(version) > 128
            or any(character.isspace() or ord(character) < 0x20 for character in version)
            or any(character in "/\\" for character in version)
        ):
            raise _SmokeFailure
    return wheel


def _venv_python_path(venv_directory: Path) -> Path:
    if _IS_WINDOWS:
        return venv_directory / "Scripts" / "python.exe"
    return venv_directory / "bin" / "python"


def _venv_create_command(venv_directory: Path) -> tuple[str, ...]:
    options = () if _IS_WINDOWS else ("--copies",)
    return (sys.executable, "-I", "-m", "venv", *options, str(venv_directory))


def _validate_venv(venv_directory: Path) -> Path:
    interpreter = _venv_python_path(venv_directory)
    if interpreter.is_symlink() or not interpreter.is_file():
        raise _SmokeFailure
    config_path = venv_directory / "pyvenv.cfg"
    if config_path.is_symlink() or not config_path.is_file():
        raise _SmokeFailure
    raw_config = config_path.read_bytes()
    if len(raw_config) > _MAX_PYVENV_CFG_BYTES:
        raise _SmokeFailure
    if re.search(rb"(?mi)^include-system-site-packages\s*=\s*false\s*$", raw_config) is None:
        raise _SmokeFailure
    return interpreter


def _execute_smoke(runner: _Runner) -> PackageSmokeResult:
    try:
        with tempfile.TemporaryDirectory(prefix="yonerai-package-smoke-") as raw_temp_root:
            temp_root = Path(raw_temp_root).resolve()
            wheel_directory = temp_root / "wheel"
            wheel_directory.mkdir()
            venv_directory = temp_root / "venv"
            environment = _child_environment(temp_root)

            _require_success(
                runner(
                    (
                        sys.executable,
                        "-I",
                        "-m",
                        "pip",
                        "--isolated",
                        "--disable-pip-version-check",
                        "--no-input",
                        "wheel",
                        "--no-deps",
                        "--no-build-isolation",
                        "--no-index",
                        "--no-cache-dir",
                        "--wheel-dir",
                        str(wheel_directory),
                        str(_REPO_ROOT),
                    ),
                    _REPO_ROOT,
                    environment,
                    _BUILD_TIMEOUT_SECONDS,
                )
            )
            wheel = _validate_wheel(wheel_directory)

            _require_success(
                runner(
                    _venv_create_command(venv_directory),
                    temp_root,
                    environment,
                    _VENV_TIMEOUT_SECONDS,
                )
            )
            isolated_interpreter = _validate_venv(venv_directory)

            _require_success(
                runner(
                    (
                        str(isolated_interpreter),
                        "-I",
                        "-m",
                        "pip",
                        "--isolated",
                        "--disable-pip-version-check",
                        "--no-input",
                        "install",
                        "--no-deps",
                        "--no-index",
                        "--no-cache-dir",
                        "--only-binary",
                        ":all:",
                        str(wheel),
                    ),
                    temp_root,
                    environment,
                    _INSTALL_TIMEOUT_SECONDS,
                )
            )
            _require_success(
                runner(
                    (str(isolated_interpreter), "-I", "-c", _IMPORT_CHECK),
                    temp_root,
                    environment,
                    _IMPORT_TIMEOUT_SECONDS,
                )
            )
    except Exception:
        return _failure()
    return PackageSmokeResult(success=True, error=None)


def run_self_host_package_smoke() -> PackageSmokeResult:
    return _execute_smoke(_default_runner)


def render_result(result: PackageSmokeResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = tuple(sys.argv[1:] if argv is None else argv)
        result = _failure() if arguments else run_self_host_package_smoke()
    except Exception:
        result = _failure()
    try:
        print(render_result(result))
    except Exception:
        return 1
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
