import asyncio
import html
import logging
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL


# =========================
# Environment
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_NAME = os.environ.get("BOT_NAME", "تحميل الأمراء | 𝔞𝔩 𝔭𝔯𝔧𝔫𝔠𝔢𝔰")
SOURCE_LINK = os.environ.get("SOURCE_LINK", "https://t.me/alprinces")
SECOND_CHANNEL_LINK = os.environ.get("SECOND_CHANNEL_LINK", "https://t.me/Lhzat_Alomara")
DEV_LINK = os.environ.get("DEV_LINK", "https://t.me/alprinces")
START_IMAGE_URL = os.environ.get("START_IMAGE_URL", "")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "49"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")


# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("princes_downloader_bot")


# =========================
# State
# =========================
@dataclass
class PendingJob:
    user_id: int
    url: str
    title: str
    duration: Optional[int]
    thumbnail: Optional[str]
    uploader: str


PENDING: Dict[str, PendingJob] = {}


# =========================
# yt-dlp helpers
# =========================
YDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "geo_bypass": True,
    "source_address": "0.0.0.0",
    "concurrent_fragment_downloads": 8,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "restrictfilenames": True,
    "windowsfilenames": True,
    "noprogress": True,
}

QUALITY_SELECTORS = {
    "480": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]",
    "720": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
    "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]",
    "2k": "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/best[height<=1440][ext=mp4]/best[height<=1440]",
}


def hms(seconds: Optional[int]) -> str:
    if not seconds:
        return "غير معروف"
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def safe_html(text: str) -> str:
    return html.escape(text or "", quote=False)


def looks_like_url(text: str) -> bool:
    text = (text or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def normalize_info(info: dict) -> dict:
    if info and "entries" in info and info["entries"]:
        for entry in info["entries"]:
            if entry:
                return entry
    return info or {}


def extract_metadata(url: str) -> dict:
    opts = dict(YDL_BASE)
    opts.update({"skip_download": True})
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return normalize_info(info)


def find_downloaded_file(base_dir: str) -> Tuple[Optional[str], Optional[str]]:
    video_or_audio = None
    thumb = None
    for p in Path(base_dir).glob("*"):
        if p.is_file():
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and thumb is None:
                thumb = str(p)
            elif p.suffix.lower() in {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus"} and video_or_audio is None:
                video_or_audio = str(p)
    return video_or_audio, thumb


def download_media(url: str, mode: str) -> Tuple[str, Optional[str], dict, str]:
    temp_dir = tempfile.mkdtemp(prefix="princes_dl_")
    outtmpl = str(Path(temp_dir) / "%(title).80s_%(id)s.%(ext)s")

    opts = dict(YDL_BASE)
    opts.update(
        {
            "outtmpl": outtmpl,
            "writethumbnail": True,
            "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}],
        }
    )

    if mode == "audio":
        opts.update(
            {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
            }
        )
    else:
        selector = QUALITY_SELECTORS[mode]
        opts.update(
            {
                "format": selector,
                "merge_output_format": "mp4",
            }
        )

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        info = normalize_info(info)

    media_path, thumb_path = find_downloaded_file(temp_dir)
    if not media_path:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise FileNotFoundError("Downloaded file not found")

    return media_path, thumb_path, info, temp_dir


# =========================
# UI
# =========================
def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("𝑪𝑨𝑵𝑨𝑳 ✦", url=SECOND_CHANNEL_LINK)],
            [InlineKeyboardButton("𝐒𝐎𝐔𝐑𝐂𝐄 ✦", url=SOURCE_LINK), InlineKeyboardButton("𝐃𝐄𝐕 ✦", url=DEV_LINK)],
        ]
    )


def quality_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("480p", callback_data=f"dl|480|{token}"),
                InlineKeyboardButton("720p", callback_data=f"dl|720|{token}"),
            ],
            [
                InlineKeyboardButton("1080p", callback_data=f"dl|1080|{token}"),
                InlineKeyboardButton("2K", callback_data=f"dl|2k|{token}"),
            ],
            [InlineKeyboardButton("🎵 Audio", callback_data=f"dl|audio|{token}")],
            [InlineKeyboardButton("✖️ إلغاء", callback_data=f"dl|cancel|{token}")],
        ]
    )


# =========================
# Handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.type != "private":
        return

    text = (
        f"<b>{safe_html(BOT_NAME)}</b>\n\n"
        "✦ أرسل رابط المقطع من المنصات المدعومة، وبعدها أحدد لك خيارات التحميل.\n"
        "✦ الجودات المتاحة داخل الواجهة: <b>480 / 720 / 1080 / 2K / Audio</b>.\n"
        "✦ البوت يعمل في <b>الخاص فقط</b>.\n\n"
        "<b>ملاحظة:</b> إذا المنصة ما توفر دقة معيّنة أو كان الملف أكبر من حد رفع البوت، راح أبلغك مباشرة."
    )

    if START_IMAGE_URL:
        await update.effective_message.reply_photo(
            photo=START_IMAGE_URL,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=start_keyboard(),
        )
    else:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=start_keyboard(),
            disable_web_page_preview=True,
        )


async def private_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user or chat.type != "private":
        return

    text = (message.text or "").strip()
    if not looks_like_url(text):
        await message.reply_text(
            "ارسل رابط مباشر يبدأ بـ https:// وبعدها أظهر لك واجهة الجودة والصوت.",
            reply_markup=start_keyboard(),
            disable_web_page_preview=True,
        )
        return

    status = await message.reply_text("⏳ جاري قراءة الرابط وتحضير الواجهة الفخمة...")
    try:
        info = await asyncio.to_thread(extract_metadata, text)
        title = info.get("title") or "بدون عنوان"
        duration = info.get("duration")
        thumbnail = info.get("thumbnail")
        uploader = info.get("uploader") or info.get("channel") or "Unknown"

        token = secrets.token_urlsafe(8)
        PENDING[token] = PendingJob(
            user_id=user.id,
            url=text,
            title=title,
            duration=duration,
            thumbnail=thumbnail,
            uploader=uploader,
        )

        caption = (
            "✨ <b>واجهة التحميل الجاهزة</b>\n\n"
            f"<b>العنوان:</b> {safe_html(title)}\n"
            f"<b>المدة:</b> {safe_html(hms(duration))}\n"
            f"<b>الناشر:</b> {safe_html(uploader)}\n\n"
            "اختر الجودة أو حمّل الملف كصوت واضح ونقي."
        )

        if thumbnail:
            await message.reply_photo(
                photo=thumbnail,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=quality_keyboard(token),
            )
            await status.delete()
        else:
            await status.edit_text(
                caption,
                parse_mode=ParseMode.HTML,
                reply_markup=quality_keyboard(token),
                disable_web_page_preview=True,
            )
    except Exception as exc:
        logger.exception("Metadata extraction failed")
        await status.edit_text(f"❌ تعذر قراءة الرابط أو المنصة غير مدعومة.\n<code>{safe_html(str(exc))}</code>", parse_mode=ParseMode.HTML)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    try:
        _, mode, token = query.data.split("|", 2)
    except ValueError:
        return

    job = PENDING.get(token)
    if not job:
        await query.answer("انتهت صلاحية هذا الطلب. ارسل الرابط من جديد.", show_alert=True)
        return

    user = update.effective_user
    if not user or user.id != job.user_id:
        await query.answer("هذه الأزرار تخص صاحب الطلب فقط.", show_alert=True)
        return

    if mode == "cancel":
        PENDING.pop(token, None)
        await query.edit_message_caption("تم إلغاء الطلب ✖️") if query.message and query.message.photo else await query.edit_message_text("تم إلغاء الطلب ✖️")
        return

    msg = query.message
    if msg:
        try:
            if msg.photo:
                await query.edit_message_caption(
                    f"⏬ جاري التحميل الآن: <b>{safe_html(job.title)}</b>\n<b>الوضع:</b> {safe_html(mode.upper())}",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.edit_message_text(
                    f"⏬ جاري التحميل الآن: <b>{safe_html(job.title)}</b>\n<b>الوضع:</b> {safe_html(mode.upper())}",
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            pass

    await context.bot.send_chat_action(chat_id=job.user_id, action=ChatAction.UPLOAD_DOCUMENT)

    media_path = thumb_path = temp_dir = None
    try:
        media_path, thumb_path, info, temp_dir = await asyncio.to_thread(download_media, job.url, mode)

        size_mb = os.path.getsize(media_path) / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            raise RuntimeError(
                f"حجم الملف بعد التحميل {size_mb:.1f}MB، وهذا أعلى من الحد المسموح للبوت ({MAX_UPLOAD_MB}MB). اختر جودة أقل أو صوت."
            )

        title = info.get("title") or job.title
        uploader = info.get("uploader") or job.uploader
        caption = (
            f"<b>{safe_html(BOT_NAME)}</b>\n"
            f"<b>العنوان:</b> {safe_html(title)}\n"
            f"<b>بواسطة:</b> {safe_html(uploader)}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("𝑪𝑨𝑵𝑨𝑳 ✦", url=SECOND_CHANNEL_LINK)],
                [InlineKeyboardButton("𝐒𝐎𝐔𝐑𝐂𝐄 ✦", url=SOURCE_LINK), InlineKeyboardButton("𝐃𝐄𝐕 ✦", url=DEV_LINK)],
            ]
        )

        if mode == "audio":
            await context.bot.send_audio(
                chat_id=job.user_id,
                audio=media_path,
                caption=caption,
                parse_mode=ParseMode.HTML,
                title=title[:64],
                performer=uploader[:64],
                thumbnail=thumb_path if thumb_path and Path(thumb_path).exists() else None,
                reply_markup=keyboard,
                read_timeout=120,
                write_timeout=120,
            )
        else:
            await context.bot.send_video(
                chat_id=job.user_id,
                video=media_path,
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                thumbnail=thumb_path if thumb_path and Path(thumb_path).exists() else None,
                reply_markup=keyboard,
                read_timeout=180,
                write_timeout=180,
            )

        if msg:
            done_text = "✅ تم التحميل والإرسال بنجاح."
            try:
                await query.edit_message_caption(done_text) if msg.photo else await query.edit_message_text(done_text)
            except Exception:
                pass

    except Exception as exc:
        logger.exception("Download or upload failed")
        fail_text = f"❌ فشل التحميل أو الإرسال.\n<code>{safe_html(str(exc))}</code>"
        if msg:
            try:
                await query.edit_message_caption(fail_text, parse_mode=ParseMode.HTML) if msg.photo else await query.edit_message_text(fail_text, parse_mode=ParseMode.HTML)
            except Exception:
                await context.bot.send_message(job.user_id, fail_text, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_message(job.user_id, fail_text, parse_mode=ParseMode.HTML)
    finally:
        PENDING.pop(token, None)
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


# =========================
# Main
# =========================
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_link_handler)
    )
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    app = build_app()
    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
