import asyncio
import json
import time
import platform
import psutil
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
import os
import static_ffmpeg
static_ffmpeg.add_paths()

import logging
from pyrogram import Client, filters, idle
from pyrogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import pyrogram.errors
try:
    from pyrogram.errors import GroupcallForbidden
except ImportError:
    class GroupcallForbidden(pyrogram.errors.exceptions.forbidden_403.Forbidden):
        pass
    pyrogram.errors.GroupcallForbidden = GroupcallForbidden

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, Update, StreamEnded
from yt_dlp import YoutubeDL
from config import API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID, YTDL_COOKIEFILE

logging.basicConfig(level=logging.INFO)

# Create downloads directory
if not os.path.exists("downloads"):
    os.makedirs("downloads")

# Initialize Bot Client
bot = Client(
    "MusicBot3",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user = Client(
    "UserSession3",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

call_py = PyTgCalls(user)

ydl_opts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(id)s.%(ext)s',
    'noplaylist': True,
    'quiet': True,
}
if YTDL_COOKIEFILE:
    cookie_path = os.path.abspath(YTDL_COOKIEFILE)
    if os.path.isfile(cookie_path):
        ydl_opts['cookiefile'] = cookie_path
    else:
        logging.warning("YTDL_COOKIEFILE is set but the file does not exist: %s", cookie_path)

QUEUE = {}
PLAYING = {}
START_TIME = time.time()

def load_chats():
    if os.path.exists("chats.json"):
        try:
            with open("chats.json", "r") as f:
                return set(json.load(f))
        except:
            pass
    return set()

def save_chats():
    with open("chats.json", "w") as f:
        json.dump(list(TRACKED_CHATS), f)

TRACKED_CHATS = load_chats()

@bot.on_message(filters.group | filters.private, group=1)
async def track_chats(client: Client, message: Message):
    if message.chat:
        chat_id = message.chat.id
        if chat_id not in TRACKED_CHATS:
            TRACKED_CHATS.add(chat_id)
            save_chats()

def get_control_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸ Pause", callback_data="pause"),
            InlineKeyboardButton("⏹ Stop", callback_data="stop"),
            InlineKeyboardButton("▶️ Resume", callback_data="resume")
        ],
        [
            InlineKeyboardButton("⏭ Skip", callback_data="skip")
        ]
    ])

async def play_next(chat_id):
    if chat_id in QUEUE and len(QUEUE[chat_id]) > 0:
        next_song = QUEUE[chat_id].pop(0)
        try:
            await call_py.play(chat_id, MediaStream(next_song['file'], video_flags=MediaStream.Flags.IGNORE))
            PLAYING[chat_id] = True
            await bot.send_photo(
                chat_id,
                photo=next_song['thumbnail'],
                caption=f"▶️ **Now playing from queue:**\n{next_song['title']}",
                reply_markup=get_control_markup()
            )
        except Exception as e:
            await bot.send_message(chat_id, f"❌ Error playing next song: {str(e)}")
            await play_next(chat_id)
    else:
        PLAYING[chat_id] = False
        try:
            await call_py.leave_call(chat_id)
        except:
            pass

@call_py.on_update()
async def stream_handler(client, update: Update):
    if isinstance(update, StreamEnded):
        chat_id = update.chat_id
        await play_next(chat_id)

@bot.on_message(filters.command("play") & filters.group)
async def play_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /play <song name or youtube link>")
    
    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text("🔎 Searching and downloading...")

    try:
        def extract_info():
            with YoutubeDL(ydl_opts) as ydl:
                search_query = query if query.startswith("http") else f"ytsearch:{query}"
                info = ydl.extract_info(search_query, download=True)
                if 'entries' in info:
                    info = info['entries'][0]
                
                filepath = os.path.abspath(ydl.prepare_filename(info))
                return info, filepath

        info, filepath = await asyncio.to_thread(extract_info)
        title = info.get('title', 'Unknown Title')
        thumbnail = info.get('thumbnail', 'https://telegra.ph/file/default.jpg')
        
        chat_id = message.chat.id
        song_data = {"file": filepath, "title": title, "thumbnail": thumbnail}

        if PLAYING.get(chat_id, False):
            if chat_id not in QUEUE:
                QUEUE[chat_id] = []
            QUEUE[chat_id].append(song_data)
            await message.reply_photo(
                photo=thumbnail,
                caption=f"📝 **Added to Queue:**\n{title}"
            )
            await processing_msg.delete()
        else:
            await processing_msg.edit_text(f"🎵 Joining Voice Chat...\n**{title}**")
            await call_py.play(chat_id, MediaStream(filepath, video_flags=MediaStream.Flags.IGNORE))
            PLAYING[chat_id] = True
            
            await message.reply_photo(
                photo=thumbnail,
                caption=f"▶️ **Now playing:**\n{title}",
                reply_markup=get_control_markup()
            )
            await processing_msg.delete()
        
    except Exception as e:
        error_text = str(e)
        if "Sign in to confirm you\'re not a bot" in error_text or "cookies" in error_text.lower():
            error_text += (
                "\n\n⚠️ YouTube may require cookies for this video. "
                "Set the environment variable `YTDL_COOKIEFILE` to a valid cookies.txt file "
                "exported from your browser, then restart the bot."
            )
        await processing_msg.edit_text(f"❌ Error: {error_text}")

@bot.on_message(filters.command("playforce") & filters.group)
async def playforce_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /playforce <song name>")
    
    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text("🔎 Searching, downloading, and forcing play...")

    try:
        def extract_info():
            with YoutubeDL(ydl_opts) as ydl:
                search_query = query if query.startswith("http") else f"ytsearch:{query}"
                info = ydl.extract_info(search_query, download=True)
                if 'entries' in info:
                    info = info['entries'][0]
                
                filepath = os.path.abspath(ydl.prepare_filename(info))
                return info, filepath

        info, filepath = await asyncio.to_thread(extract_info)
        title = info.get('title', 'Unknown Title')
        thumbnail = info.get('thumbnail', 'https://telegra.ph/file/default.jpg')
        
        chat_id = message.chat.id
        
        await call_py.play(chat_id, MediaStream(filepath, video_flags=MediaStream.Flags.IGNORE))
        PLAYING[chat_id] = True
        
        await message.reply_photo(
            photo=thumbnail,
            caption=f"⚡ **Force Playing:**\n{title}",
            reply_markup=get_control_markup()
        )
        await processing_msg.delete()
        
    except Exception as e:
        error_text = str(e)
        if "Sign in to confirm you\'re not a bot" in error_text or "cookies" in error_text.lower():
            error_text += (
                "\n\n⚠️ YouTube may require cookies for this video. "
                "Set the environment variable `YTDL_COOKIEFILE` to a valid cookies.txt file "
                "exported from your browser, then restart the bot."
            )
        await processing_msg.edit_text(f"❌ Error: {error_text}")

@bot.on_message(filters.command("skip") & filters.group)
async def skip_command(client: Client, message: Message):
    chat_id = message.chat.id
    if not PLAYING.get(chat_id, False):
        return await message.reply_text("Nothing is playing to skip.")
    
    await message.reply_text("⏭️ Skipped current song.")
    await play_next(chat_id)

@bot.on_message(filters.command("stop") & filters.group)
async def stop_command(client: Client, message: Message):
    try:
        chat_id = message.chat.id
        QUEUE[chat_id] = []
        PLAYING[chat_id] = False
        await call_py.leave_call(chat_id)
        await message.reply_text("⏹️ Stopped playback, cleared queue, and left the voice chat.")
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}\n\nMake sure I'm in a voice chat.")

@bot.on_message(filters.command("ping"))
async def ping_command(client: Client, message: Message):
    await message.reply_text("Pong! Bot is alive and receiving messages! 🏓")

@bot.on_message(filters.command("stats"))
async def stats_command(client: Client, message: Message):
    uptime = time.time() - START_TIME
    m, s = divmod(int(uptime), 60)
    h, m = divmod(m, 60)
    uptime_str = f"{h}h {m}m {s}s"
    
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory().percent
    active_chats = sum(1 for v in PLAYING.values() if v)
    total_tracked = len(TRACKED_CHATS)
    
    stats = f"""📊 **Bot Statistics**
    
⏱ **Uptime:** `{uptime_str}`
🖥 **CPU Usage:** `{cpu}%`
💾 **RAM Usage:** `{ram}%`

🎵 **Active Voice Chats:** `{active_chats}`
👥 **Total Tracked Chats:** `{total_tracked}`
"""
    await message.reply_text(stats)

@bot.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_command(client: Client, message: Message):
    if not message.reply_to_message and len(message.command) < 2:
        return await message.reply_text("Usage: Reply to a message or provide text to broadcast.")
    
    processing_msg = await message.reply_text("⏳ Broadcasting message...")
    
    success = 0
    failed = 0
    
    for chat_id in list(TRACKED_CHATS):
        try:
            if message.reply_to_message:
                await message.reply_to_message.copy(chat_id)
            else:
                await bot.send_message(chat_id, message.text.split(None, 1)[1])
            success += 1
            await asyncio.sleep(0.1)
        except Exception:
            failed += 1
            
    await processing_msg.edit_text(f"✅ **Broadcast Completed!**\n\n🎯 **Success:** `{success}`\n❌ **Failed:** `{failed}`")

@bot.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    chat_id = query.message.chat.id
    if query.data == "pause":
        if PLAYING.get(chat_id, False):
            await call_py.pause_stream(chat_id)
            await query.answer("Paused ⏸")
        else:
            await query.answer("Nothing is playing", show_alert=True)
    elif query.data == "resume":
        if PLAYING.get(chat_id, False):
            await call_py.resume_stream(chat_id)
            await query.answer("Resumed ▶️")
        else:
            await query.answer("Nothing is playing", show_alert=True)
    elif query.data == "skip":
        if PLAYING.get(chat_id, False):
            await query.answer("Skipped ⏭")
            await play_next(chat_id)
        else:
            await query.answer("Nothing is playing", show_alert=True)
    elif query.data == "stop":
        if PLAYING.get(chat_id, False):
            QUEUE[chat_id] = []
            PLAYING[chat_id] = False
            try:
                await call_py.leave_call(chat_id)
            except:
                pass
            await query.answer("Stopped ⏹")
        else:
            await query.answer("Nothing is playing", show_alert=True)

async def main():
    print("Starting bot...")
    await bot.start()
    try:
        await bot.set_bot_commands([
            BotCommand("play", "Play a song in voice chat"),
            BotCommand("playforce", "Force play a song immediately"),
            BotCommand("skip", "Skip the current playing song"),
            BotCommand("stop", "Stop playback and leave voice chat"),
            BotCommand("ping", "Check if bot is alive"),
            BotCommand("stats", "Show bot statistics"),
            BotCommand("broadcast", "Broadcast a message (Owner only)")
        ])
        print("Bot commands set successfully.")
    except Exception as e:
        print(f"Failed to set bot commands: {e}")
    print("Starting user session...")
    await user.start()
    print("Starting PyTgCalls...")
    await call_py.start()
    print("Bot is now running! Send /play <song> in your group.")
    await idle()
    print("Stopping...")
    await bot.stop()
    await user.stop()

if __name__ == "__main__":
    loop.run_until_complete(main())
