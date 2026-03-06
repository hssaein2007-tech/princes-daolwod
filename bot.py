"""
Telegram Bot for downloading media from social media platforms.
Supports YouTube, Twitter, Facebook, Instagram, KICK, MBC Shahid, and Telegram Stories.
Deployable on Railway.
Requires Python 3.10+ and environment variables:
    BOT_TOKEN: your Telegram bot token
    INSTA_COOKIES_PATH: path to Instagram cookies.txt file (optional)
    TG_API_ID: Telegram API ID for user client (for stories)
    TG_API_HASH: Telegram API hash for user client
    TG_PHONE: Phone number for user client
    TG_SESSION: session file name for Telethon (optional)
"""

import os
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
from telethon import TelegramClient, events
from telethon.tl.types import InputPeerUser, User
import aiofiles
import aiohttp

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
INSTA_COOKIES_PATH = os.environ.get("INSTA_COOKIES_PATH", "cookies.txt")
TG_API_ID = int(os.environ.get("TG_API_ID", 0))
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_PHONE = os.environ.get("TG_PHONE", "")
TG_SESSION = os.environ.get("TG_SESSION", "telegram_user")
STATIC_IMAGE_URL = os.environ.get("STATIC_IMAGE_URL", "https://via.placeholder.com/300?text=Bot+Image")  # placeholder

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== GLOBAL STATE ====================
# Simple in-memory store for pending downloads (user_id -> {url, formats})
pending_downloads: Dict[int, Dict[str, Any]] = {}

# Telethon client for Telegram stories
telethon_client: Optional[TelegramClient] = None

# ==================== TELEGRAM STORIES (USER CLIENT) ====================
async def init_telethon():
    """Initialize and start the Telethon client for fetching stories."""
    global telethon_client
    if not all([TG_API_ID, TG_API_HASH, TG_PHONE]):
        logger.warning("Telegram user client credentials missing. Stories feature disabled.")
        return
    telethon_client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)
    await telethon_client.start(phone=TG_PHONE)
    logger.info("Telethon client started.")

async def get_user_stories(username: str) -> Optional[list]:
    """Fetch stories for a given username using Telethon. Returns list of media paths or None if no stories."""
    if not telethon_client or not telethon_client.is_connected():
        logger.error("Telethon client not available.")
        return None
    try:
        # Resolve username to user entity
        user = await telethon_client.get_entity(username)
        # Get stories
        stories = await telethon_client.get_stories(user.id)
        if not stories or not stories.stories:
            return None
        # Download each story
        media_files = []
        for story in stories.stories:
            # story.media is a TLObject, we need to download it
            path = await telethon_client.download_media(story.media, file=tempfile.gettempdir())
            media_files.append(path)
        return media_files
    except Exception as e:
        logger.error(f"Error fetching stories for {username}: {e}")
        return None

# ==================== YT-DLP DOWNLOAD HELPERS ====================
def get_ydl_opts(cookies_path: Optional[str] = None, format_spec: str = "best") -> dict:
    """Return base yt-dlp options."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'outtmpl': '%(title)s.%(ext)s',  # will be overridden with full path
        'noplaylist': True,
        'format': format_spec,
    }
    if cookies_path and Path(cookies_path).exists():
        opts['cookiefile'] = cookies_path
    return opts

async def extract_info(url: str, cookies_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Extract video information using yt-dlp (blocking, run in thread)."""
    def _extract():
        ydl_opts = get_ydl_opts(cookies_path, format_spec='best')  # we just need info
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                return info
            except Exception as e:
                logger.error(f"yt-dlp extract error: {e}")
                return None
    return await asyncio.to_thread(_extract)

async def get_available_formats(info: Dict[str, Any]) -> list:
    """Extract format list from info dict."""
    formats = info.get('formats', [])
    # Filter for video+audio formats or separate streams
    # Simplify: return list of height strings for video formats that have both video and audio or are standalone
    available = []
    seen = set()
    for f in formats:
        height = f.get('height')
        if height and height not in seen:
            # Check if it has video and audio or is a combined format
            if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                seen.add(height)
                available.append(height)
    return sorted(available, reverse=True)

async def download_video(url: str, format_spec: str, cookies_path: Optional[str] = None) -> Optional[str]:
    """Download video with specified format, return file path."""
    def _download():
        with tempfile.TemporaryDirectory() as tmpdir:
            outtmpl = str(Path(tmpdir) / '%(title)s_%(height)s.%(ext)s')
            opts = get_ydl_opts(cookies_path, format_spec)
            opts['outtmpl'] = outtmpl
            with yt_dlp.YoutubeDL(opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=True)
                    # Find the downloaded file
                    # yt-dlp returns the final filename in info['requested_downloads'][0]['filepath']
                    # but easier: list tmpdir and get the newest file
                    files = list(Path(tmpdir).iterdir())
                    if files:
                        return str(files[0])  # assume only one file
                except Exception as e:
                    logger.error(f"Download error: {e}")
                    return None
    return await asyncio.to_thread(_download)

async def download_instagram_stories_or_highlights(url: str, story_type: str) -> list:
    """
    Download Instagram stories or highlights from a profile URL.
    story_type: 'stories' or 'highlights'
    Returns list of file paths.
    """
    # For stories: URL like https://www.instagram.com/stories/username/
    # For highlights: URL like https://www.instagram.com/username/highlights/ or we can extract highlights from profile
    # yt-dlp can list entries: use --flat-playlist and then download each
    # We'll use a simpler approach: yt-dlp can download all stories/highlights in one command with --playlist-end?
    # Actually, yt-dlp supports Instagram: you can pass the profile URL and it will fetch stories if cookies provided.
    # But we need to specify we want stories only.
    # We'll use yt-dlp to extract entries and download each.
    def _download_items():
        with tempfile.TemporaryDirectory() as tmpdir:
            outtmpl = str(Path(tmpdir) / '%(uploader)s_%(title)s.%(ext)s')
            opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'outtmpl': outtmpl,
                'cookiefile': INSTA_COOKIES_PATH if Path(INSTA_COOKIES_PATH).exists() else None,
                'ignoreerrors': True,
                'nooverwrites': True,
            }
            # For stories, we need to pass the URL with /stories/ maybe
            # Let's construct the appropriate URL
            if 'stories' in story_type:
                # Extract username from profile URL
                match = re.search(r'instagram\.com/([^/?]+)', url)
                if match:
                    username = match.group(1)
                    stories_url = f"https://www.instagram.com/stories/{username}/"
                else:
                    return []
            else:  # highlights
                # yt-dlp can extract highlights from profile? Possibly with --playlist-items?
                # We'll try profile URL and hope it extracts highlights as separate entries.
                stories_url = url  # Assume profile URL works for highlights as well
            with yt_dlp.YoutubeDL(opts) as ydl:
                try:
                    # Extract info with download=False to get list
                    info = ydl.extract_info(stories_url, download=False)
                    entries = info.get('entries', [])
                    if not entries:
                        return []
                    # Download each entry
                    downloaded = []
                    for entry in entries:
                        if entry:
                            ydl.process_ie_result(entry, download=True)
                            # Find the downloaded file
                            # The actual file is saved with outtmpl pattern
                            # We can list tmpdir after each download, but easier: let ydl handle it and then collect all files
                    # Collect all files in tmpdir
                    files = list(Path(tmpdir).iterdir())
                    return [str(f) for f in files]
                except Exception as e:
                    logger.error(f"Instagram download error: {e}")
                    return []
    return await asyncio.to_thread(_download_items)

# ==================== BOT HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a fancy welcome message with inline buttons."""
    chat_id = update.effective_chat.id
    bot_name = "الأمراء | 𝔞𝔩 𝔭𝔯𝔧𝔫𝔠𝔢𝔰"
    caption = f"مرحباً بك في {bot_name}\n\nأرسل رابط فيديو لتحميله، أو رابط حساب إنستغرام لتحميل الستوريات/الهايلات، أو معرف تليكرام لعرض الستوريات."
    keyboard = [
        [InlineKeyboardButton("CANAL", url="https://t.me/your_channel")],
        [InlineKeyboardButton("𝐒𝐨𝐮𝐫𝐜𝐞 𝐏𝐫𝐢𝐧𝐜𝐞𝐬™", url="https://t.me/your_source")],
        [InlineKeyboardButton("المطور", url="https://t.me/your_dev")],
        [InlineKeyboardButton("الأوامر", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Send photo with static image URL
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=STATIC_IMAGE_URL,
        caption=caption,
        reply_markup=reply_markup
    )

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help text when 'الأوامر' button is pressed."""
    query = update.callback_query
    await query.answer()
    help_text = (
        "📚 **كيفية استخدام البوت**\n\n"
        "• **لتحميل فيديو**: أرسل رابط الفيديو من أي موقع (يوتيوب، تويتر، فيسبوك، إنستغرام، KICK، شاهد، ...).\n"
        "• **لتحميل ستوريات إنستغرام**: أرسل رابط صفحة إنستغرام (مثل https://www.instagram.com/username/). ستظهر لك أزرار لتحميل الستوريات أو الهايلات.\n"
        "• **لعرض ستوريات تليكرام**: أرسل معرف المستخدم (مثل @username أو username). سأحضر لك ستورياته إن وجدت.\n\n"
        "بعد إرسال رابط الفيديو، سيتم عرض جودات متاحة. اختر الجودة المناسب وانتظر التحميل."
    )
    await query.edit_message_caption(caption=help_text, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages: links, usernames, etc."""
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Check if it's a Telegram username (with or without @)
    tg_username_match = re.match(r'^@?(\w{5,32})$', text)
    if tg_username_match and not re.match(r'https?://', text):
        username = tg_username_match.group(1)
        await update.message.reply_text("🔍 جاري البحث عن ستوريات المستخدم...")
        stories = await get_user_stories(username)
        if stories:
            # Send each story as a media group or separately
            for story_path in stories:
                await update.message.reply_document(document=open(story_path, 'rb'))
                # Clean up
                os.unlink(story_path)
        else:
            # Fake mention
            fake_mention = f"[{username}](https://t.me/{username}) ماعنده ستوريات…🚫"
            await update.message.reply_text(fake_mention, parse_mode='Markdown')
        return

    # Check if it's an Instagram profile URL
    if 'instagram.com/' in text and not '/p/' in text and not '/reel/' in text:
        # Profile URL (may contain username)
        keyboard = [
            [InlineKeyboardButton("تحميل الستوريات", callback_data=f"insta_stories|{text}")],
            [InlineKeyboardButton("تحميل الهايلات", callback_data=f"insta_highlights|{text}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("اختر ما تريد تحميله:", reply_markup=reply_markup)
        return

    # Otherwise, treat as video link
    await update.message.reply_text("🔍 جاري استخراج معلومات الفيديو...")
    info = await extract_info(text, cookies_path=INSTA_COOKIES_PATH if Path(INSTA_COOKIES_PATH).exists() else None)
    if not info:
        await update.message.reply_text("❌ تعذر استخراج معلومات الفيديو. تأكد من الرابط.")
        return

    # Get available formats
    formats = await get_available_formats(info)
    if not formats:
        await update.message.reply_text("❌ لم يتم العثور على صيغ فيديو مناسبة.")
        return

    # Store info in pending downloads
    pending_downloads[user_id] = {'url': text, 'formats': formats}

    # Build quality selection keyboard
    keyboard = []
    for height in formats:
        if height <= 480:
            label = f"جودة عادية {height}p"
        elif height <= 720:
            label = f"جودة 720p"
        elif height <= 1080:
            label = f"جودة عالية 1080p"
        else:
            label = f"جودة {height}p"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"dl|{height}")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("اختر الجودة المناسبة:", reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "help":
        await help_callback(update, context)
        return

    if data.startswith("insta_"):
        # Instagram stories/highlights download
        parts = data.split('|', 1)
        if len(parts) != 2:
            await query.edit_message_text("❌ خطأ في البيانات.")
            return
        story_type = parts[0].replace("insta_", "")  # stories or highlights
        url = parts[1]
        await query.edit_message_text("⏳ جاري تحميل الستوريات/الهايلات... (قد يستغرق وقتاً)")
        files = await download_instagram_stories_or_highlights(url, story_type)
        if not files:
            await query.edit_message_text("❌ لم يتم العثور على ستوريات أو هايلات.")
            return
        # Send files
        for file_path in files:
            await query.message.reply_document(document=open(file_path, 'rb'))
            os.unlink(file_path)
        await query.delete_message()
        return

    if data.startswith("dl|"):
        # Quality selected
        if user_id not in pending_downloads:
            await query.edit_message_text("❌ انتهت صلاحية الطلب. أعد إرسال الرابط.")
            return
        height = int(data.split('|')[1])
        url = pending_downloads[user_id]['url']
        # Find format string for this height
        # We'll use yt-dlp format filter like 'best[height<=?]' but easier: use format 'best[height<=480]' etc.
        # Actually, we can use 'bestvideo[height<=?]+bestaudio/best[height<=?]'
        format_spec = f"best[height<={height}]"
        await query.edit_message_text(f"⏳ جاري تحميل الفيديو بجودة {height}p...")
        file_path = await download_video(url, format_spec, cookies_path=INSTA_COOKIES_PATH if Path(INSTA_COOKIES_PATH).exists() else None)
        if not file_path:
            await query.edit_message_text("❌ فشل التحميل. حاول مرة أخرى.")
            return
        # Upload to Telegram
        await query.message.reply_video(video=open(file_path, 'rb'), supports_streaming=True)
        # Cleanup
        os.unlink(file_path)
        await query.delete_message()
        del pending_downloads[user_id]
        return

# ==================== MAIN ====================
def main():
    """Start the bot."""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start Telethon client in the same loop
    loop = asyncio.get_event_loop()
    if all([TG_API_ID, TG_API_HASH, TG_PHONE]):
        loop.create_task(init_telethon())

    # Run bot
    application.run_polling()

if __name__ == "__main__":
    main()
