
import os
import subprocess
from uuid import uuid4
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
START_IMAGE_URL = os.getenv("START_IMAGE_URL", "https://example.com/start.jpg")

TEMP_DIR = "downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

WELCOME_TEXT = '''
✨ الأمراء | 𝔞𝔩 𝔭𝔯𝔦𝔫𝔠𝔢𝔰 ✨

أهلاً بك في بوت تحميل الفيديوهات من مواقع التواصل.
أرسل رابط الفيديو وسيظهر لك اختيار الجودة.
'''

HELP_TEXT = '''
طريقة الاستخدام:

1- أرسل رابط الفيديو.
2- اختر الجودة.
3- سيقوم البوت بإرسال الفيديو.
'''

def start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("CANAL", url="https://t.me/")],
        [InlineKeyboardButton("𝐒𝐨𝐮𝐫𝐜𝐞 𝐏𝐫𝐢𝐧𝐜𝐞𝐬™", url="https://t.me/")],
        [InlineKeyboardButton("المطور", url="https://t.me/")],
        [InlineKeyboardButton("الأوامر", callback_data="help")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_photo(
        photo=START_IMAGE_URL,
        caption=WELCOME_TEXT,
        reply_markup=start_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "help":
        await query.message.reply_text(HELP_TEXT)
        return

    url, quality = query.data.split("|")
    msg = await query.message.reply_text("جاري التحميل...")

    file_id = str(uuid4())
    output = f"{TEMP_DIR}/{file_id}.mp4"

    format_map = {
        "480": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "720": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "best": "best"
    }

    fmt = format_map.get(quality, "best")

    cmd = ["yt-dlp", "-f", fmt, "-o", output, url]

    try:
        subprocess.run(cmd, check=True)
        size = os.path.getsize(output)

        if size > 49 * 1024 * 1024:
            await msg.edit_text("الملف كبير للإرسال.")
            os.remove(output)
            return

        await msg.delete()
        await query.message.reply_video(video=open(output, "rb"))
        os.remove(output)

    except Exception:
        await msg.edit_text("حدث خطأ أثناء التحميل.")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("480P", callback_data=f"{url}|480")],
        [InlineKeyboardButton("720P", callback_data=f"{url}|720")],
        [InlineKeyboardButton("1080P", callback_data=f"{url}|1080")],
        [InlineKeyboardButton("أفضل جودة", callback_data=f"{url}|best")]
    ])

    await update.message.reply_text("اختر الجودة:", reply_markup=keyboard)

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    app.run_polling()

if __name__ == "__main__":
    main()
