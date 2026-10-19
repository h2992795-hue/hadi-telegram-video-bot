import asyncio
import base64
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "48"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
# Optional, only if the operator deliberately configures their own authorized session.
# User-facing flow does not ask for Instagram username/password.
INSTAGRAM_COOKIES_B64 = os.getenv("INSTAGRAM_COOKIES_B64", "").strip()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("hadi-video-bot")
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS

QUALITY_CHOICES = {
    "best": ("أعلى جودة متاحة", None),
    "720": ("720p HD", 720),
    "1080": ("1080p Full HD", 1080),
    "1440": ("1440p / 2K", 1440),
    "2160": ("2160p / 4K", 2160),
    "480": ("480p", 480),
    "360": ("360p", 360),
}


def valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def normalize_instagram_profile_url(url: str) -> tuple[str, bool]:
    """Convert a plain Instagram profile URL into the Stories endpoint, best-effort."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        return url, False
    parts = [part for part in parsed.path.split("/") if part]
    # Treat a plain /username/ URL as a request for that profile's current stories.
    reserved = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "about"}
    if len(parts) == 1 and parts[0].lower() not in reserved:
        username = parts[0]
        return f"https://www.instagram.com/stories/{username}/", True
    if len(parts) == 2 and parts[0].lower() == "stories":
        return url, True
    if len(parts) >= 3 and parts[0].lower() == "stories":
        return url, True
    return url, False


def make_cookiefile(folder: str) -> str | None:
    if not INSTAGRAM_COOKIES_B64:
        return None
    try:
        data = base64.b64decode(INSTAGRAM_COOKIES_B64, validate=True)
        cookie_path = Path(folder) / "instagram_cookies.txt"
        cookie_path.write_bytes(data)
        return str(cookie_path)
    except Exception:
        log.warning("Instagram cookies secret is invalid base64; continuing without cookies.")
        return None


def list_media_files(folder: str) -> list[Path]:
    files = []
    for p in Path(folder).iterdir():
        if not p.is_file() or p.name == "instagram_cookies.txt":
            continue
        if p.suffix.lower() in MEDIA_EXTS and not p.name.endswith((".part", ".ytdl")):
            files.append(p)
    files.sort(key=lambda p: p.name.lower())
    return files


def download_media(url: str, folder: str, quality_key: str, stories_mode: bool) -> tuple[list[str], str | None]:
    label, max_height = QUALITY_CHOICES.get(quality_key, QUALITY_CHOICES["best"])
    output_template = str(Path(folder) / "%(playlist_index)03d-%(title).55s-%(id)s.%(ext)s")
    if max_height is None:
        fmt = "bestvideo*+bestaudio/best"
    else:
        fmt = f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"

    ydl_opts = {
        "format": fmt,
        "outtmpl": output_template,
        "noplaylist": not stories_mode,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 25,
        "overwrites": True,
        "merge_output_format": "mp4",
        "ignoreerrors": False,
    }
    cookiefile = make_cookiefile(folder)
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        media_files = list_media_files(folder)
        if not media_files:
            return [], "ما حصلت ملفات وسائط جاهزة بعد محاولة التنزيل."
        too_large = [p.name for p in media_files if p.stat().st_size > MAX_UPLOAD_BYTES]
        deliverable = [p for p in media_files if p.stat().st_size <= MAX_UPLOAD_BYTES]
        if not deliverable:
            return [], (
                f"الملفات نزلت لكن حجمها يتجاوز حد الإرسال الحالي ({MAX_UPLOAD_MB} MB لكل ملف). "
                "جرّب دقة أقل. رفع حد المتغير وحده لا يرفع حد Telegram Bot API القياسي."
            )
        note = None
        if too_large:
            note = f"تخطّيت {len(too_large)} ملف/ملفات لأن حجم كل منها يتجاوز {MAX_UPLOAD_MB} MB."
        return [str(p) for p in deliverable], note
    except yt_dlp.utils.DownloadError as exc:
        log.warning("yt-dlp download error: %s", exc)
        if stories_mode:
            return [], (
                "إنستغرام رفض إظهار الستوري بدون تسجيل دخول أو لم يوفّرها للتنزيل. "
                "هذا شائع حتى مع الحسابات العامة، لذلك لا أستطيع ضمان تنزيلها من رابط الحساب وحده. "
                "البوت لا يطلب منك كلمة مرور إنستغرام ولا يتجاوز قيود الوصول."
            )
        return [], (
            "تعذّر تنزيل الرابط. قد يكون الموقع غير مدعوم حالياً، أو المحتوى غير متاح، "
            "أو أن الموقع يتطلب تسجيل دخول. جرّب رابطاً عاماً آخر."
        )
    except Exception:
        log.exception("Unexpected download error")
        return [], "صار خطأ غير متوقع أثناء التنزيل. راجع Logs في Railway."


def quality_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("أعلى جودة متاحة", callback_data="quality:best")],
        [InlineKeyboardButton("4K · 2160p", callback_data="quality:2160"), InlineKeyboardButton("2K · 1440p", callback_data="quality:1440")],
        [InlineKeyboardButton("1080p", callback_data="quality:1080"), InlineKeyboardButton("720p", callback_data="quality:720")],
        [InlineKeyboardButton("480p", callback_data="quality:480"), InlineKeyboardButton("360p", callback_data="quality:360")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "هلا بيك بـ HADI Video Bot 👋\n\n"
        "أرسل رابط فيديو، أو رابط حساب إنستغرام عام مثل instagram.com/username حتى أحاول جلب الستوري الحالية. "
        "بعدها اختَر الجودة. إذا نجح الوصول إلى الستوري، يحاول البوت إرسال كل الوسائط المتاحة.\n\n"
        "مهم: إنستغرام قد يطلب تسجيل دخول حتى للحساب العام؛ تنزيل الستوري من رابط الحساب وحده غير مضمون. "
        "البوت لا يطلب كلمة مرورك.\n"
        f"حد الإرسال الحالي: {MAX_UPLOAD_MB} MB لكل ملف. الدقات المتاحة تعتمد على المصدر وحدود تيليگرام."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "أرسل رابط فيديو أو رابط حساب إنستغرام عام، ثم اختَر الدقة.\n"
        "إذا أرسلت رابط حساب إنستغرام، يحاول البوت فتح مسار الستوري وإرسال كل العناصر التي يستطيع الوصول إليها.\n"
        "هذا لا يضمن الوصول دون تسجيل دخول لأن إنستغرام قد يفرض قيوداً.\n"
        f"حد الإرسال الحالي: {MAX_UPLOAD_MB} MB لكل ملف."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    match = URL_RE.search(message.text.strip())
    if not match:
        await message.reply_text("أرسل رابطاً يبدأ بـ https:// أو http://")
        return
    url = match.group(0).rstrip(".,!?)،؛")
    if not valid_url(url):
        await message.reply_text("الرابط غير صالح.")
        return
    normalized_url, stories_mode = normalize_instagram_profile_url(url)
    context.user_data["pending_video_url"] = normalized_url
    context.user_data["pending_stories_mode"] = stories_mode
    intro = "رابط ستوري/حساب إنستغرام" if stories_mode else "الرابط"
    await message.reply_text(f"استلمت {intro}. اختَر الجودة المطلوبة:", reply_markup=quality_keyboard())


async def handle_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    url = context.user_data.get("pending_video_url")
    stories_mode = bool(context.user_data.get("pending_stories_mode", False))
    if not url:
        await query.edit_message_text("ما عندي رابط محفوظ. أرسل الرابط مرة ثانية.")
        return

    quality_key = query.data.split(":", 1)[1]
    label, _ = QUALITY_CHOICES.get(quality_key, QUALITY_CHOICES["best"])
    await query.edit_message_text(
        "⏳ أحاول جلب الستوري الحالية من الحساب…" if stories_mode else f"⏳ جاري تنزيل الفيديو: {label}…"
    )
    await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory(prefix="hadi-media-") as tmpdir:
        file_paths, note = await asyncio.to_thread(download_media, url, tmpdir, quality_key, stories_mode)
        if not file_paths:
            await query.message.reply_text(note or "ما قدرت أنزّل الوسائط من هذا الرابط.")
            return
        sent = 0
        failed = 0
        for file_path in file_paths:
            path = Path(file_path)
            try:
                with path.open("rb") as media:
                    if path.suffix.lower() in IMAGE_EXTS:
                        await query.message.reply_photo(photo=media, caption=path.stem[:100])
                    elif path.suffix.lower() in VIDEO_EXTS:
                        await query.message.reply_video(
                            video=media, caption=f"{label}\n{path.stem[:90]}", supports_streaming=True,
                            read_timeout=240, write_timeout=240, connect_timeout=30,
                        )
                    else:
                        await query.message.reply_document(
                            document=media, caption=path.stem[:100],
                            read_timeout=240, write_timeout=240, connect_timeout=30,
                        )
                sent += 1
            except Exception:
                failed += 1
                log.exception("Telegram media upload failed: %s", path.name)
        summary = f"✅ أرسلت {sent} ملف/ملفات."
        if failed:
            summary += f" تعذّر إرسال {failed} ملف/ملفات بسبب الحجم أو حدود تيليگرام."
        if note:
            summary += "\n" + note
        await query.message.reply_text(summary)


def main() -> None:
    if not TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_quality, pattern=r"^quality:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("HADI Video Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
