import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Telegram's standard Bot API upload limit is around 50 MB for video/document uploads.
# Keep a small buffer to reduce failed uploads.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "48"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("hadi-video-bot")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def download_video(url: str, folder: str) -> tuple[str | None, str | None]:
    output_template = str(Path(folder) / "%(title).70s-%(id)s.%(ext)s")
    # Prefer the best combined format that fits the Bot API upload limit.
    # If the extractor can estimate size, yt-dlp uses that to choose a smaller format.
    # We do not force a low resolution: highest quality fitting the limit is preferred.
    ydl_opts = {
        "format": (
            f"best[filesize<={MAX_UPLOAD_BYTES}]/"
            f"best[filesize_approx<={MAX_UPLOAD_BYTES}]/best"
        ),
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 25,
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None, "ما قدرت أستخرج معلومات الفيديو من هذا الرابط."
            candidates = []
            requested = info.get("requested_downloads") or []
            for item in requested:
                fp = item.get("filepath")
                if fp:
                    p = Path(fp)
                    if p.exists() and p.is_file():
                        candidates.append(p)
            if not candidates:
                candidates = [
                    p for p in Path(folder).iterdir()
                    if p.is_file() and not p.name.endswith((".part", ".ytdl"))
                ]
            candidates = [p for p in candidates if p.exists() and p.is_file()]
            if not candidates:
                return None, "ما حصلت ملف فيديو جاهز بعد التنزيل."
            candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
            path = candidates[0]
            if path.stat().st_size > MAX_UPLOAD_BYTES:
                return None, (
                    f"تم تنزيل الفيديو لكن حجمه أكبر من حد الإرسال المضبوط ({MAX_UPLOAD_MB} MB). "
                    "جرّب رابطاً آخر أو نحتاج نضيف خادماً وسيطاً لإرسال الملفات الكبيرة."
                )
            return str(path), None
    except yt_dlp.utils.DownloadError as exc:
        log.warning("yt-dlp error: %s", exc)
        return None, (
            "تعذّر تنزيل الرابط. قد يكون الموقع غير مدعوم حالياً، أو الفيديو خاصاً، "
            "أو يتطلب تسجيل دخول، أو يمنع التنزيل. جرّب رابطاً عاماً آخر."
        )
    except Exception:
        log.exception("Unexpected download error")
        return None, "صار خطأ غير متوقع. راجع سجل الاستضافة أو جرّب رابطاً آخر."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "هلا بيك 👋\n"
        "أرسل رابط فيديو عام من TikTok أو Instagram أو YouTube أو Facebook أو X أو Pinterest.\n"
        "راح أحاول تنزيل أفضل جودة متاحة ضمن حد إرسال تيليگرام.\n\n"
        "مهم: المواقع تتغير، لذلك ما أضمن كل رابط. ما أتجاوز الفيديوهات الخاصة أو DRM، "
        "ولا أزيل العلامة المائية من الملف؛ أرسل النسخة الأصلية التي يوفّرها الموقع."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "طريقة الاستخدام: أرسل رابط الفيديو العام برسالة واحدة.\n"
        f"حد حجم الملف الحالي: {MAX_UPLOAD_MB} MB.\n"
        "إذا فشل رابط معيّن، فهذا لا يعني أن كل روابط الموقع لا تعمل؛ جرّب رابطاً عاماً آخر."
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

    status = await message.reply_text("⏳ جاري تنزيل الفيديو بأفضل جودة متاحة…")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory(prefix="tg-video-") as tmpdir:
        file_path, error = await asyncio.to_thread(download_video, url, tmpdir)
        if error:
            await status.edit_text(error)
            return
        path = Path(file_path)
        try:
            await status.edit_text("✅ اكتمل التنزيل، جاري الإرسال…")
            with path.open("rb") as media:
                # MP4/MOV are sent as video; other formats as document to avoid Telegram transcoding issues.
                if path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
                    await message.reply_video(
                        video=media,
                        caption=f"تم التنزيل: {path.stem[:120]}",
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                    )
                else:
                    await message.reply_document(
                        document=media,
                        caption=f"تم التنزيل: {path.stem[:120]}",
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                    )
            await status.delete()
        except Exception:
            log.exception("Telegram upload failed")
            await status.edit_text(
                "نزل الفيديو لكن تعذّر إرساله عبر تيليگرام. غالباً الحجم أو صيغة الملف أو حد الإرسال."
            )


def main() -> None:
    if not TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
