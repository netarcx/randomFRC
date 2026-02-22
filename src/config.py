from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env_vars(value: Any) -> Any:
    """Recursively resolve ${VAR} references in strings."""
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            var = match.group(1)
            env_val = os.environ.get(var)
            if env_val is None:
                raise ValueError(f"Environment variable '{var}' is not set")
            return env_val
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


@dataclass(frozen=True)
class TBAConfig:
    api_key: str
    base_url: str = "https://www.thebluealliance.com/api/v3"
    request_delay: float = 0.5
    cache_ttl: int = 3600


@dataclass(frozen=True)
class FilterConfig:
    years: list[int] = field(default_factory=lambda: [2024])
    teams: list[int] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    districts: list[str] = field(default_factory=list)
    comp_levels: list[str] = field(default_factory=lambda: ["qm", "qf", "sf", "f"])


@dataclass(frozen=True)
class StreamConfig:
    rtmp_url: str = "rtmp://restreamer:1935/live/external.stream"
    rtmp_token: str = ""
    hw_accel: str = "auto"
    video_bitrate: str = "2500k"
    audio_bitrate: str = "128k"
    preset: str = "veryfast"
    ytdlp_format: str = "best[height<=1080]"
    max_retries_per_video: int = 2
    retry_delay: float = 5.0
    error_cooldown: float = 10.0


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class AppConfig:
    tba: TBAConfig
    filters: FilterConfig = field(default_factory=FilterConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load and parse config from a YAML file, resolving env vars."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Config file must contain a YAML mapping")

    raw = _resolve_env_vars(raw)

    tba_raw = raw.get("tba", {})
    if "api_key" not in tba_raw:
        raise ValueError("tba.api_key is required")
    tba = TBAConfig(**tba_raw)

    filters = FilterConfig(**raw["filters"]) if "filters" in raw else FilterConfig()
    stream = StreamConfig(**raw["stream"]) if "stream" in raw else StreamConfig()
    logging_cfg = LoggingConfig(**raw["logging"]) if "logging" in raw else LoggingConfig()

    return AppConfig(tba=tba, filters=filters, stream=stream, logging=logging_cfg)


def validate_config(config: AppConfig) -> list[str]:
    """Return a list of warnings/errors for the given config."""
    issues: list[str] = []

    if not config.tba.api_key:
        issues.append("ERROR: tba.api_key is empty")

    if config.tba.request_delay < 0:
        issues.append("ERROR: tba.request_delay must be non-negative")

    if not config.filters.years and not config.filters.events:
        issues.append("ERROR: Must specify at least one of filters.years or filters.events")

    valid_levels = {"qm", "qf", "sf", "f"}
    for level in config.filters.comp_levels:
        if level not in valid_levels:
            issues.append(f"WARNING: Unknown comp_level '{level}' (valid: {valid_levels})")

    valid_hw = {"auto", "nvenc", "videotoolbox", "vaapi", "none"}
    if config.stream.hw_accel not in valid_hw:
        issues.append(f"ERROR: stream.hw_accel must be one of {valid_hw}")

    if not config.stream.rtmp_url:
        issues.append("ERROR: stream.rtmp_url is required")

    valid_log_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if config.logging.level.upper() not in valid_log_levels:
        issues.append(f"WARNING: Unknown logging level '{config.logging.level}'")

    return issues
