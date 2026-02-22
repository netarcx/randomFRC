from __future__ import annotations

import logging
import random
import time

from .config import FilterConfig, TBAConfig
from .tba import EventInfo, MatchVideo, TBAClient

logger = logging.getLogger(__name__)


class VideoPicker:
    """Builds and manages a shuffled pool of FRC match videos."""

    def __init__(self, tba_client: TBAClient, tba_config: TBAConfig, filters: FilterConfig) -> None:
        self._client = tba_client
        self._tba_config = tba_config
        self._filters = filters
        self._pool: list[MatchVideo] = []
        self._index = 0
        self._pool_built_at = 0.0

    def _resolve_events(self) -> list[EventInfo]:
        """Resolve which events to pull matches from based on filters."""
        if self._filters.events:
            # Direct event keys specified — build minimal EventInfo stubs
            logger.info("Using %d explicitly configured events", len(self._filters.events))
            events = []
            for ek in self._filters.events:
                try:
                    year = int(ek[:4]) if len(ek) >= 4 else 0
                except ValueError:
                    year = 0
                events.append(EventInfo(key=ek, name=ek, year=year,
                                        state_prov="", district_abbrev=None))
            return events

        all_events: list[EventInfo] = []
        for year in self._filters.years:
            all_events.extend(self._client.get_events_for_year(year))

        # Filter by states (OR within)
        if self._filters.states:
            states_lower = {s.lower() for s in self._filters.states}
            all_events = [e for e in all_events if e.state_prov.lower() in states_lower]
            logger.info("After state filter: %d events", len(all_events))

        # Filter by districts (OR within)
        if self._filters.districts:
            districts_lower = {d.lower() for d in self._filters.districts}
            all_events = [
                e for e in all_events
                if e.district_abbrev and e.district_abbrev.lower() in districts_lower
            ]
            logger.info("After district filter: %d events", len(all_events))

        return all_events

    def _fetch_videos_for_events(self, events: list[EventInfo]) -> list[MatchVideo]:
        """Fetch match videos for a list of events."""
        all_videos: list[MatchVideo] = []
        for event in events:
            try:
                matches = self._client.get_event_matches(event.key)
                videos = self._client.extract_videos_from_matches(matches)
                all_videos.extend(videos)
            except Exception:
                logger.warning("Failed to fetch matches for event %s", event.key, exc_info=True)
        return all_videos

    def _fetch_team_match_keys(self) -> set[str] | None:
        """If teams filter is set, return the set of match keys those teams played in."""
        if not self._filters.teams:
            return None

        match_keys: set[str] = set()
        years = self._filters.years
        if self._filters.events:
            parsed_years = set()
            for ek in self._filters.events:
                try:
                    if len(ek) >= 4:
                        parsed_years.add(int(ek[:4]))
                except ValueError:
                    pass
            if parsed_years:
                years = list(parsed_years)

        for team in self._filters.teams:
            for year in years:
                try:
                    matches = self._client.get_team_matches(team, year)
                    for m in matches:
                        match_keys.add(m.get("key", ""))
                except Exception:
                    logger.warning("Failed to fetch matches for team %d/%d", team, year, exc_info=True)

        logger.info("Team filter resolved %d match keys", len(match_keys))
        return match_keys

    def build_pool(self) -> int:
        """Build or rebuild the video pool. Returns pool size."""
        logger.info("Building video pool...")
        events = self._resolve_events()
        logger.info("Resolved %d events", len(events))

        videos = self._fetch_videos_for_events(events)
        logger.info("Found %d raw videos", len(videos))

        # Filter by team matches (AND with teams)
        team_keys = self._fetch_team_match_keys()
        if team_keys is not None:
            videos = [v for v in videos if v.match_key in team_keys]
            logger.info("After team filter: %d videos", len(videos))

        # Filter by comp_levels
        if self._filters.comp_levels:
            levels = set(self._filters.comp_levels)
            videos = [v for v in videos if v.comp_level in levels]
            logger.info("After comp_level filter: %d videos", len(videos))

        # Deduplicate by youtube_id
        seen: set[str] = set()
        deduped: list[MatchVideo] = []
        for v in videos:
            if v.youtube_id not in seen:
                seen.add(v.youtube_id)
                deduped.append(v)
        videos = deduped

        random.shuffle(videos)
        self._pool = videos
        self._index = 0
        self._pool_built_at = time.monotonic()
        logger.info("Video pool built: %d unique videos", len(self._pool))
        return len(self._pool)

    def next_video(self) -> MatchVideo | None:
        """Return the next video from the pool. Rebuilds if exhausted or TTL expired."""
        cache_ttl = self._tba_config.cache_ttl

        if self._index >= len(self._pool):
            elapsed = time.monotonic() - self._pool_built_at
            if elapsed >= cache_ttl:
                logger.info("Pool exhausted and TTL expired, rebuilding...")
                self.build_pool()
            else:
                logger.info("Pool exhausted, reshuffling...")
                random.shuffle(self._pool)
                self._index = 0

        if not self._pool:
            logger.error("Video pool is empty!")
            return None

        video = self._pool[self._index]
        self._index += 1
        return video

    @property
    def pool_size(self) -> int:
        return len(self._pool)
