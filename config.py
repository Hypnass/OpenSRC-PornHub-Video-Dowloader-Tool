"""Static configuration: defaults, HTTP headers, and user-agent rotation pool.

This module holds no logic beyond small helper factories so that every other
module shares a single source of truth for network behaviour and CLI defaults.
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# CLI / behavioural defaults
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_DIR: str = "./downloads"
DEFAULT_QUALITY: str = "best"
DEFAULT_FORMAT: str = "mp4"
DEFAULT_THREADS: int = 16
DEFAULT_RATE_LIMIT_MBPS: float = 0.0  # 0 == unlimited

# Filename template (yt-dlp style). The directory component yields the
# per-uploader folder required by the output structure spec.
DEFAULT_FILENAME_TEMPLATE: str = "%(uploader)s/%(title)s_%(id)s.%(ext)s"

# Containers we know how to remux into without re-encoding.
SUPPORTED_CONTAINERS: tuple = ("mp4", "mkv", "webm", "mov")

# Quality presets in descending order. Used for "best" selection and for
# nearest-match fallback when an exact requested height is unavailable.
QUALITY_PRESETS: List[int] = [2160, 1440, 1080, 720, 480, 360, 240]

MAX_FILENAME_LENGTH: int = 200

# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------

RETRY_ATTEMPTS: int = 3
RETRY_BASE_DELAY: float = 2.0  # seconds; doubled each attempt (exp. backoff)
RETRY_MAX_DELAY: float = 30.0

# HTTP status codes that should trigger a proxy rotation + retry.
ROTATE_STATUS_CODES: frozenset = frozenset({403, 429, 503})

# Network timeouts (connect, read) in seconds.
CONNECT_TIMEOUT: float = 15.0
READ_TIMEOUT: float = 60.0

# Streaming chunk size for direct downloads (1 MiB).
CHUNK_SIZE: int = 1024 * 1024

# ---------------------------------------------------------------------------
# User-agent rotation pool
# ---------------------------------------------------------------------------

USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# Cookies required to bypass the PornHub age-verification interstitial.
PH_COOKIES: Dict[str, str] = {
    "age_verified": "1",
    "accessAgeDisclaimerPH": "1",
    "accessAgeDisclaimerUK": "1",
    "accessPH": "1",
    "platform": "pc",
}

PH_REFERER: str = "https://www.pornhub.com/"


def random_user_agent() -> str:
    """Return a random user-agent string from the rotation pool."""
    return random.choice(USER_AGENTS)


def cookie_header() -> str:
    """Render the default PornHub cookie jar as a single Cookie header value."""
    return "; ".join(f"{key}={value}" for key, value in PH_COOKIES.items())


def base_headers(user_agent: Optional[str] = None) -> Dict[str, str]:
    """Build a fresh header dict suitable for both requests and aiohttp.

    Used for requests to the pornhub.com page/API (where the age-verification
    cookies are required).

    Args:
        user_agent: Explicit UA to use; a random one is chosen when omitted.

    Returns:
        A new dictionary of HTTP headers including age-verification cookies.
    """
    return {
        "User-Agent": user_agent or random_user_agent(),
        "Referer": PH_REFERER,
        "Origin": "https://www.pornhub.com",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_header(),
        "Connection": "keep-alive",
    }


def media_headers(user_agent: Optional[str] = None) -> Dict[str, str]:
    """Build headers for CDN media requests (HLS segments, MP4, thumbnails).

    A real browser does **not** send the pornhub.com session cookies or an
    ``Origin`` header to the ``*.phncdn.com`` CDN when fetching media; doing so
    can trip the CDN's anti-hotlinking/WAF rules and yield 403 responses. We
    therefore send only the browser-like ``User-Agent`` and ``Referer`` here.

    Args:
        user_agent: Explicit UA to use; a random one is chosen when omitted.

    Returns:
        A new dictionary of CDN-appropriate HTTP headers.
    """
    return {
        "User-Agent": user_agent or random_user_agent(),
        "Referer": PH_REFERER,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
