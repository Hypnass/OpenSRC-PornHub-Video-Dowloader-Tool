"""Video information extraction.

Primary path uses yt-dlp (the most reliable PornHub extractor). When yt-dlp
fails, a best-effort custom extractor parses the ``flashvars`` /
``mediaDefinitions`` JSON embedded in the page HTML.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests

import config
from utils import get_logger

logger = get_logger()


class ExtractionError(Exception):
    """Raised when video information cannot be extracted by any method."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Stream:
    """A single downloadable media stream for one quality level."""

    height: int
    url: str
    protocol: str  # "hls" or "http"
    ext: str = "mp4"
    format_id: str = ""
    has_video: bool = True
    has_audio: bool = True
    tbr: float = 0.0  # total bitrate (kbps); used to break quality ties
    filesize: Optional[int] = None
    audio_url: Optional[str] = None  # set when audio must be merged separately

    @property
    def is_hls(self) -> bool:
        return self.protocol == "hls"


@dataclass
class VideoInfo:
    """Normalised metadata plus the list of available streams for a video."""

    id: str
    title: str
    uploader: str
    duration: int
    upload_date: str
    tags: List[str]
    view_count: int
    thumbnail: str
    webpage_url: str
    streams: List[Stream]
    raw: Dict[str, object] = field(default_factory=dict)

    def template_data(self, ext: str, quality: int) -> Dict[str, object]:
        """Build the substitution dict used by filename templates."""
        from utils import format_duration  # local import avoids a cycle

        return {
            "id": self.id,
            "title": self.title,
            "uploader": self.uploader or "unknown_uploader",
            "ext": ext,
            "quality": f"{quality}p" if quality else "NA",
            "height": quality,
            "upload_date": self.upload_date or "NA",
            "duration": self.duration,
            "duration_string": format_duration(self.duration),
            "view_count": self.view_count,
        }

    def metadata_dict(self) -> Dict[str, object]:
        """Return the dictionary to serialise into the ``.info.json`` sidecar.

        Prefers the rich yt-dlp payload when present; otherwise emits the
        normalised fields gathered by the custom extractor.
        """
        if self.raw:
            return self.raw
        return {
            "id": self.id,
            "title": self.title,
            "uploader": self.uploader,
            "duration": self.duration,
            "upload_date": self.upload_date,
            "tags": self.tags,
            "view_count": self.view_count,
            "thumbnail": self.thumbnail,
            "webpage_url": self.webpage_url,
            "formats": [
                {
                    "format_id": s.format_id,
                    "height": s.height,
                    "protocol": s.protocol,
                    "ext": s.ext,
                    "tbr": s.tbr,
                    "url": s.url,
                }
                for s in self.streams
            ],
        }


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class Extractor:
    """Resolves a PornHub URL into a :class:`VideoInfo`."""

    _URL_RE = re.compile(
        r"https?://(?:[a-z0-9-]+\.)?pornhub\.(?:com|org|net)/", re.IGNORECASE
    )
    _FLASHVARS_RE = re.compile(
        r"var\s+flashvars_\d+\s*=\s*(\{.*?\});", re.DOTALL
    )

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        cookiefile: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.proxy = proxy
        self.cookiefile = cookiefile
        self.headers = headers or config.base_headers()

    # -- public API ---------------------------------------------------------

    @classmethod
    def is_supported_url(cls, url: str) -> bool:
        """Return True if ``url`` looks like a PornHub video URL."""
        return bool(cls._URL_RE.match(url.strip()))

    def extract_info(self, url: str) -> VideoInfo:
        """Extract video info, trying yt-dlp first then the custom fallback.

        Args:
            url: A PornHub video URL.

        Returns:
            A populated :class:`VideoInfo`.

        Raises:
            ExtractionError: If both extraction strategies fail.
        """
        url = url.strip()
        try:
            return self._extract_with_ytdlp(url)
        except Exception as exc:  # noqa: BLE001 - we deliberately fall back
            logger.warning("yt-dlp extraction failed (%s); trying fallback.", exc)
            try:
                return self._extract_custom(url)
            except Exception as fallback_exc:  # noqa: BLE001
                raise ExtractionError(
                    f"Both yt-dlp and custom extraction failed: "
                    f"yt-dlp={exc}; custom={fallback_exc}"
                ) from fallback_exc

    # -- yt-dlp path --------------------------------------------------------

    def _ytdlp_options(self) -> Dict[str, object]:
        opts: Dict[str, object] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "http_headers": dict(self.headers),
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        if self.cookiefile:
            opts["cookiefile"] = self.cookiefile
        return opts

    def _extract_with_ytdlp(self, url: str) -> VideoInfo:
        import yt_dlp  # imported lazily so a missing dep degrades gracefully

        with yt_dlp.YoutubeDL(self._ytdlp_options()) as ydl:
            info = ydl.extract_info(url, download=False)

        if info is None:
            raise ExtractionError("yt-dlp returned no information.")
        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries", []) if e]
            if not entries:
                raise ExtractionError("Playlist contained no entries.")
            info = entries[0]

        streams = self._streams_from_ytdlp(info.get("formats") or [])
        if not streams:
            raise ExtractionError("yt-dlp found no usable video streams.")

        return VideoInfo(
            id=str(info.get("id") or "unknown"),
            title=str(info.get("title") or "untitled"),
            uploader=str(
                info.get("uploader")
                or info.get("channel")
                or info.get("uploader_id")
                or "unknown_uploader"
            ),
            duration=int(info.get("duration") or 0),
            upload_date=str(info.get("upload_date") or ""),
            tags=list(info.get("tags") or info.get("categories") or []),
            view_count=int(info.get("view_count") or 0),
            thumbnail=str(info.get("thumbnail") or ""),
            webpage_url=str(info.get("webpage_url") or url),
            streams=streams,
            raw=info,
        )

    @staticmethod
    def _streams_from_ytdlp(formats: List[Dict[str, object]]) -> List[Stream]:
        streams: List[Stream] = []
        for fmt in formats:
            vcodec = str(fmt.get("vcodec") or "none")
            acodec = str(fmt.get("acodec") or "none")
            has_video = vcodec != "none"
            has_audio = acodec != "none"
            if not has_video:
                # Pure audio-only formats are handled later via --merge-audio.
                continue
            height = int(fmt.get("height") or 0)
            if height <= 0:
                continue
            proto = str(fmt.get("protocol") or "")
            is_hls = "m3u8" in proto or str(fmt.get("ext")) == "m3u8"
            url = str(fmt.get("url") or "")
            if not url:
                continue
            streams.append(
                Stream(
                    height=height,
                    url=url,
                    protocol="hls" if is_hls else "http",
                    ext="mp4" if is_hls else str(fmt.get("ext") or "mp4"),
                    format_id=str(fmt.get("format_id") or ""),
                    has_video=True,
                    has_audio=has_audio,
                    tbr=float(fmt.get("tbr") or 0.0),
                    filesize=fmt.get("filesize") or fmt.get("filesize_approx"),  # type: ignore[arg-type]
                )
            )
        return streams

    # -- custom fallback path ----------------------------------------------

    def _extract_custom(self, url: str) -> VideoInfo:
        """Parse ``flashvars`` from the page HTML when yt-dlp is unavailable."""
        if not self.is_supported_url(url):
            raise ExtractionError(f"Not a recognised PornHub URL: {url}")

        html = self._fetch_html(url)
        flashvars = self._parse_flashvars(html)

        video_id = self._extract_video_id(url, html)
        title = str(
            flashvars.get("video_title") or self._og(html, "title") or "untitled"
        )
        thumbnail = str(
            flashvars.get("image_url") or self._og(html, "image") or ""
        )
        duration = int(flashvars.get("video_duration") or 0)
        uploader = self._extract_uploader(html)

        streams = self._streams_from_flashvars(flashvars)
        if not streams:
            raise ExtractionError("No media definitions found in page HTML.")

        return VideoInfo(
            id=video_id,
            title=title,
            uploader=uploader,
            duration=duration,
            upload_date="",
            tags=self._extract_tags(html),
            view_count=self._extract_view_count(html),
            thumbnail=thumbnail,
            webpage_url=url,
            streams=streams,
            raw={},
        )

    def _fetch_html(self, url: str) -> str:
        resp = requests.get(
            url,
            headers=self.headers,
            proxies=self._requests_proxies(),
            timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT),
        )
        resp.raise_for_status()
        return resp.text

    def _requests_proxies(self) -> Optional[Dict[str, str]]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _parse_flashvars(self, html: str) -> Dict[str, object]:
        match = self._FLASHVARS_RE.search(html)
        if not match:
            raise ExtractionError("Could not locate flashvars in page HTML.")
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"flashvars JSON was malformed: {exc}") from exc

    def _streams_from_flashvars(self, flashvars: Dict[str, object]) -> List[Stream]:
        media_definitions = flashvars.get("mediaDefinitions")
        if not isinstance(media_definitions, list):
            return []

        # Some entries embed the real list of qualities behind a "get_media"
        # endpoint instead of a direct video URL; resolve those once.
        resolved: List[Dict[str, object]] = []
        for entry in media_definitions:
            if not isinstance(entry, dict):
                continue
            video_url = str(entry.get("videoUrl") or "")
            quality = entry.get("quality")
            if isinstance(quality, list) and video_url and "get_media" in video_url:
                resolved.extend(self._resolve_get_media(video_url))
            elif video_url:
                resolved.append(entry)

        streams: List[Stream] = []
        for entry in resolved:
            video_url = str(entry.get("videoUrl") or "")
            if not video_url:
                continue
            fmt = str(entry.get("format") or "").lower()
            quality = entry.get("quality")
            if isinstance(quality, list):
                # An HLS master playlist exposing several heights at once.
                height = max((int(q) for q in quality if str(q).isdigit()), default=0)
            else:
                height = int(quality) if str(quality).isdigit() else 0
            is_hls = fmt == "hls" or video_url.endswith(".m3u8")
            streams.append(
                Stream(
                    height=height,
                    url=video_url,
                    protocol="hls" if is_hls else "http",
                    ext="mp4" if is_hls else "mp4",
                    format_id=f"{fmt or 'http'}-{height}",
                    has_video=True,
                    has_audio=True,
                )
            )
        # Drop any zero-height entries we could not classify.
        return [s for s in streams if s.height > 0]

    def _resolve_get_media(self, url: str) -> List[Dict[str, object]]:
        try:
            resp = requests.get(
                url,
                headers=self.headers,
                proxies=self._requests_proxies(),
                timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT),
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            logger.debug("get_media resolution failed: %s", exc)
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    # -- HTML scraping helpers ---------------------------------------------

    @staticmethod
    def _og(html: str, prop: str) -> Optional[str]:
        match = re.search(
            rf'<meta\s+property=["\']og:{prop}["\']\s+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        return match.group(1) if match else None

    @staticmethod
    def _extract_video_id(url: str, html: str) -> str:
        query = urlparse(url).query
        match = re.search(r"viewkey=([0-9a-zA-Z]+)", query)
        if match:
            return match.group(1)
        match = re.search(r'"video_id"\s*:\s*"?(\d+)"?', html)
        return match.group(1) if match else "unknown"

    @staticmethod
    def _extract_uploader(html: str) -> str:
        for pattern in (
            r'<meta\s+property=["\']og:author["\']\s+content=["\']([^"\']+)["\']',
            r'"author"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
            r'class="usernameBadgesWrapper"[^>]*>\s*<a[^>]*>([^<]+)</a>',
        ):
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return "unknown_uploader"

    @staticmethod
    def _extract_tags(html: str) -> List[str]:
        tags = re.findall(r'data-label="Tag"[^>]*>\s*([^<]+?)\s*<', html)
        return [t.strip() for t in tags if t.strip()]

    @staticmethod
    def _extract_view_count(html: str) -> int:
        match = re.search(r'"interactionCount"\s*:\s*"?(\d+)"?', html)
        return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# Stream selection
# ---------------------------------------------------------------------------

def select_stream(streams: List[Stream], quality: str) -> Stream:
    """Choose the stream matching the requested quality.

    Args:
        streams: Candidate streams (one or more per height).
        quality: ``"best"`` or a target height string (e.g. ``"1080"``).

    Returns:
        The selected :class:`Stream`.

    Raises:
        ExtractionError: If ``streams`` is empty.
    """
    if not streams:
        raise ExtractionError("No streams available to choose from.")

    # Keep the highest-bitrate stream for each distinct height.
    best_by_height: Dict[int, Stream] = {}
    for stream in streams:
        existing = best_by_height.get(stream.height)
        if existing is None or stream.tbr > existing.tbr:
            best_by_height[stream.height] = stream

    available_heights = sorted(best_by_height, reverse=True)

    if quality == "best":
        chosen = available_heights[0]
    else:
        target = int(quality)
        if target in best_by_height:
            chosen = target
        else:
            lower = [h for h in available_heights if h <= target]
            chosen = lower[0] if lower else available_heights[-1]
            logger.warning(
                "Requested %sp unavailable; using %sp instead.", target, chosen
            )

    return best_by_height[chosen]
