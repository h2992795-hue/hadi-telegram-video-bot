from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
try:
    import boto3
    from botocore.config import Config
    from boto3.s3.transfer import TransferConfig
except ImportError:  # Bot can still run in Telegram-only mode until requirements are updated.
    boto3 = None
    Config = None
    TransferConfig = None

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------- Configuration ---------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MAX_UPLOAD_MB = max(1, min(int(os.getenv("MAX_UPLOAD_MB", "48")), 49))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = max(1, min(int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3")), 8))
DOWNLOAD_TIMEOUT_SECONDS = max(60, min(int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "900")), 3600))
JOB_TTL_SECONDS = max(300, min(int(os.getenv("JOB_TTL_SECONDS", "1800")), 7200))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# S3-compatible storage: Cloudflare R2 is a supported example.
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").strip()
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "").strip()
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "").strip()
S3_REGION = os.getenv("S3_REGION", "auto").strip() or "auto"
S3_URL_EXPIRY_SECONDS = max(300, min(int(os.getenv("S3_URL_EXPIRY_SECONDS", "86400")), 604800))

# These are only for a separately deployed Local Telegram Bot API Server.
TELEGRAM_API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "").strip()
TELEGRAM_API_FILE_URL = os.getenv("TELEGRAM_API_FILE_URL", "").strip()
TELEGRAM_API_LOCAL_MODE = os.getenv("TELEGRAM_API_LOCAL_MODE", "false").lower() in {"1", "true", "yes"}

logging.basicConfig(
    level=LOG_LEVEL if LOG_LEVEL in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO",
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("hadi-video-bot")

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".3gp"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS
RESERVED_IG_PATHS = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct",
    "about", "developer", "challenge", "privacy", "terms", "web", "emails",
}
QUALITY_CHOICES: dict[str, tuple[str, int | None]] = {
    "best": ("أعلى جودة متاحة", None),
    "2160": ("4K · 2160p", 2160),
    "1440": ("2K · 1440p", 1440),
    "1080": ("1080p Full HD", 1080),
    "720": ("720p HD", 720),
    "480": ("480p", 480),
    "360": ("360p", 360),
}

JOBS: dict[str, dict] = {}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


def is_storage_configured() -> bool:
    return boto3 is not None and Config is not None and TransferConfig is not None and all(
        (S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY)
    )


def is_safe_public_http_url(url: str) -> bool:
    """Accept public HTTP(S) URLs and reject obvious local/private destinations."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain", "metadata.google.internal"} or host.endswith(".local"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return False
        except ValueError:
            # Resolve obvious internal hostnames too. If DNS fails, let yt-dlp report that clearly.
            try:
                addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
                if addresses and any(not ipaddress.ip_address(addr.split("%", 1)[0]).is_global for addr in addresses):
                    return False
            except (OSError, ValueError):
                pass
        return True
    except Exception:
        return False


def normalize_instagram_url(url: str) -> tuple[str, bool]:
    """Turn an Instagram profile URL into a best-effort stories URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        return url, False
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 1 and parts[0].lower() not in RESERVED_IG_PATHS:
        return f"https://www.instagram.com/stories/{parts[0]}/", True
    if parts and parts[0].lower() == "stories":
        return url, True
    return url, False


def list_media_files(folder: str) -> list[Path]:
    root = Path(folder)
    found: list[Path] = []
    for path in root.iterdir():
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTS:
            continue
        if path.name.endswith((".part", ".ytdl", ".temp")):
            continue
        if path.name == "instagram_cookies.txt":
            continue
        found.append(path)
    return sorted(found, key=lambda item: item.name.lower())


def download_media(
    url: str,
    folder: str,
    quality_key: str,
    stories_mode: bool,
    progress_state: dict,
) -> tuple[list[str], str | None]:
    """Download media without deliberately re-encoding it; safe to call in a worker thread."""
    label, max_height = QUALITY_CHOICES.get(quality_key, QUALITY_CHOICES["best"])
    output_template = str(Path(folder) / "%(playlist_index)03d-%(title).70s-%(id)s.%(ext)s")
    if max_height is None:
        fmt = "bestvideo*+bestaudio/best"
    else:
        fmt = f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"

    def progress_hook(data: dict) -> None:
        if progress_state.get("cancelled"):
            raise yt_dlp.utils.DownloadError("Download cancelled after configured timeout")
        status = data.get("status")
        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            speed = data.get("speed") or 0
            eta = data.get("eta")
            progress_state.update({
                "status": "downloading",
                "downloaded": int(downloaded),
                "total": int(total),
                "speed": float(speed),
                "eta": int(eta) if eta is not None else None,
                "filename": Path(data.get("filename") or "").name,
            })
        elif status == "finished":
            progress_state.update({"status": "merging", "filename": Path(data.get("filename") or "").name})

    ydl_opts = {
        "format": fmt,
        "outtmpl": output_template,
        "noplaylist": not stories_mode,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 3,
        "extractor_retries": 2,
        "fragment_retries": 3,
        "file_access_retries": 2,
        "socket_timeout": 25,
        "overwrites": True,
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress_hook],
        "ignoreerrors": bool(stories_mode),
        "noprogress": True,
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }

    progress_state.update({"status": "starting", "downloaded": 0, "total": 0, "speed": 0, "eta": None})
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        found = list_media_files(folder)
        if not found:
            return [], (
                "ما حصلت ملف وسائط قابل للإرسال. قد يكون الرابط غير مدعوم، أو المحتوى غير متاح، "
                "أو الموقع يطلب تسجيل دخول."
            )
        note = None
        if stories_mode:
            note = (
                "تنبيه: جلب ستوري إنستغرام من رابط حساب عام يعتمد على ما يسمح الموقع بالوصول إليه؛ "
                "إنستغرام قد يمنع التنزيل دون تسجيل دخول."
            )
        return [str(path) for path in found], note
    except yt_dlp.utils.DownloadError as exc:
        log.warning("Download failed: %s", exc)
        if stories_mode:
            return [], (
                "إنستغرام لم يوفّر الستوري للتنزيل. قد يتطلب تسجيل دخول أو يمنع الوصول الآلي، "
                "وهذا قد يحدث حتى للحسابات العامة. البوت لا يطلب كلمة مرورك ولا يتجاوز قيود الوصول."
            )
        return [], (
            "تعذّر تنزيل هذا الرابط. تأكد أن المحتوى متاح للعامة، وجرّب رابطاً مباشراً آخر. "
            "قد تتغيّر إتاحة المواقع من وقت لآخر."
        )
    except Exception:
        log.exception("Unexpected downloader error")
        return [], "صار خطأ غير متوقع. جرّب مرة ثانية، وإذا تكرر افحص Logs في Railway."
    finally:
        progress_state["finished_at"] = time.time()


def make_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        region_name=S3_REGION,
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=15,
            read_timeout=120,
            max_pool_connections=8,
            s3={"addressing_style": "path"},
        ),
    )


def upload_to_storage(path: Path) -> str:
    """Upload bytes unchanged to S3/R2 and return a temporary signed download URL."""
    if not is_storage_configured():
        raise RuntimeError("External storage dependency or credentials are not configured")
    key = f"hadi-downloads/{time.strftime('%Y/%m/%d')}/{uuid.uuid4().hex}-{path.name}"
    content_type = "application/octet-stream"
    if path.suffix.lower() in VIDEO_EXTS:
        content_type = "video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream"
    elif path.suffix.lower() in IMAGE_EXTS:
        content_type = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif",
        }.get(path.suffix.lower(), "application/octet-stream")

    client = make_s3_client()
    transfer_config = TransferConfig(
        multipart_threshold=16 * 1024 * 1024,
        multipart_chunksize=16 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )
    client.upload_file(
        str(path), S3_BUCKET, key,
        ExtraArgs={"ContentType": content_type},
        Config=transfer_config,
    )
    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": S3_BUCKET,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{path.name}"',
        },
        ExpiresIn=S3_URL_EXPIRY_SECONDS,
    )


def cleanup_job(job_id: str) -> None:
    job = JOBS.pop(job_id, None)
    if job:
        folder = job.get("folder")
        if folder:
            shutil.rmtree(folder, ignore_errors=True)


async def expire_job(job_id: str) -> None:
    await asyncio.sleep(JOB_TTL_SECONDS)
    while True:
        job = JOBS.get(job_id)
        if not job:
            return
        if job.get("status") not in {"downloading", "delivering", "cancelling"}:
            cleanup_job(job_id)
            log.info("Expired temporary job %s", job_id)
            return
        await asyncio.sleep(60)


async def cleanup_after_worker(job_id: str, worker: asyncio.Task) -> None:
    try:
        await worker
    except Exception:
        pass
    cleanup_job(job_id)


def quality_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ أعلى جودة متاحة", callback_data=f"q:{job_id}:best")],
        [InlineKeyboardButton("4K · 2160p", callback_data=f"q:{job_id}:2160"), InlineKeyboardButton("2K · 1440p", callback_data=f"q:{job_id}:1440")],
        [InlineKeyboardButton("1080p Full HD", callback_data=f"q:{job_id}:1080"), InlineKeyboardButton("720p HD", callback_data=f"q:{job_id}:720")],
        [InlineKeyboardButton("480p", callback_data=f"q:{job_id}:480"), InlineKeyboardButton("360p", callback_data=f"q:{job_id}:360")],
        [InlineKeyboardButton("✖ إلغاء", callback_data=f"cancel:{job_id}")],
    ])


def delivery_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 استلام داخل تلغرام", callback_data=f"d:{job_id}:telegram")],
        [InlineKeyboardButton("🔗 رابط تنزيل مباشر (أصلي)", callback_data=f"d:{job_id}:link")],
        [InlineKeyboardButton("✖ إلغاء وحذف الملف المؤقت", callback_data=f"cancel:{job_id}")],
    ])


def format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(max(value, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_eta(seconds: int | None) -> str:
    if seconds is None:
        return ""
    seconds = max(0, seconds)
    mins, secs = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


async def safe_edit(message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            log.debug("Could not edit status message: %s", exc)
    except TelegramError as exc:
        log.debug("Could not edit status message: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    storage_status = "مفعّل ✅" if is_storage_configured() else "يحتاج إعداد مساحة تخزين خارجية ⚙️"
    await message.reply_text(
        "هلا بيك بـ HADI Video Bot PRO ⚡\n\n"
        "أرسل رابط فيديو أو صورة من موقع مدعوم. ولجلب الستوري جرّب رابط حساب إنستغرام عام.\n\n"
        "✅ اختيار الجودة حتى 4K إذا كانت متاحة بالمصدر.\n"
        "✅ تنزيل الملف وإرساله دون إعادة ترميز مقصودة.\n"
        "✅ تختار الاستلام داخل تلغرام أو برابط مباشر للملف الأصلي.\n"
        f"✅ حالة روابط التنزيل: {storage_status}\n\n"
        f"حد الإرسال عبر Bot API القياسي مضبوط بحذر على {MAX_UPLOAD_MB} MB لكل ملف. "
        "الملفات الأكبر تحتاج خيار الرابط أو خادم Bot API محلي مُعدّ بشكل منفصل.\n\n"
        "ملاحظة: دعم المواقع والستوري يعتمد على إتاحة المصدر؛ بعض المواقع تمنع الوصول الآلي. "
        "نزّل المحتوى الذي تملك حق استخدامه فقط."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "طريقة الاستخدام:\n"
            "1) أرسل رابطاً عاماً يبدأ بـ https://\n"
            "2) اختَر أعلى جودة أو دقة محددة.\n"
            "3) بعد اكتمال التنزيل، اختَر الإرسال داخل تلغرام أو رابط تنزيل مباشر.\n\n"
            "الرابط المباشر يحتاج إعداد S3/R2 في Railway، ويكون مؤقتاً. "
            "الإرسال داخل تلغرام محدود بحجم Bot API القياسي ما لم تربط خادماً محلياً مستقلاً.\n"
            "أرسل /cancel لإلغاء طلب لم يبدأ تسليمه."
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    uid = update.effective_user.id
    own_ids = [job_id for job_id, job in JOBS.items() if job.get("user_id") == uid and job.get("status") not in {"downloading", "delivering"}]
    for job_id in own_ids:
        cleanup_job(job_id)
    if own_ids:
        await update.effective_message.reply_text("تم إلغاء الطلب وحذف الملفات المؤقتة. ✅")
    else:
        await update.effective_message.reply_text("ما عندك طلب قابل للإلغاء حالياً.")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return
    match = URL_RE.search(message.text.strip())
    if not match:
        await message.reply_text("أرسل رابطاً يبدأ بـ https:// أو http://")
        return
    url = match.group(0).rstrip(".,!?)،؛]}")
    if len(url) > 2048 or not is_safe_public_http_url(url):
        await message.reply_text("الرابط غير صالح أو يشير إلى عنوان محلي غير مسموح.")
        return
    normalized, stories_mode = normalize_instagram_url(url)
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {
        "user_id": user.id,
        "chat_id": message.chat_id,
        "url": normalized,
        "stories_mode": stories_mode,
        "status": "awaiting_quality",
        "created_at": time.time(),
        "folder": None,
        "files": [],
    }
    asyncio.create_task(expire_job(job_id))
    what = "ستوري/حساب إنستغرام" if stories_mode else "الرابط"
    await message.reply_text(
        f"استلمت {what} ✅\nاختَر الجودة. التنزيل بأفضل جودة لا يعني تكبير المصدر؛ الجودة الأعلى تظهر فقط إذا وفرها الموقع.",
        reply_markup=quality_keyboard(job_id),
    )


async def watch_download(status_message, task: asyncio.Task, progress_state: dict) -> None:
    last_bucket = -1
    last_update_at = 0.0
    while not task.done():
        await asyncio.sleep(3)
        state = progress_state.copy()
        total = state.get("total", 0)
        downloaded = state.get("downloaded", 0)
        percent = int(downloaded * 100 / total) if total > 0 else None
        bucket = percent // 10 if percent is not None else -1
        now = time.monotonic()
        if bucket != last_bucket or now - last_update_at >= 12:
            if percent is not None:
                line = f"⏳ جاري التنزيل: {percent}% ({format_bytes(downloaded)} / {format_bytes(total)})"
            else:
                line = "⏳ جاري الاتصال بالموقع وتجهيز التنزيل…"
            speed = state.get("speed") or 0
            if speed > 0:
                line += f"\nالسرعة: {format_bytes(int(speed))}/ث"
            if state.get("eta") is not None:
                line += f"\nالوقت المتبقي تقريباً: {format_eta(state.get('eta'))}"
            if state.get("status") == "merging":
                line = "⏳ اكتمل جلب البيانات، جارٍ تجهيز الملف دون إعادة ترميز مقصودة…"
            await safe_edit(status_message, line)
            last_bucket = bucket
            last_update_at = now
    await task


async def handle_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    await query.answer()
    try:
        _, job_id, quality_key = query.data.split(":", 2)
    except (ValueError, AttributeError):
        await safe_edit(query.message, "زر غير صالح. أرسل الرابط مرة ثانية.")
        return
    job = JOBS.get(job_id)
    if not job or job.get("user_id") != query.from_user.id:
        await safe_edit(query.message, "انتهت صلاحية الطلب أو أنه لا يخصك. أرسل الرابط مرة ثانية.")
        return
    if job.get("status") != "awaiting_quality":
        await query.answer("هذا الطلب قيد المعالجة أو انتهت صلاحيته.", show_alert=False)
        return
    if quality_key not in QUALITY_CHOICES:
        await safe_edit(query.message, "اختيار الجودة غير صحيح. أرسل الرابط مرة ثانية.")
        return

    job["status"] = "downloading"
    job["quality_key"] = quality_key
    label, _ = QUALITY_CHOICES[quality_key]
    status_message = await query.edit_message_text("⏳ جاري بدء التنزيل…")
    await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
    folder = tempfile.mkdtemp(prefix="hadi-pro-")
    job["folder"] = folder
    progress_state: dict = {}

    async def download_task_runner():
        async with DOWNLOAD_SEMAPHORE:
            return await asyncio.to_thread(
                download_media, job["url"], folder, quality_key, job["stories_mode"], progress_state
            )

    task = asyncio.create_task(download_task_runner())
    try:
        await asyncio.wait_for(watch_download(status_message, task, progress_state), timeout=DOWNLOAD_TIMEOUT_SECONDS)
        files, note = await task
    except asyncio.TimeoutError:
        progress_state["cancelled"] = True
        job["status"] = "cancelling"
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=35)
        except asyncio.TimeoutError:
            # The worker thread may still be waiting on a remote server. Keep its temp
            # folder until the thread exits, then clean up in the background.
            asyncio.create_task(cleanup_after_worker(job_id, task))
        except Exception:
            cleanup_job(job_id)
        else:
            cleanup_job(job_id)
        await safe_edit(status_message, f"انتهت مهلة التنزيل ({DOWNLOAD_TIMEOUT_SECONDS // 60} دقيقة). جرّب رابطاً آخر أو جودة أقل.")
        return
    except asyncio.CancelledError:
        progress_state["cancelled"] = True
        asyncio.create_task(cleanup_after_worker(job_id, task))
        raise
    except Exception:
        log.exception("Download worker failed")
        cleanup_job(job_id)
        await safe_edit(status_message, "صار خطأ أثناء التنزيل. جرّب لاحقاً، وإذا تكرر راجع Logs في Railway.")
        return

    if not files:
        cleanup_job(job_id)
        await safe_edit(status_message, note or "ما قدرت أنزّل الوسائط من هذا الرابط.")
        return

    job["files"] = files
    job["note"] = note
    job["quality_label"] = label
    job["status"] = "awaiting_delivery"
    total_bytes = sum(Path(name).stat().st_size for name in files if Path(name).exists())
    message_text = (
        f"✅ جهزت {len(files)} ملف/ملفات ({format_bytes(total_bytes)}) بدون إعادة ترميز مقصودة.\n"
        "اختَر طريقة الاستلام:"
    )
    if note:
        message_text += f"\n\n{note}"
    await safe_edit(status_message, message_text, reply_markup=delivery_keyboard(job_id))


async def handle_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    await query.answer()
    try:
        _, job_id, mode = query.data.split(":", 2)
    except (ValueError, AttributeError):
        await safe_edit(query.message, "زر غير صالح. أرسل الرابط مرة ثانية.")
        return
    job = JOBS.get(job_id)
    if not job or job.get("user_id") != query.from_user.id:
        await safe_edit(query.message, "انتهت صلاحية الطلب. أرسل الرابط مرة ثانية.")
        return
    if job.get("status") != "awaiting_delivery":
        await query.answer("هذا الطلب قيد المعالجة أو انتهت صلاحيته.", show_alert=False)
        return
    if mode not in {"telegram", "link"}:
        await safe_edit(query.message, "طريقة الاستلام غير صحيحة.")
        return
    if mode == "link" and not is_storage_configured():
        await safe_edit(
            query.message,
            "🔗 خيار الرابط غير مفعّل بعد. صاحب البوت يحتاج إعداد تخزين S3/R2 في Railway.\n\n"
            "تقدر تختار الاستلام داخل تلغرام، أو انتظر إلى أن يُجهّز صاحب البوت التخزين.",
            reply_markup=delivery_keyboard(job_id),
        )
        return

    files = [Path(item) for item in job.get("files", [])]
    valid_files = [path for path in files if path.is_file()]
    if not valid_files:
        cleanup_job(job_id)
        await safe_edit(query.message, "انتهت صلاحية الملفات المؤقتة. أرسل الرابط مرة ثانية.")
        return

    if mode == "telegram":
        oversized = [p for p in valid_files if p.stat().st_size > MAX_UPLOAD_BYTES]
        if oversized:
            total = sum(p.stat().st_size for p in oversized)
            await safe_edit(
                query.message,
                f"⚠️ أكو {len(oversized)} ملف/ملفات أكبر من حد الإرسال الحالي ({MAX_UPLOAD_MB} MB لكل ملف).\n"
                f"حجم الملفات الكبيرة: {format_bytes(total)}.\n\n"
                "اختَر رابط التنزيل المباشر بعد إعداد التخزين، أو صاحب البوت يربط Local Telegram Bot API Server لرفع ملفات أكبر.",
                reply_markup=delivery_keyboard(job_id),
            )
            return

    job["status"] = "delivering"
    await safe_edit(query.message, "⏳ جاري تسليم الملف/الملفات…")
    sent = 0
    failed = 0
    try:
        for index, path in enumerate(valid_files, 1):
            if mode == "link":
                await safe_edit(query.message, f"⏳ جارٍ رفع الملف {index} من {len(valid_files)} إلى مساحة التخزين…")
                try:
                    link = await asyncio.to_thread(upload_to_storage, path)
                    ttl_hours = max(1, S3_URL_EXPIRY_SECONDS // 3600)
                    await query.message.reply_text(
                        f"🔗 رابط تنزيل الملف الأصلي ({format_bytes(path.stat().st_size)}):\n{link}\n\n"
                        f"صلاحية الرابط تقريباً {ttl_hours} ساعة. لا تشاركه مع أشخاص غير مقصودين."
                    )
                    sent += 1
                except Exception:
                    log.exception("Storage upload failed for %s", path.name)
                    await query.message.reply_text(
                        "تعذّر رفع الملف إلى مساحة التخزين. تحقق من إعدادات S3/R2 في Railway وحالة مزود التخزين."
                    )
                    failed += 1
                    break
            else:
                await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                try:
                    with path.open("rb") as media:
                        await query.message.reply_document(
                            document=media,
                            filename=path.name,
                            caption=f"{job.get('quality_label', 'أعلى جودة متاحة')} · الملف الأصلي دون ضغط مقصود"[:1000],
                            read_timeout=240,
                            write_timeout=240,
                            connect_timeout=30,
                            pool_timeout=30,
                        )
                    sent += 1
                except (TimedOut, NetworkError, RetryAfter, TelegramError):
                    log.exception("Telegram upload failed for %s", path.name)
                    failed += 1
                    await query.message.reply_text(
                        f"تعذّر إرسال الملف {path.name} داخل تلغرام. إذا كان حجمه قريباً من الحد، جرّب رابط التنزيل بعد إعداد التخزين."
                    )

        summary = f"✅ اكتمل التسليم: {sent} ملف/ملفات."
        if failed:
            summary += f"\n⚠️ تعذّر تسليم {failed} ملف/ملفات."
        await query.message.reply_text(summary)
        if failed == 0:
            cleanup_job(job_id)
        else:
            job["status"] = "awaiting_delivery"
            await query.message.reply_text("تقدر تعيد المحاولة بطريقة استلام أخرى:", reply_markup=delivery_keyboard(job_id))
    finally:
        if job_id in JOBS and JOBS[job_id].get("status") == "delivering":
            JOBS[job_id]["status"] = "awaiting_delivery"


def main() -> None:
    if not TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable. Set it in Railway Variables.")
    builder = Application.builder().token(TOKEN).concurrent_updates(8)
    if TELEGRAM_API_BASE_URL:
        builder = builder.base_url(TELEGRAM_API_BASE_URL)
    if TELEGRAM_API_FILE_URL:
        builder = builder.base_file_url(TELEGRAM_API_FILE_URL)
    if TELEGRAM_API_LOCAL_MODE:
        builder = builder.local_mode(True)

    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(handle_quality, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(handle_delivery, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(handle_cancel_button, pattern=r"^cancel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info(
        "HADI Video Bot PRO started | concurrent downloads=%s | storage=%s | upload MB=%s",
        MAX_CONCURRENT_DOWNLOADS, is_storage_configured(), MAX_UPLOAD_MB,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


async def handle_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    await query.answer()
    job_id = query.data.split(":", 1)[1]
    job = JOBS.get(job_id)
    if not job or job.get("user_id") != query.from_user.id:
        await safe_edit(query.message, "انتهت صلاحية الطلب.")
        return
    if job.get("status") in {"downloading", "delivering"}:
        await query.answer("ما أگدر أوقف عملية بدأت فعلياً من هذا الزر. جرّب /cancel بعد اكتمالها.", show_alert=True)
        return
    cleanup_job(job_id)
    await safe_edit(query.message, "تم إلغاء الطلب وحذف الملفات المؤقتة. ✅")


if __name__ == "__main__":
    main()
