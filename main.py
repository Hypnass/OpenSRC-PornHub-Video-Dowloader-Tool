"""Command-line entry point for the PornHub video downloader.

Usage examples::

    python main.py -u "https://www.pornhub.com/view_video.php?viewkey=XXXX"
    python main.py -b urls.txt -q 1080 -o ./downloads -t 32
    python main.py -u URL --filename-template "%(uploader)s - %(title)s [%(id)s].%(ext)s"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

import config
from downloader import Downloader, DownloadResult
from extractor import Extractor
from utils import dedupe_preserve_order, format_size, read_lines, setup_logging


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse command-line parser."""
    parser = argparse.ArgumentParser(
        prog="phdl",
        description="Download PornHub videos at maximum available quality.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-u", "--url", help="Single video URL.")
    parser.add_argument(
        "-b", "--batch", help="Path to a text file with one URL per line."
    )
    parser.add_argument(
        "-o", "--output", default=config.DEFAULT_OUTPUT_DIR,
        help="Output directory.",
    )
    parser.add_argument(
        "-q", "--quality", default=config.DEFAULT_QUALITY,
        choices=["best", "2160", "1440", "1080", "720", "480", "360", "240"],
        help="Preferred quality; 'best' picks the highest available.",
    )
    parser.add_argument(
        "-f", "--format", default=config.DEFAULT_FORMAT,
        choices=["mp4", "mkv"], help="Output container format.",
    )
    parser.add_argument(
        "-t", "--threads", type=int, default=config.DEFAULT_THREADS,
        help="Concurrent HLS segment downloads.",
    )
    parser.add_argument(
        "-p", "--proxy",
        help="Proxy URL (http://host:port) or path to a proxies.txt file.",
    )
    parser.add_argument(
        "-c", "--cookies", help="Path to a cookies.txt file (for premium content)."
    )
    parser.add_argument(
        "--no-metadata", action="store_true", help="Skip writing the .info.json file."
    )
    parser.add_argument(
        "--no-thumbnail", action="store_true", help="Skip downloading the thumbnail."
    )
    parser.add_argument(
        "-r", "--rate-limit", type=float, default=config.DEFAULT_RATE_LIMIT_MBPS,
        help="Maximum download speed in MB/s (0 = unlimited).",
    )
    parser.add_argument(
        "--filename-template", default=config.DEFAULT_FILENAME_TEMPLATE,
        help="Output filename template (yt-dlp %%(key)s syntax).",
    )
    parser.add_argument(
        "--convert",
        help="Transcode/remux output. A container (mp4/mkv/webm/mov) remuxes "
             "without re-encoding; any other value is treated as a video codec "
             "(e.g. libx265) and triggers a transcode.",
    )
    parser.add_argument(
        "--merge-audio", action="store_true",
        help="Merge a separate audio stream when the video has none embedded.",
    )
    parser.add_argument(
        "--no-ffmpeg", action="store_true",
        help="Run without ffmpeg: direct MP4s download normally and unencrypted "
             "HLS is saved as a byte-merged .ts (plays in VLC/MPV). Encrypted "
             "HLS, --convert and --merge-audio are unavailable in this mode.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging."
    )
    return parser


def collect_urls(args: argparse.Namespace) -> List[str]:
    """Gather, validate, and de-duplicate URLs from --url and --batch."""
    urls: List[str] = []
    if args.url:
        urls.append(args.url.strip())
    if args.batch:
        batch_path = Path(args.batch)
        if not batch_path.is_file():
            raise SystemExit(f"Batch file not found: {batch_path}")
        urls.extend(read_lines(batch_path))

    urls = dedupe_preserve_order(u for u in urls if u)
    valid, invalid = [], []
    for url in urls:
        (valid if Extractor.is_supported_url(url) else invalid).append(url)
    for bad in invalid:
        print(f"  ! Skipping unrecognised URL: {bad}", file=sys.stderr)
    return valid


def resolve_proxies(proxy_arg: Optional[str]) -> List[str]:
    """Resolve the --proxy argument into a list of proxy URLs."""
    if not proxy_arg:
        return []
    path = Path(proxy_arg)
    if path.is_file():
        return read_lines(path)
    return [proxy_arg]


def print_summary(results: List[DownloadResult], elapsed: float) -> None:
    """Print the end-of-run summary line required by the spec."""
    succeeded = [r for r in results if r.status == "success"]
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in succeeded if r.skipped]
    total_size = sum(r.size for r in succeeded)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Succeeded : {len(succeeded)} ({len(skipped)} already present)")
    print(f"  Failed    : {len(failed)}")
    print(f"  Total size: {format_size(total_size)}")
    print(f"  Time      : {elapsed:.1f}s")
    if failed:
        print("\n  Failures:")
        for result in failed:
            print(f"    - {result.url}\n        {result.error}")
    print("=" * 60)


def main(argv: Optional[List[str]] = None) -> int:
    """Program entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    output_dir = Path(args.output).expanduser().resolve()
    logger = setup_logging(output_dir, verbose=args.verbose)

    urls = collect_urls(args)
    if not urls:
        print("No valid URLs provided. Use --url or --batch.", file=sys.stderr)
        return 2

    try:
        downloader = Downloader(
            output_dir=output_dir,
            quality=args.quality,
            file_format=args.format,
            threads=args.threads,
            proxies=resolve_proxies(args.proxy),
            cookiefile=args.cookies,
            no_metadata=args.no_metadata,
            no_thumbnail=args.no_thumbnail,
            rate_limit_mbps=args.rate_limit,
            filename_template=args.filename_template,
            convert=args.convert,
            merge_audio=args.merge_audio,
            no_ffmpeg=args.no_ffmpeg,
        )
    except RuntimeError as exc:  # e.g. ffmpeg missing
        print(f"Setup error: {exc}", file=sys.stderr)
        return 1

    logger.info("Queued %d URL(s). Output -> %s", len(urls), output_dir)
    start = time.monotonic()
    results: List[DownloadResult] = []
    for index, url in enumerate(urls, start=1):
        logger.info("\n[%d/%d] %s", index, len(urls), url)
        results.append(downloader.download(url))

    print_summary(results, time.monotonic() - start)
    return 0 if all(r.status == "success" for r in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        sys.exit(130)
