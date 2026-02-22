from __future__ import annotations

import logging
import signal
import sys
import time

from .config import AppConfig, load_config, validate_config
from .picker import VideoPicker
from .streamer import StreamResult, VideoStreamer
from .tba import TBAClient

logger = logging.getLogger("randomFRC")

CIRCUIT_BREAKER_THRESHOLD = 10
CIRCUIT_BREAKER_PAUSE = 60.0


class Application:
    """Main application orchestrator."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client = TBAClient(config.tba)
        self._picker = VideoPicker(self._client, config.tba, config.filters)
        self._streamer = VideoStreamer(config.stream)
        self._running = True
        self._consecutive_errors = 0

    def _safe_rebuild_pool(self) -> None:
        """Rebuild pool with error handling so TBA outages don't crash the loop."""
        try:
            self._picker.build_pool()
        except Exception:
            logger.error("Failed to rebuild video pool", exc_info=True)

    def _setup_signals(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _frame: object) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down gracefully...", sig_name)
        self._running = False
        self._streamer.interrupt()

    def _handle_result(self, result: StreamResult) -> None:
        if result == StreamResult.SUCCESS:
            self._consecutive_errors = 0
            return

        if result == StreamResult.INTERRUPTED:
            return

        if result == StreamResult.VIDEO_UNAVAILABLE:
            logger.warning("Video unavailable, skipping")
            return

        self._consecutive_errors += 1

        if result == StreamResult.RTMP_ERROR:
            backoff = min(2 ** self._consecutive_errors, 120)
            logger.error(
                "RTMP error (consecutive: %d), backing off %ds",
                self._consecutive_errors, backoff,
            )
            self._sleep(backoff)
            return

        # DOWNLOAD_ERROR or ENCODE_ERROR
        logger.warning(
            "Stream error: %s (consecutive: %d), cooling down %ds",
            result.value, self._consecutive_errors, self._config.stream.error_cooldown,
        )
        self._sleep(self._config.stream.error_cooldown)

    def _sleep(self, seconds: float) -> None:
        """Sleep that respects shutdown signals."""
        end = time.monotonic() + seconds
        while self._running:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))

    def run(self) -> None:
        self._setup_signals()

        logger.info("Building initial video pool...")
        try:
            pool_size = self._picker.build_pool()
        except Exception:
            logger.error("Failed to build initial video pool", exc_info=True)
            sys.exit(1)
        if pool_size == 0:
            logger.error("No videos found matching filters! Check your config.")
            sys.exit(1)

        logger.info("Starting stream loop with %d videos in pool", pool_size)

        while self._running:
            # Circuit breaker
            if self._consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
                logger.error(
                    "Circuit breaker: %d consecutive errors, pausing %ds",
                    self._consecutive_errors, CIRCUIT_BREAKER_PAUSE,
                )
                self._sleep(CIRCUIT_BREAKER_PAUSE)
                self._consecutive_errors = 0
                continue

            video = self._picker.next_video()
            if video is None:
                logger.error("No videos available, retrying in 60s...")
                self._sleep(60)
                self._safe_rebuild_pool()
                continue

            # Retry loop for this video
            result = StreamResult.INTERRUPTED
            retries = 0
            while retries <= self._config.stream.max_retries_per_video and self._running:
                result = self._streamer.stream_video(video)

                if result in (StreamResult.SUCCESS, StreamResult.INTERRUPTED, StreamResult.VIDEO_UNAVAILABLE):
                    break

                retries += 1
                if retries <= self._config.stream.max_retries_per_video:
                    logger.info(
                        "Retrying video %s (attempt %d/%d)",
                        video.youtube_id, retries, self._config.stream.max_retries_per_video,
                    )
                    self._sleep(self._config.stream.retry_delay)

            self._handle_result(result)

        logger.info("Shutdown complete")


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"

    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"Failed to load config: {e}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    issues = validate_config(config)
    for issue in issues:
        if issue.startswith("ERROR"):
            logger.error(issue)
        else:
            logger.warning(issue)

    errors = [i for i in issues if i.startswith("ERROR")]
    if errors:
        logger.error("Config validation failed, exiting")
        sys.exit(1)

    app = Application(config)
    app.run()


if __name__ == "__main__":
    main()
