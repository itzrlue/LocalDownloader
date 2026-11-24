# LocalDownloader

A simple local app for downloading videos as **MP4** or extracting audio as **MP3**.
Runs entirely on your own machine — no ads, no telemetry, nothing uploaded anywhere.

## Features

- **MP4 video** — Best / 1080p / 720p / 480p / 360p
- **MP3 audio** — 320 / 256 / 192 / 128 kbps via ffmpeg
- **Choose where to save** — a native folder picker opens on every download
- **Live progress** — percentage, speed and ETA while downloading
- **Show in folder** — jump straight to the finished file in Explorer
- **Remembers your last folder** — the picker opens where you saved last time
- **Download list** — everything saved to the app folder, with one-click open
- Works with **YouTube, TikTok, Instagram, X/Twitter** and 1000+ other sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))

## Requirements

- Windows with **Python 3.10+**
- **ffmpeg** on PATH (used for merging and MP3 conversion)
- Python packages: `pip install -r requirements.txt`

## Usage

```bash
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:7331** in your browser.

Or double-click **`Launch LocalDownloader.bat`** — it starts the server and
opens the page for you. A `LocalDownloader.lnk` shortcut can be placed on the
Desktop for one-click access.

### Flow

1. Paste a link → **Fetch** (shows title, thumbnail, channel, duration)
2. Pick **MP4** or **MP3** and a quality
3. **Download** → choose a save folder in the dialog
4. Watch the progress bar → **Show in folder** when it's done

## Project layout

```
LocalDownloader/
├── app.py                 # FastAPI server + yt-dlp engine
├── static/index.html      # Single-page UI (no build step, no frameworks)
├── downloads/             # Default save location
├── Launch LocalDownloader.bat
├── requirements.txt
└── README.md
```

## Notes

- The server binds to `127.0.0.1` only — it is never reachable from the network.
- Some Instagram/X posts require being logged in; if a link fails, open it in
  your browser first.
- Playlists are not supported — paste a single video link.

## Disclaimer

Only download content you have the right to download. Respect the terms of
service of the platforms you use and the rights of content creators.

## Credits

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FastAPI](https://fastapi.tiangolo.com/) and [ffmpeg](https://ffmpeg.org/).
