from __future__ import annotations

import asyncio
from contextlib import contextmanager
import ipaddress
import logging
import mimetypes
import os
import re
import socket
import sqlite3
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import aiohttp
from dotenv import load_dotenv
try:
    from playwright.async_api import async_playwright, Browser, Playwright
except ModuleNotFoundError:
    # Keep the lightweight Telegram-only test mode usable without Chromium.
    async_playwright = None
    Browser = Any
    Playwright = Any
from telegram import BotCommand, InputFile, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from diskwala_utils import parse_diskwala_link, safe_filename
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ------------------------------ Configuration ------------------------------


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = env_int("OWNER_ID", 0)
INITIAL_GROUP_ID = os.getenv("DESTINATION_CHAT_ID", "").strip()
LOCAL_BOT_API_URL = os.getenv("LOCAL_BOT_API_URL", "").strip().rstrip("/")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "diskwala.sqlite3"
CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_FILE_BYTES = env_int("MAX_FILE_SIZE_BYTES", 2_000_000_000)
UPLOAD_LIMIT_BYTES = MAX_FILE_BYTES if LOCAL_BOT_API_URL else min(MAX_FILE_BYTES, 50 * 1024 * 1024)
# Keep concurrent update processing finite to avoid exhausting RAM/Chromium.
# There is no daily-download quota; actual simultaneous work depends on the host.
UPDATE_WORKERS = env_int("UPDATE_WORKERS", 32)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Copy .env.example to .env and set it.")
if OWNER_ID <= 0:
    raise RuntimeError("OWNER_ID is missing or invalid. Set your numeric Telegram user ID.")

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("diskwala_bot")
logger.setLevel(logging.INFO)
_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
_console = logging.StreamHandler()
_console.setFormatter(_formatter)
_file = RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
_file.setFormatter(_formatter)
logger.addHandler(_console)
logger.addHandler(_file)

@dataclass(frozen=True)
class ResolvedMedia:
    name: str
    mime_type: str
    size_bytes: int | None
    url: str
    share_url: str


class DiskWalaError(Exception):
    """Expected error while resolving or downloading a DiskWala share."""


class Storage:
    """Small SQLite store for users, settings, delivery stats and 24h cache."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def initialize(self) -> None:
        async with self._lock:
            with self._connection() as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        first_seen INTEGER NOT NULL,
                        last_seen INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS video_cache (
                        file_key TEXT PRIMARY KEY,
                        share_url TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        telegram_file_id TEXT,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        group_attempted INTEGER NOT NULL DEFAULT 0,
                        group_posted INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS deliveries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        file_key TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_cache_expiry ON video_cache(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_deliveries_created ON deliveries(created_at);
                    """
                )
                if INITIAL_GROUP_ID:
                    db.execute(
                        "INSERT OR IGNORE INTO settings(key, value) VALUES('group_id', ?)",
                        (INITIAL_GROUP_ID,),
                    )
                db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES('maintenance', 'off')"
                )

    async def register_user(self, user_id: int) -> None:
        now = int(time.time())
        async with self._lock:
            with self._connection() as db:
                db.execute(
                    "INSERT INTO users(user_id, first_seen, last_seen) VALUES(?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
                    (user_id, now, now),
                )

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._lock:
            with self._connection() as db:
                row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self._lock:
            with self._connection() as db:
                db.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    async def get_cache(self, file_key: str) -> dict[str, Any] | None:
        now = int(time.time())
        async with self._lock:
            with self._connection() as db:
                row = db.execute(
                    "SELECT * FROM video_cache WHERE file_key=? AND expires_at>?",
                    (file_key, now),
                ).fetchone()
                return dict(row) if row else None

    async def put_cache(
        self,
        file_key: str,
        share_url: str,
        file_path: Path,
        filename: str,
        size_bytes: int,
    ) -> None:
        now = int(time.time())
        async with self._lock:
            with self._connection() as db:
                db.execute(
                    "INSERT INTO video_cache(file_key, share_url, file_path, filename, size_bytes, "
                    "created_at, expires_at, group_attempted, group_posted) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, 0, 0) "
                    "ON CONFLICT(file_key) DO UPDATE SET share_url=excluded.share_url, "
                    "file_path=excluded.file_path, filename=excluded.filename, "
                    "size_bytes=excluded.size_bytes, telegram_file_id=NULL, "
                    "created_at=excluded.created_at, expires_at=excluded.expires_at, "
                    "group_attempted=0, group_posted=0",
                    (file_key, share_url, str(file_path), filename, size_bytes, now, now + CACHE_TTL_SECONDS),
                )

    async def set_telegram_file_id(self, file_key: str, file_id: str) -> None:
        async with self._lock:
            with self._connection() as db:
                db.execute(
                    "UPDATE video_cache SET telegram_file_id=? WHERE file_key=?",
                    (file_id, file_key),
                )

    async def mark_group_attempted(self, file_key: str) -> bool:
        """Atomically claim the one allowed group-post attempt."""
        async with self._lock:
            with self._connection() as db:
                cursor = db.execute(
                    "UPDATE video_cache SET group_attempted=1 "
                    "WHERE file_key=? AND group_attempted=0",
                    (file_key,),
                )
                return cursor.rowcount == 1

    async def mark_group_posted(self, file_key: str) -> None:
        async with self._lock:
            with self._connection() as db:
                db.execute("UPDATE video_cache SET group_posted=1 WHERE file_key=?", (file_key,))

    async def add_delivery(self, user_id: int, file_key: str) -> None:
        async with self._lock:
            with self._connection() as db:
                db.execute(
                    "INSERT INTO deliveries(user_id, file_key, created_at) VALUES(?, ?, ?)",
                    (user_id, file_key, int(time.time())),
                )

    async def all_user_ids(self) -> list[int]:
        async with self._lock:
            with self._connection() as db:
                rows = db.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
                return [int(row["user_id"]) for row in rows]

    async def stats(self) -> tuple[int, int, int]:
        async with self._lock:
            with self._connection() as db:
                users = int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                deliveries = int(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])
                unique_videos = int(db.execute("SELECT COUNT(*) FROM video_cache").fetchone()[0])
                return users, deliveries, unique_videos

    async def prune_expired(self) -> list[str]:
        now = int(time.time())
        async with self._lock:
            with self._connection() as db:
                rows = db.execute(
                    "SELECT file_path FROM video_cache WHERE expires_at<=?", (now,)
                ).fetchall()
                db.execute("DELETE FROM video_cache WHERE expires_at<=?", (now,))
                return [str(row["file_path"]) for row in rows]

    async def remove_cache(self, file_key: str) -> str | None:
        async with self._lock:
            with self._connection() as db:
                row = db.execute(
                    "SELECT file_path FROM video_cache WHERE file_key=?", (file_key,)
                ).fetchone()
                db.execute("DELETE FROM video_cache WHERE file_key=?", (file_key,))
                return str(row["file_path"]) if row else None


class DiskWalaResolver:
    """Resolve a public share link by running DiskWala's own page JavaScript.

    The browser watches the page's temp_info response. This does not forge or
    replay integrity headers. The page must itself be able to access the file.
    The flow is undocumented and may change if DiskWala updates its website.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._start_lock = asyncio.Lock()

    async def _ensure_browser(self) -> Browser:
        if async_playwright is None:
            raise DiskWalaError(
                "Browser downloader is not installed in this test setup. "
                "Install requirements-browser.txt and Playwright Chromium to enable video downloads."
            )
        async with self._start_lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            return self._browser

    async def resolve(self, share_url: str) -> ResolvedMedia:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        loop = asyncio.get_running_loop()
        captured: asyncio.Future[dict[str, Any]] = loop.create_future()

        async def on_response(response: Any) -> None:
            if "/api/v1/file/temp_info" not in response.url or captured.done():
                return
            try:
                body = await response.json()
                if captured.done():
                    return
                if isinstance(body, dict):
                    captured.set_result(body)
                else:
                    captured.set_exception(DiskWalaError("DiskWala returned an invalid response."))
            except Exception as exc:  # response may be non-JSON on platform errors
                if not captured.done():
                    captured.set_exception(DiskWalaError("DiskWala could not resolve this link."))

        page.on("response", on_response)
        try:
            try:
                await page.goto(share_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                # The API response may still have been captured despite a page navigation error.
                if not captured.done():
                    raise DiskWalaError("DiskWala share page could not be opened.") from exc

            try:
                payload = await asyncio.wait_for(asyncio.shield(captured), timeout=50)
            except asyncio.TimeoutError as exc:
                raise DiskWalaError(
                    "DiskWala did not provide file details. The link may be unavailable, "
                    "or DiskWala may have changed its website."
                ) from exc

            info = payload.get("fileInfo")
            if not isinstance(info, dict):
                message = str(payload.get("message") or payload.get("error") or "")
                if message:
                    logger.warning("DiskWala resolver returned an error response: %s", message[:200])
                raise DiskWalaError("Video not found, expired, private, or not accessible.")

            media_url = str(info.get("url") or "").strip()
            name = str(info.get("name") or "video.mp4").strip()
            mime_type = str(info.get("type") or "").strip().lower()
            raw_size = info.get("size")
            try:
                size_bytes = int(raw_size) if raw_size not in (None, "") else None
            except (TypeError, ValueError):
                size_bytes = None
            if not media_url:
                raise DiskWalaError("DiskWala did not return a downloadable file URL.")

            suffix = Path(name).suffix.lower()
            if not (mime_type.startswith("video/") or suffix in VIDEO_EXTENSIONS):
                raise DiskWalaError("This DiskWala link does not contain a supported video.")
            if not mime_type.startswith("video/"):
                mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return ResolvedMedia(name, mime_type, size_bytes, media_url, share_url)
        finally:
            await context.close()

    async def close(self) -> None:
        if self._browser and self._browser.is_connected():
            await self._browser.close()
        self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None


# ------------------------------ Link handling -------------------------------


async def validate_public_https_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise DiskWalaError("DiskWala returned an unsafe download URL.")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise DiskWalaError("The video download server could not be reached.") from exc
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise DiskWalaError("DiskWala returned an unsafe download address.")


ProgressCallback = Callable[[int, int], Awaitable[None]]


async def download_video(
    media: ResolvedMedia,
    destination: Path,
    progress_callback: ProgressCallback,
) -> int:
    """Stream the original bytes to disk. Redirects are validated one by one."""
    current_url = media.url
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=180)
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Referer": media.share_url,
        "Accept": "*/*",
    }
    total_done = 0
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for redirect_count in range(6):
            await validate_public_https_url(current_url)
            async with session.get(current_url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise DiskWalaError("The video server returned an invalid redirect.")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status == 404 or response.status == 410:
                    raise DiskWalaError("The video link has expired or is unavailable.")
                if response.status >= 400:
                    raise DiskWalaError(f"The video server returned HTTP {response.status}.")

                header_size = response.headers.get("Content-Length")
                total_size = int(header_size) if header_size and header_size.isdigit() else (media.size_bytes or 0)
                if total_size and total_size > UPLOAD_LIMIT_BYTES:
                    raise DiskWalaError(
                        f"Video is larger than the active Telegram upload limit ({UPLOAD_LIMIT_BYTES // (1024 * 1024)} MB)."
                    )

                with destination.open("wb") as output:
                    last_percent = -1
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if not chunk:
                            continue
                        total_done += len(chunk)
                        if total_done > UPLOAD_LIMIT_BYTES:
                            raise DiskWalaError(
                                f"Video exceeded the active Telegram upload limit ({UPLOAD_LIMIT_BYTES // (1024 * 1024)} MB)."
                            )
                        output.write(chunk)
                        if total_size > 0:
                            percent = min(100, int(total_done * 100 / total_size))
                            if percent >= last_percent + 5 or percent == 100:
                                await progress_callback(percent, total_size)
                                last_percent = percent
                if total_done == 0:
                    raise DiskWalaError("DiskWala returned an empty video file.")
                await progress_callback(100, total_size or total_done)
                return total_done
        raise DiskWalaError("Too many redirects from the video server.")


# ------------------------------ User messages -------------------------------

WELCOME = (
    "👋 Welcome!\n"
    "Send your DiskWala link to download the video.\n\n"
    "वीडियो डाउनलोड करने के लिए अपना DiskWala लिंक भेजें।"
)
HELP_TEXT = (
    "Send one valid DiskWala video link here.\n"
    "यहाँ एक सही DiskWala वीडियो लिंक भेजें।"
)
MAINTENANCE_TEXT = (
    "⚠️ Bot is currently under maintenance. Please try again later.\n"
    "⚠️ बॉट अभी मेंटेनेंस में है। कृपया बाद में कोशिश करें।"
)
INVALID_LINK_TEXT = (
    "❌ Invalid link. Please send a valid DiskWala link.\n"
    "❌ गलत लिंक। कृपया सही DiskWala लिंक भेजें।"
)
def friendly_error(exc: Exception) -> str:
    if isinstance(exc, DiskWalaError):
        reason = str(exc)
    elif isinstance(exc, (Forbidden, BadRequest)):
        reason = "Telegram could not deliver this video to your chat. Check that the bot is not blocked."
    elif isinstance(exc, TelegramError):
        reason = "Telegram upload/delivery failed."
    else:
        reason = "The server could not download or deliver this video."
    return f"❌ Reason: {reason}\n❌ कारण: {reason}"


# ------------------------------ Bot handlers --------------------------------

storage = Storage(DB_PATH)
resolver = DiskWalaResolver()
_file_locks: dict[str, asyncio.Lock] = {}


def file_lock_for(file_key: str) -> asyncio.Lock:
    lock = _file_locks.get(file_key)
    if lock is None:
        lock = asyncio.Lock()
        _file_locks[file_key] = lock
    return lock


def is_owner(update: Update) -> bool:
    return bool(
        update.effective_user
        and update.effective_user.id == OWNER_ID
        and update.effective_chat
        and update.effective_chat.type == "private"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == "private" and update.effective_user:
        await storage.register_user(update.effective_user.id)
        await update.effective_message.reply_text(WELCOME)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == "private" and update.effective_user:
        await storage.register_user(update.effective_user.id)
        await update.effective_message.reply_text(HELP_TEXT)


async def send_video_from_cache(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    cache_entry: dict[str, Any],
) -> Any:
    file_id = cache_entry.get("telegram_file_id")
    if file_id:
        sent = await context.bot.send_video(
            chat_id=chat_id,
            video=str(file_id),
            supports_streaming=True,
            caption=None,
            read_timeout=3600,
            write_timeout=3600,
            connect_timeout=60,
            pool_timeout=60,
        )
    else:
        path = Path(str(cache_entry["file_path"]))
        if not path.is_file():
            raise DiskWalaError("The cached video is no longer available. Please send the link again.")
        with path.open("rb") as file_handle:
            sent = await context.bot.send_video(
                chat_id=chat_id,
                video=InputFile(file_handle, filename=str(cache_entry["filename"])),
                supports_streaming=True,
                caption=None,
                read_timeout=3600,
                write_timeout=3600,
                connect_timeout=60,
                pool_timeout=60,
            )

    if not getattr(sent, "video", None):
        # Telegram can classify unsupported containers as Documents. Remove that
        # message so the bot never silently violates the requested Video format.
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
        except TelegramError:
            logger.exception("Could not remove non-video Telegram upload (chat=%s)", chat_id)
        raise DiskWalaError(
            "Telegram did not accept this file as a playable chat video. MP4/MPEG-4 is recommended; "
            "the original file was not converted."
        )
    return sent


def get_sent_file_id(message: Any) -> str | None:
    video = getattr(message, "video", None)
    if video and getattr(video, "file_id", None):
        return str(video.file_id)
    document = getattr(message, "document", None)
    if document and getattr(document, "file_id", None):
        return str(document.file_id)
    return None


async def maybe_post_to_group(
    context: ContextTypes.DEFAULT_TYPE,
    file_key: str,
    cache_entry: dict[str, Any],
) -> None:
    group_id = await storage.get_setting("group_id", "")
    if not group_id:
        logger.error("Group post skipped: destination group is not configured (cache=%s)", file_key[:10])
        return
    # Claim before sending: at-most-one attempt even if the process crashes mid-post.
    claimed = await storage.mark_group_attempted(file_key)
    if not claimed:
        return
    try:
        await send_video_from_cache(context, int(group_id), cache_entry)
        await storage.mark_group_posted(file_key)
    except Exception:
        # Do not retry group posting automatically; user delivery has already succeeded.
        logger.exception("Group posting failed (cache=%s, group=%s)", file_key[:10], group_id)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or chat.type != "private" or not message.text:
        return

    await storage.register_user(user.id)
    if await storage.get_setting("maintenance", "off") == "on":
        await message.reply_text(MAINTENANCE_TEXT)
        return

    parsed = parse_diskwala_link(message.text)
    if not parsed:
        await message.reply_text(INVALID_LINK_TEXT)
        return
    file_key, share_url = parsed

    async with file_lock_for(file_key):
        cache_entry = await storage.get_cache(file_key)
        if not cache_entry:
            stale_path = await storage.remove_cache(file_key)
            if stale_path:
                Path(stale_path).unlink(missing_ok=True)
        if cache_entry:
            cached_path = Path(str(cache_entry["file_path"]))
            if not cache_entry.get("telegram_file_id") and not cached_path.is_file():
                old_path = await storage.remove_cache(file_key)
                if old_path:
                    Path(old_path).unlink(missing_ok=True)
                cache_entry = None

        progress_message = await message.reply_text("Preparing download / डाउनलोड तैयार हो रहा है…")
        try:
            if not cache_entry:
                media = await resolver.resolve(share_url)
                if media.size_bytes and media.size_bytes > UPLOAD_LIMIT_BYTES:
                    raise DiskWalaError(
                        f"This video is larger than the active Telegram upload limit ({UPLOAD_LIMIT_BYTES // (1024 * 1024)} MB). "
                        "Configure a Local Telegram Bot API server for the requested 2 GB support."
                    )

                filename = safe_filename(media.name)
                final_path = CACHE_DIR / f"{file_key}_{filename}"
                partial_path = final_path.with_suffix(final_path.suffix + ".part")

                async def progress(percent: int, total: int) -> None:
                    try:
                        await progress_message.edit_text(f"Downloading / डाउनलोड: {percent}%")
                    except TelegramError:
                        pass

                try:
                    size = await download_video(media, partial_path, progress)
                    partial_path.replace(final_path)
                except Exception:
                    partial_path.unlink(missing_ok=True)
                    raise
                await storage.put_cache(file_key, share_url, final_path, filename, size)
                cache_entry = await storage.get_cache(file_key)
                if not cache_entry:
                    raise RuntimeError("Could not register the downloaded cache file")

            sent_message = await send_video_from_cache(context, user.id, cache_entry)
            telegram_file_id = get_sent_file_id(sent_message)
            if telegram_file_id:
                await storage.set_telegram_file_id(file_key, telegram_file_id)
                cache_entry["telegram_file_id"] = telegram_file_id
            await storage.add_delivery(user.id, file_key)
            await maybe_post_to_group(context, file_key, cache_entry)
        except Exception as exc:
            logger.exception("Request failed (cache=%s, user=%s)", file_key[:10], user.id)
            try:
                await message.reply_text(friendly_error(exc))
            except TelegramError:
                logger.exception("Could not send failure reason to user=%s", user.id)
        finally:
            try:
                await progress_message.delete()
            except TelegramError:
                pass


async def owner_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update) or not update.effective_message:
        return
    users, deliveries, unique_videos = await storage.stats()
    await update.effective_message.reply_text(
        f"📊 DiskWala Bot Stats\n\n👥 Total users: {users}\n"
        f"📥 Successful deliveries: {deliveries}\n🎞 Unique cached videos: {unique_videos}\n\n"
        f"📊 बॉट आँकड़े\n👥 कुल यूज़र: {users}\n"
        f"📥 सफल डिलीवरी: {deliveries}\n🎞 अलग-अलग कैश वीडियो: {unique_videos}"
    )


async def owner_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setgroup -1001234567890\nउपयोग: /setgroup -1001234567890"
        )
        return
    candidate = context.args[0].strip()
    try:
        group_id = int(candidate)
        chat = await context.bot.get_chat(group_id)
        member = await context.bot.get_chat_member(group_id, context.bot.id)
        if member.status not in {"administrator", "creator"}:
            raise ValueError("The bot must be an administrator in that group/channel.")
        if getattr(member, "can_post_messages", True) is False:
            raise ValueError("The bot does not have permission to post messages there.")
        await storage.set_setting("group_id", str(chat.id))
        await update.effective_message.reply_text(
            f"✅ Destination group updated: {chat.id}\n✅ डेस्टिनेशन ग्रुप अपडेट हो गया।"
        )
    except Exception as exc:
        logger.warning("Owner setgroup failed: %s", str(exc)[:300])
        await update.effective_message.reply_text(
            "❌ Could not set this group. Add the bot as an admin with posting permission and use its numeric chat ID.\n"
            "❌ ग्रुप सेट नहीं हुआ। बॉट को पोस्ट करने की अनुमति के साथ admin बनाएँ।"
        )


async def owner_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update) or not update.effective_message:
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await update.effective_message.reply_text(
            "Usage: /maintenance on OR /maintenance off\nउपयोग: /maintenance on या /maintenance off"
        )
        return
    state = context.args[0].lower()
    await storage.set_setting("maintenance", state)
    if state == "on":
        await update.effective_message.reply_text(
            "✅ Maintenance ON. New downloads are blocked; active downloads can finish.\n"
            "✅ मेंटेनेंस चालू है। नए डाउनलोड बंद हैं; चल रहे डाउनलोड पूरे होंगे।"
        )
    else:
        await update.effective_message.reply_text(
            "✅ Maintenance OFF. New downloads are enabled.\n"
            "✅ मेंटेनेंस बंद है। नए डाउनलोड फिर से चालू हैं।"
        )


async def owner_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update) or not update.effective_message:
        return
    announcement = " ".join(context.args).strip()
    if not announcement:
        await update.effective_message.reply_text(
            "Usage: /broadcast Your announcement text\nउपयोग: /broadcast अपना संदेश लिखें"
        )
        return
    user_ids = await storage.all_user_ids()
    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=announcement)
            sent += 1
        except (Forbidden, BadRequest):
            failed += 1
        except TelegramError:
            failed += 1
            logger.exception("Broadcast delivery error (user=%s)", user_id)
        await asyncio.sleep(0.05)  # Stay below Telegram's free broadcast rate.
    await update.effective_message.reply_text(
        f"📢 Broadcast finished.\nSent: {sent}\nFailed: {failed}\n\n"
        f"📢 ब्रॉडकास्ट पूरा हुआ।\nभेजे गए: {sent}\nअसफल: {failed}"
    )


async def cleanup_loop(application: Application) -> None:
    while True:
        await asyncio.sleep(300)
        expired_paths = await storage.prune_expired()
        for raw_path in expired_paths:
            try:
                Path(raw_path).unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not delete expired cache file")

        # Remove abandoned partial files from interrupted downloads.
        cutoff = time.time() - 6 * 60 * 60
        for path in CACHE_DIR.glob("*.part"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not clean partial cache file")


async def post_init(application: Application) -> None:
    await storage.initialize()
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start DiskWala Bot"),
            BotCommand("help", "How to use the bot"),
        ]
    )
    application.bot_data["cleanup_task"] = application.create_task(
        cleanup_loop(application), name="cache-cleanup"
    )
    me = await application.bot.get_me()
    logger.info("DiskWala Bot started as @%s", me.username)


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("cleanup_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await resolver.close()


def build_application() -> Application:
    builder: ApplicationBuilder = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(UPDATE_WORKERS)
        .connection_pool_size(max(32, UPDATE_WORKERS * 2))
        .pool_timeout(120)
        .read_timeout(120)
        .write_timeout(120)
        .media_write_timeout(3600)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if LOCAL_BOT_API_URL:
        builder = (
            builder.base_url(f"{LOCAL_BOT_API_URL}/bot")
            .base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
            .local_mode(True)
        )
    application = builder.build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", owner_stats))
    application.add_handler(CommandHandler("setgroup", owner_setgroup))
    application.add_handler(CommandHandler("maintenance", owner_maintenance))
    application.add_handler(CommandHandler("broadcast", owner_broadcast))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    return application


def main() -> None:
    app = build_application()
    logger.info("Starting DiskWala Bot polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
