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
        )
