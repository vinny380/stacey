from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    memory_dir: Path
    timezone: str = "America/Toronto"
    realtime_model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    noise_reduction: str = "far_field"
    mcp_config: Path | None = None
    summary_model: str = "gpt-4.1-mini"
    text_model: str = "gpt-6-luna"
    log_level: str = "INFO"
    work_sound: bool = True
    telemetry_enabled: bool = True
    soloist_host_bridge: bool = True
    spotify_backend: str = "auto"
    audio_mode: str = "speaker"

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()
        default = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "stacy"
        data = Path(os.environ.get("STACY_DATA_DIR", default)).expanduser().resolve()
        memory = Path(os.environ.get("STACY_MEMORY_DIR", default / "memory")).expanduser().resolve()
        # Fail early on an invalid timezone rather than silently scheduling in UTC.
        timezone = os.environ.get("STACY_TIMEZONE", "America/Toronto")
        ZoneInfo(timezone)
        noise_reduction = os.environ.get("STACY_NOISE_REDUCTION", cls.noise_reduction)
        if noise_reduction not in {"near_field", "far_field"}:
            raise ValueError("STACY_NOISE_REDUCTION must be near_field or far_field")
        spotify_backend = os.environ.get("STACY_SPOTIFY_BACKEND", cls.spotify_backend)
        if spotify_backend not in {"auto", "webapi", "soloist"}:
            raise ValueError("STACY_SPOTIFY_BACKEND must be auto, webapi or soloist")
        audio_mode = os.environ.get("STACY_AUDIO_MODE", cls.audio_mode)
        if audio_mode not in {"speaker", "headphones"}:
            raise ValueError("STACY_AUDIO_MODE must be speaker or headphones")
        mcp_path = os.environ.get("STACY_MCP_CONFIG")
        return cls(
            data_dir=data,
            memory_dir=memory,
            timezone=timezone,
            realtime_model=os.environ.get("STACY_REALTIME_MODEL", cls.realtime_model),
            voice=os.environ.get("STACY_VOICE", cls.voice),
            noise_reduction=noise_reduction,
            mcp_config=Path(mcp_path).expanduser().resolve() if mcp_path else None,
            summary_model=os.environ.get("STACY_SUMMARY_MODEL", cls.summary_model),
            text_model=os.environ.get("STACY_TEXT_MODEL", cls.text_model),
            log_level=os.environ.get("STACY_LOG_LEVEL", cls.log_level).upper(),
            work_sound=enabled("STACY_WORK_SOUND", default="typing"),
            telemetry_enabled=enabled("STACY_OTEL_ENABLED"),
            soloist_host_bridge=enabled("SOLOIST_HOST_BRIDGE"),
            spotify_backend=spotify_backend,
            audio_mode=audio_mode,
        )


def enabled(name: str, *, default: str = "true") -> bool:
    return os.environ.get(name, default).casefold() not in {"false", "0", "off"}
