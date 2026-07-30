"""音楽と読み上げが共有するDiscord PCM基盤。"""

from .mixer import (
    PCM_FRAME_BYTES,
    DuckingMixer,
    MixerSnapshot,
    PCMSource,
    mix_pcm16le,
)
from .doctor import AudioDoctorReport, AudioDoctorStatus, run_audio_doctor
from .library import ALLOWED_AUDIO_EXTENSIONS, LocalMediaLibrary, MediaLibraryError
from .models import LoopMode, QueueSnapshot, RecentTrack, RecentTrackState, Track
from .session import (
    AudioCoordinator,
    GuildAudioSession,
    PlayerStateError,
    QueueFullError,
    RequesterQueueLimitError,
    SeekUnsupportedError,
    TrackSeekError,
    TrackSourceAuthorizationError,
)
from .source import FfmpegPCMSourceFactory, SeekableTrackSourceFactory, TrackSourceFactory

__all__ = [
    "ALLOWED_AUDIO_EXTENSIONS",
    "AudioDoctorReport",
    "AudioDoctorStatus",
    "AudioCoordinator",
    "PCM_FRAME_BYTES",
    "DuckingMixer",
    "FfmpegPCMSourceFactory",
    "GuildAudioSession",
    "LocalMediaLibrary",
    "LoopMode",
    "MediaLibraryError",
    "MixerSnapshot",
    "PCMSource",
    "PlayerStateError",
    "QueueSnapshot",
    "QueueFullError",
    "RecentTrack",
    "RecentTrackState",
    "RequesterQueueLimitError",
    "SeekUnsupportedError",
    "SeekableTrackSourceFactory",
    "TrackSeekError",
    "TrackSourceAuthorizationError",
    "Track",
    "TrackSourceFactory",
    "mix_pcm16le",
    "run_audio_doctor",
]
