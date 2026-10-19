from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MAX_UPLOAD_MB = max(1, min(int(os.getenv("MAX_UPLOAD_MB", "48")), 49))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = max(60, min(int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "900")), 3600))
LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
JOBS: dict[str, dict] = {}
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("hadi-video-bot")

QUALITIES = {
    "best": ("⭐ أعلى جودة متاحة", None),
    "2160": ("4K · 2160p", 2160),
    "1440": ("2K · 1440p", 1440),
    "1080": ("1080p Full HD", 1080),
    "720": ("720p HD", 720),
    "480": ("480p", 480),
    "360": ("360p", 360),
}

def valid_public_url(url: str) -> bool:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return p.scheme in ("http", "https") and bool(host) and host not in {
            "localhost", "127.0.0.1", "::1"
        } and not host.endswith(".local")
    except Exception:
        return False

def quality_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(QUALITIES["best"][0], callback_data=f"q:{job_id}:best")],
        [InlineKeyboardButton("4K · 2160p", callback_data=f"q:{job_id}:2160"),
         InlineKeyboardButton("2K · 1440p", callback_data=f"q:{job_id}:1440")],
        [InlineKeyboardButton("1080p Full HD", callback_data=f"q:{job_id}:1080"),
         InlineKeyboardButton("720p HD", callback_data=f"q:{job_id}:720")],
        [InlineKeyboardButton("480p", callback_data=f"q:{job_id}:480"),
         InlineKeyboardButton("360p", callback_data=f"q:{job_id}:360")],
        [InlineKeyboardButton("✖ إلغاء", callback_data=f"c:{job_id}")],
    ])

def delivery_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 استلام داخل تلغرام", callback_data=f"d:{job_id}:tg")],
        [InlineKeyboardButton("🔗 رابط Litterbox المؤقت", callback_data=f"d:{job_id}:link")],
        [InlineKeyboardButton("✖ إلغاء", callback_data=f"c:{job_id}")],
    ])

def download_video(url: str, folder: str, quality_key: str) -> list[Path]:
    _, height = QUALITIES.get(quality_key, QUALITIES["best"])
    fmt = "bestvideo*+bestaudio/best" if height is None else (
        f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
    )
    opts = {
        "format": fmt,
        "outtmpl": str(Path(folder) / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "continuedl": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    extensions = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".3gp",
                  ".jpg", ".jpeg", ".png", ".webp", ".gif"}
    return [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in extensions]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "هلا بيك بـ HADI Video Bot PRO ⚡\n\n"
            "أرسل رابط فيديو عام، اختَر الجودة، وبعد التنزيل اختَر الاستلام داخل تلغرام "
            "أو رابط Litterbox المؤقت.\n\n"
            "الجودة 4K تظهر فقط إذا كان المصدر يوفرها. البوت لا يعيد ضغط الفيديو عمداً. "
            "قد تمنع بعض المواقع التنزيل، وستوريات إنستغرام لا تعمل دائماً دون تسجيل دخول."
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "أرسل رابطاً عاماً يبدأ بـ https:// ثم اختَر الجودة وطريقة الاستلام.\n"
            "Litterbox يقبل عبر API ملفات حتى 90 MB للطلب الواحد، ويحذف الملفات التي لا تُفتح "
            "لمدة 7 أيام. الملفات الأكبر من ذلك لن تُرفع بهذه الطريقة."
        )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text:
        return
    match = URL_RE.search(msg.text)
    if not match:
        return
    url = match.group(0).rstrip(".,!?)،؛]}")
    if len(url) > 2048 or not valid_public_url(url):
        await msg.reply_text("الرابط غير صالح.")
        return
    import uuid
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"user_id": user.id, "chat_id": msg.chat_id, "url": url,
                    "status": "quality", "folder": None, "files": []}
    await msg.reply_text("استلمت الرابط ✅ اختَر الجودة المطلوبة:",
                         reply_markup=quality_keyboard(job_id))

async def handle_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user or not q.message:
        return
    await q.answer()
    try:
        _, job_id, quality = q.data.split(":", 2)
    except Exception:
        return
    job = JOBS.get(job_id)
    if not job or job["user_id"] != q.from_user.id or job["status"] != "quality":
        await q.message.reply_text("انتهى الطلب أو لا يخصك. أرسل الرابط مرة ثانية.")
        return
    if quality not in QUALITIES:
        return
    job["status"] = "downloading"
    folder = tempfile.mkdtemp(prefix="hadi-video-")
    job["folder"] = folder
    await q.edit_message_text("⏳ جارٍ تنزيل الفيديو بأفضل جودة متاحة…")
    try:
        files = await asyncio.wait_for(
            asyncio.to_thread(download_video, job["url"], folder, quality),
            timeout=DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        shutil.rmtree(folder, ignore_errors=True)
        JOBS.pop(job_id, None)
        await q.message.reply_text("انتهت مهلة التنزيل. جرّب رابطاً آخر أو جودة أقل.")
        return
    except Exception as e:
        log.warning("Download failed: %s", e)
        shutil.rmtree(folder, ignore_errors=True)
        JOBS.pop(job_id, None)
        await q.message.reply_text(
            "تعذّر التنزيل. قد يكون الرابط غير مدعوم أو يتطلب تسجيل دخول. "
            "ستوريات إنستغرام قد لا تعمل دون تسجيل دخول."
        )
        return
    if not files:
        shutil.rmtree(folder, ignore_errors=True)
        JOBS.pop(job_id, None)
        await q.message.reply_text("ما حصلت ملف وسائط من هذا الرابط.")
        return
    job["files"] = files
    job["quality"] = QUALITIES[quality][0]
    job["status"] = "delivery"
    size = sum(p.stat().st_size for p in files)
    await q.message.reply_text(
        f"اكتمل التنزيل: {len(files)} ملف/ملفات، الحجم {size / 1024 / 1024:.1f} MB.\n"
        "اختَر طريقة الاستلام:",
        reply_markup=delivery_keyboard(job_id)
    )


def strip_audio_stream_copy(path: Path) -> Path:
    """Remove audio tracks without re-encoding the video stream."""
    output = path.with_name(path.stem + "-no-audio.mp4")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(path), "-map", "0:v:0", "-c:v", "copy",
        "-an", "-movflags", "+faststart", str(output)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        try:
            output.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError("FFmpeg failed to remove audio: " + (result.stderr[-500:] or "unknown error"))
    return output


async def upload_savedly(path: Path) -> str:
    # Temporary Litterbox upload, maximum 1 GB, expires after 72 hours.
    if path.stat().st_size > 1024 * 1024 * 1024:
        raise ValueError("Litterbox limit is 1 GB per file")
    timeout = aiohttp.ClientTimeout(total=900, connect=30)
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("time", "72h")
    with path.open("rb") as f:
        form.add_field("fileToUpload", f, filename=path.name,
                       content_type="application/octet-stream")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(LITTERBOX_API, data=form) as response:
                body = (await response.text()).strip()
                if response.status != 200:
                    raise RuntimeError(f"Litterbox HTTP {response.status}: {body[:300]}")
                if not body.startswith("https://litter.catbox.moe/"):
                    raise RuntimeError(f"Unexpected Litterbox response: {body[:300]}")
                return body

async def handle_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user or not q.message:
        return
    await q.answer()
    try:
        _, job_id, mode = q.data.split(":", 2)
    except Exception:
        return
    job = JOBS.get(job_id)
    if not job or job["user_id"] != q.from_user.id or job["status"] != "delivery":
        await q.message.reply_text("انتهى الطلب أو لا يخصك. أرسل الرابط مرة ثانية.")
        return
    files = [p for p in job["files"] if p.is_file()]
    if not files:
        await q.message.reply_text("انتهت صلاحية الملف المؤقت. أرسل الرابط مرة ثانية.")
        return
    job["status"] = "sending"
    sent = 0
    try:
        for path in files:
            # Produce a silent copy by removing audio tracks while copying video without re-encoding.
            silent_path = await asyncio.to_thread(strip_audio_stream_copy, path)
            path = silent_path
            if mode == "link":
                await q.message.reply_text(f"⏳ جارٍ رفع {path.name} إلى Litterbox…")
                link = await upload_savedly(path)
                await q.message.reply_text(
                    f"🔗 رابط تحميل الملف الأصلي ({path.stat().st_size / 1024 / 1024:.1f} MB):\n{link}\n\n"
                    "تنبيه: الرابط مؤقت وينتهي بعد 72 ساعة. نزّل الملف قبل انتهاء المدة."
                )
            elif mode == "tg":
                if path.stat().st_size > MAX_UPLOAD_BYTES:
                    await q.message.reply_text(
                        f"حجم الملف {path.stat().st_size / 1024 / 1024:.1f} MB ويتجاوز حد تيليگرام "
                        f"المضبوط ({MAX_UPLOAD_MB} MB). اختَر رابط Litterbox المؤقت."
                    )
                    continue
                await context.bot.send_chat_action(chat_id=q.message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                with path.open("rb") as f:
                    await q.message.reply_document(document=f, filename=path.name,
                        caption=f"{job.get('quality', 'أعلى جودة')} · دون ضغط مقصود")
            else:
                return
            sent += 1
        await q.message.reply_text(f"✅ اكتمل التسليم: {sent} ملف/ملفات.")
    except Exception as e:
        log.exception("Delivery failed: %s", e)
        await q.message.reply_text(
            "تعذّر تسليم الملف. قد يكون حجمه أكبر من حد Litterbox (1 GB) أو حدث خطأ بالموقع."
        )
    finally:
        shutil.rmtree(job.get("folder") or "", ignore_errors=True)
        JOBS.pop(job_id, None)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    uid = update.effective_user.id
    ids = [k for k, v in JOBS.items() if v["user_id"] == uid and v["status"] in {"quality", "delivery"}]
    for k in ids:
        shutil.rmtree(JOBS[k].get("folder") or "", ignore_errors=True)
        JOBS.pop(k, None)
    await update.effective_message.reply_text("تم إلغاء الطلبات القابلة للإلغاء.")

async def cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user or not q.message:
        return
    await q.answer()
    job_id = q.data.split(":", 1)[1]
    job = JOBS.get(job_id)
    if not job or job["user_id"] != q.from_user.id:
        await q.message.reply_text("انتهى الطلب.")
        return
    if job["status"] not in {"quality", "delivery"}:
        await q.answer("العملية بدأت؛ لا يمكن إلغاؤها من هذا الزر.", show_alert=True)
        return
    shutil.rmtree(job.get("folder") or "", ignore_errors=True)
    JOBS.pop(job_id, None)
    await q.edit_message_text("تم إلغاء الطلب وحذف الملف المؤقت.")

def main() -> None:
    if not TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(handle_quality, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(handle_delivery, pattern=r"^d:"))
    app.add_handler(CallbackQueryHandler(cancel_button, pattern=r"^c:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
