from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import yt_dlp
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "الأمراء | 𝔞𝔩 𝔭𝔯𝔧𝔫𝔠𝔢𝔰")
CANAL_URL = os.getenv("CANAL_URL", "https://t.me/example")
SOURCE_URL = os.getenv("SOURCE_URL", "https://github.com/example/princes-bot")
DEVELOPER_URL = os.getenv("DEVELOPER_URL", "https://t.me/example_dev")
START_PHOTO_URL = os.getenv("START_PHOTO_URL", "https://example.com/your-static-image.jpg")

COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/princes_downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Optional Instagram session support
IG_LOGIN_USER = os.getenv("IG_LOGIN_USER", "").strip()
IG_SESSIONFILE = os.getenv("IG_SESSIONFILE", "").strip()

# Optional TikTok tuning
TIKTOK_DEVICE_ID = os.getenv("TIKTOK_DEVICE_ID", "").strip()
TIKTOK_APP_INFO = os.getenv("TIKTOK_APP_INFO", "").strip()
TIKTOK_API_HOSTNAME = os.getenv(
    "TIKTOK_API_HOSTNAME",
    "api16-normal-c-useast1a.tiktokv.com",
).strip()

# Optional placeholder for future Telegram-user-session integration
ENABLE_TELEGRAM_STORIES = os.getenv("ENABLE_TELEGRAM_STORIES", "false").lower() == "true"

# Keep callback payloads tiny by storing state server-side.
REQUEST_CACHE: dict[str, dict[str, Any]] = {}

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
IG_PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?(?:\?.*)?$",
    re.IGNORECASE,
)
TG_USERNAME_RE = re.compile(r"^@([A-Za-z0-9_]{5,32})$")
TIKTOK_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:m\.)?(?:tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)/",
    re.IGNORECASE,
)

SUPPORTED_QUALITY_LABELS = [
    (480, "480P"),
    (720, "720P"),
    (1080, "1080P"),
    (1440, "2K"),
]


@dataclass
class MediaInfo:
    url: str
    title: str
    extractor: str
    webpage_url: str
    available_heights: list[int]


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args):
        return


def start_health_server_if_needed() -> None:
    port = os.getenv("PORT")
    if not port:
        return

    def _serve() -> None:
        server = HTTPServer(("0.0.0.0", int(port)), _HealthHandler)
        server.serve_forever()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()


def get_logger() -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    return logging.getLogger("princes-bot")


logger = get_logger()


def help_text() -> str:
    return (
        "<b>طريقة الاستخدام</b>\n\n"
        "1) <b>أرسل رابط أي مقطع عام</b> من المواقع المدعومة.\n"
        "سيقرأه البوت تلقائيًا ويعرض لك الجودة المتاحة مثل 480P / 720P / 1080P / 2K.\n\n"
        "2) <b>أرسل رابط صفحة إنستغرام</b> بهذا الشكل:\n"
        "<code>https://www.instagram.com/username/</code>\n"
        "سيظهر لك زران: تحميل الستوري أو تحميل جميع الهايلايت.\n"
        "<i>مهم:</i> هذه الميزة تحتاج جلسة إنستغرام صالحة مفعلة في السيرفر.\n\n"
        "3) <b>أرسل معرف تيليجرام</b> بهذا الشكل:\n"
        "<code>@username</code>\n"
        "ميزة ستوريات تيليجرام لأي مستخدم لا تعمل عبر Bot API وحده، وتحتاج ربط جلسة مستخدم منفصلة إذا رغبت بتفعيلها لاحقًا.\n\n"
        "4) إذا كان الرابط خاصًا أو يحتاج تسجيل دخول أو Cookies، فعّل ملف الكوكيز في متغيرات البيئة.\n\n"
        "<b>أوامر البوت</b>\n"
        "/start - الواجهة الرئيسية\n"
        "/help - شرح الاستخدام\n"
        "/about - معلومات سريعة"
    )


def start_caption() -> str:
    return (
        f"<b>{html.escape(BOT_NAME)}</b>\n\n"
        "بوت تحميل سريع للمقاطع العامة من مواقع متعددة مثل يوتيوب و X وفيسبوك وإنستغرام وKick وShahid وغيرها عبر محرك تنزيل واحد.\n"
        "أرسل رابط المقطع مباشرة، أو رابط صفحة إنستغرام، أو اكتب /help لمعرفة كل شيء."
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("CANAL", url=CANAL_URL),
                InlineKeyboardButton("𝐒𝐨𝐮𝐫𝐜𝐞 𝐏𝐫𝐢𝐧𝐜𝐞𝐬™", url=SOURCE_URL),
            ],
            [InlineKeyboardButton("المطور", url=DEVELOPER_URL)],
            [InlineKeyboardButton("الأوامر", callback_data="menu:help")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="menu:home")]])


def beautiful_empty_story_message() -> str:
    return '<a href="https://t.me/share/url">هذا الشخص ماعنده ستوريات…🚫</a>'


def short_token() -> str:
    return secrets.token_hex(4)


def is_tiktok_url(url: str) -> bool:
    return bool(TIKTOK_URL_RE.match(url.strip()))


def build_tiktok_extractor_args() -> dict[str, list[str]]:
    args: dict[str, list[str]] = {
        "api_hostname": [TIKTOK_API_HOSTNAME],
    }
    if TIKTOK_DEVICE_ID:
        args["device_id"] = [TIKTOK_DEVICE_ID]
    if TIKTOK_APP_INFO:
        args["app_info"] = [TIKTOK_APP_INFO]
    return args


def humanize_ydlp_error(url: str, exc: Exception) -> str:
    raw = str(exc).strip()
    lower = raw.lower()

    if is_tiktok_url(url) and ("10231" in raw or "video not available" in lower):
        return (
            "تيك توك رفض هذا الرابط من جهة السيرفر أو يحتاج كوكيز/هوية جهاز أحدث.\n\n"
            "الحلول المقترحة:\n"
            "1) حدّث cookies.txt من حساب TikTok مسجل دخول.\n"
            "2) تأكد أن COOKIES_FILE يشير للمسار الصحيح.\n"
            "3) إذا استمرت المشكلة أضف PROXY_URL.\n"
            "4) اختياريًا أضف TIKTOK_DEVICE_ID و TIKTOK_APP_INFO.\n\n"
            f"الخطأ الأصلي:\n{raw}"
        )

    if "cookies" in lower or "login required" in lower or "authentication" in lower:
        return (
            "هذا الرابط يحتاج تسجيل دخول أو كوكيز صالحة.\n"
            f"الخطأ الأصلي:\n{raw}"
        )

    return raw
opts = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "retries": 10,
    "fragment_retries": 10,
    "extractor_retries": 5,
    "socket_timeout": 30,

    "impersonate": "chrome110",
    "forceipv4": True,

    "http_headers": {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.tiktok.com/"
    }
}
        # مهم مع TikTok
        "impersonate": "chrome",
        "forceipv4": True,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
            "Referer": "https://www.tiktok.com/",
        },
    }

    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE

    if PROXY_URL:
        opts["proxy"] = PROXY_URL

    if is_tiktok_url(url):
        opts["extractor_args"] = {
            "tiktok": build_tiktok_extractor_args(),
        }
        opts["format_sort"] = ["res", "codec:h264", "ext:mp4:m4a"]

    if extra:
        opts.update(extra)

    return opts


def run_ytdlp_extract(
    url: str,
    *,
    download: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = [build_ytdlp_opts(extra=extra, url=url)]

    if is_tiktok_url(url) and COOKIES_FILE:
        second = build_ytdlp_opts(extra=extra, url=url)
        second.pop("cookiefile", None)
        attempts.append(second)

    last_exc: Exception | None = None

    for opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                if not info:
                    raise RuntimeError("تعذر قراءة الرابط.")
                return info
        except Exception as exc:
            last_exc = exc

    raise RuntimeError(humanize_ydlp_error(url, last_exc or RuntimeError("Unknown error")))


def extract_media_info(url: str) -> MediaInfo:
    info = run_ytdlp_extract(url, download=False)

    if info.get("_type") == "url":
        raise RuntimeError("هذا الرابط يحيل إلى رابط آخر ولم أستطع قراءته بشكل مباشر.")

    formats = info.get("formats") or []
    heights = sorted(
        {
            int(fmt["height"])
            for fmt in formats
            if fmt.get("height")
            and fmt.get("vcodec") != "none"
            and not str(fmt.get("format_note", "")).lower().startswith("audio")
        }
    )

    title = str(info.get("title") or "Untitled")[:180]
    extractor = str(info.get("extractor_key") or info.get("extractor") or "Unknown")
    webpage_url = str(info.get("webpage_url") or url)

    return MediaInfo(
        url=url,
        title=title,
        extractor=extractor,
        webpage_url=webpage_url,
        available_heights=heights,
    )


def quality_keyboard(token: str, info: MediaInfo) -> InlineKeyboardMarkup:
    max_height = max(info.available_heights) if info.available_heights else 0
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for height, label in SUPPORTED_QUALITY_LABELS:
        if max_height >= height:
            row.append(InlineKeyboardButton(label, callback_data=f"dl:{token}:{height}"))
            if len(row) == 2:
                rows.append(row)
                row = []

    if row:
        rows.append(row)

    if not rows:
        rows.append([InlineKeyboardButton("أفضل جودة متاحة", callback_data=f"dl:{token}:0")])

    rows.append([InlineKeyboardButton("إلغاء", callback_data=f"dlcancel:{token}")])
    return InlineKeyboardMarkup(rows)


def format_selector(url: str, max_height: int) -> str:
    if is_tiktok_url(url):
        if max_height and max_height > 0:
            return f"best[height<={max_height}][ext=mp4]/best[height<={max_height}]/best"
        return "best[ext=mp4]/best"

    if max_height and max_height > 0:
        return (
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}][ext=mp4]/"
            f"best[height<={max_height}]/best"
        )

    return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"


def download_media(url: str, max_height: int = 0) -> tuple[Path, str]:
    tempdir = Path(tempfile.mkdtemp(prefix="princes_media_", dir=str(DOWNLOAD_DIR)))
    outtmpl = str(tempdir / "%(title).90s [%(id)s].%(ext)s")

    info = run_ytdlp_extract(
        url,
        download=True,
        extra={
            "outtmpl": outtmpl,
            "format": format_selector(url, max_height),
            "merge_output_format": "mp4",
            "windowsfilenames": True,
        },
    )

    title = str(info.get("title") or "Untitled")[:180]

    media_files = [
        p
        for p in tempdir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".mp3", ".m4a"}
    ]
    if not media_files:
        raise RuntimeError("تمت العملية لكن لم أجد ملفًا قابلاً للإرسال.")

    chosen = max(media_files, key=lambda p: p.stat().st_size)
    return chosen, title


def run_instaloader_cli(username: str, mode: str) -> list[Path]:
    if not IG_LOGIN_USER or not IG_SESSIONFILE or not Path(IG_SESSIONFILE).exists():
        raise RuntimeError("ميزة إنستغرام المتقدمة تحتاج IG_LOGIN_USER و IG_SESSIONFILE صالحين داخل Railway.")

    target_dir = Path(tempfile.mkdtemp(prefix=f"ig_{mode}_", dir=str(DOWNLOAD_DIR)))

    cmd = [
        "instaloader",
        "--quiet",
        "--sessionfile",
        IG_SESSIONFILE,
        "--login",
        IG_LOGIN_USER,
        "--dirname-pattern",
        str(target_dir),
        "--filename-pattern",
        "{date_utc:%Y-%m-%d_%H-%M-%S}",
        "--title-pattern",
        "{date_utc:%Y-%m-%d_%H-%M-%S}_{typename}",
        "--no-posts",
        "--no-profile-pic",
        "--no-captions",
        "--no-metadata-json",
        "--no-compress-json",
        "--abort-on=400,401,429",
        "--request-timeout=90",
    ]

    if mode == "stories":
        cmd.append("--stories")
    elif mode == "highlights":
        cmd.append("--highlights")
    else:
        raise RuntimeError("نوع طلب إنستغرام غير معروف.")

    cmd.append(username)

    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)

    if proc.returncode not in (0, 1):
        err = (proc.stderr or proc.stdout or "خطأ غير معروف").strip()[-800:]
        raise RuntimeError(f"فشل تحميل محتوى إنستغرام: {err}")

    files = [
        p
        for p in target_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4", ".webm", ".mov"}
    ]
    return sorted(files)


async def safe_delete(path: Path | None) -> None:
    if not path:
        return
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
            root = path.parent
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
        elif path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        logger.exception("Cleanup failed for %s", path)


async def send_media_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_path: Path,
    caption: str,
) -> None:
    size_mb = file_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        await update.effective_message.reply_text(
            f"تم التحميل لكن حجم الملف {size_mb:.1f}MB وتجاوز الحد المضبوط في البوت ({MAX_UPLOAD_MB}MB)."
        )
        return

    suffix = file_path.suffix.lower()
    mime, _ = mimetypes.guess_type(str(file_path))

    with file_path.open("rb") as fh:
        if suffix == ".mp4":
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=fh,
                caption=caption,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
            )
        elif mime and mime.startswith("image/"):
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=fh,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
            )
        else:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=fh,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
            )


async def send_home_message(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if START_PHOTO_URL and START_PHOTO_URL.startswith("http"):
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=START_PHOTO_URL,
                caption=start_caption(),
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
            )
            return
        except Exception:
            logger.exception("Failed to send start image; falling back to text")

    await context.bot.send_message(
        chat_id=chat_id,
        text=start_caption(),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
        disable_web_page_preview=True,
    )


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_home_message(update.effective_chat.id, context)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_home(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        help_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
    )


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        (
            f"<b>{html.escape(BOT_NAME)}</b>\n\n"
            "نسخة Railway جاهزة مع python-telegram-bot + yt-dlp + ffmpeg.\n"
            "أفضل استخدام لها: تنزيل المحتوى العام بسرعة، مع دعم اختياري لكوكيز بعض المواقع وميزة إنستغرام عبر Session."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "menu:help":
        try:
            await query.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=help_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "menu:home":
        try:
            await query.message.delete()
        except Exception:
            pass
        await send_home_message(query.message.chat.id, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_message.text:
        return

    text = update.effective_message.text.strip()

    ig_match = IG_PROFILE_RE.match(text)
    if ig_match and "/p/" not in text and "/reel/" not in text and "/stories/" not in text:
        username = ig_match.group(1)
        token = short_token()
        REQUEST_CACHE[token] = {"kind": "ig_profile", "username": username}
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("تحميل ستوريات الشخص", callback_data=f"ig:{token}:stories")],
                [InlineKeyboardButton("تحميل جميع الهايلايت", callback_data=f"ig:{token}:highlights")],
            ]
        )
        await update.effective_message.reply_text(
            f"تم التعرف على صفحة إنستغرام: <b>@{html.escape(username)}</b>\nاختر المطلوب:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return

    tg_match = TG_USERNAME_RE.match(text)
    if tg_match:
        username = tg_match.group(1)

        if not ENABLE_TELEGRAM_STORIES:
            await update.effective_message.reply_text(
                (
                    f"<b>@{html.escape(username)}</b>\n"
                    "ميزة جلب ستوريات تيليجرام لأي مستخدم ليست مفعلة في هذه النسخة، لأن Bot API لا يوفر قراءة عامة لستوريات المستخدمين.\n"
                    "إذا أردتها لاحقًا، أضف طبقة MTProto بحساب مستخدم تملكه أنت."
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

        await update.effective_message.reply_text(
            beautiful_empty_story_message(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    url_match = URL_RE.search(text)
    if not url_match:
        await update.effective_message.reply_text(
            "أرسل رابط مقطع أو رابط صفحة إنستغرام أو اكتب /help.",
            reply_markup=back_keyboard(),
        )
        return

    url = url_match.group(0)
    wait_msg = await update.effective_message.reply_text("جاري قراءة الرابط واستخراج الجودات المتاحة…")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(extract_media_info, url), timeout=90)
    except Exception as exc:
        logger.exception("Failed to extract info for %s", url)
        await wait_msg.edit_text(
            f"تعذر قراءة الرابط:\n<code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    token = short_token()
    REQUEST_CACHE[token] = {
        "kind": "download",
        "url": info.webpage_url,
        "title": info.title,
        "extractor": info.extractor,
        "heights": info.available_heights,
    }

    heights_text = ", ".join(f"{h}p" for h in info.available_heights[-6:]) if info.available_heights else "غير معروفة"

    await wait_msg.edit_text(
        (
            f"<b>{html.escape(info.title)}</b>\n"
            f"المصدر: <code>{html.escape(info.extractor)}</code>\n"
            f"الجودات المكتشفة: <code>{html.escape(heights_text)}</code>\n\n"
            "اختر الجودة التي تريد تنزيلها:"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=quality_keyboard(token, info),
        disable_web_page_preview=True,
    )


async def on_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("dlcancel:"):
        token = data.split(":", 1)[1]
        REQUEST_CACHE.pop(token, None)
        await query.message.edit_text("تم الإلغاء.")
        return

    _, token, height_str = data.split(":", 2)
    payload = REQUEST_CACHE.get(token)

    if not payload:
        await query.message.edit_text("انتهت صلاحية هذا الطلب. أرسل الرابط من جديد.")
        return

    max_height = int(height_str)
    title = payload["title"]
    url = payload["url"]

    await query.message.edit_text(
        f"جاري تنزيل: <b>{html.escape(title)}</b>",
        parse_mode=ParseMode.HTML,
    )
    await context.bot.send_chat_action(chat_id=query.message.chat.id, action=ChatAction.UPLOAD_VIDEO)

    file_path: Path | None = None
    try:
        file_path, final_title = await asyncio.wait_for(
            asyncio.to_thread(download_media, url, max_height),
            timeout=DOWNLOAD_TIMEOUT,
        )

        quality_label = f"{max_height}P" if max_height else "أفضل جودة"
        if max_height == 1440:
            quality_label = "2K"

        await send_media_file(
            update,
            context,
            file_path,
            caption=f"{final_title}\nالجودة: {quality_label}",
        )
        await query.message.delete()
    except Exception as exc:
        logger.exception("Download failed for %s", url)
        await query.message.edit_text(
            f"فشل التنزيل:\n<code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        if file_path:
            await safe_delete(file_path)
        REQUEST_CACHE.pop(token, None)


async def on_instagram_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, token, mode = (query.data or "").split(":", 2)
    payload = REQUEST_CACHE.get(token)

    if not payload:
        await query.message.edit_text("انتهت صلاحية الطلب. أرسل رابط الصفحة من جديد.")
        return

    username = payload["username"]
    title = "الستوريات" if mode == "stories" else "الهايلايت"

    await query.message.edit_text(
        f"جاري محاولة تحميل {title} لـ <b>@{html.escape(username)}</b>…",
        parse_mode=ParseMode.HTML,
    )

    try:
        files = await asyncio.wait_for(
            asyncio.to_thread(run_instaloader_cli, username, mode),
            timeout=DOWNLOAD_TIMEOUT,
        )

        if not files:
            await query.message.edit_text(
                beautiful_empty_story_message(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

        await query.message.edit_text(
            f"تم العثور على {len(files)} ملف/ملفات لـ <b>@{html.escape(username)}</b>. جاري الإرسال…",
            parse_mode=ParseMode.HTML,
        )

        sent = 0
        for path in files[:20]:
            await send_media_file(update, context, path, caption=f"@{username} | {title}")
            sent += 1
            await safe_delete(path)

        if files:
            await safe_delete(files[0].parent)

        extra = ""
        if len(files) > 20:
            extra = f"\nتم إرسال أول 20 ملف فقط من أصل {len(files)} لتجنب الإغراق."

        await query.message.edit_text(
            f"اكتمل الإرسال لـ <b>@{html.escape(username)}</b>.\nعدد الملفات المرسلة: {sent}{extra}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("Instagram request failed for @%s", username)
        await query.message.edit_text(
            f"تعذر إكمال طلب إنستغرام:\n<code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        REQUEST_CACHE.pop(token, None)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "الواجهة الرئيسية"),
            BotCommand("help", "شرح الاستخدام"),
            BotCommand("about", "معلومات عن البوت"),
        ]
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


def build_app() -> Application:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود في متغيرات البيئة.")

    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CallbackQueryHandler(on_menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_download_callback, pattern=r"^(dl:|dlcancel:)"))
    app.add_handler(CallbackQueryHandler(on_instagram_callback, pattern=r"^ig:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    start_health_server_if_needed()
    app = build_app()
    logger.info("Bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()
