from __future__ import annotations

import enum
import logging
import shutil
import signal
import subprocess
import threading
from typing import IO

from .config import StreamConfig
from .tba import MatchVideo

logger = logging.getLogger(__name__)


class StreamResult(enum.Enum):
    SUCCESS = "success"
    VIDEO_UNAVAILABLE = "video_unavailable"
    DOWNLOAD_ERROR = "download_error"
    ENCODE_ERROR = "encode_error"
    RTMP_ERROR = "rtmp_error"
    INTERRUPTED = "interrupted"


def _detect_hw_encoder(preference: str) -> tuple[str, list[str]]:
    """Detect and return (encoder_name, extra_ffmpeg_args) for hardware acceleration."""
    if preference == "none":
        return "libx264", []

    candidates: list[tuple[str, str, list[str]]] = []

    if preference in ("nvenc", "auto"):
        candidates.append(("nvenc", "h264_nvenc", ["-preset", "p4"]))
    if preference in ("videotoolbox", "auto"):
        candidates.append(("videotoolbox", "h264_videotoolbox", []))
    if preference in ("vaapi", "auto"):
        candidates.append(("vaapi", "h264_vaapi", []))

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        logger.warning("ffmpeg not found in PATH, falling back to libx264")
        return "libx264", []

    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        available = result.stdout
    except Exception:
        logger.warning("Failed to probe ffmpeg encoders", exc_info=True)
        return "libx264", []

    for label, encoder, extra_args in candidates:
        if encoder in available:
            logger.info("Using GPU encoder: %s (%s)", encoder, label)
            return encoder, extra_args

    logger.info("No GPU encoder available, using libx264")
    return "libx264", []


def _drain_stderr(stream: IO[str], label: str, lines: list[str]) -> None:
    """Read stderr lines into a list for error classification."""
    try:
        for line in stream:
            line = line.rstrip()
            if line:
                lines.append(line)
                logger.debug("[%s] %s", label, line)
    except ValueError:
        pass  # stream closed


def _classify_error(ytdlp_stderr: list[str], ffmpeg_stderr: list[str]) -> StreamResult:
    """Classify the error based on stderr output."""
    ytdlp_text = "\n".join(ytdlp_stderr).lower()
    ffmpeg_text = "\n".join(ffmpeg_stderr).lower()

    if any(phrase in ytdlp_text for phrase in [
        "video unavailable", "private video", "removed", "account terminated",
        "this video is not available", "sign in to confirm your age",
        "join this channel to get access",
    ]):
        return StreamResult.VIDEO_UNAVAILABLE

    if any(phrase in ytdlp_text for phrase in [
        "error", "unable to download", "http error", "urlopen error",
    ]):
        return StreamResult.DOWNLOAD_ERROR

    if any(phrase in ffmpeg_text for phrase in [
        "connection refused", "connection reset", "broken pipe",
        "i/o error", "rtmp", "failed to connect",
    ]):
        return StreamResult.RTMP_ERROR

    if any(phrase in ffmpeg_text for phrase in [
        "error", "invalid", "codec not found", "encoder",
    ]):
        return StreamResult.ENCODE_ERROR

    return StreamResult.DOWNLOAD_ERROR


class VideoStreamer:
    """Manages the yt-dlp -> ffmpeg -> RTMP pipeline."""

    def __init__(self, config: StreamConfig) -> None:
        self._config = config
        self._encoder, self._encoder_args = _detect_hw_encoder(config.hw_accel)
        self._interrupted = False
        self._current_procs: list[subprocess.Popen] = []

    def interrupt(self) -> None:
        """Signal that streaming should stop."""
        self._interrupted = True
        self._kill_current()

    def _kill_current(self) -> None:
        """Gracefully stop current subprocesses."""
        for proc in self._current_procs:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except OSError:
                    pass
        # Wait then force kill
        for proc in self._current_procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except OSError:
                    pass
        self._current_procs.clear()

    def _build_rtmp_url(self) -> str:
        url = self._config.rtmp_url
        if self._config.rtmp_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={self._config.rtmp_token}"
        return url

    def _build_ffmpeg_args(self, rtmp_url: str) -> list[str]:
        args = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]

        # VAAPI device init must come before input
        if "vaapi" in self._encoder:
            args.extend(["-vaapi_device", "/dev/dri/renderD128"])

        args.extend(["-re", "-i", "pipe:0"])

        # Video encoding
        args.extend(["-c:v", self._encoder])

        if self._encoder == "libx264":
            args.extend(["-preset", self._config.preset])

        # Extra encoder-specific args (e.g., nvenc preset)
        args.extend(self._encoder_args)

        args.extend(["-b:v", self._config.video_bitrate])

        # Audio encoding
        args.extend(["-c:a", "aac", "-b:a", self._config.audio_bitrate])

        # Output
        args.extend(["-f", "flv", rtmp_url])

        return args

    def stream_video(self, video: MatchVideo) -> StreamResult:
        """Stream a single video. Returns the result status."""
        if self._interrupted:
            return StreamResult.INTERRUPTED

        youtube_url = f"https://www.youtube.com/watch?v={video.youtube_id}"
        rtmp_url = self._build_rtmp_url()

        logger.info("Streaming: %s (%s)", video.description, video.youtube_id)

        ytdlp_cmd = [
            "yt-dlp",
            "--no-warnings",
            "-f", self._config.ytdlp_format,
            "-o", "-",
            youtube_url,
        ]

        ffmpeg_cmd = self._build_ffmpeg_args(rtmp_url)

        logger.debug("yt-dlp cmd: %s", " ".join(ytdlp_cmd))
        logger.debug("ffmpeg cmd: %s", " ".join(ffmpeg_cmd))

        ytdlp_stderr_lines: list[str] = []
        ffmpeg_stderr_lines: list[str] = []

        try:
            ytdlp_proc = subprocess.Popen(
                ytdlp_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
            )
            ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=ytdlp_proc.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            # Allow ytdlp_proc to receive SIGPIPE if ffmpeg exits
            if ytdlp_proc.stdout:
                ytdlp_proc.stdout.close()

            self._current_procs = [ytdlp_proc, ffmpeg_proc]

            # Drain stderr in background threads
            ytdlp_stderr_text: list[str] = []

            def drain_ytdlp() -> None:
                if ytdlp_proc.stderr:
                    for line in ytdlp_proc.stderr:
                        decoded = line.decode("utf-8", errors="replace").rstrip()
                        if decoded:
                            ytdlp_stderr_text.append(decoded)
                            logger.debug("[yt-dlp] %s", decoded)

            t1 = threading.Thread(target=drain_ytdlp, daemon=True)
            t2 = threading.Thread(
                target=_drain_stderr,
                args=(ffmpeg_proc.stderr, "ffmpeg", ffmpeg_stderr_lines),
                daemon=True,
            )
            t1.start()
            t2.start()

            # Wait for ffmpeg to finish (it's the pipeline endpoint)
            ffmpeg_proc.wait()
            ytdlp_proc.wait()
            t1.join(timeout=5)
            t2.join(timeout=5)

            ytdlp_stderr_lines = ytdlp_stderr_text

        except Exception:
            logger.error("Pipeline exception", exc_info=True)
            self._kill_current()
            if self._interrupted:
                return StreamResult.INTERRUPTED
            return StreamResult.DOWNLOAD_ERROR
        finally:
            self._current_procs.clear()

        if self._interrupted:
            return StreamResult.INTERRUPTED

        ytdlp_rc = ytdlp_proc.returncode
        ffmpeg_rc = ffmpeg_proc.returncode

        if ytdlp_rc == 0 and ffmpeg_rc == 0:
            logger.info("Stream completed successfully: %s", video.description)
            return StreamResult.SUCCESS

        logger.warning(
            "Pipeline failed (yt-dlp=%s, ffmpeg=%s): %s",
            ytdlp_rc, ffmpeg_rc, video.description,
        )
        return _classify_error(ytdlp_stderr_lines, ffmpeg_stderr_lines)
