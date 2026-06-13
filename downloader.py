"""Core download logic: direct MP4 streaming, concurrent HLS segments, muxing.

A :class:`Downloader` is constructed once with the resolved CLI options and then
called repeatedly via :meth:`Downloader.download` for each URL. All network
failures are funnelled through retry/backoff with optional proxy rotation, and
every individual video failure is captured so batch processing can continue.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import aiohttp
import requests
from tqdm import tqdm

import config
from extractor import Extractor, Stream, VideoInfo, select_stream
from utils import (
    AsyncRateLimiter,
    SyncRateLimiter,
    build_output_path,
    ensure_ffmpeg,
    find_executable,
    format_size,
    get_logger,
    log_error,
    safe_remove,
    sidecar_path,
)

logger = get_logger()


class DownloadError(Exception):
    """Raised for unrecoverable errors during a single video download."""


class DownloadCancelled(Exception):
    """Raised internally when a cooperative cancel is requested via an event."""


# Type alias for the optional GUI/progress callback. Receives a dict with the
# keys: stage, downloaded, total, speed, eta, frac (see ``_report_progress``).
ProgressHook = Callable[[Dict[str, object]], None]
# Called once per download right after extraction with (info, stream, path).
InfoHook = Callable[["VideoInfo", "Stream", Path], None]


@dataclass
class DownloadResult:
    """Outcome of attempting to download one URL."""

    url: str
    status: str  # "success", "failed" or "cancelled"
    path: Optional[Path]
    size: int
    error: Optional[str] = None
    skipped: bool = False


class Downloader:
    """Downloads PornHub videos according to the supplied options."""

    def __init__(
        self,
        *,
        output_dir: Path,
        quality: str = config.DEFAULT_QUALITY,
        file_format: str = config.DEFAULT_FORMAT,
        threads: int = config.DEFAULT_THREADS,
        proxies: Optional[List[str]] = None,
        cookiefile: Optional[str] = None,
        no_metadata: bool = False,
        no_thumbnail: bool = False,
        rate_limit_mbps: float = config.DEFAULT_RATE_LIMIT_MBPS,
        filename_template: str = config.DEFAULT_FILENAME_TEMPLATE,
        convert: Optional[str] = None,
        merge_audio: bool = False,
        no_ffmpeg: bool = False,
        progress_hook: Optional[ProgressHook] = None,
        info_hook: Optional[InfoHook] = None,
        cancel_event: Optional[threading.Event] = None,
        show_progress_bar: bool = True,
    ) -> None:
        self.output_dir = output_dir
        self.quality = quality
        self.file_format = file_format.lstrip(".").lower()
        self.threads = max(1, threads)
        self.proxies = proxies or []
        self.cookiefile = cookiefile
        self.no_metadata = no_metadata
        self.no_thumbnail = no_thumbnail
        self.rate_bytes = max(0.0, rate_limit_mbps) * 1024 * 1024
        self.filename_template = filename_template
        self.convert = convert
        self.merge_audio = merge_audio
        self.no_ffmpeg = no_ffmpeg

        # Optional integration hooks (used by the GUI). All are no-ops by
        # default so the command-line behaviour is completely unchanged.
        self.progress_hook = progress_hook
        self.info_hook = info_hook
        self.cancel_event = cancel_event
        self.show_progress_bar = show_progress_bar
        self._dl_start = 0.0
        self._last_report = 0.0

        self._proxy_idx = 0
        self._session = requests.Session()
        self._ffmpeg = find_executable("ffmpeg") or "ffmpeg"
        self._ffprobe = find_executable("ffprobe") or "ffprobe"

        if self.no_ffmpeg:
            # In ffmpeg-free mode the operations that strictly require it are
            # rejected up front so the user gets a clear error, not a late crash.
            if convert:
                raise RuntimeError(
                    "--convert requires ffmpeg and cannot be used with --no-ffmpeg."
                )
            if merge_audio:
                raise RuntimeError(
                    "--merge-audio requires ffmpeg and cannot be used with --no-ffmpeg."
                )
        else:
            ensure_ffmpeg()  # fail fast if the system tools are absent

        self.extractor = Extractor(
            proxy=self._current_proxy(),
            cookiefile=cookiefile,
            headers=config.base_headers(),
        )

    # -- public API ---------------------------------------------------------

    def download(self, url: str) -> DownloadResult:
        """Download a single URL, returning a :class:`DownloadResult`.

        Never raises for expected failures (network, deleted/private video,
        bad URL); those are logged and reported via the result object so batch
        runs continue uninterrupted.
        """
        url = url.strip()
        try:
            # Keep extraction aligned with any proxy rotation from prior videos.
            self.extractor.proxy = self._current_proxy()
            info = self.extractor.extract_info(url)
            stream = select_stream(info.streams, self.quality)
            logger.info(
                "Selected %dp (%s) for %r", stream.height, stream.protocol, info.title
            )

            out_ext = self._output_ext(stream)
            template_data = info.template_data(out_ext, stream.height)
            video_path = build_output_path(
                self.output_dir, self.filename_template, template_data
            )
            video_path.parent.mkdir(parents=True, exist_ok=True)

            if video_path.exists() and self._verify_output(video_path):
                logger.info("Already downloaded, skipping: %s", video_path.name)
                return DownloadResult(
                    url, "success", video_path, video_path.stat().st_size, skipped=True
                )

            if not self.no_metadata:
                self._write_metadata(info, video_path)
            if not self.no_thumbnail and info.thumbnail:
                self._download_thumbnail(info.thumbnail, video_path)

            # Notify integrators (e.g. the GUI) of the resolved metadata/paths
            # before the byte-heavy download starts.
            if self.info_hook:
                self.info_hook(info, stream, video_path)

            self._dl_start = time.monotonic()
            self._last_report = 0.0
            if stream.is_hls:
                self._download_hls(stream, video_path)
            else:
                self._download_direct(stream, video_path)

            if self.merge_audio and not stream.has_audio:
                self._merge_audio(info, video_path)

            if self.convert:
                video_path = self._convert(video_path)

            if not self._verify_output(video_path):
                raise DownloadError("Output failed the integrity check.")

            size = video_path.stat().st_size
            logger.info("Done: %s (%s)", video_path.name, format_size(size))
            return DownloadResult(url, "success", video_path, size)

        except DownloadCancelled:
            logger.info("Cancelled: %s", url)
            return DownloadResult(url, "cancelled", None, 0, error="cancelled")
        except Exception as exc:  # noqa: BLE001 - batch must survive any failure
            log_error(logger, url, str(exc))
            logger.error("Failed: %s -- %s", url, exc)
            return DownloadResult(url, "failed", None, 0, error=str(exc))

    def _check_cancel(self) -> None:
        """Raise :class:`DownloadCancelled` if a cancel has been requested."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise DownloadCancelled()

    def _report_progress(
        self,
        stage: str,
        downloaded: int,
        total: Optional[int],
        *,
        frac: Optional[float] = None,
        force: bool = False,
    ) -> None:
        """Compute speed/ETA and forward a progress dict to ``progress_hook``.

        Updates are throttled to ~10/second to avoid flooding the GUI event
        loop, except when ``force`` is set (used for the final 100% update).
        """
        if self.progress_hook is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_report) < 0.1:
            return
        self._last_report = now
        elapsed = max(now - self._dl_start, 1e-6)
        speed = downloaded / elapsed
        if frac is None and total:
            frac = downloaded / total if total else None
        if total and speed > 0:
            eta: Optional[float] = max(total - downloaded, 0) / speed
        elif frac and frac > 0:
            eta = elapsed * (1.0 - frac) / frac
        else:
            eta = None
        self.progress_hook(
            {
                "stage": stage,
                "downloaded": downloaded,
                "total": total,
                "speed": speed,
                "eta": eta,
                "frac": frac,
            }
        )

    def _output_ext(self, stream: Stream) -> str:
        """Pick the output container extension.

        Without ffmpeg we cannot remux, so HLS is written as a raw ``.ts`` (a
        byte-concatenation of its TS segments, playable in VLC/MPV) and direct
        streams keep their native container. With ffmpeg the user's ``--format``
        choice is honoured.
        """
        if self.no_ffmpeg:
            return "ts" if stream.is_hls else (stream.ext or "mp4")
        return self.file_format

    # -- proxy / header helpers --------------------------------------------

    def _current_proxy(self) -> Optional[str]:
        return self.proxies[self._proxy_idx] if self.proxies else None

    def _current_proxies(self) -> Optional[Dict[str, str]]:
        proxy = self._current_proxy()
        return {"http": proxy, "https": proxy} if proxy else None

    def _rotate_proxy(self) -> None:
        if self.proxies:
            self._proxy_idx = (self._proxy_idx + 1) % len(self.proxies)
            logger.warning("Rotating to proxy: %s", self._current_proxy())

    @staticmethod
    def _backoff(attempt: int) -> None:
        delay = min(
            config.RETRY_BASE_DELAY * (2 ** attempt), config.RETRY_MAX_DELAY
        )
        time.sleep(delay)

    # -- generic HTTP with retry/backoff/proxy rotation --------------------

    def _request_text(self, url: str) -> str:
        """GET a text resource (e.g. an m3u8 playlist) with full retry logic."""
        last_error: Optional[Exception] = None
        for attempt in range(config.RETRY_ATTEMPTS):
            try:
                resp = self._session.get(
                    url,
                    headers=config.media_headers(),
                    proxies=self._current_proxies(),
                    timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT),
                )
                if resp.status_code in config.ROTATE_STATUS_CODES:
                    self._rotate_proxy()
                    raise DownloadError(f"HTTP {resp.status_code} for {url}")
                resp.raise_for_status()
                return resp.text
            except (requests.RequestException, DownloadError) as exc:
                last_error = exc
                self._backoff(attempt)
        raise DownloadError(f"Failed to fetch {url}: {last_error}")

    # -- metadata / thumbnail ----------------------------------------------

    def _write_metadata(self, info: VideoInfo, video_path: Path) -> None:
        meta_path = sidecar_path(video_path, ".info.json")
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(
                info.metadata_dict(), handle, ensure_ascii=False, indent=2, default=str
            )
        logger.debug("Wrote metadata: %s", meta_path.name)

    def _download_thumbnail(self, thumb_url: str, video_path: Path) -> None:
        thumb_path = sidecar_path(video_path, ".jpg")
        for attempt in range(config.RETRY_ATTEMPTS):
            try:
                resp = self._session.get(
                    thumb_url,
                    headers=config.media_headers(),
                    proxies=self._current_proxies(),
                    timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT),
                )
                if resp.status_code in config.ROTATE_STATUS_CODES:
                    self._rotate_proxy()
                    raise DownloadError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                thumb_path.write_bytes(resp.content)
                logger.debug("Wrote thumbnail: %s", thumb_path.name)
                return
            except (requests.RequestException, DownloadError) as exc:
                logger.debug("Thumbnail attempt %d failed: %s", attempt + 1, exc)
                self._backoff(attempt)
        logger.warning("Could not download thumbnail from %s", thumb_url)

    # -- direct MP4 download ------------------------------------------------

    def _download_direct(self, stream: Stream, video_path: Path) -> None:
        """Stream a direct MP4 to disk with resume, progress and throttling."""
        part_path = video_path.with_name(video_path.name + ".part")
        resume_pos = part_path.stat().st_size if part_path.exists() else 0

        opened = self._open_direct(stream.url, resume_pos)
        if opened is None:
            # Server reported the requested range as already satisfied (416):
            # the .part file is complete, so just finalise it.
            os.replace(part_path, video_path)
            return

        resp, total_size, resume_pos = opened
        mode = "ab" if resume_pos > 0 else "wb"
        limiter = SyncRateLimiter(self.rate_bytes)

        downloaded = resume_pos
        with tqdm(
            total=total_size,
            initial=resume_pos,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=video_path.name[:38],
            dynamic_ncols=True,
            disable=not self.show_progress_bar,
        ) as bar:
            with open(part_path, mode) as handle:
                for chunk in resp.iter_content(chunk_size=config.CHUNK_SIZE):
                    if not chunk:
                        continue
                    self._check_cancel()
                    handle.write(chunk)
                    limiter.throttle(len(chunk))
                    bar.update(len(chunk))
                    downloaded += len(chunk)
                    self._report_progress("direct", downloaded, total_size)
        resp.close()

        actual = part_path.stat().st_size
        if total_size and actual < total_size:
            raise DownloadError(
                f"Incomplete download: {actual}/{total_size} bytes for {video_path.name}"
            )
        self._report_progress("direct", actual, total_size, force=True)
        os.replace(part_path, video_path)

    def _open_direct(
        self, url: str, resume_pos: int
    ) -> Optional[Tuple[requests.Response, Optional[int], int]]:
        """Open a streaming response, honouring resume via HTTP Range.

        Returns ``(response, total_size, resume_pos)`` or ``None`` when the
        server signals the byte range is already fully satisfied (HTTP 416).
        """
        last_error: Optional[Exception] = None
        for attempt in range(config.RETRY_ATTEMPTS):
            headers = config.media_headers()
            if resume_pos > 0:
                headers["Range"] = f"bytes={resume_pos}-"
            try:
                resp = self._session.get(
                    url,
                    headers=headers,
                    stream=True,
                    proxies=self._current_proxies(),
                    timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT),
                )
                if resp.status_code == 416:
                    resp.close()
                    return None
                if resp.status_code in config.ROTATE_STATUS_CODES:
                    resp.close()
                    self._rotate_proxy()
                    raise DownloadError(f"HTTP {resp.status_code}")
                resp.raise_for_status()

                if resp.status_code == 206:
                    content_range = resp.headers.get("Content-Range", "")
                    total = (
                        int(content_range.rsplit("/", 1)[-1])
                        if "/" in content_range
                        else None
                    )
                else:
                    # Range ignored (or none requested): start from scratch.
                    resume_pos = 0
                    length = resp.headers.get("Content-Length")
                    total = int(length) if length and length.isdigit() else None
                return resp, total, resume_pos
            except (requests.RequestException, DownloadError) as exc:
                last_error = exc
                self._backoff(attempt)
        raise DownloadError(f"Failed to open {url}: {last_error}")

    # -- HLS download -------------------------------------------------------

    def _download_hls(self, stream: Stream, video_path: Path) -> None:
        """Download an HLS stream and mux it into the target container."""
        media_url, segments, encrypted, has_init = self._prepare_hls(stream)

        if encrypted or has_init:
            if self.no_ffmpeg:
                raise DownloadError(
                    "Encrypted/fMP4 HLS requires ffmpeg and cannot be handled in "
                    "--no-ffmpeg mode. Install ffmpeg or pick a different video."
                )
            logger.info(
                "Encrypted/fMP4 HLS detected; delegating to ffmpeg for muxing."
            )
            self._ffmpeg_hls_direct(media_url, video_path)
            return

        if not segments:
            raise DownloadError("HLS media playlist contained no segments.")

        temp_dir = video_path.with_name(video_path.stem + ".segments")
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            seg_files = asyncio.run(self._download_segments(segments, temp_dir))
            tmp_out = video_path.with_name(
                video_path.stem + ".muxing" + video_path.suffix
            )
            if self.no_ffmpeg:
                # No remux available: concatenate the raw TS segments byte-for-byte.
                # The resulting .ts plays in VLC/MPV with no quality loss.
                self._concat_segments_binary(seg_files, tmp_out)
            else:
                self._concat_segments(seg_files, tmp_out)
            os.replace(tmp_out, video_path)
            safe_remove(temp_dir)
        except DownloadCancelled:
            # Preserve the already-downloaded segments so a later resume can
            # continue from where it stopped instead of starting over.
            raise
        except Exception:
            safe_remove(temp_dir)
            raise

    @staticmethod
    def _concat_segments_binary(seg_files: List[Path], output_path: Path) -> None:
        """Concatenate TS segments byte-for-byte (ffmpeg-free fallback)."""
        with open(output_path, "wb") as out:
            for seg in seg_files:
                with open(seg, "rb") as handle:
                    while True:
                        chunk = handle.read(config.CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)

    def _prepare_hls(self, stream: Stream) -> Tuple[str, List[str], bool, bool]:
        """Resolve master->variant and parse the media playlist's segments."""
        text = self._request_text(stream.url)
        media_url = stream.url
        if "#EXT-X-STREAM-INF" in text:
            media_url = self._select_variant(text, stream.url, stream.height)
            text = self._request_text(media_url)

        segments: List[str] = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                segments.append(urljoin(media_url, line))

        encrypted = "#EXT-X-KEY" in text and "METHOD=NONE" not in text
        has_init = "#EXT-X-MAP" in text
        return media_url, segments, encrypted, has_init

    @staticmethod
    def _select_variant(master_text: str, base_url: str, target_height: int) -> str:
        """Pick the variant playlist URL closest to ``target_height``."""
        variants: List[Tuple[int, str]] = []
        lines = master_text.splitlines()
        for idx, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                height = 0
                if "RESOLUTION=" in line:
                    res = line.split("RESOLUTION=", 1)[1].split(",", 1)[0]
                    if "x" in res:
                        height = int(res.split("x")[1].strip())
                # The URI is the next non-comment line.
                for follow in lines[idx + 1:]:
                    follow = follow.strip()
                    if follow and not follow.startswith("#"):
                        variants.append((height, urljoin(base_url, follow)))
                        break
        if not variants:
            return base_url
        # Prefer the exact height; otherwise the closest available.
        variants.sort(key=lambda hv: abs(hv[0] - target_height))
        return variants[0][1]

    async def _download_segments(
        self, segments: List[str], temp_dir: Path
    ) -> List[Path]:
        """Download all TS segments concurrently, returning ordered paths."""
        seg_files = [temp_dir / f"{i:06d}.ts" for i in range(len(segments))]
        semaphore = asyncio.Semaphore(self.threads)
        limiter = AsyncRateLimiter(self.rate_bytes)
        timeout = aiohttp.ClientTimeout(
            connect=config.CONNECT_TIMEOUT, sock_read=config.READ_TIMEOUT, total=None
        )
        connector = aiohttp.TCPConnector(limit=self.threads)
        downloaded_bytes = [0]
        start = time.monotonic()
        # Use a dummy jar so our explicit age-verification Cookie header is sent
        # verbatim and never shadowed by aiohttp's automatic cookie handling.
        cookie_jar = aiohttp.DummyCookieJar()

        total_segs = len(segments)
        completed = [0]
        bar = tqdm(
            total=total_segs,
            unit="seg",
            desc="HLS segments",
            dynamic_ncols=True,
            disable=not self.show_progress_bar,
        )

        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=config.media_headers(),
            cookie_jar=cookie_jar,
        ) as session:

            async def worker(index: int) -> None:
                self._check_cancel()
                async with semaphore:
                    self._check_cancel()
                    written = await self._fetch_segment(
                        session, segments[index], seg_files[index], limiter
                    )
                downloaded_bytes[0] += written
                completed[0] += 1
                elapsed = max(time.monotonic() - start, 1e-6)
                speed = downloaded_bytes[0] / elapsed
                bar.update(1)
                bar.set_postfix_str(
                    f"{format_size(downloaded_bytes[0])} @ {format_size(speed)}/s"
                )
                self._report_progress(
                    "hls",
                    downloaded_bytes[0],
                    None,
                    frac=completed[0] / total_segs if total_segs else 1.0,
                )

            try:
                await asyncio.gather(*(worker(i) for i in range(total_segs)))
            finally:
                bar.close()

        self._report_progress("hls", downloaded_bytes[0], None, frac=1.0, force=True)
        return seg_files

    async def _fetch_segment(
        self,
        session: aiohttp.ClientSession,
        url: str,
        dest: Path,
        limiter: AsyncRateLimiter,
    ) -> int:
        """Download one segment with retry/backoff; skip if already present."""
        if dest.exists() and dest.stat().st_size > 0:
            return dest.stat().st_size  # resume: segment already complete

        tmp = dest.with_suffix(".part")
        last_error: Optional[Exception] = None
        for attempt in range(config.RETRY_ATTEMPTS):
            written = 0
            try:
                async with session.get(
                    url, proxy=self._current_proxy()
                ) as resp:
                    if resp.status in config.ROTATE_STATUS_CODES:
                        self._rotate_proxy()
                        raise DownloadError(f"HTTP {resp.status}")
                    resp.raise_for_status()
                    with open(tmp, "wb") as handle:
                        async for chunk in resp.content.iter_chunked(65536):
                            await limiter.acquire(len(chunk))
                            handle.write(chunk)
                            written += len(chunk)
                os.replace(tmp, dest)
                return written
            except (aiohttp.ClientError, DownloadError, asyncio.TimeoutError) as exc:
                last_error = exc
                safe_remove(tmp)
                await asyncio.sleep(
                    min(config.RETRY_BASE_DELAY * (2 ** attempt), config.RETRY_MAX_DELAY)
                )
        raise DownloadError(f"Segment failed after retries ({last_error})")

    # -- ffmpeg muxing / post-processing -----------------------------------

    def _ffmpeg_headers(self) -> str:
        """Build a CRLF-joined header block for ffmpeg's ``-headers`` option."""
        headers = config.base_headers()
        headers.pop("User-Agent", None)  # passed separately via -user_agent
        return "".join(f"{key}: {value}\r\n" for key, value in headers.items())

    def _concat_segments(self, seg_files: List[Path], output_path: Path) -> None:
        """Concatenate TS segments into ``output_path`` without re-encoding."""
        list_file = output_path.with_name(output_path.stem + ".ffconcat.txt")
        with open(list_file, "w", encoding="utf-8") as handle:
            for seg in seg_files:
                handle.write(f"file '{seg.resolve().as_posix()}'\n")

        args = [
            self._ffmpeg, "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy",
        ]
        if output_path.suffix.lower() == ".mp4":
            args += ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]
        args.append(str(output_path))

        try:
            self._run(args, "segment concatenation")
        finally:
            safe_remove(list_file)

    def _ffmpeg_hls_direct(self, media_url: str, video_path: Path) -> None:
        """Let ffmpeg fetch and mux an (often encrypted) HLS stream directly."""
        tmp_out = video_path.with_name(video_path.stem + ".muxing" + video_path.suffix)
        args = [
            self._ffmpeg, "-y",
            "-user_agent", config.random_user_agent(),
            "-headers", self._ffmpeg_headers(),
            "-i", media_url, "-c", "copy",
        ]
        if video_path.suffix.lower() == ".mp4":
            args += ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"]
        args.append(str(tmp_out))
        self._run(args, "ffmpeg HLS mux")
        os.replace(tmp_out, video_path)

    def _merge_audio(self, info: VideoInfo, video_path: Path) -> None:
        """Download a separate audio track and mux it into the video file."""
        audio_url = self._best_audio_url(info)
        if not audio_url:
            logger.warning("No separate audio stream found; skipping audio merge.")
            return

        audio_tmp = video_path.with_name(video_path.stem + ".audio.tmp")
        try:
            self._download_simple(audio_url, audio_tmp)
            merged = video_path.with_name(video_path.stem + ".merged" + video_path.suffix)
            args = [
                self._ffmpeg, "-y",
                "-i", str(video_path), "-i", str(audio_tmp),
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
            ]
            if video_path.suffix.lower() == ".mp4":
                args += ["-bsf:a", "aac_adtstoasc"]
            args.append(str(merged))
            self._run(args, "audio merge")
            os.replace(merged, video_path)
        finally:
            safe_remove(audio_tmp)

    @staticmethod
    def _best_audio_url(info: VideoInfo) -> Optional[str]:
        formats = info.raw.get("formats") if info.raw else None
        if not isinstance(formats, list):
            return None
        audio_only = [
            f for f in formats
            if isinstance(f, dict)
            and str(f.get("acodec") or "none") != "none"
            and str(f.get("vcodec") or "none") == "none"
            and f.get("url")
        ]
        if not audio_only:
            return None
        best = max(audio_only, key=lambda f: float(f.get("abr") or 0.0))
        return str(best.get("url"))

    def _download_simple(self, url: str, dest: Path) -> None:
        """Download a small resource (e.g. an audio track) with retries."""
        last_error: Optional[Exception] = None
        for attempt in range(config.RETRY_ATTEMPTS):
            try:
                resp = self._session.get(
                    url,
                    headers=config.media_headers(),
                    proxies=self._current_proxies(),
                    stream=True,
                    timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT),
                )
                if resp.status_code in config.ROTATE_STATUS_CODES:
                    self._rotate_proxy()
                    raise DownloadError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                with open(dest, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=config.CHUNK_SIZE):
                        if chunk:
                            handle.write(chunk)
                return
            except (requests.RequestException, DownloadError) as exc:
                last_error = exc
                self._backoff(attempt)
        raise DownloadError(f"Failed to download {url}: {last_error}")

    def _convert(self, video_path: Path) -> Path:
        """Transcode or remux per ``--convert`` and return the new path."""
        target = (self.convert or "").strip()
        if not target:
            return video_path

        if target.lower() in config.SUPPORTED_CONTAINERS:
            out = video_path.with_suffix("." + target.lower())
            if out == video_path:
                return video_path
            self._run(
                [self._ffmpeg, "-y", "-i", str(video_path), "-c", "copy", str(out)],
                "container remux",
            )
            safe_remove(video_path)
            logger.info("Remuxed to %s", out.name)
            return out

        # Otherwise treat the value as a target video codec and transcode.
        out = video_path.with_name(video_path.stem + ".converted" + video_path.suffix)
        self._run(
            [
                self._ffmpeg, "-y", "-i", str(video_path),
                "-c:v", target, "-c:a", "aac", str(out),
            ],
            f"transcode to {target}",
        )
        os.replace(out, video_path)
        logger.info("Transcoded video to %s", target)
        return video_path

    def _verify_output(self, video_path: Path) -> bool:
        """Verify the finished file, using ffprobe when ffmpeg is available.

        In ``--no-ffmpeg`` mode ffprobe is unavailable, so we fall back to a
        basic non-empty-file check.
        """
        if self.no_ffmpeg:
            return video_path.exists() and video_path.stat().st_size > 0
        return self._verify(video_path)

    def _verify(self, video_path: Path) -> bool:
        """Return True if ffprobe reports at least one valid video stream."""
        if not video_path.exists() or video_path.stat().st_size == 0:
            return False
        try:
            result = subprocess.run(
                [
                    self._ffprobe, "-v", "error",
                    "-show_entries", "stream=codec_type",
                    "-of", "json", str(video_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout or "{}")
            streams = data.get("streams", [])
            return any(s.get("codec_type") == "video" for s in streams)
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            logger.debug("ffprobe verification error: %s", exc)
            return False

    def _run(self, args: List[str], description: str) -> None:
        """Run an ffmpeg command, raising :class:`DownloadError` on failure."""
        logger.debug("Running %s: %s", description, " ".join(args[:6]) + " ...")
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-5:]
            raise DownloadError(
                f"{description} failed (ffmpeg exit {result.returncode}): "
                + " | ".join(tail)
            )
