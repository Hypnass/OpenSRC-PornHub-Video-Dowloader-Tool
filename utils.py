"""Shared utilities: filename sanitisation, logging, formatting, rate limiting.

These helpers carry no PornHub-specific knowledge; they are generic building
blocks used by the extractor and downloader modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOGGER_NAME = "phdl"

# Characters that are illegal in filenames on Windows (a strict superset of the
# POSIX restrictions), plus ASCII control characters handled separately.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows reserved device names (case-insensitive, optionally with extension).
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def setup_logging(output_dir: Path, verbose: bool = False) -> logging.Logger:
    """Configure and return the shared application logger.

    A console handler emits human-readable progress at INFO (or DEBUG when
    ``verbose``) while a file handler records WARNING+ entries to
    ``<output_dir>/errors.log`` with timestamps, as required by the spec.

    Args:
        output_dir: Directory in which ``errors.log`` is written.
        verbose: When True, console verbosity is raised to DEBUG.

    Returns:
        The configured :class:`logging.Logger` instance.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    error_file = output_dir / "errors.log"
    file_handler = logging.FileHandler(error_file, encoding="utf-8")
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    """Return the shared logger (configuring a basic console one if needed)."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    return logger


def log_error(logger: logging.Logger, url: str, message: str) -> None:
    """Record a failure against a specific URL in a consistent format."""
    logger.error("URL=%s :: %s", url, message)


# ---------------------------------------------------------------------------
# Filename handling
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, max_length: int = config.MAX_FILENAME_LENGTH) -> str:
    """Sanitise a single path component, preserving unicode where possible.

    Illegal characters are replaced, whitespace collapsed, and the result is
    truncated to ``max_length`` characters. Any file extension present is kept
    intact when truncating so the container suffix is never lost.

    Args:
        name: Raw filename component (without directory separators).
        max_length: Maximum length of the returned component.

    Returns:
        A filesystem-safe filename component.
    """
    if not name:
        return "untitled"

    # Normalise unicode so visually-identical sequences compare/serialise the
    # same way without stripping legitimate non-ASCII characters.
    name = unicodedata.normalize("NFC", name)
    name = _ILLEGAL_FILENAME_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Windows forbids trailing dots/spaces on names.
    name = name.rstrip(". ")

    if not name:
        return "untitled"

    stem, dot, ext = name.rpartition(".")
    if dot and 1 <= len(ext) <= 5 and ext.isalnum():
        # Looks like a real extension; preserve it during truncation.
        keep = max_length - (len(ext) + 1)
        if keep < 1:
            keep = 1
        name = f"{stem[:keep].rstrip('. ')}.{ext}"
    else:
        name = name[:max_length].rstrip(". ")

    base = name.split(".")[0].upper()
    if base in _RESERVED_NAMES:
        name = f"_{name}"

    return name or "untitled"


def render_template(template: str, data: Dict[str, object]) -> str:
    """Render a yt-dlp-style ``%(key)s`` template against ``data``.

    Supports ``s`` and ``d`` conversions; missing keys resolve to ``"NA"``.
    Path separators in the template are preserved so callers can express a
    directory structure (e.g. ``%(uploader)s/...``).
    """
    def _replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        value = data.get(key, "NA")
        if value is None or value == "":
            value = "NA"
        return str(value)

    return re.sub(r"%\((\w+)\)([sd])", _replace, template)


def build_output_path(
    output_dir: Path,
    template: str,
    template_data: Dict[str, object],
) -> Path:
    """Build the final output path from a template, sanitising each component.

    Dynamic string values (e.g. title, uploader) are sanitised *before* the
    template is rendered so that separators inside a value (such as a ``/`` in a
    title) cannot inject spurious directory levels. Only the template's own
    structural separators survive to define the per-uploader folder structure.
    """
    safe_data = {
        key: (sanitize_filename(str(value)) if isinstance(value, str) else value)
        for key, value in template_data.items()
    }
    rendered = render_template(template, safe_data)
    parts = [p for p in rendered.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        parts = ["untitled"]
    safe_parts = [sanitize_filename(part) for part in parts]
    return output_dir.joinpath(*safe_parts)


def sidecar_path(video_path: Path, suffix: str) -> Path:
    """Return a sibling path sharing the video stem with a new suffix.

    Example: ``video.mp4`` + ``.info.json`` -> ``video.info.json``.
    """
    return video_path.with_name(video_path.stem + suffix)


# ---------------------------------------------------------------------------
# Human-readable formatting
# ---------------------------------------------------------------------------

def format_size(num_bytes: float) -> str:
    """Format a byte count as a human-readable string (e.g. ``1.42 GiB``)."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TiB"


def format_duration(seconds: Optional[float]) -> str:
    """Format a duration in seconds as ``H:MM:SS`` / ``M:SS``."""
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def read_lines(path: Path) -> List[str]:
    """Read non-empty, non-comment lines from a text file."""
    lines: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def find_executable(name: str) -> Optional[str]:
    """Return the absolute path to an executable on PATH, or None."""
    return shutil.which(name)


def ensure_ffmpeg() -> None:
    """Verify ffmpeg and ffprobe are available, raising otherwise."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not find_executable(tool)]
    if missing:
        raise RuntimeError(
            "Required system tool(s) not found on PATH: "
            f"{', '.join(missing)}. Install ffmpeg and ensure it is on PATH."
        )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class SyncRateLimiter:
    """Token-bucket limiter for synchronous (chunked) downloads.

    A ``rate`` of zero disables throttling entirely.
    """

    def __init__(self, rate_bytes_per_sec: float) -> None:
        self.rate = max(0.0, rate_bytes_per_sec)
        self._allowance = self.rate
        self._last = time.monotonic()

    def throttle(self, num_bytes: int) -> None:
        """Block as needed so the long-run throughput stays under ``rate``."""
        if self.rate <= 0:
            return
        now = time.monotonic()
        self._allowance += (now - self._last) * self.rate
        self._last = now
        if self._allowance > self.rate:
            self._allowance = self.rate
        if num_bytes > self._allowance:
            deficit = num_bytes - self._allowance
            time.sleep(deficit / self.rate)
            self._allowance = 0.0
        else:
            self._allowance -= num_bytes


class AsyncRateLimiter:
    """Async token-bucket limiter shared across concurrent segment downloads."""

    def __init__(self, rate_bytes_per_sec: float) -> None:
        self.rate = max(0.0, rate_bytes_per_sec)
        self._allowance = self.rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, num_bytes: int) -> None:
        """Asynchronously wait until ``num_bytes`` may be consumed."""
        if self.rate <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            self._allowance += (now - self._last) * self.rate
            self._last = now
            if self._allowance > self.rate:
                self._allowance = self.rate
            if num_bytes > self._allowance:
                deficit = num_bytes - self._allowance
                wait = deficit / self.rate
                self._allowance = 0.0
            else:
                self._allowance -= num_bytes
                wait = 0.0
        if wait > 0:
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    """Return items with duplicates removed, preserving first-seen order."""
    seen = set()
    result: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def safe_remove(path: Path) -> None:
    """Remove a file or directory tree, ignoring missing-path errors."""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            os.remove(path)
    except OSError:
        pass
