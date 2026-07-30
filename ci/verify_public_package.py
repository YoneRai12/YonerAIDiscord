"""公開wheelをネットワークなしでbuild/install/smoke検証する。

呼び出し側は、runtime dependenciesとhatchlingを含むwheelhouseを事前に用意する。
このprocess以降はpipへ ``--no-index`` を強制し、secretや外部serviceを使用しない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from email.parser import BytesParser
from pathlib import Path


_DISTRIBUTION = "yonerai-discord-suite"
_IMPORT_NAME = "yonerai_discord"
_SCHEMA = "yonerai.public-package-verification.v1"
_MAX_WHEEL_BYTES = 64 * 1024 * 1024
_MAX_ENTRIES = 4096
_MAX_EXPANDED_BYTES = 128 * 1024 * 1024
_MAX_METADATA_BYTES = 64 * 1024
_EXPECTED_ENTRY_POINTS = frozenset(
    {
        "yonerai-discord",
        "yonerai-discord-audio-doctor",
        "yonerai-discord-preview",
        "yonerai-discord-renderer-doctor",
        "yonerai-discord-sandbox-doctor",
        "yonerai-discord-web-doctor",
    }
)
_HELP_ENTRY_POINTS = tuple(sorted(_EXPECTED_ENTRY_POINTS - {"yonerai-discord"}))
_TIMEOUT_SECONDS = 180.0


class VerificationError(RuntimeError):
    """公開artifact検証のfail-closed error。"""


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _safe_archive_name(name: str) -> bool:
    if not name or len(name) > 1024 or "\x00" in name or "\\" in name:
        return False
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False
    trimmed = name[:-1] if name.endswith("/") else name
    return bool(trimmed) and "//" not in name and all(part not in {"", ".", ".."} for part in trimmed.split("/"))


def _validate_wheel(wheel: Path) -> str:
    wheel = wheel.absolute()
    wheel_stat = wheel.lstat()
    if wheel.is_symlink() or not stat.S_ISREG(wheel_stat.st_mode):
        raise VerificationError("wheel_not_regular")
    wheel = wheel.resolve(strict=True)
    if wheel_stat.st_size < 1 or wheel_stat.st_size > _MAX_WHEEL_BYTES:
        raise VerificationError("wheel_size_invalid")

    with zipfile.ZipFile(wheel, "r") as archive:
        entries = archive.infolist()
        if not entries or len(entries) > _MAX_ENTRIES:
            raise VerificationError("wheel_entry_count_invalid")
        names: set[str] = set()
        expanded_bytes = 0
        for info in entries:
            if not _safe_archive_name(info.filename) or info.filename in names:
                raise VerificationError("wheel_member_invalid")
            names.add(info.filename)
            if info.file_size < 0:
                raise VerificationError("wheel_member_size_invalid")
            expanded_bytes += info.file_size
            if expanded_bytes > _MAX_EXPANDED_BYTES:
                raise VerificationError("wheel_expanded_size_invalid")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_IFMT(unix_mode) == stat.S_IFLNK or info.flag_bits & 0x1:
                raise VerificationError("wheel_member_type_invalid")

        if f"{_IMPORT_NAME}/__init__.py" not in names:
            raise VerificationError("package_missing")
        metadata_members = [
            info for info in entries if info.filename.count("/") == 1 and info.filename.endswith(".dist-info/METADATA")
        ]
        entry_point_members = [
            info
            for info in entries
            if info.filename.count("/") == 1 and info.filename.endswith(".dist-info/entry_points.txt")
        ]
        if len(metadata_members) != 1 or len(entry_point_members) != 1:
            raise VerificationError("metadata_missing")
        if any(info.file_size > _MAX_METADATA_BYTES for info in (*metadata_members, *entry_point_members)):
            raise VerificationError("metadata_size_invalid")
        metadata = BytesParser().parsebytes(archive.read(metadata_members[0]))
        names_found = metadata.get_all("Name", [])
        versions_found = metadata.get_all("Version", [])
        if len(names_found) != 1 or _normalized_distribution(names_found[0]) != _DISTRIBUTION:
            raise VerificationError("distribution_mismatch")
        if len(versions_found) != 1 or not versions_found[0] or len(versions_found[0]) > 128:
            raise VerificationError("version_invalid")
        entry_points = archive.read(entry_point_members[0]).decode("utf-8", errors="strict")
        for command in _EXPECTED_ENTRY_POINTS:
            if re.search(rf"(?m)^{re.escape(command)}\s*=", entry_points) is None:
                raise VerificationError("entry_point_missing")
        return versions_found[0]


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _entry_point_path(venv: Path, command: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    directory = "Scripts" if os.name == "nt" else "bin"
    return venv / directory / f"{command}{suffix}"


def _offline_environment(temp_root: Path) -> dict[str, str]:
    environment = {
        "HOME": str(temp_root),
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "TEMP": str(temp_root),
        "TMP": str(temp_root),
    }
    if os.name == "nt":
        for name in ("SystemRoot", "WINDIR"):
            if value := os.environ.get(name):
                environment[name] = value
    return environment


def _run(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> None:
    completed = subprocess.run(
        tuple(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise VerificationError("command_failed")


def _create_venv(path: Path, *, cwd: Path, env: Mapping[str, str]) -> Path:
    copies = () if os.name == "nt" else ("--copies",)
    _run((sys.executable, "-I", "-m", "venv", *copies, str(path)), cwd=cwd, env=env)
    interpreter = _venv_python(path)
    if interpreter.is_symlink() or not interpreter.is_file():
        raise VerificationError("venv_invalid")
    config = path / "pyvenv.cfg"
    if (
        not config.is_file()
        or re.search(
            rb"(?mi)^include-system-site-packages\s*=\s*false\s*$",
            config.read_bytes(),
        )
        is None
    ):
        raise VerificationError("venv_not_clean")
    return interpreter


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_hash_ledger(output: Path, wheel: Path) -> tuple[Path, str]:
    digest = _sha256(wheel)
    ledger = output / "SHA256SUMS.txt"
    ledger.write_text(f"{digest}  {wheel.name}\n", encoding="ascii", newline="\n")
    fields = ledger.read_text(encoding="ascii").rstrip("\n").split("  ", maxsplit=1)
    if fields != [digest, wheel.name] or _sha256(wheel) != digest:
        raise VerificationError("hash_ledger_invalid")
    return ledger, digest


def verify_public_package(source: Path, wheelhouse: Path, output: Path) -> dict[str, object]:
    source = source.absolute()
    wheelhouse = wheelhouse.absolute()
    output = output.absolute()
    project_file = source / "pyproject.toml"
    if not source.is_dir() or source.is_symlink() or project_file.is_symlink() or not project_file.is_file():
        raise VerificationError("source_invalid")
    wheelhouse_wheels = tuple(wheelhouse.glob("*.whl"))
    if (
        not wheelhouse.is_dir()
        or wheelhouse.is_symlink()
        or not wheelhouse_wheels
        or any(wheel.is_symlink() or not wheel.is_file() for wheel in wheelhouse_wheels)
    ):
        raise VerificationError("wheelhouse_invalid")
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise VerificationError("output_not_clean")
    source = source.resolve(strict=True)
    wheelhouse = wheelhouse.resolve(strict=True)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="yonerai-public-package-") as raw_temp:
        temp_root = Path(raw_temp).resolve()
        env = _offline_environment(temp_root)
        build_venv = temp_root / "build-venv"
        build_python = _create_venv(build_venv, cwd=temp_root, env=env)
        find_links = ("--no-index", "--find-links", str(wheelhouse))
        _run(
            (
                str(build_python),
                "-I",
                "-m",
                "pip",
                "install",
                "--isolated",
                "--disable-pip-version-check",
                "--no-input",
                *find_links,
                "hatchling==1.31.0",
            ),
            cwd=temp_root,
            env=env,
        )
        built = temp_root / "built"
        built.mkdir()
        _run(
            (
                str(build_python),
                "-I",
                "-m",
                "pip",
                "wheel",
                "--isolated",
                "--disable-pip-version-check",
                "--no-input",
                "--no-deps",
                "--no-build-isolation",
                "--no-index",
                "--wheel-dir",
                str(built),
                str(source),
            ),
            cwd=temp_root,
            env=env,
        )
        wheels = tuple(built.glob("*.whl"))
        if len(wheels) != 1:
            raise VerificationError("wheel_count_invalid")
        version = _validate_wheel(wheels[0])

        install_venv = temp_root / "install-venv"
        install_python = _create_venv(install_venv, cwd=temp_root, env=env)
        _run(
            (
                str(install_python),
                "-I",
                "-m",
                "pip",
                "install",
                "--isolated",
                "--disable-pip-version-check",
                "--no-input",
                *find_links,
                str(wheels[0]),
            ),
            cwd=temp_root,
            env=env,
        )
        import_check = (
            "from importlib import metadata\n"
            f"import {_IMPORT_NAME}\n"
            f"expected={version!r}\n"
            f"actual=metadata.version({_DISTRIBUTION!r})\n"
            f"declared={_IMPORT_NAME}.__version__\n"
            "raise SystemExit(0 if expected == actual == declared else 1)\n"
        )
        _run((str(install_python), "-I", "-c", import_check), cwd=temp_root, env=env)
        for command in sorted(_EXPECTED_ENTRY_POINTS):
            executable = _entry_point_path(install_venv, command)
            if executable.is_symlink() or not executable.is_file():
                raise VerificationError("installed_entry_point_missing")
        for command in _HELP_ENTRY_POINTS:
            _run(
                (str(_entry_point_path(install_venv, command)), "--help"),
                cwd=temp_root,
                env=env,
            )

        copied_wheel = output / wheels[0].name
        shutil.copyfile(wheels[0], copied_wheel)
        _validate_wheel(copied_wheel)
        ledger, digest = _write_hash_ledger(output, copied_wheel)

    return {
        "cli_help_checked": list(_HELP_ENTRY_POINTS),
        "hash_ledger": ledger.name,
        "schema_version": _SCHEMA,
        "sha256": digest,
        "success": True,
        "version": version,
        "wheel": copied_wheel.name,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        result = verify_public_package(arguments.source, arguments.wheelhouse, arguments.output)
    except (OSError, UnicodeError, VerificationError, subprocess.SubprocessError, zipfile.BadZipFile):
        result = {"error": "public_package_verification_failed", "schema_version": _SCHEMA, "success": False}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
