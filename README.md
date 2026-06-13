<div align="center">

# 🎬 PHDownloader

**A modern PornHub video downloader — command-line tool + polished PyQt6 desktop GUI.**

Downloads at the highest available quality (up to 4K / 2160p) using
[yt-dlp](https://github.com/yt-dlp/yt-dlp) for extraction, concurrent HLS
segment downloading, and ffmpeg muxing — all wrapped in a dark, native desktop app.

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![PyQt6](https://img.shields.io/badge/GUI-PyQt6-41cd52)
![Platform](https://img.shields.io/badge/Windows-supported-0078d6)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

---

## ⚠️ Disclaimer — read first

- **For personal, lawful use only.** You alone are responsible for how you use this software.
- Only download content you **own or are explicitly authorised** to download. Downloading
  copyrighted material without permission may be illegal in your jurisdiction.
- Respect the target website's **Terms of Service** — automated downloading may violate them.
- **18+ only.** This tool accesses an adult website. Do not use it if you are a minor or
  where such content is unlawful.
- This project is **not affiliated with, endorsed by, or connected to** PornHub or its
  operators. All trademarks belong to their respective owners.
- Provided **"as is", without warranty**. The authors accept no liability (see `LICENSE`).

---

## 📸 Screenshots

> Add your own screenshots to `docs/` and they will show here.

| Home | Queue | History | Settings |
| --- | --- | --- | --- |
| ![Home](docs/home.png) | ![Queue](docs/queue.png) | ![History](docs/history.png) | ![Settings](docs/settings.png) |

---

## ✨ Features

**Desktop GUI (PyQt6)**
- Dark / light themes (accent orange `#ff9000`), frameless window with custom title bar,
  edge-resize, drop shadow, smooth page transitions.
- Paste a URL or **drag-and-drop a `.txt`** list to batch import.
- Live download **cards**: thumbnail, title, uploader/duration/quality, gradient progress
  bar with `% · speed · ETA`, status badge, per-item pause/resume/cancel/open-folder.
- **Searchable history** (SQLite) with a right-click menu (re-download, open, copy URL, remove).
- Full **settings** (proxy, timeouts, retries, rate limit, ffmpeg path, cookies, template).
- **System tray**, desktop notifications, keyboard shortcuts.
- Downloads run on background threads — the UI never freezes.

**Engine (shared by GUI & CLI)**
- Best-quality auto-select (240p → 2160p) or pick a specific resolution.
- HLS (m3u8): concurrent segment download + ffmpeg merge. Direct MP4 with **resume**.
- yt-dlp extraction with a custom `mediaDefinitions` HTML fallback.
- Metadata `.info.json` + `.jpg` thumbnail sidecars, unicode-safe filenames.
- Retry with exponential backoff, proxy rotation, rate limiting, integrity check.
- **Works without ffmpeg too** — unencrypted HLS is byte-merged to a playable `.ts`.

---

## 🚀 Quick start

### Option A — Windows executable (no Python needed)

1. Download **`PHDownloader-win64.zip`** (from the release / forum attachment).
2. Extract it anywhere.
3. Run **`PHDownloader/PHDownloader.exe`**.

ffmpeg is downloaded automatically on first run; until then HLS saves as `.ts`.

### Option B — Run from source (auto-installs dependencies)

```bash
git clone <your-repo-url>
cd phvid
python launcher.py
```

`launcher.py` installs any missing Python packages on first run, then starts the GUI.

> **Türkçe hızlı başlangıç:** Exe'yi indir → `PHDownloader.exe`'ye çift tıkla. Veya kaynaktan:
> `python launcher.py` (eksik paketleri ve ffmpeg'i otomatik kurar).

---

## 🛠️ Build the executable yourself

```bash
python build_exe.py            # -> dist/PHDownloader.exe   (single file, default)
python build_exe.py --onedir   # -> dist/PHDownloader/...   (one folder, faster start)
```

PyInstaller is installed automatically if missing.
For the **one-folder** build, distribute the whole `PHDownloader` folder.

---

## 💻 Command-line usage

The engine is fully usable from the terminal via `main.py`:

```bash
pip install -r requirements.txt        # backend deps only

python main.py -u "https://www.pornhub.com/view_video.php?viewkey=XXXX"
python main.py -b urls.txt -q 1080 -o ./downloads -t 32
python main.py -u "URL" --no-ffmpeg    # save as .ts without ffmpeg
```

Common flags: `-u/--url`, `-b/--batch`, `-o/--output`, `-q/--quality`
(`best/2160/1440/1080/720/480`), `-f/--format` (`mp4/mkv`), `-t/--threads`,
`-p/--proxy`, `-c/--cookies`, `-r/--rate-limit`, `--filename-template`,
`--no-metadata`, `--no-thumbnail`, `--no-ffmpeg`. Run `python main.py --help` for all.

---

## 📦 Requirements

- **Python 3.9+** (only to run from source / build)
- **ffmpeg + ffprobe** — auto-downloaded on Windows; otherwise `winget install Gyan.FFmpeg`,
  `brew install ffmpeg`, or `apt install ffmpeg`.
- Python packages: see [`requirements.txt`](requirements.txt) (CLI) /
  [`gui/requirements.txt`](gui/requirements.txt) (GUI).

---

## 🗂️ Project layout

```
phvid/
├── main.py            # CLI entry point
├── launcher.py        # GUI bootstrap (auto-installs deps, then starts the app)
├── build_exe.py       # PyInstaller build script
├── config.py          # headers, user-agents, defaults
├── extractor.py       # URL parsing, quality detection, metadata (yt-dlp + fallback)
├── downloader.py      # HLS/MP4 download, retries, ffmpeg muxing, progress hooks
├── utils.py           # filename sanitisation, logging, rate limiting
└── gui/               # PyQt6 desktop app (see gui/README.md)
    ├── main.py · main_window.py
    ├── widgets/ · pages/ · core/ · utils/ · resources/
```

The GUI drives the same backend through background `QThread` workers — see
[`gui/README.md`](gui/README.md) for the full GUI documentation and architecture.

---

## 🧯 Troubleshooting

- **`Segment failed (HTTP 403)`** — the CDN may be rate-limiting. Lower
  *Settings → Network → Segment threads* (e.g. to 4) and retry. Full errors are logged to
  `errors.log` in the app data folder.
- **Output is `.ts` instead of `.mp4`** — ffmpeg isn't installed yet. Wait for the first-run
  auto-download to finish, or install ffmpeg manually.
- **Extraction fails for every URL** — update yt-dlp: `pip install -U yt-dlp` (PornHub changes
  its page often; yt-dlp ships fixes quickly).

---

## 🤝 Contributing

Issues and pull requests are welcome. Keep changes focused, typed, and documented.
The backend exposes optional `progress_hook` / `info_hook` / `cancel_event` hooks so new
front-ends can integrate cleanly.

## 📄 License

[MIT](LICENSE) © CheatGlobal-Hypnass

## 🙏 Credits

[yt-dlp](https://github.com/yt-dlp/yt-dlp) · [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) ·
[ffmpeg](https://ffmpeg.org/) · icons inspired by [Feather Icons](https://feathericons.com/) (MIT).
