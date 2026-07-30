from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Sequence

import discord

from .mixer import PCM_FRAME_BYTES


_SAMPLE_RATE = 48_000
_CHANNELS = 2
_SAMPLE_WIDTH = 2
_FRAME_COUNT = 48_000
_MAX_TOOL_OUTPUT_BYTES = 65_536
_MAX_AUDIO_OUTPUT_BYTES = 1_048_576


class AudioDoctorStatus(StrEnum):
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AudioDoctorReport:
    status: AudioDoctorStatus
    ffmpeg_ready: bool
    opus_transcode_ready: bool
    probe_ready: bool
    pcm_decode_ready: bool
    discord_opus_ready: bool
    cleanup_confirmed: bool
    codec_name: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    error_code: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": "yonerai.audio.doctor.v1",
            "status": self.status.value,
            "checks": {
                "ffmpeg": self.ffmpeg_ready,
                "opus_transcode": self.opus_transcode_ready,
                "ffprobe": self.probe_ready,
                "pcm_decode": self.pcm_decode_ready,
                "discord_opus": self.discord_opus_ready,
                "cleanup": self.cleanup_confirmed,
            },
            "audio": {
                "codec": self.codec_name,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
            },
            "error_code": self.error_code,
        }


def run_audio_doctor(
    ffmpeg_executable: Path | str,
    ffprobe_executable: Path | str,
    *,
    scratch_root: Path | str | None = None,
    timeout_seconds: float = 12.0,
) -> AudioDoctorReport:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 1.0 <= float(timeout_seconds) <= 60.0
    ):
        return _report(error_code="invalid_timeout")
    ffmpeg = _trusted_executable(ffmpeg_executable, allowed_names={"ffmpeg", "ffmpeg.exe"})
    ffprobe = _trusted_executable(ffprobe_executable, allowed_names={"ffprobe", "ffprobe.exe"})
    root = _trusted_scratch_root(scratch_root)
    if ffmpeg is None or ffprobe is None or ffmpeg.parent != ffprobe.parent or root is None:
        return _report(error_code="executable_unavailable")

    workspace: Path | None = None
    ffmpeg_ready = True
    transcode_ready = False
    probe_ready = False
    decode_ready = False
    discord_opus_ready = _discord_opus_ready()
    cleanup_confirmed = False
    codec_name: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    error_code: str | None = None
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        workspace = Path(tempfile.mkdtemp(prefix="yonerai-audio-doctor-", dir=root))
        input_wav = workspace / "input.wav"
        opus_output = workspace / "output.opus"
        pcm_output = workspace / "decoded.pcm"
        _write_probe_wav(input_wav)

        _run_checked(
            (
                str(ffmpeg),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-threads",
                "1",
                "-i",
                str(input_wav),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
                "-ar",
                str(_SAMPLE_RATE),
                "-ac",
                str(_CHANNELS),
                "-f",
                "ogg",
                "-y",
                str(opus_output),
            ),
            cwd=workspace,
            executable=ffmpeg,
            deadline=deadline,
            failure_code="opus_transcode_failed",
        )
        _bounded_file(opus_output)
        transcode_ready = True

        probe = _run_checked(
            (
                str(ffprobe),
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels",
                "-of",
                "json",
                str(opus_output),
            ),
            cwd=workspace,
            executable=ffprobe,
            deadline=deadline,
            failure_code="ffprobe_failed",
            capture_stdout=True,
        )
        codec_name, sample_rate, channels = _decode_probe(probe.stdout)
        probe_ready = True

        _run_checked(
            (
                str(ffmpeg),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-threads",
                "1",
                "-i",
                str(opus_output),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(_SAMPLE_RATE),
                "-ac",
                str(_CHANNELS),
                "-y",
                str(pcm_output),
            ),
            cwd=workspace,
            executable=ffmpeg,
            deadline=deadline,
            failure_code="pcm_decode_failed",
        )
        decoded_size = _bounded_file(pcm_output)
        if decoded_size != _FRAME_COUNT * _CHANNELS * _SAMPLE_WIDTH or decoded_size % PCM_FRAME_BYTES:
            raise _AudioDoctorFailure("decoded_audio_invalid")
        decode_ready = True
    except subprocess.TimeoutExpired:
        error_code = "command_timeout"
    except _AudioDoctorFailure as exc:
        error_code = exc.code
    except (OSError, ValueError):
        error_code = "doctor_execution_failed"
    finally:
        if workspace is not None:
            try:
                shutil.rmtree(workspace)
            except OSError:
                pass
            cleanup_confirmed = not workspace.exists()
        else:
            cleanup_confirmed = True
        if not cleanup_confirmed:
            error_code = "cleanup_failed"

    ready = bool(
        error_code is None
        and ffmpeg_ready
        and transcode_ready
        and probe_ready
        and decode_ready
        and discord_opus_ready
        and cleanup_confirmed
    )
    if error_code is None and not discord_opus_ready:
        error_code = "discord_opus_unavailable"
        ready = False
    return AudioDoctorReport(
        status=AudioDoctorStatus.READY if ready else AudioDoctorStatus.FAILED,
        ffmpeg_ready=ffmpeg_ready,
        opus_transcode_ready=transcode_ready,
        probe_ready=probe_ready,
        pcm_decode_ready=decode_ready,
        discord_opus_ready=discord_opus_ready,
        cleanup_confirmed=cleanup_confirmed,
        codec_name=codec_name,
        sample_rate=sample_rate,
        channels=channels,
        error_code=error_code,
    )


def _report(*, error_code: str) -> AudioDoctorReport:
    return AudioDoctorReport(
        status=AudioDoctorStatus.FAILED,
        ffmpeg_ready=False,
        opus_transcode_ready=False,
        probe_ready=False,
        pcm_decode_ready=False,
        discord_opus_ready=_discord_opus_ready(),
        cleanup_confirmed=True,
        error_code=error_code,
    )


def _trusted_executable(
    value: Path | str,
    *,
    allowed_names: set[str],
) -> Path | None:
    try:
        candidate = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate if candidate.is_file() and candidate.name.casefold() in allowed_names else None


def _trusted_scratch_root(value: Path | str | None) -> Path | None:
    try:
        candidate = (Path(tempfile.gettempdir()) if value is None else Path(value).expanduser()).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate if candidate.is_dir() and not candidate.is_symlink() else None


def _write_probe_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(_CHANNELS)
        output.setsampwidth(_SAMPLE_WIDTH)
        output.setframerate(_SAMPLE_RATE)
        output.writeframes(b"\x00" * (_FRAME_COUNT * _CHANNELS * _SAMPLE_WIDTH))


def _run_checked(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    executable: Path,
    deadline: float,
    failure_code: str,
    capture_stdout: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(argv[0], 0)
    if capture_stdout:
        return _run_with_bounded_stdout(
            argv,
            cwd=cwd,
            executable=executable,
            deadline=deadline,
            failure_code=failure_code,
        )
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=_minimal_environment(executable),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=remaining,
        check=False,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise _AudioDoctorFailure(failure_code)
    return subprocess.CompletedProcess(argv, completed.returncode, stdout=b"", stderr=b"")


def _run_with_bounded_stdout(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    executable: Path,
    deadline: float,
    failure_code: str,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=_minimal_environment(executable),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.stdout is None:
        _kill_and_reap(process)
        raise _AudioDoctorFailure("probe_output_invalid")

    output = bytearray()
    overflow = threading.Event()
    reader_failed = threading.Event()

    def read_stdout() -> None:
        try:
            while chunk := process.stdout.read(8_192):
                if len(output) + len(chunk) > _MAX_TOOL_OUTPUT_BYTES:
                    overflow.set()
                    return
                output.extend(chunk)
        except OSError:
            reader_failed.set()

    reader = threading.Thread(
        target=read_stdout,
        name="yonerai-audio-doctor-probe-reader",
        daemon=False,
    )
    reader.start()
    try:
        while process.poll() is None:
            if overflow.is_set():
                _kill_and_reap(process)
                raise _AudioDoctorFailure("tool_output_too_large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_and_reap(process)
                raise subprocess.TimeoutExpired(argv[0], 0)
            try:
                process.wait(timeout=min(remaining, 0.05))
            except subprocess.TimeoutExpired:
                continue

        reader.join(timeout=max(0.0, deadline - time.monotonic()))
        if reader.is_alive():
            _kill_and_reap(process)
            raise subprocess.TimeoutExpired(argv[0], 0)
        if overflow.is_set():
            raise _AudioDoctorFailure("tool_output_too_large")
        if reader_failed.is_set():
            raise _AudioDoctorFailure("probe_output_invalid")
        if process.returncode != 0:
            raise _AudioDoctorFailure(failure_code)
        return subprocess.CompletedProcess(argv, process.returncode, stdout=bytes(output), stderr=b"")
    finally:
        if process.poll() is None:
            _kill_and_reap(process)
        process.stdout.close()
        reader.join(timeout=1.0)


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _minimal_environment(executable: Path) -> dict[str, str]:
    environment = {
        "PATH": str(executable.parent),
        "LANG": "C",
        "LC_ALL": "C",
    }
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment["SystemRoot"] = system_root
        environment["WINDIR"] = system_root
    return environment


def _bounded_file(path: Path) -> int:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise _AudioDoctorFailure("audio_output_missing") from exc
    if not path.is_file() or not 1 <= size <= _MAX_AUDIO_OUTPUT_BYTES:
        raise _AudioDoctorFailure("audio_output_invalid")
    return size


def _decode_probe(payload: bytes) -> tuple[str, int, int]:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_TOOL_OUTPUT_BYTES:
        raise _AudioDoctorFailure("probe_output_invalid")
    try:
        decoded = payload.decode("utf-8", errors="strict")
        root = json.loads(decoded, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _AudioDoctorFailure("probe_output_invalid") from exc
    if (
        not isinstance(root, dict)
        or "streams" not in root
        or not set(root) <= {"programs", "stream_groups", "streams"}
        or any(root.get(key) != [] for key in ("programs", "stream_groups") if key in root)
    ):
        raise _AudioDoctorFailure("probe_output_invalid")
    streams = root["streams"]
    if not isinstance(streams, list) or len(streams) != 1:
        raise _AudioDoctorFailure("probe_output_invalid")
    stream = streams[0]
    if not isinstance(stream, dict) or set(stream) != {"codec_name", "sample_rate", "channels"}:
        raise _AudioDoctorFailure("probe_output_invalid")
    codec_name = stream["codec_name"]
    sample_rate_raw = stream["sample_rate"]
    channels = stream["channels"]
    if (
        codec_name != "opus"
        or not isinstance(sample_rate_raw, str)
        or not sample_rate_raw.isascii()
        or not sample_rate_raw.isdecimal()
        or int(sample_rate_raw) != _SAMPLE_RATE
        or isinstance(channels, bool)
        or channels != _CHANNELS
    ):
        raise _AudioDoctorFailure("probe_contract_mismatch")
    return codec_name, int(sample_rate_raw), channels


def _discord_opus_ready() -> bool:
    encoder: discord.opus.Encoder | None = None
    try:
        encoder = discord.opus.Encoder()
        packet = encoder.encode(
            b"\x00" * discord.opus.Encoder.FRAME_SIZE,
            discord.opus.Encoder.SAMPLES_PER_FRAME,
        )
    except Exception:
        return False
    finally:
        encoder = None
    return isinstance(packet, bytes) and bool(packet) and discord.opus.is_loaded()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


class _AudioDoctorFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yonerai-discord-audio-doctor",
        description="FFmpeg/Opus audio readiness doctor",
    )
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe") or "")
    parser.add_argument("--scratch-root")
    parser.add_argument("--timeout-seconds", type=float, default=12.0)
    args = parser.parse_args(argv)
    report = run_audio_doctor(
        args.ffmpeg,
        args.ffprobe,
        scratch_root=args.scratch_root,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(report.to_mapping(), ensure_ascii=False, separators=(",", ":")))
    return 0 if report.status is AudioDoctorStatus.READY else 2


__all__ = [
    "AudioDoctorReport",
    "AudioDoctorStatus",
    "main",
    "run_audio_doctor",
]
