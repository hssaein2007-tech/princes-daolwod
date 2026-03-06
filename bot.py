import asyncio
import logging
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    Update,
)
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
from yt_dlp.utils import DownloadError

load_dotenv()

# =========================
# الإعدادات
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CANAL_URL = os.getenv("CANAL_URL", "https://t.me/your_channel")
SOURCE_URL = os.getenv("SOURCE_URL", "https://github.com/yourname/princes-bot")
DEVELOPER_URL = os.getenv("DEVELOPER_URL", "https://t.me/your_username")
STATIC_IMAGE_URL = os.getenv("STATIC_IMAGE_URL", "")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/princes_downloads"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "49"))
MAX_ACTIVE_DOWNLOADS = int(os.getenv("MAX_ACTIVE_DOWNLOADS", "2"))
MAX_AGE_SECONDS = int(os.getenv("TASK_TTL_SECONDS", str(60 * 20)))

BOT_TITLE = "الأمراء | 𝔞𝔩 𝔭𝔯𝔦𝔫𝔠𝔢𝔰"
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
TG_USERNAME_RE = re.compile(r"^@([A-Za-z0-9_]{5,32})$")
INSTAGRAM_PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?(?:\?.*)?$",
    re.IGNORECASE,
)
INSTAGRAM_MEDIA_PATHS = (
    "/p/",
    "/reel/",
    "/reels/",
    "/tv/",
    "/stories/",
    "/share/",
)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("princes_bot")

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_ACTIVE_DOWNLOADS)


def _safe_filename_piece(text: str, limit: int = 80) -> str:
    text = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", text or "file")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or "file"


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return "غير معروف"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _extract_first_url(text: str) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0) if match else None


def _is_instagram_profile_url(url: str) -> bool:
    if "instagram.com" not in url.lower():
        return False
    lowered = url.lower()
    return not any(path in lowered for path in INSTAGRAM_MEDIA_PATHS)


def _quality_format_selector(quality: str) -> str:
    selectors = {
        "480": "bv*[height<=480]+ba/b[height<=480]/best[height<=480]",
        "720": "bv*[height<=720]+ba/b[height<=720]/best[height<=720]",
        "1080": "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]",
        "best": "bv*+ba/b",
    }
    return selectors.get(quality, selectors["best"])


def _base_ydl_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "ignoreerrors": False,
        "socket_timeout": 25,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            )
        },
    }


def extract_media_info(url: str) -> dict[str, Any]:
    opts = _base_ydl_opts() | {
        "skip_download": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("تعذر قراءة الرابط. تأكد أن الرابط عام ومباشر.")

    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries", []) if e]
        if not entries:
            raise ValueError("تم العثور على قائمة بدون عناصر قابلة للتحميل.")
        if len(entries) > 1:
            raise ValueError("أرسل رابط فيديو/ريل/منشور واحد فقط، وليس قائمة تشغيل أو صفحة كاملة.")
        info = entries[0]

    if info.get("is_live"):
        raise ValueError("الرابط يبدو بثًا مباشرًا الآن، وهذا القالب مخصص للمقاطع/الريلز/المنشورات المحفوظة.")

    return info


def list_heights(info: dict[str, Any]) -> list[int]:
    heights: set[int] = set()
    for fmt in info.get("formats", []) or []:
        h = fmt.get("height")
        if isinstance(h, int):
            heights.add(h)
    return sorted(heights)


def download_media(url: str, quality: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
    workdir.mkdir(parents=True, exist_ok=True)
    before = {p.resolve() for p in workdir.glob("**/*") if p.is_file()}
    opts = _base_ydl_opts() | {
        "format": _quality_format_selector(quality),
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "%(title).80B [%(id)s].%(ext)s"),
        "nopart": True,
        "overwrites": True,
    }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    after = [p.resolve() for p in workdir.glob("**/*") if p.is_file() and p.resolve() not in before]
    candidates = [
        p for p in after
        if p.suffix.lower() not in {".part", ".ytdl", ".json", ".vtt", ".srt", ".tmp", ".jpg", ".webp", ".png"}
    ]
    if not candidates:
        # fallback: pick the largest file موجود في المجلد
        candidates = [p.resolve() for p in workdir.glob("**/*") if p.is_file()]
        candidates = [
            p for p in candidates
            if p.suffix.lower() not in {".part", ".ytdl", ".json", ".vtt", ".srt", ".tmp", ".jpg", ".webp", ".png"}
        ]

    if not candidates:
        raise FileNotFoundError("اكتمل التحميل لكن لم أجد الملف الناتج.")

    final_file = max(candidates, key=lambda p: p.stat().st_size)
    return final_file, info


def build_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("CANAL", url=CANAL_URL), InlineKeyboardButton("𝐒𝐨𝐮𝐫𝐜𝐞 𝐏𝐫𝐢𝐧𝐜𝐞𝐬™", url=SOURCE_URL)],
            [InlineKeyboardButton("المطور", url=DEVELOPER_URL)],
            [InlineKeyboardButton("الأوامر", callback_data="show_help")],
        ]
    )


def build_quality_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("480P", callback_data=f"dl:{token}:480"),
                InlineKeyboardButton("720P", callback_data=f"dl:{token}:720"),
            ],
            [
                InlineKeyboardButton("1080P", callback_data=f"dl:{token}:1080"),
                InlineKeyboardButton("أعلى جودة", callback_data=f"dl:{token}:best"),
            ],
        ]
    )


def build_ig_profile_stub(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("تحميل الستوريات العامة", callback_data=f"igstub:{username}:stories"),
            ],
            [
                InlineKeyboardButton("تحميل الهايلايت/المنشورات", callback_data=f"igstub:{username}:highlights"),
            ],
        ]
    )


def help_text() -> str:
    return (
        "<b>طريقة الاستخدام</b>\n\n"
        "1) أرسل رابط <b>فيديو/ريل/منشور عام</b> من أي موقع يدعمه yt-dlp.\n"
        "2) البوت يفحص الرابط تلقائيًا.\n"
        "3) تظهر لك أزرار الجودة: <b>480P / 720P / 1080P / أعلى جودة</b>.\n"
        "4) اختر الجودة، وسيبدأ التحميل ثم الإرسال.\n\n"
        "<b>أمثلة روابط مناسبة:</b>\n"
        "• YouTube\n• Facebook\n• Instagram (روابط مباشرة للمنشور/الريل/الستوري العامة)\n"
        "• X/Twitter\n• TikTok\n• Kick\n• Telegram public post links\n\n"
        "<b>ملاحظات مهمة:</b>\n"
        "• لا يدعم هذا القالب تجاوز DRM أو تنزيل المحتوى الخاص/المدفوع أو المحمي.\n"
        "• روابط الصفحات الكاملة/البروفايلات ليست مضمونة. الأفضل دائمًا إرسال الرابط المباشر للمحتوى.\n"
        "• لو كان الملف كبيرًا جدًا، اختر جودة أقل أو استخدم Local Bot API لاحقًا."
    )


def start_caption() -> str:
    return (
        f"<b>{BOT_TITLE}</b>\n\n"
        "بوت تحميل سريع للروابط <b>العامة</b> مع اختيار الجودة وواجهة أنيقة.\n"
        "أرسل الآن رابط المقطع، وأنا أجهزه لك مباشرة."
    )


async def send_start_ui(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = build_start_keyboard()
    text = start_caption()
    if update.effective_message is None:
        return

    if STATIC_IMAGE_URL:
        try:
            await update.effective_message.reply_photo(
                photo=STATIC_IMAGE_URL,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except Exception as exc:
            logger.warning("Failed to send start photo: %s", exc)

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=False,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_start_ui(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(help_text(), parse_mode=ParseMode.HTML)


async def setup_commands(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "واجهة البوت الرئيسية"),
            BotCommand("help", "شرح الاستخدام"),
        ]
    )


async def on_startup(app: Application) -> None:
    app.bot_data.setdefault("tasks", {})
    await setup_commands(app)
    logger.info("Bot is ready")


def cleanup_old_tasks(tasks: dict[str, dict[str, Any]]) -> None:
    now = asyncio.get_event_loop().time()
    expired = [token for token, data in tasks.items() if now - data.get("created_at", now) > MAX_AGE_SECONDS]
    for token in expired:
        tasks.pop(token, None)


async def handle_show_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    await query.message.reply_text(help_text(), parse_mode=ParseMode.HTML)


async def handle_instagram_profile_stub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    parts = query.data.split(":")
    username = parts[1] if len(parts) > 1 else "instagram"
    await query.message.reply_text(
        (
            f"<b>@{username}</b>\n\n"
            "روابط البروفايل/الهايلايت في إنستغرام ليست مستقرة حاليًا في هذا القالب.\n"
            "أرسل <b>الرابط المباشر</b> للريل أو المنشور أو الستوري العامة، وسأحمّلها لك فورًا."
        ),
        parse_mode=ParseMode.HTML,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()

    # إذا أرسل المستخدم @username تيليجرام
    tg_match = TG_USERNAME_RE.fullmatch(text)
    if tg_match:
        username = tg_match.group(1)
        await message.reply_text(
            (
                f'<a href="https://t.me/{username}">هذا الشخص ماعنده ستوريات…🚫</a>\n\n'
                "<i>ملاحظة:</i> Telegram Bot API لا يتيح للبوتات العامة قراءة ستوريات أي مستخدم عشوائيًا عبر المعرف فقط."
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    url = _extract_first_url(text)
    if not url:
        await message.reply_text(
            "أرسل رابط فيديو/ريل/منشور عام، أو استخدم /start لعرض الواجهة.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Instagram profile URL stub
    if _is_instagram_profile_url(url):
        match = INSTAGRAM_PROFILE_RE.match(url)
        username = match.group(1) if match else "instagram"
        await message.reply_text(
            (
                f"تم التعرف على رابط بروفايل إنستغرام: <b>@{username}</b>\n"
                "اختر من الأزرار بالأسفل."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=build_ig_profile_stub(username),
        )
        return

    progress = await message.reply_text("جاري فحص الرابط واستخراج المعلومات…")
    try:
        info = await asyncio.to_thread(extract_media_info, url)
    except DownloadError as exc:
        await progress.edit_text(f"تعذر قراءة الرابط:\n<code>{_safe_filename_piece(str(exc), 160)}</code>", parse_mode=ParseMode.HTML)
        return
    except Exception as exc:
        await progress.edit_text(f"حدث خطأ أثناء فحص الرابط:\n<code>{_safe_filename_piece(str(exc), 160)}</code>", parse_mode=ParseMode.HTML)
        return

    tasks: dict[str, dict[str, Any]] = context.application.bot_data.setdefault("tasks", {})
    cleanup_old_tasks(tasks)
    token = secrets.token_hex(4)
    tasks[token] = {
        "url": url,
        "user_id": update.effective_user.id if update.effective_user else 0,
        "chat_id": update.effective_chat.id if update.effective_chat else 0,
        "title": info.get("title", "بدون عنوان"),
        "site": info.get("extractor_key") or info.get("webpage_url_domain") or "Unknown",
        "duration": info.get("duration"),
        "created_at": asyncio.get_event_loop().time(),
    }

    heights = list_heights(info)
    heights_text = ", ".join(f"{h}p" for h in heights[:8]) if heights else "غير معروف"
    title = _safe_filename_piece(info.get("title", "بدون عنوان"), 120)
    caption = (
        f"<b>تم التعرف على الرابط بنجاح</b>\n\n"
        f"<b>العنوان:</b> {title}\n"
        f"<b>المصدر:</b> {tasks[token]['site']}\n"
        f"<b>المدة:</b> {_format_duration(info.get('duration'))}\n"
        f"<b>الجودات المرصودة:</b> {heights_text}\n\n"
        "اختر الجودة المناسبة:"
    )
    await progress.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=build_quality_keyboard(token))


async def handle_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    _, token, quality = query.data.split(":")
    tasks: dict[str, dict[str, Any]] = context.application.bot_data.setdefault("tasks", {})
    task = tasks.get(token)

    if not task:
        await query.edit_message_text("انتهت صلاحية هذا الطلب. أرسل الرابط مرة أخرى.")
        return

    user_id = update.effective_user.id if update.effective_user else 0
    if user_id != task.get("user_id"):
        await query.answer("هذا الزر ليس لك.", show_alert=True)
        return

    await query.edit_message_text(
        (
            f"<b>جاري التحميل الآن…</b>\n\n"
            f"<b>الجودة:</b> {quality.upper()}\n"
            f"<b>العنوان:</b> {_safe_filename_piece(task.get('title', 'media'), 100)}"
        ),
        parse_mode=ParseMode.HTML,
    )

    workdir = DOWNLOAD_DIR / f"{user_id}_{token}_{quality}"
    try:
        async with DOWNLOAD_SEMAPHORE:
            await context.bot.send_chat_action(chat_id=task["chat_id"], action=ChatAction.UPLOAD_DOCUMENT)
            final_file, info = await asyncio.to_thread(download_media, task["url"], quality, workdir)

        file_size = final_file.stat().st_size
        limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
        if file_size > limit_bytes:
            await context.bot.send_message(
                chat_id=task["chat_id"],
                text=(
                    f"اكتمل التحميل لكن حجم الملف { _format_size(file_size) } أكبر من الحد المضبوط للبوت ({MAX_FILE_SIZE_MB} MB).\n"
                    "اختر جودة أقل، أو شغّل Local Bot API إذا أردت رفع ملفات أكبر."
                ),
            )
            return

        title = _safe_filename_piece(info.get("title", task.get("title", "media")), 80)
        caption = (
            f"<b>{title}</b>\n"
            f"<b>الجودة:</b> {quality.upper()}\n"
            f"<b>الحجم:</b> {_format_size(file_size)}"
        )

        suffix = final_file.suffix.lower()
        if suffix == ".mp4":
            await context.bot.send_chat_action(chat_id=task["chat_id"], action=ChatAction.UPLOAD_VIDEO)
            with final_file.open("rb") as fp:
                await context.bot.send_video(
                    chat_id=task["chat_id"],
                    video=fp,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                    pool_timeout=120,
                )
        else:
            await context.bot.send_chat_action(chat_id=task["chat_id"], action=ChatAction.UPLOAD_DOCUMENT)
            with final_file.open("rb") as fp:
                await context.bot.send_document(
                    chat_id=task["chat_id"],
                    document=fp,
                    filename=final_file.name,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                    pool_timeout=120,
                )

        await context.bot.send_message(chat_id=task["chat_id"], text="تم الإرسال بنجاح ✅")
    except DownloadError as exc:
        await context.bot.send_message(
            chat_id=task["chat_id"],
            text=f"فشل التحميل:\n<code>{_safe_filename_piece(str(exc), 180)}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("Download failed")
        await context.bot.send_message(
            chat_id=task["chat_id"],
            text=f"حدث خطأ غير متوقع:\n<code>{_safe_filename_piece(str(exc), 180)}</code>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        tasks.pop(token, None)
        shutil.rmtree(workdir, ignore_errors=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Exception while handling an update:", exc_info=context.error)


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it as an environment variable.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.post_init = on_startup

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_show_help_callback, pattern=r"^show_help$"))
    app.add_handler(CallbackQueryHandler(handle_instagram_profile_stub, pattern=r"^igstub:"))
    app.add_handler(CallbackQueryHandler(handle_download_callback, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    app = build_app()
    logger.info("Starting Princes bot with polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
