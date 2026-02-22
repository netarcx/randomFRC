from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import TBAConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatchVideo:
    youtube_id: str
    match_key: str
    comp_level: str
    event_key: str
    year: int
    description: str


@dataclass(frozen=True)
class EventInfo:
    key: str
    name: str
    year: int
    state_prov: str
    district_abbrev: str | None


class TBAClient:
    """The Blue Alliance API v3 client with rate limiting and ETag caching."""

    def __init__(self, config: TBAConfig) -> None:
        self._base_url = config.base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["X-TBA-Auth-Key"] = config.api_key
        self._session.headers["Accept"] = "application/json"
        self._delay = config.request_delay
        self._last_request_time = 0.0
        self._etag_cache: dict[str, tuple[str, Any]] = {}  # url -> (etag, data)

    def _get(self, path: str) -> Any:
        """Rate-limited GET with ETag caching."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)

        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {}
        if url in self._etag_cache:
            headers["If-None-Match"] = self._etag_cache[url][0]

        self._last_request_time = time.monotonic()
        resp = self._session.get(url, headers=headers, timeout=30)

        if resp.status_code == 304:
            logger.debug("Cache hit (304) for %s", path)
            return self._etag_cache[url][1]

        resp.raise_for_status()
        data = resp.json()

        etag = resp.headers.get("ETag")
        if etag:
            self._etag_cache[url] = (etag, data)

        return data

    def get_events_for_year(self, year: int) -> list[EventInfo]:
        """Fetch all events for a given year."""
        raw = self._get(f"/events/{year}")
        events = []
        for e in raw:
            district = None
            if e.get("district"):
                district = e["district"].get("abbreviation")
            events.append(EventInfo(
                key=e["key"],
                name=e["name"],
                year=e["year"],
                state_prov=e.get("state_prov", ""),
                district_abbrev=district,
            ))
        logger.info("Fetched %d events for %d", len(events), year)
        return events

    def get_event_matches(self, event_key: str) -> list[dict]:
        """Fetch all matches for an event."""
        matches = self._get(f"/event/{event_key}/matches")
        logger.debug("Fetched %d matches for event %s", len(matches), event_key)
        return matches

    def get_team_matches(self, team_number: int, year: int) -> list[dict]:
        """Fetch all matches for a team in a given year."""
        matches = self._get(f"/team/frc{team_number}/matches/{year}")
        logger.debug("Fetched %d matches for team %d in %d", len(matches), team_number, year)
        return matches

    def extract_videos_from_matches(self, matches: list[dict]) -> list[MatchVideo]:
        """Extract YouTube videos from match data."""
        videos = []
        for match in matches:
            for video in match.get("videos", []):
                if video.get("type") != "youtube":
                    continue
                yt_id = video.get("key", "")
                if not yt_id:
                    continue
                event_key = match.get("event_key", "")
                year = int(event_key[:4]) if len(event_key) >= 4 else 0
                description = match.get("key", "")
                videos.append(MatchVideo(
                    youtube_id=yt_id,
                    match_key=match.get("key", ""),
                    comp_level=match.get("comp_level", ""),
                    event_key=event_key,
                    year=year,
                    description=description,
                ))
        return videos
