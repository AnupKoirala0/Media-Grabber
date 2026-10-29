import base64
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT_DIR = Path(__file__).resolve().parent
PROJECT_VENV = PROJECT_DIR / ".venv"
PROJECT_PYTHON = PROJECT_VENV / "bin" / "python"

if (
    __name__ == "__main__"
    and PROJECT_PYTHON.is_file()
    and Path(sys.prefix).resolve() != PROJECT_VENV.resolve()
):
    os.execv(
        str(PROJECT_PYTHON),
        [str(PROJECT_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
    )

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yt_dlp

APP_NAME = "Freaky Media Grabber"
HOST = "127.0.0.1"
PORT = 8765
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoDownloads"
CONFIG_DIR = Path.home() / ".url_video_downloader"
CONFIG_FILE = CONFIG_DIR / "settings.json"
LOG_FILE = CONFIG_DIR / "app.log"
LEGACY_CACHE_FILES = (
    Path(__file__).resolve().parent / "_pycache_" / "cache.dat",
)
MAX_BODY_SIZE = 64 * 1024
DOWNLOAD_LIMIT_OPTIONS = (1, 2, 3, 4)
FRAGMENT_THREAD_OPTIONS = (4, 8, 12, 16)
MAX_CONCURRENT_DOWNLOADS = 4
CONCURRENT_FRAGMENT_DOWNLOADS = 16

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

DOWNLOAD_DIR = DEFAULT_DOWNLOAD_DIR
HIDDEN_DOWNLOAD_PATHS = set()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_LOCK = threading.Lock()


def configure_temp_directory(download_dir):
    temp_dir = Path(get_temp_directory(download_dir))
    with tempfile.NamedTemporaryFile(dir=temp_dir):
        pass
    temp_dir = str(temp_dir)
    tempfile.tempdir = temp_dir
    for variable in ("TEMP", "TMP", "TMPDIR"):
        os.environ[variable] = temp_dir
    logging.info("Temporary files will be stored in %s", temp_dir)


def get_temp_directory(download_dir=None):
    if download_dir is None:
        with DOWNLOAD_LOCK:
            download_dir = DOWNLOAD_DIR
    temp_dir = Path(download_dir) / "_pycache_" / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return str(temp_dir.resolve())


def load_download_cache():
    cache_dir = DOWNLOAD_DIR / "_pycache_"
    cache_file = cache_dir / "cache.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if not cache_file.exists():
        legacy_cache_files = (
            cache_dir / "cache.dat",
            *LEGACY_CACHE_FILES,
        )
        for legacy_file in legacy_cache_files:
            if not legacy_file.exists():
                continue
            cache_file.write_text(legacy_file.read_text(encoding="utf-8"), encoding="utf-8")
            try:
                legacy_file.unlink()
                if legacy_file.parent != cache_dir:
                    legacy_file.parent.rmdir()
            except OSError:
                logging.warning("Could not remove legacy download cache at %s", legacy_file)
            break
    if not cache_file.exists():
        cache_file.write_text("[]", encoding="utf-8")
        return {}
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Download cache must contain a list of files.")
    entries = {}
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path = str(Path(entry["path"]).expanduser().resolve())
        entries[path.casefold()] = {
            "path": path,
            "name": entry.get("name") if isinstance(entry.get("name"), str) else Path(path).name,
            "location": (
                entry["location"]
                if isinstance(entry.get("location"), str)
                and entry["location"] in {"Music", "Videos"}
                else "Videos"
            ),
            "size": entry.get("size") if type(entry.get("size")) is int else None,
            "modified": entry.get("modified") if isinstance(entry.get("modified"), (int, float)) else None,
            "status": "downloaded",
        }
    return entries


def save_download_cache(entries):
    cache_file = DOWNLOAD_DIR / "_pycache_" / "cache.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(sorted(entries.values(), key=lambda entry: entry["name"].casefold()), indent=2),
        encoding="utf-8",
    )


def download_location(path):
    for parent in Path(path).parents:
        if parent.name.casefold() in {"music", "videos"}:
            return parent.name.title()
    return "Videos"


class DownloadLimiter:
    def __init__(self, limit):
        self._condition = threading.Condition()
        self._limit = limit
        self._active = 0

    def set_limit(self, limit):
        with self._condition:
            self._limit = limit
            self._condition.notify_all()

    def __enter__(self):
        with self._condition:
            self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        with self._condition:
            self._active -= 1
            self._condition.notify_all()


DOWNLOAD_SLOTS = DownloadLimiter(MAX_CONCURRENT_DOWNLOADS)
DOWNLOAD_JOBS = {}
SERVER = None
SERVER_THREAD = None
APP = None


def load_config():
    global DOWNLOAD_DIR, HIDDEN_DOWNLOAD_PATHS
    global MAX_CONCURRENT_DOWNLOADS, CONCURRENT_FRAGMENT_DOWNLOADS
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        candidate = Path(data.get("download_dir", str(DEFAULT_DOWNLOAD_DIR))).expanduser()
        DOWNLOAD_DIR = candidate
        hidden_paths = data.get("hidden_download_paths", [])
        HIDDEN_DOWNLOAD_PATHS = {
            str(Path(path).expanduser().resolve()).casefold()
            for path in hidden_paths
            if isinstance(path, str)
        } if isinstance(hidden_paths, list) else set()
        max_downloads = data.get("max_concurrent_downloads", MAX_CONCURRENT_DOWNLOADS)
        fragment_threads = data.get("concurrent_fragment_downloads", CONCURRENT_FRAGMENT_DOWNLOADS)
        MAX_CONCURRENT_DOWNLOADS = (
            max_downloads if type(max_downloads) is int and max_downloads in DOWNLOAD_LIMIT_OPTIONS
            else 4
        )
        CONCURRENT_FRAGMENT_DOWNLOADS = (
            fragment_threads if type(fragment_threads) is int and fragment_threads in FRAGMENT_THREAD_OPTIONS
            else 16
        )
    except (OSError, ValueError, TypeError):
        DOWNLOAD_DIR = DEFAULT_DOWNLOAD_DIR
        HIDDEN_DOWNLOAD_PATHS = set()
        MAX_CONCURRENT_DOWNLOADS = 4
        CONCURRENT_FRAGMENT_DOWNLOADS = 16
    DOWNLOAD_SLOTS.set_limit(MAX_CONCURRENT_DOWNLOADS)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def save_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps({
            "download_dir": str(DOWNLOAD_DIR),
            "hidden_download_paths": sorted(HIDDEN_DOWNLOAD_PATHS),
            "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
            "concurrent_fragment_downloads": CONCURRENT_FRAGMENT_DOWNLOADS,
        }, indent=2),
        encoding="utf-8",
    )


def set_download_settings(max_downloads, fragment_threads):
    global MAX_CONCURRENT_DOWNLOADS, CONCURRENT_FRAGMENT_DOWNLOADS
    if type(max_downloads) is not int or max_downloads not in DOWNLOAD_LIMIT_OPTIONS:
        raise ValueError("Maximum downloads must be between 1 and 4.")
    if type(fragment_threads) is not int or fragment_threads not in FRAGMENT_THREAD_OPTIONS:
        raise ValueError("Fragment threads must be 4, 8, 12, or 16.")

    previous_max = MAX_CONCURRENT_DOWNLOADS
    previous_fragments = CONCURRENT_FRAGMENT_DOWNLOADS
    MAX_CONCURRENT_DOWNLOADS = max_downloads
    CONCURRENT_FRAGMENT_DOWNLOADS = fragment_threads
    DOWNLOAD_SLOTS.set_limit(max_downloads)
    try:
        save_config()
    except OSError:
        MAX_CONCURRENT_DOWNLOADS = previous_max
        CONCURRENT_FRAGMENT_DOWNLOADS = previous_fragments
        DOWNLOAD_SLOTS.set_limit(previous_max)
        raise


def set_download_dir(path):
    global DOWNLOAD_DIR
    previous_dir = DOWNLOAD_DIR
    candidate = Path(path).expanduser().resolve()
    candidate.mkdir(parents=True, exist_ok=True)
    configure_temp_directory(candidate)
    DOWNLOAD_DIR = candidate
    try:
        save_config()
    except OSError:
        DOWNLOAD_DIR = previous_dir
        configure_temp_directory(previous_dir)
        raise


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def extract_formats(url):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
        "paths": {"temp": get_temp_directory()},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = []
    seen = set()
    for item in info.get("formats", []):
        if not item.get("url") and not item.get("format_id"):
            continue
        format_id = str(item.get("format_id", "best"))
        if format_id in seen:
            continue
        seen.add(format_id)
        ext = (item.get("ext") or "unknown").lower()
        height = item.get("height")
        note = item.get("format_note") or ""
        resolution = f"{height}p" if height else (item.get("resolution") or note or "Audio/Video")
        size = item.get("filesize") or item.get("filesize_approx")
        size_label = f"{size / 1024 / 1024:.1f} MB" if size else "Size unknown"
        has_video = item.get("vcodec") not in (None, "none")
        has_audio = item.get("acodec") not in (None, "none")
        if has_video and has_audio:
            kind = "Video + audio"
        elif has_video:
            kind = "Video only"
        else:
            kind = "Audio"
        formats.append({
            "format_id": format_id,
            "ext": ext,
            "resolution": resolution,
            "filesize": size_label,
            "format_note": note,
            "kind": kind,
            "label": f"{resolution} · {ext.upper()} · {size_label}",
        })

    formats.sort(key=lambda f: (
        0 if f["kind"] == "Video + audio" else 1 if f["kind"] == "Video only" else 2,
        -(int(f["resolution"].replace("p", "")) if f["resolution"].endswith("p") and f["resolution"][:-1].isdigit() else 0),
        f["ext"],
    ))
    return {
        "title": info.get("title") or "Video",
        "webpage_url": info.get("webpage_url") or url,
        "duration": info.get("duration"),
        "formats": formats,
    }


def inspect_download_target(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "paths": {"temp": get_temp_directory()},
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = info.get("entries")
    is_playlist = entries is not None or info.get("_type") == "playlist"
    if not is_playlist:
        return {"title": info.get("title") or "Video", "playlist_count": None}

    playlist_count = info.get("playlist_count")
    if isinstance(playlist_count, bool) or not isinstance(playlist_count, int) or playlist_count < 1:
        playlist_count = sum(1 for entry in entries or () if entry is not None)
    if playlist_count < 1:
        raise ValueError("Could not determine the number of items in this playlist.")

    return {
        "title": info.get("title") or "Playlist",
        "playlist_count": playlist_count,
    }


def run_download(payload, progress_callback=None):
    url = str(payload.get("url") or "").strip()
    if not url:
        raise ValueError("A URL is required.")

    format_id = payload.get("format_id")
    output_format = str(payload.get("output_format") or "").lower().strip()
    if output_format not in {"", "mp4", "mkv", "mp3", "m4a"}:
        raise ValueError("Unsupported output format.")
    height = payload.get("height")
    audio_bitrate = payload.get("audio_bitrate")
    playlist_limit = payload.get("playlist_limit")

    if height is not None:
        if isinstance(height, bool) or not isinstance(height, int) or height not in {120, 240, 480, 720, 1080, 1440, 2160}:
            raise ValueError("Unsupported video resolution.")
        if output_format not in {"mp4", "mkv"}:
            raise ValueError("Video resolution can only be used with MP4 or MKV.")
    if audio_bitrate is not None:
        if isinstance(audio_bitrate, bool) or not isinstance(audio_bitrate, int) or audio_bitrate not in {128, 256}:
            raise ValueError("Unsupported MP3 bitrate.")
        if output_format != "mp3":
            raise ValueError("Audio bitrate can only be used with MP3.")
    if playlist_limit is not None:
        if isinstance(playlist_limit, bool) or not isinstance(playlist_limit, int) or playlist_limit < 1:
            raise ValueError("Playlist item count must be a positive integer.")

    with DOWNLOAD_LOCK:
        target_dir = DOWNLOAD_DIR
    media_folder = "Music" if output_format in {"mp3", "m4a"} else "Videos"
    target_dir = target_dir / media_folder
    target_dir.mkdir(parents=True, exist_ok=True)

    opts = {
        "outtmpl": str(target_dir / "%(title).180B [%(id)s].%(ext)s"),
        "noplaylist": playlist_limit is None,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "overwrites": False,
        "ignoreerrors": playlist_limit is not None,
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENT_DOWNLOADS,
        "paths": {"temp": get_temp_directory(target_dir.parent)},
        "progress_hooks": [lambda data: logging.info("yt-dlp status: %s", data.get("status", ""))],
    }
    if playlist_limit is not None:
        opts["playlistend"] = playlist_limit

    def report_item(info, status, progress=0, filename=None):
        if not progress_callback:
            return
        info = info or {}
        item_id = info.get("id")
        extractor = str(info.get("extractor_key") or info.get("extractor") or "unknown").casefold()
        item_key = f"{extractor}:{item_id}" if item_id else None
        path = filename or info.get("filepath") or info.get("_filename")
        name = str(info.get("title") or (Path(path).name if path else info.get("id") or url))
        if status == "downloading" and output_format and path:
            progress_path = Path(path)
            if progress_path.suffix.lower() in {".part", ".ytdl"}:
                progress_path = progress_path.with_suffix("")
            path = str(progress_path.with_suffix("." + output_format))
        progress_callback({
            "key": item_key or path or f"{extractor}:{info.get('title') or url}",
            "name": name,
            "path": path,
            "status": status,
            "progress": max(0, min(100, int(progress))),
        })

    download_errors = []

    class DownloadLogger:
        def debug(self, message):
            logging.debug("%s", message)

        def warning(self, message):
            logging.warning("%s", message)

        def error(self, message):
            logging.error("%s", message)
            download_errors.append(str(message))
            match = re.search(r"\[([^\]]+)\]\s+([^:\s]+):", message)
            if match:
                report_item(
                    {"extractor_key": match.group(1), "id": match.group(2)},
                    "error",
                )

    opts["logger"] = DownloadLogger()

    def progress_hook(data):
        info = data.get("info_dict") or {}
        status = data.get("status")
        if status == "error":
            report_item(info, "error")
            return
        if status not in {"downloading", "finished"}:
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        downloaded = data.get("downloaded_bytes") or 0
        percentage = downloaded * 100 / total if total else 0
        if status == "finished":
            percentage = 100
        report_item(info, "downloading", percentage, data.get("filename"))

    opts["progress_hooks"] = [progress_hook]

    if output_format in {"mp3", "m4a"}:
        opts["format"] = format_id or "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": output_format,
            "preferredquality": str(audio_bitrate) if audio_bitrate else "0",
        }]
    else:
        if height:
            opts["format"] = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        else:
            opts["format"] = format_id or "bestvideo*+bestaudio/best"
        if output_format:
            opts["merge_output_format"] = output_format
        elif format_id:
            opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        completed_items = 0
        failed_items = 0
        completed_files = []

        def final_output_path(item):
            path = item.get("filepath") or ydl.prepare_filename(item)
            if output_format:
                path = str(Path(path).with_suffix("." + output_format))
            return path

        if info:
            entries = info.get("entries")
            if entries is not None:
                for entry in entries:
                    if entry:
                        completed_items += 1
                        completed_info = dict(entry)
                        completed_info["filepath"] = final_output_path(entry)
                        completed_files.append(completed_info["filepath"])
                        report_item(completed_info, "completed", 100)
                    else:
                        failed_items += 1
            else:
                completed_items = 1
                completed_info = dict(info)
                completed_info["filepath"] = final_output_path(info)
                completed_files.append(completed_info["filepath"])
                report_item(completed_info, "completed", 100)
        if playlist_limit is not None and completed_items == 0:
            detail = next(
                (error for error in download_errors if "403" in error),
                None,
            )
            reason = (
                " YouTube returned HTTP 403; some formats may require a PO Token."
                if detail else ""
            )
            raise RuntimeError(
                "No playlist items could be downloaded." + reason
            )
        filename = None
        if playlist_limit is None:
            filename = final_output_path(info)
        return {
            "title": info.get("title", "Download"),
            "file": filename,
            "directory": str(target_dir),
            "completed_items": completed_items,
            "failed_items": failed_items,
            "files": completed_files,
        }


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "URLVideoDownloader/1.0"

    def log_message(self, fmt, *args):
        logging.info("%s\n%s", self.address_string(), fmt % args)

    def _send(self, status, payload):
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, {"ok": True, "service": APP_NAME, "download_dir": str(DOWNLOAD_DIR)})
        elif path == "/formats":
            from urllib.parse import parse_qs
            url = parse_qs(urlsplit(self.path).query).get("url", [""])[0]
            if not url:
                self._send(400, {"ok": False, "error": "Missing url parameter"})
                return
            try:
                result = extract_formats(url)
                result["ok"] = True
                self._send(200, result)
            except Exception as exc:
                logging.exception("Format extraction failed")
                self._send(500, {"ok": False, "error": str(exc)})
        else:
            self._send(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/formats", "/download"}:
            self._send(404, {"ok": False, "error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY_SIZE:
                self._send(413, {"ok": False, "error": "Request body too large"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            if path == "/formats":
                result = extract_formats(str(payload.get("url") or "").strip())
                result["ok"] = True
                self._send(200, result)
            else:
                job_id = str(int(time.time() * 1000)) + "-" + str(threading.get_ident())
                self._send(202, {"ok": True, "job_id": job_id, "message": "Download started"})
                threading.Thread(
                    target=self._download_worker,
                    args=(job_id, payload),
                    daemon=True,
                    name="download-" + job_id,
                ).start()
        except Exception as exc:
            logging.exception("Request failed")
            self._send(400, {"ok": False, "error": str(exc)})

    @staticmethod
    def _download_worker(job_id, payload):
        try:
            with DOWNLOAD_SLOTS:
                result = run_download(
                    payload,
                    progress_callback=lambda item: APP.post_event(("download_progress", item)) if APP else None,
                )
            logging.info("Download complete: %s", result.get("file"))
            if APP:
                APP.post_event(("download_done", result))
        except Exception as exc:
            logging.exception("Download failed")
            if APP:
                APP.post_event(("download_error", str(exc)))


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server():
    global SERVER, SERVER_THREAD
    if SERVER is not None:
        return
    SERVER = ReusableThreadingHTTPServer((HOST, PORT), RequestHandler)
    SERVER_THREAD = threading.Thread(target=SERVER.serve_forever, daemon=True, name="local-http-server")
    SERVER_THREAD.start()
    logging.info("Server started on %s:%s", HOST, PORT)


def stop_server():
    global SERVER, SERVER_THREAD
    if SERVER is not None:
        SERVER.shutdown()
        SERVER.server_close()
        SERVER = None
        SERVER_THREAD = None
        logging.info("Server stopped")


class DownloaderApp:
    BG = "#1b1d1f"
    PANEL = "#242628"
    PANEL_2 = "#2d3032"
    BORDER = "#3a3d3f"
    TEXT = "#eceeef"
    MUTED = "#a5aaad"
    GREEN = "#a5c8ae"
    RED = "#d9a4a4"

    def __init__(self, root):
        global APP
        APP = self
        self.root = root
        self.events = queue.Queue()
        self.closing = False
        self.query = tk.StringVar()
        self.url_var = tk.StringVar()
        self.playlist_limit_var = tk.StringVar()
        self.max_downloads_var = tk.StringVar(value=str(MAX_CONCURRENT_DOWNLOADS))
        self.fragment_threads_var = tk.StringVar(value=str(CONCURRENT_FRAGMENT_DOWNLOADS))
        self.download_items = {}
        self.hidden_download_keys = {}
        self.remove_mode = False
        self.delete_mode = False
        self.checked_entries = set()
        self.path_var = tk.StringVar(value=str(DOWNLOAD_DIR))
        self.status_var = tk.StringVar(value=f"Server running at http://{HOST}:{PORT}")
        self.count_var = tk.StringVar(value="0 files")
        try:
            self.download_cache = load_download_cache()
            cache_load_error = None
        except (OSError, ValueError, TypeError) as exc:
            logging.exception("Could not load download cache")
            self.download_cache = {}
            cache_load_error = exc
        self._build_ui()
        if cache_load_error:
            messagebox.showerror("Download cache error", str(cache_load_error), parent=self.root)
            self.status_var.set("Could not load download cache")
        self.url_var.trace_add("write", self.invalidate_format_options)
        self.refresh_files()
        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)
        self.root.after(250, self.process_events)
        self.root.after(5000, self.periodic_refresh)

    def _build_ui(self):
        self.root.title(APP_NAME)
        self.root.geometry("1000x650")
        self.root.minsize(780, 500)
        self.root.configure(bg=self.BG)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=self.PANEL, foreground=self.TEXT, fieldbackground=self.PANEL,
                        rowheight=32, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=self.PANEL_2, foreground=self.MUTED,
                        relief="flat", font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#45494b")], foreground=[("selected", "#ffffff")])
        style.configure("Vertical.TScrollbar", background=self.PANEL_2, troughcolor=self.PANEL,
                        bordercolor=self.PANEL, arrowcolor=self.MUTED)

        shell = tk.Frame(self.root, bg=self.BG)
        shell.pack(fill="both", expand=True, padx=18, pady=18)

        sidebar = tk.Frame(shell, bg=self.PANEL, width=205, highlightbackground=self.BORDER, highlightthickness=1)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=self.PANEL)
        brand.pack(fill="x", padx=18, pady=(22, 24))
        tk.Label(brand, text="URL", bg=self.PANEL, fg=self.GREEN, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(brand, text="VIDEO DOWNLOADER", bg=self.PANEL, fg=self.TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(3, 0))
        tk.Label(brand, text="LOCAL DOWNLOAD MANAGER", bg=self.PANEL, fg=self.MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(5, 0))

        self.nav_buttons = {}
        self._nav_button(sidebar, "Download from URL", "url")
        self._nav_button(sidebar, "Downloads", "downloads")

        spacer = tk.Frame(sidebar, bg=self.PANEL)
        spacer.pack(fill="both", expand=True)
        status_card = tk.Frame(sidebar, bg=self.PANEL_2, highlightbackground=self.BORDER, highlightthickness=1)
        status_card.pack(fill="x", padx=12, pady=12)
        tk.Label(status_card, text="LOCAL SERVER", bg=self.PANEL_2, fg=self.MUTED,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=12, pady=(12, 4))
        tk.Label(status_card, text="●  Running", bg=self.PANEL_2, fg=self.GREEN,
                 font=("Segoe UI", 10)).pack(anchor="w", padx=12)
        tk.Label(status_card, text=f"{HOST}:{PORT}", bg=self.PANEL_2, fg=self.MUTED,
                 font=("Consolas", 9)).pack(anchor="w", padx=12, pady=(4, 12))
        self._nav_button(sidebar, "Settings", "settings")

        main = tk.Frame(shell, bg=self.BG)
        main.pack(side="left", fill="both", expand=True, padx=(18, 0))
        self.pages = {}
        self.pages["url"] = tk.Frame(main, bg=self.BG)
        self.pages["downloads"] = tk.Frame(main, bg=self.BG)
        self.pages["settings"] = tk.Frame(main, bg=self.BG)
        self._build_url_page(self.pages["url"])
        self._build_downloads_page(self.pages["downloads"])
        self._build_settings_page(self.pages["settings"])
        self.show_page("url")

    def _build_page_header(self, parent, title, description):
        header = tk.Frame(parent, bg=self.BG)
        header.pack(fill="x", pady=(3, 20))
        tk.Label(header, text=title, bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI Semibold", 23)).pack(anchor="w")
        tk.Label(header, text=description, bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(5, 0))

    def _build_url_page(self, page):
        self._build_page_header(page, "Download from URL", "Paste a video URL to download it directly.")
        card = tk.Frame(page, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        card.pack(fill="x")
        content = tk.Frame(card, bg=self.PANEL)
        content.pack(fill="x", padx=16, pady=18)
        tk.Label(content, text="VIDEO/PLAYLIST URL", bg=self.PANEL, fg=self.MUTED,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w")
        self.url_entry = tk.Entry(
            content, textvariable=self.url_var, bg=self.PANEL_2, fg=self.TEXT,
            insertbackground=self.TEXT, relief="flat", font=("Segoe UI", 11),
            highlightbackground=self.BORDER, highlightthickness=1,
        )
        self.url_entry.pack(fill="x", pady=(8, 14), ipady=10)
        self.url_entry.bind("<Return>", lambda _: self.download_from_url())
        self.download_url_button = self._button(content, "Download", self.download_from_url)
        self.download_url_button.pack(anchor="e")
        options_container = tk.Frame(page, bg=self.BG)
        options_container.pack(fill="both", expand=True, pady=(16, 0))
        self.options_canvas = tk.Canvas(
            options_container,
            bg=self.BG,
            highlightthickness=0,
            yscrollincrement=24,
        )
        self.options_scrollbar = ttk.Scrollbar(
            options_container,
            orient="vertical",
            command=self.options_canvas.yview,
        )
        self.options_canvas.configure(yscrollcommand=self.options_scrollbar.set)
        self.options_scrollbar.pack(side="right", fill="y")
        self.options_canvas.pack(side="left", fill="both", expand=True)
        self.options_frame = tk.Frame(self.options_canvas, bg=self.BG)
        self.options_window = self.options_canvas.create_window(
            (0, 0),
            window=self.options_frame,
            anchor="nw",
        )
        self.options_frame.bind("<Configure>", self._update_options_scrollregion)
        self.options_canvas.bind("<Configure>", self._resize_options_content)
        self.root.bind_all("<MouseWheel>", self._scroll_format_options)
        tk.Label(page, text="Downloads are saved to the folder configured in Settings.",
                 bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(14, 0))

    def _update_options_scrollregion(self, _event=None):
        self.options_canvas.configure(scrollregion=self.options_canvas.bbox("all"))

    def _resize_options_content(self, event):
        self.options_canvas.itemconfigure(self.options_window, width=event.width)

    def _scroll_format_options(self, event):
        canvas = self.options_canvas
        if not canvas.winfo_ismapped():
            return
        x, y = canvas.winfo_pointerxy()
        if (canvas.winfo_rootx() <= x < canvas.winfo_rootx() + canvas.winfo_width()
                and canvas.winfo_rooty() <= y < canvas.winfo_rooty() + canvas.winfo_height()):
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

    def _build_downloads_page(self, page):
        self._build_page_header(page, "Downloads", "Manage files saved by your browser extension and direct downloads.")
        toolbar = tk.Frame(page, bg=self.BG)
        toolbar.pack(fill="x", pady=(0, 10))
        search = tk.Entry(toolbar, textvariable=self.query, bg=self.PANEL, fg=self.TEXT,
                          insertbackground=self.TEXT, relief="flat", font=("Segoe UI", 10),
                          highlightbackground=self.BORDER, highlightthickness=1)
        search.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 10))
        search.insert(0, "")
        self.query.trace_add("write", lambda *_: self.refresh_files())
        self._button(toolbar, "Refresh", self.refresh_files).pack(side="left")
        self.remove_button = self._button(toolbar, "Remove", self.remove_selected_entry)
        self.remove_button.pack(side="left", padx=(8, 0))
        self.delete_button = self._button(toolbar, "Delete file", self.delete_selected, danger=True)
        self.delete_button.pack(side="left", padx=(8, 0))

        table_card = tk.Frame(page, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        table_card.pack(fill="both", expand=True)
        columns = ("select", "name", "location", "status", "progress", "size", "modified")
        self.tree = ttk.Treeview(table_card, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("select", text="")
        self.tree.heading("name", text="FILE NAME")
        self.tree.heading("location", text="LOCATION")
        self.tree.heading("status", text="STATUS")
        self.tree.heading("progress", text="DOWNLOADED")
        self.tree.heading("size", text="SIZE")
        self.tree.heading("modified", text="DATE MODIFIED")
        self.tree.column("select", width=0, minwidth=0, stretch=False, anchor="center")
        self.tree.column("name", width=290, anchor="w")
        self.tree.column("location", width=90, anchor="center")
        self.tree.column("status", width=105, anchor="center")
        self.tree.column("progress", width=105, anchor="e")
        self.tree.column("size", width=100, anchor="e")
        self.tree.column("modified", width=155, anchor="e")
        scroll = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.tree.bind("<Button-1>", self._toggle_entry_checkbox)
        self.tree.bind("<Double-1>", lambda _: self.open_selected())

        footer = tk.Frame(page, bg=self.BG)
        footer.pack(fill="x", pady=(12, 0))
        tk.Label(footer, textvariable=self.count_var, bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(footer, textvariable=self.status_var, bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 9)).pack(side="right")

    def _build_settings_page(self, page):
        self._build_page_header(page, "Settings", "Choose your download folder and concurrency limits.")
        card = tk.Frame(page, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1)
        card.pack(fill="x")
        content = tk.Frame(card, bg=self.PANEL)
        content.pack(fill="x", padx=16, pady=16)

        concurrency = tk.Frame(content, bg=self.PANEL)
        concurrency.pack(fill="x", pady=(0, 18))
        tk.Label(concurrency, text="MAX SIMULTANEOUS DOWNLOADS", bg=self.PANEL, fg=self.MUTED,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w")
        self.max_downloads_box = ttk.Combobox(
            concurrency, textvariable=self.max_downloads_var,
            values=tuple(str(value) for value in DOWNLOAD_LIMIT_OPTIONS),
            state="readonly", width=8,
        )
        self.max_downloads_box.pack(anchor="w", pady=(6, 12))
        self.max_downloads_box.bind("<<ComboboxSelected>>", self.apply_download_settings)

        tk.Label(concurrency, text="PARALLEL FRAGMENT THREADS PER DOWNLOAD",
                 bg=self.PANEL, fg=self.MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        self.fragment_threads_box = ttk.Combobox(
            concurrency, textvariable=self.fragment_threads_var,
            values=tuple(str(value) for value in FRAGMENT_THREAD_OPTIONS),
            state="readonly", width=8,
        )
        self.fragment_threads_box.pack(anchor="w", pady=(6, 0))
        self.fragment_threads_box.bind("<<ComboboxSelected>>", self.apply_download_settings)
        tk.Label(
            concurrency,
            text="Fragment threading applies to supported media streams.",
            bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(8, 0))

        tk.Label(content, text="DOWNLOAD FOLDER", bg=self.PANEL, fg=self.MUTED,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(content, textvariable=self.path_var, bg=self.PANEL, fg=self.TEXT,
                 font=("Segoe UI", 10), anchor="w", justify="left", wraplength=560).pack(
                     anchor="w", fill="x", pady=(7, 14))
        actions = tk.Frame(content, bg=self.PANEL)
        actions.pack(anchor="w")
        self._button(actions, "Change folder", self.choose_folder).pack(side="left")
        self._button(actions, "Open folder", self.open_folder).pack(side="left", padx=(8, 0))

        attribution = tk.Frame(
            page,
            bg=self.PANEL,
            highlightbackground=self.BORDER,
            highlightthickness=1,
        )
        attribution.pack(fill="x", side="bottom", pady=(16, 0))
        tk.Label(
            attribution,
            text="© 2026 @anupkoirala0 · Developer",
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", padx=16, pady=(12, 4))
        tk.Label(
            attribution,
            text="Created this application for simple, local video downloads without any data collection. Please use responsibly and respect creators’ rights.",
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Segoe UI", 9),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", fill="x", padx=16, pady=(0, 12))

    def apply_download_settings(self, _event=None):
        try:
            set_download_settings(
                int(self.max_downloads_var.get()),
                int(self.fragment_threads_var.get()),
            )
        except (OSError, ValueError) as exc:
            self.max_downloads_var.set(str(MAX_CONCURRENT_DOWNLOADS))
            self.fragment_threads_var.set(str(CONCURRENT_FRAGMENT_DOWNLOADS))
            messagebox.showerror("Settings update failed", str(exc), parent=self.root)
            return
        self.status_var.set(
            f"Download limits updated: {MAX_CONCURRENT_DOWNLOADS} downloads, "
            f"{CONCURRENT_FRAGMENT_DOWNLOADS} fragment threads each"
        )

    def _nav_button(self, parent, label, page):
        button = tk.Button(parent, text=label, command=lambda: self.show_page(page),
                           anchor="w", padx=16, pady=11, bg=self.PANEL, fg=self.MUTED,
                           activebackground=self.PANEL_2, activeforeground=self.TEXT, relief="flat", bd=0,
                           font=("Segoe UI", 10), cursor="hand2")
        button.pack(fill="x", padx=10, pady=3)
        self.nav_buttons[page] = button

    def show_page(self, page):
        for frame in self.pages.values():
            frame.pack_forget()
        self.pages[page].pack(fill="both", expand=True)
        for name, button in self.nav_buttons.items():
            active = name == page
            button.configure(
                bg=self.PANEL_2 if active else self.PANEL,
                fg=self.TEXT if active else self.MUTED,
                font=("Segoe UI Semibold" if active else "Segoe UI", 10),
            )
        if page == "downloads":
            self.refresh_files()
        elif page == "settings":
            self.path_var.set(str(DOWNLOAD_DIR))

    def _button(self, parent, label, command, compact=False, danger=False):
        bg = "#443333" if danger else self.PANEL_2
        fg = self.RED if danger else self.TEXT
        return tk.Button(parent, text=label, command=command, bg=bg, fg=fg,
                         activebackground="#3a3d3f", activeforeground=self.TEXT,
                         relief="flat", bd=0, padx=12 if compact else 15,
                         pady=7 if compact else 9, font=("Segoe UI Semibold", 9),
                         cursor="hand2")

    def post_event(self, event):
        self.events.put(event)

    def process_events(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "download_progress":
                    self._update_download_item(data)
                elif kind == "download_done":
                    self._register_completed_files(data.get("files", []))
                    self.status_var.set("Download completed")
                    self.refresh_files()
                elif kind == "download_error":
                    self.status_var.set("A download failed")
                    messagebox.showerror("Download failed", data, parent=self.root)
                elif kind == "manual_download_done":
                    self._register_completed_files(data.get("files", []))
                    self._finish_format_download()
                    self.download_url_button.configure(state="normal", text="Download")
                    failed_items = data.get("failed_items", 0)
                    completed_items = data.get("completed_items", 0)
                    if failed_items:
                        self.status_var.set(
                            f"Download finished: {completed_items} succeeded, "
                            f"{failed_items} failed"
                        )
                        messagebox.showwarning(
                            "Some downloads failed",
                            f"{completed_items} item(s) downloaded; "
                            f"{failed_items} item(s) could not be downloaded.",
                            parent=self.root,
                        )
                    else:
                        self.status_var.set("Direct download completed")
                    self.refresh_files()
                elif kind == "manual_download_error":
                    self._finish_format_download()
                    self.download_url_button.configure(state="normal", text="Download")
                    self.status_var.set("Direct download failed")
                    messagebox.showerror("Download failed", data, parent=self.root)
                elif kind == "url_info_done":
                    url, info = data
                    self.download_url_button.configure(state="normal", text="Load formats")
                    self.url_entry.configure(state="normal")
                    self.active_download_url = url
                    self.show_format_options(info)
                    self.status_var.set("Choose a format to start downloading")
                elif kind == "url_info_error":
                    error = data
                    self.download_url_button.configure(state="normal", text="Load formats")
                    self.url_entry.configure(state="normal")
                    self.status_var.set("Could not load download options")
                    messagebox.showerror("Could not load formats", error, parent=self.root)
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(250, self.process_events)

    def download_from_url(self):
        url = self.url_var.get().strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            messagebox.showerror(
                "Invalid URL",
                "Enter a valid HTTP or HTTPS video URL.",
                parent=self.root,
            )
            return

        for child in self.options_frame.winfo_children():
            child.destroy()
        self.download_url_button.configure(state="disabled", text="Loading…")
        self.url_entry.configure(state="disabled")
        self.status_var.set("Loading available download options…")
        threading.Thread(
            target=self._inspect_download_worker,
            args=(url,),
            daemon=True,
            name="inspect-download-url",
        ).start()

    def _inspect_download_worker(self, url):
        try:
            info = inspect_download_target(url)
            self.post_event(("url_info_done", (url, info)))
        except Exception as exc:
            logging.exception("Could not load URL formats")
            self.post_event(("url_info_error", str(exc)))

    def show_format_options(self, info):
        for child in self.options_frame.winfo_children():
            child.destroy()

        title = tk.Label(
            self.options_frame,
            text=f"Available formats · {info['title']}",
            bg=self.BG,
            fg=self.TEXT,
            font=("Segoe UI Semibold", 13),
            anchor="w",
        )
        title.pack(fill="x", pady=(0, 8))

        playlist_count = info.get("playlist_count")
        self.active_playlist_limit = None
        if playlist_count is not None:
            row = tk.Frame(self.options_frame, bg=self.PANEL, highlightbackground=self.BORDER,
                           highlightthickness=1)
            row.pack(fill="x", pady=(0, 10))
            tk.Label(
                row,
                text=f"Playlist items: {playlist_count}",
                bg=self.PANEL,
                fg=self.TEXT,
                font=("Segoe UI", 10),
            ).pack(side="left", padx=12, pady=10)
            tk.Label(
                row,
                text="Items to download",
                bg=self.PANEL,
                fg=self.MUTED,
                font=("Segoe UI", 9),
            ).pack(side="left", padx=(12, 6))
            self.playlist_limit_var.set(str(playlist_count))
            self.playlist_limit_spinbox = tk.Spinbox(
                row,
                from_=1,
                to=playlist_count,
                textvariable=self.playlist_limit_var,
                width=8,
                bg=self.PANEL_2,
                fg=self.TEXT,
                insertbackground=self.TEXT,
                buttonbackground=self.PANEL_2,
                relief="flat",
                font=("Segoe UI", 10),
            )
            self.playlist_limit_spinbox.pack(side="left", padx=(0, 12), pady=7)
            self.active_playlist_limit = playlist_count

        choices = [
            ("MP4", [{"label": f"{height}p", "output_format": "mp4", "height": height}
                     for height in (2160, 1440, 1080, 720, 480, 240, 120)]),
            ("MKV", [{"label": f"{height}p", "output_format": "mkv", "height": height}
                     for height in (2160, 1440, 1080, 720, 480, 240, 120)]),
            ("Audio", [
                {"label": "256k MP3", "output_format": "mp3", "audio_bitrate": 256},
                {"label": "128k MP3", "output_format": "mp3", "audio_bitrate": 128},
                {"label": "M4A", "output_format": "m4a"},
            ]),
        ]
        columns = tk.Frame(self.options_frame, bg=self.BG)
        self.format_columns = columns
        columns.pack(fill="both", expand=True)
        self.format_buttons = []
        for column_index, (heading, formats) in enumerate(choices):
            section = tk.Frame(columns, bg=self.PANEL, highlightbackground=self.BORDER,
                               highlightthickness=1)
            section.grid(row=0, column=column_index, sticky="nsew",
                         padx=(0 if column_index == 0 else 8, 0), pady=0)
            columns.grid_columnconfigure(column_index, weight=1)
            tk.Label(
                section,
                text=heading,
                bg=self.PANEL,
                fg=self.MUTED,
                font=("Segoe UI Semibold", 9),
            ).pack(anchor="w", padx=10, pady=(9, 5))
            for choice in formats:
                button = self._button(
                    section,
                    choice["label"],
                    lambda selected=choice: self.start_preset_download(selected),
                    compact=True,
                )
                button.pack(fill="x", padx=8, pady=3)
                self.format_buttons.append(button)

    def invalidate_format_options(self, *_):
        if not hasattr(self, "options_frame"):
            return
        if getattr(self, "active_download_url", None) == self.url_var.get().strip():
            return
        self.active_download_url = None
        self.active_playlist_limit = None
        for child in self.options_frame.winfo_children():
            child.destroy()
        if hasattr(self, "download_url_button"):
            self.download_url_button.configure(text="Download")

    def start_preset_download(self, choice):
        url = getattr(self, "active_download_url", None)
        if not url or url != self.url_var.get().strip():
            messagebox.showerror(
                "URL unavailable",
                "Load the formats for the current URL before starting a download.",
                parent=self.root,
            )
            return

        playlist_limit = None
        if self.active_playlist_limit is not None:
            try:
                playlist_limit = int(self.playlist_limit_var.get())
            except ValueError:
                messagebox.showerror(
                    "Invalid item count",
                    "Enter a whole number of playlist items.",
                    parent=self.root,
                )
                return
            if not 1 <= playlist_limit <= self.active_playlist_limit:
                messagebox.showerror(
                    "Invalid item count",
                    f"Choose between 1 and {self.active_playlist_limit} playlist items.",
                    parent=self.root,
                )
                return

        self.status_var.set(f"Starting {choice['label']} download…")
        for button in self.format_buttons:
            button.configure(state="disabled")
        self.show_page("downloads")
        payload = {
            "url": url,
            **{key: value for key, value in choice.items() if key != "label"},
        }
        if playlist_limit is not None:
            payload["playlist_limit"] = playlist_limit
        try:
            worker = threading.Thread(
                target=self._manual_download_worker,
                args=(payload,),
                daemon=True,
                name="manual-download",
            )
            worker.start()
        except RuntimeError as exc:
            logging.exception("Could not start direct download worker")
            self._finish_format_download()
            messagebox.showerror("Could not start download", str(exc), parent=self.root)

    def _finish_format_download(self):
        for button in getattr(self, "format_buttons", []):
            try:
                button.configure(state="normal")
            except tk.TclError:
                pass

    def _manual_download_worker(self, payload):
        try:
            with DOWNLOAD_SLOTS:
                result = run_download(
                    payload,
                    progress_callback=lambda item: self.post_event(("download_progress", item)),
                )
            logging.info("Direct download complete: %s", result.get("file"))
            self.post_event(("manual_download_done", result))
        except Exception as exc:
            logging.exception("Direct download failed")
            error = str(exc)
            host = (urlsplit(str(payload.get("url") or "")).hostname or "").lower()
            if "403" in error and (host == "youtu.be" or host.endswith("youtube.com")):
                error += (
                    "\n\nyt-dlp also reported that no JavaScript runtime is available. "
                    "Install Deno (or another supported runtime) and restart the app. "
                    "Some YouTube formats may additionally require a PO Token: "
                    "https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide"
                )
            self.post_event(("manual_download_error", error))

    def _register_completed_files(self, paths):
        completed_paths = {}
        for path in paths:
            if not isinstance(path, str) or not path:
                continue
            completed_paths[str(Path(path).resolve()).casefold()] = str(Path(path).resolve())

        for item in self.download_items.values():
            path = item.get("path")
            if not path or item.get("progress", 0) < 100:
                continue
            candidate = Path(path)
            if candidate.is_file():
                resolved_path = str(candidate.resolve())
                completed_paths[resolved_path.casefold()] = resolved_path

        for path in completed_paths.values():
            self._update_download_item({
                "key": "completed:" + path,
                "name": Path(path).name,
                "path": path,
                "status": "completed",
                "progress": 100,
            })

    def choose_folder(self):
        selected = filedialog.askdirectory(initialdir=str(DOWNLOAD_DIR), title="Choose download folder")
        if selected:
            try:
                set_download_dir(selected)
                save_download_cache(self.download_cache)
                self.path_var.set(str(DOWNLOAD_DIR))
                self.refresh_files()
                self.status_var.set("Download folder updated")
            except OSError as exc:
                messagebox.showerror("Folder error", str(exc), parent=self.root)

    def open_folder(self):
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(DOWNLOAD_DIR))
        except AttributeError:
            webbrowser.open(DOWNLOAD_DIR.as_uri())
        except OSError as exc:
            messagebox.showerror("Cannot open folder", str(exc), parent=self.root)

    def periodic_refresh(self):
        if self.closing:
            return
        self.refresh_files()
        self.root.after(5000, self.periodic_refresh)

    def refresh_files(self):
        if not hasattr(self, "tree"):
            return
        self._cache_finished_downloads()
        self.path_var.set(str(DOWNLOAD_DIR))
        filter_text = self.query.get().strip().lower()
        for item in self.tree.get_children():
            self.tree.delete(item)
        rows = []
        active_paths = {
            str(Path(item["path"]).resolve()).casefold()
            for item in self.download_items.values()
            if item.get("path")
        }
        cached_paths = set(self.download_cache)
        for key, entry in self.download_cache.items():
            path = Path(entry["path"])
            if (path.suffix.lower() == ".webm"
                    or str(path.resolve()).casefold() in active_paths
                    or filter_text not in entry["name"].lower()):
                continue
            try:
                stat = path.stat() if path.is_file() else None
            except OSError:
                stat = None
            row_id = "file:" + entry["path"]
            size = stat.st_size if stat else entry.get("size")
            modified = stat.st_mtime if stat else entry.get("modified")
            rows.append((2, -(stat.st_mtime if stat else 0), row_id, (
                "☑" if row_id in self.checked_entries else "☐" if self.remove_mode else "",
                entry["name"], entry["location"].lower(),
                entry.get("status", "downloaded") if stat else "missing", "100%",
                self._format_size(size) if size is not None else "—",
                datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M") if modified else "—",
            )))

        for key, item in self.download_items.items():
            path = Path(item["path"]) if item.get("path") else None
            if path and (
                    path.suffix.lower() == ".webm"
                    or path.stem.lower().endswith(".webm")
                    or str(path.resolve()).casefold() in HIDDEN_DOWNLOAD_PATHS
                    or str(path.resolve()).casefold() in cached_paths
            ):
                continue
            name = path.name if path else item["name"]
            if filter_text and filter_text not in name.lower():
                continue
            try:
                stat = path.stat() if path and path.is_file() else None
            except OSError:
                stat = None
            location = download_location(path) if path else "Videos"
            row_id = "item:" + key
            rows.append((
                {"downloading": 0, "error": 1, "completed": 2}.get(item["status"], 2),
                -(stat.st_mtime if stat else 0),
                row_id,
                (
                    "☑" if row_id in self.checked_entries else "☐" if self.remove_mode else "",
                    name,
                    location.lower(),
                    item["status"],
                    f'{item["progress"]}%',
                    self._format_size(stat.st_size) if stat else "—",
                    datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M") if stat else "—",
                ),
            ))

        rows.sort(key=lambda item: (item[0], item[1]))
        for _, _, row_id, values in rows:
            self.tree.insert("", "end", iid=row_id, values=values)
        self.tree.column("select", width=34 if self.remove_mode else 0,
                         minwidth=34 if self.remove_mode else 0)
        self._sync_select_all_heading()
        self.count_var.set(f"{len(rows)} item{'s' if len(rows) != 1 else ''}")
        self.path_var.set(str(DOWNLOAD_DIR))

    def _cache_download_file(self, path):
        resolved_path = str(Path(path).resolve())
        file_path = Path(resolved_path)
        if not file_path.is_file() or file_path.suffix.lower() == ".webm":
            return False
        try:
            stat = file_path.stat()
            cache_key = resolved_path.casefold()
            previous_entry = self.download_cache.get(cache_key)
            self.download_cache[cache_key] = {
                "path": resolved_path,
                "name": file_path.name,
                "location": download_location(resolved_path),
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "status": "downloaded",
            }
            try:
                save_download_cache(self.download_cache)
            except OSError:
                if previous_entry is None:
                    self.download_cache.pop(cache_key, None)
                else:
                    self.download_cache[cache_key] = previous_entry
                raise
        except OSError as exc:
            logging.exception("Could not save downloaded file to cache")
            self.status_var.set("Could not save download cache")
            messagebox.showerror("Download cache error", str(exc), parent=self.root)
            return False
        return True

    def _cache_finished_downloads(self):
        for key, item in tuple(self.download_items.items()):
            path = item.get("path")
            if not path or item.get("progress", 0) < 100:
                continue
            if self._cache_download_file(path):
                self.download_items.pop(key, None)

    def _update_download_item(self, update):
        key = str(update["key"])
        if key in self.hidden_download_keys:
            path = update.get("path") or self.hidden_download_keys[key]
            if path:
                self.hidden_download_keys[key] = path
            if update["status"] == "completed":
                if path:
                    HIDDEN_DOWNLOAD_PATHS.add(str(Path(path).resolve()).casefold())
                    save_config()
                self.hidden_download_keys.pop(key, None)
            elif update["status"] == "error":
                self.hidden_download_keys.pop(key, None)
            return
        existing = self.download_items.get(key, {})
        status = update["status"]
        previous_progress = existing.get("progress", 0)
        if status == "error":
            if existing.get("status") == "completed":
                status = "completed"
                progress = 100
            else:
                progress = previous_progress
        elif status == "completed":
            progress = 100
        else:
            previous_status = existing.get("status")
            if previous_status in {"completed", "error"}:
                status = previous_status
                progress = 100 if status == "completed" else previous_progress
            else:
                progress = max(previous_progress, update["progress"])
        path = update.get("path") or existing.get("path")
        name = update.get("name") or existing.get("name") or key
        if status == "completed" and path and (
                Path(path).suffix.lower() == ".webm"
                or Path(path).stem.lower().endswith(".webm")
        ):
            self.download_items.pop(key, None)
            self.refresh_files()
            return
        if status == "completed" and path:
            resolved_path = str(Path(path).resolve())
            cache_key = resolved_path.casefold()
            cached = self._cache_download_file(resolved_path)
            for active_key, active_item in tuple(self.download_items.items()):
                active_path = active_item.get("path")
                if active_key == key or (
                        active_path
                        and str(Path(active_path).resolve()).casefold() == cache_key
                ):
                    self.download_items.pop(active_key, None)
            if cached:
                self.refresh_files()
                return
        self.download_items[key] = {
            "name": name,
            "path": path,
            "status": status,
            "progress": progress,
        }
        self.refresh_files()

    @staticmethod
    def _format_size(size):
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024

    def selected_path(self):
        selection = self.tree.selection()
        if not selection:
            return None
        item_id = selection[0]
        if item_id.startswith("item:"):
            item = self.download_items.get(item_id[5:])
            return Path(item["path"]) if item and item.get("path") else None
        if item_id.startswith("file:"):
            return Path(item_id[5:])
        return Path(item_id)

    def open_selected(self):
        path = self.selected_path()
        if not path or not path.exists():
            return
        try:
            os.startfile(str(path))
        except AttributeError:
            webbrowser.open(path.as_uri())
        except OSError as exc:
            messagebox.showerror("Cannot open file", str(exc), parent=self.root)

    def delete_selected(self):
        if not self.delete_mode:
            self.remove_mode = True
            self.delete_mode = True
            self.checked_entries.clear()
            self.remove_button.configure(state="disabled")
            self.delete_button.configure(state="normal")
            self.delete_button.configure(text="Delete selected")
            self.refresh_files()
            return

        selected_items = tuple(self.checked_entries)
        file_items = tuple(item_id for item_id in selected_items if item_id.startswith("file:"))
        if not file_items:
            self.status_var.set("Select downloaded files to delete")
            return
        if not messagebox.askyesno(
                "Delete files",
                f"Permanently delete {len(file_items)} selected file(s)?",
                parent=self.root,
        ):
            return

        deleted = 0
        errors = []
        for item_id in file_items:
            path = Path(item_id[5:])
            try:
                if path.exists():
                    path.unlink()
                if self._remove_cached_entry(path):
                    deleted += 1
                else:
                    errors.append(f"{path.name}: file deleted, but its cache entry could not be removed")
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")

        self.checked_entries.clear()
        self.remove_mode = False
        self.delete_mode = False
        self.remove_button.configure(state="normal", text="Remove")
        self.delete_button.configure(state="normal", text="Delete file")
        self.refresh_files()
        self.status_var.set(f"Deleted {deleted} file(s)")
        if errors:
            messagebox.showerror("Some files could not be deleted", "\n".join(errors), parent=self.root)

    def remove_selected_entry(self):
        if not self.remove_mode:
            self.remove_mode = True
            self.checked_entries.clear()
            self.delete_button.configure(state="disabled")
            self.remove_button.configure(
                text="Done", bg="#c62828", fg="#ffffff",
                activebackground="#b71c1c", activeforeground="#ffffff",
            )
            self.refresh_files()
            return

        selected_items = tuple(self.checked_entries)
        removed_count = sum(self._remove_entry(item_id) for item_id in selected_items)
        self.checked_entries.clear()
        self.remove_mode = False
        self.delete_mode = False
        self.remove_button.configure(
            state="normal",
            text="Remove", bg=self.PANEL_2, fg=self.TEXT,
            activebackground="#3a3d3f", activeforeground=self.TEXT,
        )
        self.delete_button.configure(state="normal", text="Delete file")
        self.refresh_files()
        if removed_count == len(selected_items) and selected_items:
            self.status_var.set("Selected entries removed; files kept in destination")
        elif removed_count:
            self.status_var.set(f"Removed {removed_count} of {len(selected_items)} selected entries")
        elif selected_items:
            self.status_var.set("Could not remove selected entries")
        else:
            self.status_var.set("No entries selected")

    def _toggle_entry_checkbox(self, event):
        if not self.remove_mode:
            return
        region = self.tree.identify_region(event.x, event.y)
        column = self.tree.identify_column(event.x)
        if region == "heading" and column == "#1":
            visible_items = set(self.tree.get_children())
            if visible_items.issubset(self.checked_entries):
                self.checked_entries.difference_update(visible_items)
            else:
                self.checked_entries.update(visible_items)
            for item_id in visible_items:
                self.tree.set(
                    item_id, "select",
                    "☑" if item_id in self.checked_entries else "☐",
                )
            self._sync_select_all_heading()
            return "break"
        if region != "cell" or column != "#1":
            return
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return "break"
        if item_id in self.checked_entries:
            self.checked_entries.remove(item_id)
        else:
            self.checked_entries.add(item_id)
        self.tree.set(item_id, "select", "☑" if item_id in self.checked_entries else "☐")
        self._sync_select_all_heading()
        return "break"

    def _sync_select_all_heading(self):
        if not hasattr(self, "tree"):
            return
        visible_items = set(self.tree.get_children())
        checked = bool(visible_items) and visible_items.issubset(self.checked_entries)
        self.tree.heading(
            "select",
            text=("☑" if checked else "☐") if self.remove_mode else "",
        )

    def _remove_entry(self, item_id):
        if item_id.startswith("file:"):
            return self._remove_cached_entry(item_id[5:])
        if item_id.startswith("item:"):
            key = item_id[5:]
            item = self.download_items.pop(key, None)
            if not item:
                return False
            path = item.get("path")
            if path:
                HIDDEN_DOWNLOAD_PATHS.add(str(Path(path).resolve()).casefold())
                save_config()
            if item.get("status") == "downloading":
                self.hidden_download_keys[key] = path
            return True
        else:
            HIDDEN_DOWNLOAD_PATHS.add(str(Path(item_id).resolve()).casefold())
            save_config()
            return True

    def _remove_cached_entry(self, path):
        cache_key = str(Path(path).resolve()).casefold()
        entry = self.download_cache.pop(cache_key, None)
        if entry is None:
            return False
        try:
            save_download_cache(self.download_cache)
        except OSError as exc:
            self.download_cache[cache_key] = entry
            logging.exception("Could not update download cache")
            self.status_var.set("Could not update download cache")
            messagebox.showerror("Download cache error", str(exc), parent=self.root)
            return False
        return True


    def exit_app(self):
        if self.closing:
            return
        self.closing = True
        stop_server()
        self.root.destroy()


def main():
    load_config()
    configure_temp_directory(DOWNLOAD_DIR)
    start_server()
    root = tk.Tk()
    app = DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except OSError as exc:
        logging.exception("Application startup failed")
        try:
            messagebox.showerror(APP_NAME, f"Could not start the local server at {HOST}:{PORT}.\n{exc}")
        except Exception:
            print(f"Could not start the local server at {HOST}:{PORT}: {exc}")
