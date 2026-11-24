"""LocalDownloader - a local web UI for downloading videos as MP4 or MP3."""

import json
import os
import re
import threading
import urllib.request
import uuid
import zipfile
from pathlib import Path

import uvicorn
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_DIR.mkdir(exist_ok=True)

HOST = "127.0.0.1"
PORT = 7331

STATE_FILE = BASE_DIR / "last_folder.txt"

app = FastAPI(title="LocalDownloader")

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_dialog_lock = threading.Lock()


def _last_dest() -> Path:
    """Folder chosen last time (persisted), defaulting to the app's downloads dir."""
    try:
        saved = STATE_FILE.read_text(encoding="utf-8").strip()
        p = Path(saved)
        if saved and p.is_dir():
            return p
    except OSError:
        pass
    return DOWNLOAD_DIR


def _set_last_dest(path: Path) -> None:
    try:
        STATE_FILE.write_text(str(path), encoding="utf-8")
    except OSError:
        pass

VIDEO_QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360"}
AUDIO_QUALITIES = {"320", "256", "192", "128"}

# Spotify playlist/album links (the app resolves tracks, audio comes from YouTube)
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?(playlist|album)/([A-Za-z0-9]+)")
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"  # "video" -> mp4, "audio" -> mp3
    quality: str = "best"
    dest: str | None = None  # folder picked by the user; None -> last used
    limit: int = 0  # spotify: max tracks (0 = all)


class PathRequest(BaseModel):
    path: str


def _fetch_info(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise HTTPException(status_code=400, detail="That playlist has no videos.")
        info = entries[0]

    return {
        "title": info.get("title") or "Unknown title",
        "uploader": info.get("uploader") or info.get("channel") or "Unknown",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "site": (info.get("extractor_key") or info.get("extractor") or "web").title(),
        "view_count": info.get("view_count"),
        "webpage_url": info.get("webpage_url") or url,
    }


def _fetch_spotify_meta(url: str) -> dict:
    """Resolve a Spotify playlist/album into its track list (no API key needed)."""
    m = SPOTIFY_RE.search(url)
    if not m:
        raise HTTPException(status_code=400, detail="Paste a Spotify playlist or album link.")
    kind, sid = m.group(1), m.group(2)

    req = urllib.request.Request(
        f"https://open.spotify.com/embed/{kind}/{sid}",
        headers={"User-Agent": BROWSER_UA, "Accept": "text/html"},
    )
    html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")

    blob = None
    for mm in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S):
        if "pageProps" in mm.group(1):
            blob = json.loads(mm.group(1))
            break
    entity = (
        ((blob or {}).get("props") or {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity")
    )
    if not entity:
        raise HTTPException(status_code=400, detail="Could not read that Spotify link.")

    tracks = []
    for t in entity.get("trackList") or []:
        if t.get("title"):
            tracks.append(
                {
                    "title": t["title"],
                    "artist": t.get("subtitle") or "",
                    "duration": t.get("duration") or 0,
                }
            )
    if not tracks:
        raise HTTPException(status_code=400, detail="That playlist has no tracks (or is private).")

    cover = None
    sources = (entity.get("coverArt") or {}).get("sources") or []
    if sources:
        cover = sources[0].get("url")

    return {
        "kind": kind,
        "title": entity.get("name") or entity.get("title") or "Spotify",
        "cover": cover,
        "tracks": tracks,
    }


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
    return name[:120] or "download"


def _resolve_dest(dest: str | None) -> Path:
    if dest:
        try:
            p = Path(dest)
            if p.is_dir():
                return p
        except (OSError, ValueError):
            pass
    return DOWNLOAD_DIR


def _run_spotify(job_id: str, req: DownloadRequest, meta: dict) -> None:
    """Download every track of a Spotify playlist/album as MP3, then zip it."""
    job = _jobs[job_id]
    dest = _resolve_dest(req.dest)
    folder = dest / _safe_name(meta["title"])
    tracks = meta["tracks"][: req.limit] if req.limit > 0 else meta["tracks"]
    total = len(tracks)
    failed: list[str] = []

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        with _lock:
            job.update({"status": "error", "error": f"Cannot create folder: {exc}"})
        return

    with _lock:
        job.update({"status": "downloading", "total": total, "done": 0, "percent": 0.0})

    for i, t in enumerate(tracks, 1):
        query = (t["artist"] + " - " + t["title"]).strip(" -")[:150]

        def track_hook(d: dict, _i: int = i, _t: dict = t) -> None:
            if d.get("status") == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done_b = d.get("downloaded_bytes") or 0
                pct = int(done_b * 100 / tot) if tot else 0
                with _lock:
                    job["pstage"] = f"Track {_i}/{total} ({pct}%)"
                    job["current"] = f'{_t["title"]} — {_t["artist"]}' if _t["artist"] else _t["title"]

        opts = {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": req.quality if req.quality in AUDIO_QUALITIES else "192",
                }
            ],
            "outtmpl": str(folder / f"{i:02d} - %(title).90s [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [track_hook],
        }

        with _lock:
            job["pstage"] = f"Track {i}/{total}"
            job["current"] = f'{t["title"]} — {t["artist"]}' if t["artist"] else t["title"]
            job["percent"] = round((i - 1) * 100 / total, 1)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info("ytsearch1:" + query, download=True)
        except Exception:  # noqa: BLE001 - one bad track shouldn't kill the playlist
            failed.append(t["title"])

        with _lock:
            job["done"] = i
            job["percent"] = round(i * 100 / total, 1)

    with _lock:
        job.update({"status": "processing", "pstage": "Creating zip...", "current": ""})

    try:
        zip_path = dest / (_safe_name(meta["title"]) + ".zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(folder.iterdir()):
                if f.is_file() and f.name != "desktop.ini":
                    zf.write(f, arcname=f"{folder.name}/{f.name}")
        _set_last_dest(dest)
        with _lock:
            job.update(
                {
                    "status": "finished",
                    "percent": 100.0,
                    "file": zip_path.name,
                    "path": str(zip_path),
                    "size": zip_path.stat().st_size,
                    "failed": len(failed),
                    "pstage": None,
                    "current": None,
                    "error": None,
                }
            )
    except Exception as exc:  # noqa: BLE001
        with _lock:
            job.update({"status": "error", "error": _clean_error(exc)})


def _build_opts(req: DownloadRequest, job: dict, outdir: Path) -> dict:
    outtmpl = str(outdir / "%(title).120s [%(id)s].%(ext)s")

    def progress_hook(d: dict) -> None:
        with _lock:
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                job["status"] = "downloading"
                job["percent"] = round(done * 100 / total, 1) if total else None
                job["speed"] = (d.get("_speed_str") or "").strip()
                job["eta"] = (d.get("_eta_str") or "").strip()
            elif status == "finished":
                job["status"] = "processing"
                job["percent"] = 100.0
                job["speed"] = ""
                job["eta"] = ""

    def postprocessor_hook(d: dict) -> None:
        with _lock:
            if d.get("status") == "finished":
                job["status"] = "processing"

    if req.mode == "audio":
        opts = {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": req.quality,
                }
            ],
        }
    else:
        if req.quality == "best":
            fmt = "bestvideo+bestaudio/best"
        else:
            h = int(req.quality)
            fmt = (
                f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
                f"/bestvideo[height<={h}]/best"
            )
        opts = {
            "format": fmt,
            "merge_output_format": "mp4",
        }

    opts.update(
        {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
        }
    )
    return opts


def _run_download(job_id: str, req: DownloadRequest) -> None:
    job = _jobs[job_id]
    outdir = DOWNLOAD_DIR
    if req.dest:
        try:
            p = Path(req.dest)
            if p.is_dir():
                outdir = p
        except (OSError, ValueError):
            pass
    try:
        opts = _build_opts(req, job, outdir)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=True)

        filepath = info.get("filepath")
        if not filepath:
            filepath = ydl.prepare_filename(info)
            if req.mode == "audio":
                filepath = str(Path(filepath).with_suffix(".mp3"))

        path = Path(filepath)
        if not path.exists():
            # Fall back to the newest file in the target folder for this job.
            candidates = sorted(outdir.glob("*"), key=lambda p: p.stat().st_mtime)
            if not candidates:
                raise RuntimeError("Download finished but no file was found.")
            path = candidates[-1]

        _set_last_dest(outdir)
        with _lock:
            job.update(
                {
                    "status": "finished",
                    "percent": 100.0,
                    "file": path.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                    "error": None,
                }
            )
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        with _lock:
            job.update({"status": "error", "error": _clean_error(exc)})


def _clean_error(exc: Exception) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    # yt-dlp wraps errors in "[ERROR] ..." prefixes - keep it short for the UI.
    if "ERROR:" in msg:
        msg = msg.split("ERROR:", 1)[-1].strip()
    lines = [ln for ln in msg.splitlines() if ln.strip()]
    return lines[-1] if lines else "Download failed."


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/info")
def info(req: InfoRequest) -> JSONResponse:
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Please paste a link first.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if SPOTIFY_RE.search(url):
        meta = _fetch_spotify_meta(url)
        return JSONResponse(
            {
                "spotify": True,
                "kind": meta["kind"],
                "title": meta["title"],
                "thumbnail": meta["cover"],
                "site": "Spotify",
                "track_count": len(meta["tracks"]),
            }
        )

    try:
        data = _fetch_info(url)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=_clean_error(exc)) from exc
    return JSONResponse(data)


@app.post("/api/download")
def download(req: DownloadRequest) -> dict:
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Missing URL.")
    if req.mode not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="Invalid mode.")
    if req.mode == "video" and req.quality not in VIDEO_QUALITIES:
        raise HTTPException(status_code=400, detail="Invalid video quality.")
    if req.mode == "audio" and req.quality not in AUDIO_QUALITIES:
        raise HTTPException(status_code=400, detail="Invalid audio quality.")

    meta = None
    if SPOTIFY_RE.search(url):
        meta = _fetch_spotify_meta(url)  # validates early, raises 400 on bad links

    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "starting",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "file": None,
            "path": None,
            "size": None,
            "error": None,
            "pstage": None,
            "current": None,
            "done": 0,
            "total": 0,
            "failed": 0,
        }
    if meta is not None:
        threading.Thread(target=_run_spotify, args=(job_id, req, meta), daemon=True).start()
    else:
        threading.Thread(target=_run_download, args=(job_id, req), daemon=True).start()
    return {"id": job_id}


@app.get("/api/job/{job_id}")
def job(job_id: str) -> dict:
    with _lock:
        data = _jobs.get(job_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        return dict(data)


@app.get("/api/files")
def files() -> list[dict]:
    items = []
    for p in DOWNLOAD_DIR.glob("*"):
        if p.is_file() and not p.name.endswith((".part", ".ytdl")) and p.name != "desktop.ini":
            items.append(
                {"name": p.name, "size": p.stat().st_size, "ext": p.suffix.lstrip(".").lower()}
            )
    items.sort(key=lambda i: i["name"].lower())
    return items


@app.post("/api/pick-folder")
def pick_folder() -> dict:
    """Open a native Windows folder chooser; returns the chosen path."""
    initial = _last_dest()
    with _dialog_lock:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                chosen = filedialog.askdirectory(
                    initialdir=str(initial),
                    title="Choose where to save",
                    parent=root,
                )
            finally:
                root.destroy()
        except Exception:  # noqa: BLE001 - fall back to the default folder
            return {"path": str(DOWNLOAD_DIR), "fallback": True}
    if chosen:
        return {"path": chosen}
    return {"cancelled": True}


@app.post("/api/reveal")
def reveal(req: PathRequest) -> dict:
    """Reveal a file in Explorer (or open the folder if a folder was given)."""
    try:
        p = Path(req.path).resolve()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid path.") from exc
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found.")
    os.startfile(str(p.parent if p.is_file() else p))  # noqa: S606
    return {"ok": True}


@app.post("/api/open-folder")
def open_folder() -> dict:
    os.startfile(str(_last_dest()))  # noqa: S606 - opens Explorer locally
    return {"ok": True}


@app.get("/api/file/{name}")
def get_file(name: str) -> FileResponse:
    path = (DOWNLOAD_DIR / name).resolve()
    if DOWNLOAD_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path, filename=name)


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
