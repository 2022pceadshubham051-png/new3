import asyncio
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
import json
import time
import platform
import psutil
import os
import sys
import logging
from pathlib import Path
from pyrogram import Client, filters, idle
from pyrogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ChatMembersFilter
import pyrogram.errors
from yt_dlp import YoutubeDL
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Import configuration and database
from config import API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID, SUPPORT_GROUP_ID, YTDL_COOKIEFILE
import database

logging.basicConfig(level=logging.INFO)

# Create folders
if not os.path.exists("downloads"):
    os.makedirs("downloads")
if not os.path.exists("assets"):
    os.makedirs("assets")

# Initialize clients
bot = Client(
    "MusicBotVerse",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user = Client(
    "UserSessionVerse",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING
)

# We load pytgcalls dynamically on startup inside main() to avoid issues with native DLL blocks locally
call_py = None
user_id_self = None

# Global dictionaries
QUEUE = {}       # chat_id -> list of song dicts
PLAYING = {}     # chat_id -> current playing song dict
LOOP = {}        # chat_id -> loop count (0: off, -1: infinite, >0: count)
ACTIVE_MONITORS = {} # chat_id -> asyncio task

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

START_TIME = time.time()

# --- Helpers ---

async def is_user_admin(chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID or database.is_approved_member(user_id):
        return True
    cached_admins = database.get_cached_admins(chat_id)
    if cached_admins is not None:
        return user_id in cached_admins
    try:
        admins = await bot.get_chat_members(chat_id, filter=ChatMembersFilter.ADMINISTRATORS)
        admin_ids = [a.user.id for a in admins]
        database.set_cached_admins(chat_id, admin_ids)
        return user_id in admin_ids
    except Exception as e:
        logging.error(f"Error checking admins: {e}")
        return False

async def is_user_authorized(chat_id: int, user_id: int) -> bool:
    if await is_user_admin(chat_id, user_id):
        return True
    return database.is_auth_user(chat_id, user_id)

async def resolve_target_user(client: Client, message: Message):
    # Reply to message
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        if target:
            return target.id, target.username, target.first_name
        return None, None, None
    # Command argument
    if len(message.command) >= 2:
        arg = message.command[1]
        if arg.isdigit():
            user_id = int(arg)
            try:
                user_obj = await client.get_users(user_id)
                return user_obj.id, user_obj.username, user_obj.first_name
            except Exception:
                return user_id, None, "User"
        if arg.startswith("@"):
            username = arg[1:]
            try:
                user_obj = await client.get_users(username)
                return user_obj.id, user_obj.username, user_obj.first_name
            except Exception as e:
                logging.error(f"Failed to resolve username {username}: {e}")
    return None, None, None

def get_control_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸ Pause", callback_data="pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="resume"),
            InlineKeyboardButton("⏭ Skip", callback_data="skip")
        ],
        [
            InlineKeyboardButton("⏹ End", callback_data="end"),
            InlineKeyboardButton("🔄 Loop", callback_data="loop_toggle"),
            InlineKeyboardButton("📋 Queue", callback_data="queue_view")
        ]
    ])

# --- Playback Logic ---

def track_listen_start(chat_id, user_id, username, first_name, song_title, file, duration):
    track_listen_stop(chat_id)
    PLAYING[chat_id] = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "title": song_title,
        "file": file,
        "duration": duration,
        "seek_offset": 0,
        "start_time": time.time(),
        "is_paused": False
    }
    database.add_play(user_id, username, first_name, song_title)

def track_listen_pause(chat_id):
    play_info = PLAYING.get(chat_id)
    if play_info and not play_info["is_paused"]:
        if play_info["start_time"] is not None:
            elapsed = time.time() - play_info["start_time"]
            database.add_listen_time(play_info["user_id"], elapsed)
            play_info["start_time"] = None
        play_info["is_paused"] = True

def track_listen_resume(chat_id):
    play_info = PLAYING.get(chat_id)
    if play_info and play_info["is_paused"]:
        play_info["start_time"] = time.time()
        play_info["is_paused"] = False

def track_listen_stop(chat_id):
    play_info = PLAYING.pop(chat_id, None)
    if play_info:
        if not play_info["is_paused"] and play_info["start_time"] is not None:
            elapsed = time.time() - play_info["start_time"]
            database.add_listen_time(play_info["user_id"], elapsed)
        # Delete file if loop is not enabled
        if LOOP.get(chat_id, 0) == 0:
            try:
                if os.path.exists(play_info["file"]):
                    os.remove(play_info["file"])
            except Exception as e:
                logging.error(f"Error removing file {play_info['file']}: {e}")
        return play_info
    return None

async def active_chat_monitor(chat_id):
    await asyncio.sleep(30)
    while chat_id in PLAYING and PLAYING[chat_id]:
        try:
            participants = await call_py.get_participants(chat_id)
            if participants:
                active_users = [p for p in participants if getattr(p, "user_id", None) != user_id_self]
                if not active_users:
                    # Empty chat
                    await stop_playback(chat_id)
                    await bot.send_message(
                        chat_id,
                        "<b>[MusicVerse Notification]</b>\n\n"
                        "⚠️ <i>The voice chat is empty. To conserve server resources, "
                        "MusicVerse has stopped playback and left the call.</i>",
                        parse_mode="html"
                    )
                    break
            else:
                await stop_playback(chat_id)
                break
        except Exception as e:
            logging.error(f"Error in monitor for {chat_id}: {e}")
        await asyncio.sleep(30)

async def stop_playback(chat_id):
    QUEUE[chat_id] = []
    track_listen_stop(chat_id)
    LOOP[chat_id] = 0
    if chat_id in ACTIVE_MONITORS:
        ACTIVE_MONITORS[chat_id].cancel()
        ACTIVE_MONITORS.pop(chat_id, None)
    try:
        await call_py.leave_call(chat_id)
    except:
        pass

async def play_next(chat_id):
    from pytgcalls.types import MediaStream
    loop_status = LOOP.get(chat_id, 0)
    current_song = PLAYING.get(chat_id)
    
    if loop_status != 0 and current_song:
        # Playing song again (loop)
        if loop_status > 0:
            LOOP[chat_id] -= 1
            if LOOP[chat_id] == 0:
                pass
        
        try:
            await call_py.play(chat_id, MediaStream(current_song['file'], video_flags=MediaStream.Flags.IGNORE))
            current_song["start_time"] = time.time()
            current_song["seek_offset"] = 0
            current_song["is_paused"] = False
            database.add_play(current_song["user_id"], current_song["username"], current_song["first_name"], current_song["title"])
            return
        except Exception as e:
            logging.error(f"Error playing loop: {e}")
            LOOP[chat_id] = 0

    # Clean up previous song
    track_listen_stop(chat_id)

    if chat_id in QUEUE and len(QUEUE[chat_id]) > 0:
        next_song = QUEUE[chat_id].pop(0)
        try:
            await call_py.play(chat_id, MediaStream(next_song['file'], video_flags=MediaStream.Flags.IGNORE))
            track_listen_start(
                chat_id,
                next_song["user_id"],
                next_song["username"],
                next_song["first_name"],
                next_song["title"],
                next_song["file"],
                next_song["duration"]
            )
            
            # Send Now Playing with Cover Card
            photo_path = next_song["thumbnail"] if next_song["thumbnail"] and os.path.exists(next_song["thumbnail"]) else "assets/queue.png"
            await bot.send_photo(
                chat_id,
                photo=photo_path,
                caption=f"▶️ <b>Now playing from queue:</b>\n\n"
                        f"🎼 <b>Title:</b> {next_song['title']}\n"
                        f"⏱ <b>Duration:</b> {next_song['duration'] // 60}m {next_song['duration'] % 60}s\n"
                        f"👤 <b>Requested by:</b> <a href='tg://user?id={next_song['user_id']}'>{next_song['first_name']}</a>",
                reply_markup=get_control_markup()
            )
        except Exception as e:
            await bot.send_message(chat_id, f"❌ <b>Error playing next song:</b> {str(e)}")
            try:
                if os.path.exists(next_song['file']):
                    os.remove(next_song['file'])
            except:
                pass
            await play_next(chat_id)
    else:
        # Queue empty
        if chat_id in ACTIVE_MONITORS:
            ACTIVE_MONITORS[chat_id].cancel()
            ACTIVE_MONITORS.pop(chat_id, None)
        try:
            await call_py.leave_call(chat_id)
        except:
            pass

# --- Profile Generation ---

def generate_profile_card(avatar_path, player_id, username, first_name, listen_seconds, favorite_song, favorite_plays, output_path):
    width = 800
    height = 450
    base = Image.new("RGBA", (width, height), (15, 12, 32, 255))
    draw = ImageDraw.Draw(base)
    
    # Draw Background Gradient
    for y in range(height):
        r = int(15 + (43 - 15) * (y / height))
        g = int(12 + (16 - 12) * (y / height))
        b = int(32 + (85 - 32) * (y / height))
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))
        
    # Draw Matrix Grid
    grid_draw = ImageDraw.Draw(base)
    for x in range(0, width, 40):
        grid_draw.line([(x, 0), (x, height)], fill=(255, 255, 255, 10))
    for y in range(0, height, 40):
        grid_draw.line([(0, y), (width, y)], fill=(255, 255, 255, 10))
        
    # Draw Neon rounded borders
    border_color = (138, 43, 226, 255)
    draw.rounded_rectangle([15, 15, width-15, height-15], radius=20, outline=border_color, width=3)
    draw.rounded_rectangle([18, 18, width-18, height-18], radius=17, outline=(255, 0, 128, 100), width=1)
    
    avatar_size = 200
    avatar = None
    if avatar_path and os.path.exists(avatar_path):
        try:
            avatar = Image.open(avatar_path).convert("RGBA")
        except:
            pass
            
    if not avatar:
        avatar = Image.new("RGBA", (avatar_size, avatar_size), (40, 20, 80, 255))
        av_draw = ImageDraw.Draw(avatar)
        av_draw.ellipse([20, 20, avatar_size-20, avatar_size-20], fill=(75, 0, 130, 255), outline=(138, 43, 226), width=3)
        initial = (first_name[0] if first_name else "M").upper()
        av_draw.text((avatar_size//2, avatar_size//2), initial, fill=(255, 255, 255, 255), anchor="mm")
        
    avatar = avatar.resize((avatar_size, avatar_size))
    mask = Image.new("L", (avatar_size, avatar_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse([0, 0, avatar_size, avatar_size], fill=255)
    
    avatar_layer = Image.new("RGBA", (avatar_size + 12, avatar_size + 12), (0, 0, 0, 0))
    av_layer_draw = ImageDraw.Draw(avatar_layer)
    av_layer_draw.ellipse([0, 0, avatar_size+10, avatar_size+10], fill=None, outline=(255, 0, 128, 255), width=4)
    av_layer_draw.ellipse([3, 3, avatar_size+7, avatar_size+7], fill=None, outline=(138, 43, 226, 255), width=2)
    
    circle_avatar = ImageOps.fit(avatar, (avatar_size, avatar_size), centering=(0.5, 0.5))
    avatar_layer.paste(circle_avatar, (6, 6), mask)
    base.paste(avatar_layer, (50, (height - avatar_size)//2 - 6), avatar_layer)
    
    # Load fonts (Segoe UI default on Windows)
    font_family = "Segoe UI"
    try:
        title_font = ImageFont.truetype(font_family, size=28)
        content_font = ImageFont.truetype(font_family, size=16)
        content_font_bold = ImageFont.truetype(font_family + " Bold", size=16)
    except IOError:
        try:
            title_font = ImageFont.truetype("arial", size=28)
            content_font = ImageFont.truetype("arial", size=16)
            content_font_bold = ImageFont.truetype("arial", size=16)
        except IOError:
            title_font = ImageFont.load_default()
            content_font = ImageFont.load_default()
            content_font_bold = ImageFont.load_default()
            
    text_start_x = 300
    y_offset = 60
    
    draw.text((text_start_x, y_offset), "MUSICVERSE PLAYER CARD", fill=(0, 255, 240, 255), font=title_font)
    y_offset += 45
    draw.line([(text_start_x, y_offset), (width-50, y_offset)], fill=(255, 0, 128, 255), width=2)
    y_offset += 25
    
    fields = [
        ("Player ID:", str(player_id)),
        ("First Name:", first_name),
        ("Username:", f"@{username}" if username else "N/A"),
        ("Total Listening:", f"{listen_seconds / 3600:.2f} Hours"),
        ("Favorite Song:", favorite_song),
        ("Times Played:", f"{favorite_plays} times")
    ]
    
    for label, val in fields:
        if len(val) > 30:
            val = val[:27] + "..."
        draw.text((text_start_x, y_offset), label, fill=(180, 180, 220, 255), font=content_font)
        val_color = (0, 255, 240, 255) if label in ["Total Listening:", "Favorite Song:"] else (255, 255, 255, 255)
        try:
            draw.text((text_start_x + 150, y_offset), val, fill=val_color, font=content_font_bold)
        except:
            draw.text((text_start_x + 150, y_offset), val, fill=val_color, font=content_font)
        y_offset += 32
        
    base.convert("RGB").save(output_path, "JPEG", quality=95)

# --- Commands Implementation ---

@bot.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    # Save user info to database
    user_profile = database.get_user_profile(user_id)
    is_new_user = (user_profile is None)
    database.ensure_user(user_id, username, first_name)
    
    if message.chat.type == pyrogram.enums.ChatType.PRIVATE:
        # Private Message Start
        caption = (
            "☀️ <b>Welcome to MusicVerse</b> 🎵\n\n"
            "Elevate your Telegram group experience with high-fidelity, real-time music streaming in voice chats. "
            "<b>MusicVerse</b> is a feature-rich audio streaming client designed for communities, offering:\n\n"
            "🔹 <b>Seamless Playback:</b> Supports keywords, YouTube links, and audio files.\n"
            "🔹 <b>Queue Management:</b> Automated queuing with duration protection (max 1 hour).\n"
            "🔹 <b>Advanced Control:</b> Real-time pausing, resuming, skipping, and precise seeking.\n"
            "🔹 <b>Player Metrics:</b> Custom player profile cards and detailed top-played lists.\n"
            "🔹 <b>Admin Security:</b> Restrict bot access to authorized members or admins.\n\n"
            "ℹ️ <i>To get started, add me to your group and type /help to view all commands.</i>"
        )
        
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Add MusicVerse to Group", url=f"https://t.me/{bot.me.username}?startgroup=true")
            ],
            [
                InlineKeyboardButton("🛡️ Support Group", url="https://t.me/MusicVerseSupport"),
                InlineKeyboardButton("📚 Help Guide", callback_data="help_callback")
            ]
        ])
        
        photo_path = "assets/start.png"
        if os.path.exists(photo_path):
            await message.reply_photo(photo=photo_path, caption=caption, reply_markup=markup, parse_mode="html")
        else:
            await message.reply_text(text=caption, reply_markup=markup, parse_mode="html")
            
        # Log to support group if new user
        if is_new_user and SUPPORT_GROUP_ID:
            try:
                log_msg = (
                    "🚀 <b>New User Registered!</b>\n\n"
                    f"👤 <b>Name:</b> {first_name}\n"
                    f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"🔗 <b>Username:</b> @{username if username else 'N/A'}"
                )
                await bot.send_message(SUPPORT_GROUP_ID, log_msg, parse_mode="html")
            except Exception as e:
                logging.error(f"Failed to log new user: {e}")
    else:
        # Group Chat Start
        database.add_tracked_chat(chat_id, message.chat.title)
        await message.reply_text(
            "🎵 <b>MusicVerse online</b> inside this community!\n"
            "Use <code>/play [song name]</code> to start playing music in the voice chat.\n"
            "Type <code>/help</code> for details.",
            parse_mode="html"
        )

@bot.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    caption = (
        "📚 <b>MusicVerse Commands Dashboard</b>\n\n"
        "Configure and control the MusicVerse streaming client in your group.\n\n"
        "🎵 <b>Playback Control:</b>\n"
        "• <code>/play [song/url/reply]</code> - Stream audio in voice chat.\n"
        "• <code>/forceplay [song/url]</code> - Interrupt and play song immediately (Admin only).\n"
        "• <code>/pause</code> - Pause current streaming.\n"
        "• <code>/resume</code> - Resume paused playback.\n"
        "• <code>/skip</code> - Skip to the next song in the queue.\n"
        "• <code>/seek [seconds]</code> - Fast-forward playback.\n"
        "• <code>/seekback [seconds]</code> - Rewind playback.\n"
        "• <code>/loop [enable/disable/count]</code> - Set repeat options.\n"
        "• <code>/queue</code> - View upcoming songs.\n"
        "• <code>/end</code> - Stop playback and leave voice chat.\n\n"
        "🛡️ <b>Administration & Auth:</b>\n"
        "• <code>/auth [reply/userid/username]</code> - Grant bot access to a non-admin (GC Admin only).\n"
        "• <code>/unauth [reply/userid/username]</code> - Revoke bot access (GC Admin only).\n"
        "• <code>/reload</code> - Reload the group admin cache.\n\n"
        "🏆 <b>Analytics & Profile:</b>\n"
        "• <code>/profile</code> - View your musical player card.\n"
        "• <code>/mysongs</code> - List your top 10 most played songs.\n\n"
        "⚙️ <b>Global Owner Commands:</b>\n"
        "• <code>/botstats</code> - Display system and bot metrics.\n"
        "• <code>/approvemember [user]</code> - Globally approve a member.\n"
        "• <code>/unapprovemember [user]</code> - Remove global approval.\n"
        "• <code>/broadcast [text/reply]</code> - Broadcast to all chats.\n"
        "• <code>/restart</code> - Restart the bot instance."
    )
    
    photo_path = "assets/help.png"
    if os.path.exists(photo_path):
        await message.reply_photo(photo=photo_path, caption=caption, parse_mode="html")
    else:
        await message.reply_text(text=caption, parse_mode="html")

@bot.on_message(filters.command("play") & filters.group)
async def play_command(client: Client, message: Message):
    from pytgcalls.types import MediaStream
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    database.add_tracked_chat(chat_id, message.chat.title)
    
    if not await is_user_authorized(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> You do not have permission to play music in this group.")

    query = " ".join(message.command[1:])
    
    # Check if reply to audio file
    reply = message.reply_to_message
    if reply and (reply.audio or reply.voice):
        media = reply.audio or reply.voice
        duration = media.duration
        
        if duration > 3600:
            return await message.reply_text("❌ <b>Playback Rejected:</b> The audio file duration exceeds the maximum limit of <b>1 hour (3600 seconds)</b>.")
            
        processing_msg = await message.reply_text("📥 <b>Downloading audio file...</b>", parse_mode="html")
        try:
            file_path = await client.download_media(reply)
            file_path = os.path.abspath(file_path)
            title = media.file_name or (f"Audio_{media.file_unique_id[:6]}" if reply.audio else "Voice Message")
            thumbnail = "assets/queue.png"
            
            song_data = {
                "file": file_path,
                "title": title,
                "thumbnail": thumbnail,
                "duration": duration,
                "user_id": user_id,
                "username": username,
                "first_name": first_name
            }
            
            # Start streaming or queue
            is_playing = chat_id in PLAYING and PLAYING[chat_id]
            if is_playing:
                if chat_id not in QUEUE:
                    QUEUE[chat_id] = []
                QUEUE[chat_id].append(song_data)
                await message.reply_photo(
                    photo="assets/queue.png",
                    caption=f"📝 <b>Added to Queue:</b>\n"
                            f"🎼 <b>Title:</b> {title}\n"
                            f"👤 <b>Requested by:</b> <a href='tg://user?id={user_id}'>{first_name}</a>",
                    parse_mode="html"
                )
                await processing_msg.delete()
            else:
                await processing_msg.edit_text("🎵 <b>Connecting to Video Chat...</b>", parse_mode="html")
                await call_py.play(chat_id, MediaStream(file_path, video_flags=MediaStream.Flags.IGNORE))
                track_listen_start(chat_id, user_id, username, first_name, title, file_path, duration)
                
                # Start inactive checker task
                monitor_task = asyncio.create_task(active_chat_monitor(chat_id))
                ACTIVE_MONITORS[chat_id] = monitor_task
                
                await message.reply_photo(
                    photo="assets/queue.png",
                    caption=f"▶️ <b>Now playing:</b>\n"
                            f"🎼 <b>Title:</b> {title}\n"
                            f"👤 <b>Requested by:</b> <a href='tg://user?id={user_id}'>{first_name}</a>",
                    reply_markup=get_control_markup(),
                    parse_mode="html"
                )
                await processing_msg.delete()
        except Exception as e:
            await processing_msg.edit_text(f"❌ <b>Error:</b> {str(e)}", parse_mode="html")
        return

    if not query:
        return await message.reply_text("💡 <b>Usage:</b> <code>/play [song name/youtube url/reply to audio]</code>")
        
    processing_msg = await message.reply_text("🔎 <b>Searching & preparing stream...</b>", parse_mode="html")
    
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
        thumbnail = info.get('thumbnail', '')
        duration = info.get('duration', 0)
        
        # Check duration limit (1 hour)
        if duration > 3600:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except:
                pass
            return await processing_msg.edit_text("❌ <b>Playback Rejected:</b> The requested song duration exceeds the maximum limit of <b>1 hour (3600 seconds)</b>.")

        song_data = {
            "file": filepath,
            "title": title,
            "thumbnail": thumbnail,
            "duration": duration,
            "user_id": user_id,
            "username": username,
            "first_name": first_name
        }

        is_playing = chat_id in PLAYING and PLAYING[chat_id]
        if is_playing:
            if chat_id not in QUEUE:
                QUEUE[chat_id] = []
            QUEUE[chat_id].append(song_data)
            
            # Send queue card
            await message.reply_photo(
                photo="assets/queue.png",
                caption=f"📝 <b>Added to Queue:</b>\n"
                        f"🎼 <b>Title:</b> {title}\n"
                        f"👤 <b>Requested by:</b> <a href='tg://user?id={user_id}'>{first_name}</a>",
                parse_mode="html"
            )
            await processing_msg.delete()
        else:
            await processing_msg.edit_text("🎵 <b>Connecting to Video Chat...</b>", parse_mode="html")
            await call_py.play(chat_id, MediaStream(filepath, video_flags=MediaStream.Flags.IGNORE))
            track_listen_start(chat_id, user_id, username, first_name, title, filepath, duration)
            
            # Start inactive checker task
            monitor_task = asyncio.create_task(active_chat_monitor(chat_id))
            ACTIVE_MONITORS[chat_id] = monitor_task
            
            await message.reply_photo(
                photo="assets/queue.png",
                caption=f"▶️ <b>Now playing:</b>\n"
                        f"🎼 <b>Title:</b> {title}\n"
                        f"👤 <b>Requested by:</b> <a href='tg://user?id={user_id}'>{first_name}</a>",
                reply_markup=get_control_markup(),
                parse_mode="html"
            )
            await processing_msg.delete()

    except Exception as e:
        error_text = str(e)
        if "Sign in to confirm you\'re not a bot" in error_text or "cookies" in error_text.lower():
            error_text += (
                "\n\n⚠️ <b>System Note:</b> YouTube requires cookies. "
                "Specify a cookies.txt file inside config.py to bypass."
            )
        await processing_msg.edit_text(f"❌ <b>Search/Download Error:</b> {error_text}", parse_mode="html")

@bot.on_message(filters.command("forceplay") & filters.group)
async def forceplay_command(client: Client, message: Message):
    from pytgcalls.types import MediaStream
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    if not await is_user_admin(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> This command is restricted to group administrators.")

    if len(message.command) < 2:
        return await message.reply_text("💡 <b>Usage:</b> <code>/forceplay [song name/youtube url]</code>")

    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text("⚡ <b>Preparing immediate playback...</b>", parse_mode="html")

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
        thumbnail = info.get('thumbnail', '')
        duration = info.get('duration', 0)

        if duration > 3600:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except:
                pass
            return await processing_msg.edit_text("❌ <b>Playback Rejected:</b> The song exceeds the <b>1 hour</b> limit.")

        # Stop previous stream and play
        track_listen_stop(chat_id)
        await call_py.play(chat_id, MediaStream(filepath, video_flags=MediaStream.Flags.IGNORE))
        track_listen_start(chat_id, user_id, username, first_name, title, filepath, duration)
        
        # Start monitor if not already running
        if chat_id not in ACTIVE_MONITORS:
            monitor_task = asyncio.create_task(active_chat_monitor(chat_id))
            ACTIVE_MONITORS[chat_id] = monitor_task

        await message.reply_photo(
            photo="assets/queue.png",
            caption=f"⚡ <b>Forced Playback Activated!</b>\n"
                    f"🎼 <b>Title:</b> {title}\n"
                    f"👤 <b>Initiated by Admin:</b> <a href='tg://user?id={user_id}'>{first_name}</a>",
            reply_markup=get_control_markup(),
            parse_mode="html"
        )
        await processing_msg.delete()

    except Exception as e:
        await processing_msg.edit_text(f"❌ <b>Force Play Error:</b> {str(e)}", parse_mode="html")

@bot.on_message(filters.command("pause") & filters.group)
async def pause_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_authorized(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> You do not have playback control permissions.")

    if chat_id not in PLAYING or not PLAYING[chat_id]:
        return await message.reply_text("❌ <b>Error:</b> Nothing is currently streaming.")

    if PLAYING[chat_id]["is_paused"]:
        return await message.reply_text("⚠️ <b>Playback is already paused.</b>")

    track_listen_pause(chat_id)
    await call_py.pause_stream(chat_id)
    await message.reply_text("⏸️ <b>Stream paused successfully.</b>", parse_mode="html")

@bot.on_message(filters.command("resume") & filters.group)
async def resume_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_authorized(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> You do not have playback control permissions.")

    if chat_id not in PLAYING or not PLAYING[chat_id]:
        return await message.reply_text("❌ <b>Error:</b> Nothing is currently streaming.")

    if not PLAYING[chat_id]["is_paused"]:
        return await message.reply_text("⚠️ <b>Playback is already active.</b>")

    track_listen_resume(chat_id)
    await call_py.resume_stream(chat_id)
    await message.reply_text("▶️ <b>Playback resumed successfully.</b>", parse_mode="html")

@bot.on_message(filters.command("skip") & filters.group)
async def skip_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_authorized(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> You do not have playback control permissions.")

    if chat_id not in PLAYING or not PLAYING[chat_id]:
        return await message.reply_text("❌ <b>Error:</b> Nothing is streaming to skip.")

    await message.reply_text("⏭️ <b>Current song skipped by administrator.</b>", parse_mode="html")
    # Reset loop status for this slot so skip actually plays next
    LOOP[chat_id] = 0
    await play_next(chat_id)

@bot.on_message(filters.command("end") & filters.group)
async def end_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_authorized(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> You do not have playback control permissions.")

    await stop_playback(chat_id)
    
    caption = "⏹️ <b>Music playback ended</b> and queue cleared successfully."
    photo_path = "assets/end.png"
    if os.path.exists(photo_path):
        await message.reply_photo(photo=photo_path, caption=caption, parse_mode="html")
    else:
        await message.reply_text(text=caption, parse_mode="html")

@bot.on_message(filters.command("queue") & filters.group)
async def queue_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_authorized(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> You do not have queue viewing permissions.")

    text = "📋 <b>MusicVerse Playlist Queue</b>\n\n"
    
    if chat_id in PLAYING and PLAYING[chat_id]:
        text += f"▶️ <b>Now Playing:</b> {PLAYING[chat_id]['title']}\n\n"
    else:
        text += "⏸ <i>Player is currently idle.</i>\n\n"
        
    chat_queue = QUEUE.get(chat_id, [])
    if not chat_queue:
        text += "📝 <i>No songs are currently queued.</i>"
    else:
        for idx, song in enumerate(chat_queue[:15], start=1):
            text += f"{idx}. 🎼 <b>{song['title']}</b> - <a href='tg://user?id={song['user_id']}'>{song['first_name']}</a>\n"
        if len(chat_queue) > 15:
            text += f"\n<i>...and {len(chat_queue) - 15} more songs.</i>"
            
    photo_path = "assets/queue.png"
    if os.path.exists(photo_path):
        await message.reply_photo(photo=photo_path, caption=text, parse_mode="html")
    else:
        await message.reply_text(text=text, parse_mode="html")

@bot.on_message(filters.command("loop") & filters.group)
async def loop_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_authorized(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> You do not have permissions to loop.")

    if len(message.command) < 2:
        curr = LOOP.get(chat_id, 0)
        status = "Infinite Loop" if curr == -1 else f"{curr} times" if curr > 0 else "Disabled"
        return await message.reply_text(f"🔄 <b>Loop Status:</b> <code>{status}</code>\n💡 <i>Use:</i> <code>/loop [enable/disable/number]</code>", parse_mode="html")

    arg = message.command[1].lower()
    if arg == "enable":
        LOOP[chat_id] = -1
        status_text = "🔄 <b>Infinite loop enabled</b> for the current song."
    elif arg == "disable":
        LOOP[chat_id] = 0
        status_text = "🔄 <b>Looping disabled</b>."
    elif arg.isdigit():
        count = int(arg)
        if count <= 0:
            LOOP[chat_id] = 0
            status_text = "🔄 <b>Looping disabled</b>."
        else:
            LOOP[chat_id] = count
            status_text = f"🔄 <b>Loop enabled</b> for the next <b>{count} repetitions</b>."
    else:
        return await message.reply_text("❌ <b>Invalid Parameter:</b> Use <code>enable</code>, <code>disable</code>, or an integer count.")

    photo_path = "assets/loop.png"
    if os.path.exists(photo_path):
        await message.reply_photo(photo=photo_path, caption=status_text, parse_mode="html")
    else:
        await message.reply_text(text=status_text, parse_mode="html")

@bot.on_message(filters.command("seek") & filters.group)
async def seek_command(client: Client, message: Message):
    from pytgcalls.types import MediaStream
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_authorized(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> You do not have playback control permissions.")

    if chat_id not in PLAYING or not PLAYING[chat_id]:
        return await message.reply_text("❌ <b>Error:</b> Nothing is streaming currently.")

    if len(message.command) < 2:
        return await message.reply_text("💡 <b>Usage:</b> <code>/seek [seconds]</code>")

    try:
        seek_seconds = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ <b>Error:</b> Please specify a valid integer number of seconds.")

    play_info = PLAYING[chat_id]
    if play_info["is_paused"]:
        return await message.reply_text("❌ <b>Error:</b> Cannot seek while playback is paused.")

    elapsed = time.time() - play_info["start_time"]
    total_elapsed = play_info["seek_offset"] + elapsed
    new_position = total_elapsed + seek_seconds

    if new_position >= play_info["duration"]:
        await message.reply_text("⏭️ <b>Seek position exceeds duration.</b> Skipping to next song...")
        await play_next(chat_id)
    else:
        play_info["seek_offset"] = new_position
        play_info["start_time"] = time.time()
        try:
            await call_py.play(
                chat_id,
                MediaStream(
                    play_info["file"],
                    video_flags=MediaStream.Flags.IGNORE,
                    ffmpeg_parameters=f"---start -ss {new_position}"
                )
            )
            await message.reply_text(f"⏩ <b>Fast-forwarded:</b> Moved <b>{seek_seconds}s</b> ahead. Current position: <b>{new_position:.0f}s</b> / <b>{play_info['duration']}s</b>")
        except Exception as e:
            await message.reply_text(f"❌ <b>Seek Error:</b> {str(e)}")

@bot.on_message(filters.command("seekback") & filters.group)
async def seekback_command(client: Client, message: Message):
    from pytgcalls.types import MediaStream
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_authorized(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> You do not have playback control permissions.")

    if chat_id not in PLAYING or not PLAYING[chat_id]:
        return await message.reply_text("❌ <b>Error:</b> Nothing is streaming currently.")

    if len(message.command) < 2:
        return await message.reply_text("💡 <b>Usage:</b> <code>/seekback [seconds]</code>")

    try:
        seek_seconds = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ <b>Error:</b> Please specify a valid integer number of seconds.")

    play_info = PLAYING[chat_id]
    if play_info["is_paused"]:
        return await message.reply_text("❌ <b>Error:</b> Cannot seek while playback is paused.")

    elapsed = time.time() - play_info["start_time"]
    total_elapsed = play_info["seek_offset"] + elapsed
    new_position = max(0, total_elapsed - seek_seconds)

    play_info["seek_offset"] = new_position
    play_info["start_time"] = time.time()
    try:
        await call_py.play(
            chat_id,
            MediaStream(
                play_info["file"],
                video_flags=MediaStream.Flags.IGNORE,
                ffmpeg_parameters=f"---start -ss {new_position}"
            )
        )
        await message.reply_text(f"⏪ <b>Rewound:</b> Moved <b>{seek_seconds}s</b> backward. Current position: <b>{new_position:.0f}s</b> / <b>{play_info['duration']}s</b>")
    except Exception as e:
        await message.reply_text(f"❌ <b>Seek Error:</b> {str(e)}")

@bot.on_message(filters.command("auth") & filters.group)
async def auth_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_admin(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> Only group administrators can authorize members.")

    t_id, t_username, t_name = await resolve_target_user(client, message)
    if not t_id:
        return await message.reply_text("💡 <b>Usage:</b> Reply to a message, or use user ID or username: <code>/auth @username</code>")

    database.add_auth_user(chat_id, t_id, t_username)
    
    caption = f"🛡️ <b>User Authorized!</b>\n\n👤 <b>Name:</b> {t_name}\n🆔 <b>User ID:</b> <code>{t_id}</code>\n\n<i>Authorized users can pause, resume, skip, loop, and queue music.</i>"
    photo_path = "assets/auth.png"
    if os.path.exists(photo_path):
        await message.reply_photo(photo=photo_path, caption=caption, parse_mode="html")
    else:
        await message.reply_text(text=caption, parse_mode="html")

@bot.on_message(filters.command("unauth") & filters.group)
async def unauth_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_admin(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> Only group administrators can remove authorizations.")

    t_id, t_username, t_name = await resolve_target_user(client, message)
    if not t_id:
        return await message.reply_text("💡 <b>Usage:</b> Reply to a message, or use user ID or username: <code>/unauth @username</code>")

    database.remove_auth_user(chat_id, t_id)
    await message.reply_text(f"🛡️ <b>User Unauthorized:</b> {t_name} (<code>{t_id}</code>) has been removed from authorized users.", parse_mode="html")

@bot.on_message(filters.command("reload") & filters.group)
async def reload_command(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not await is_user_admin(chat_id, user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> Restrictive admin cache reload command.")

    database.clear_admin_cache(chat_id)
    # Re-fetch admins to populate cache
    try:
        admins = await bot.get_chat_members(chat_id, filter=ChatMembersFilter.ADMINISTRATORS)
        admin_ids = [a.user.id for a in admins]
        database.set_cached_admins(chat_id, admin_ids)
        await message.reply_text("🔄 <b>Admin cache reloaded successfully!</b> Cached list updated.", parse_mode="html")
    except Exception as e:
        await message.reply_text(f"❌ <b>Cache Reload Failed:</b> {str(e)}", parse_mode="html")

@bot.on_message(filters.command("approvemember") & filters.user(OWNER_ID))
async def approvemember_command(client: Client, message: Message):
    t_id, t_username, t_name = await resolve_target_user(client, message)
    if not t_id:
        return await message.reply_text("💡 <b>Usage:</b> <code>/approvemember [reply/userid/username]</code> (Owner only)")

    database.add_approved_member(t_id, t_username)
    await message.reply_text(f"🌟 <b>Member Approved:</b> {t_name} (<code>{t_id}</code>) has been granted global bot administrator privileges.", parse_mode="html")

@bot.on_message(filters.command("unapprovemember") & filters.user(OWNER_ID))
async def unapprovemember_command(client: Client, message: Message):
    t_id, t_username, t_name = await resolve_target_user(client, message)
    if not t_id:
        return await message.reply_text("💡 <b>Usage:</b> <code>/unapprovemember [reply/userid/username]</code> (Owner only)")

    database.remove_approved_member(t_id)
    await message.reply_text(f"🌟 <b>Member Unapproved:</b> {t_name} (<code>{t_id}</code>) has been removed from global bot administrators.", parse_mode="html")

@bot.on_message(filters.command("broadcast"))
async def broadcast_command(client: Client, message: Message):
    user_id = message.from_user.id
    # Check if owner or globally approved
    if user_id != OWNER_ID and not database.is_approved_member(user_id):
        return await message.reply_text("❌ <b>Access Denied:</b> This command is restricted to the bot owner or approved members.")

    if not message.reply_to_message and len(message.command) < 2:
        return await message.reply_text("💡 <b>Usage:</b> Reply to a message or provide text: <code>/broadcast [text]</code>")

    processing_msg = await message.reply_text("⏳ <b>Broadcasting message to all chats...</b>", parse_mode="html")
    
    chats = database.get_tracked_chats()
    success = 0
    failed = 0
    
    for c_id in chats:
        try:
            if message.reply_to_message:
                await message.reply_to_message.copy(c_id)
            else:
                text = message.text.split(None, 1)[1]
                await bot.send_message(c_id, text)
            success += 1
            await asyncio.sleep(0.1)
        except Exception:
            failed += 1
            
    await processing_msg.edit_text(
        f"✅ <b>Broadcast Completed!</b>\n\n"
        f"🎯 <b>Success:</b> <code>{success}</code>\n"
        f"❌ <b>Failed:</b> <code>{failed}</code>",
        parse_mode="html"
    )

@bot.on_message(filters.command("botstats"))
async def botstats_command(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id != OWNER_ID:
        return await message.reply_text("❌ <b>Access Denied:</b> This command is restricted to the bot owner.")

    uptime = time.time() - START_TIME
    m, s = divmod(int(uptime), 60)
    h, m = divmod(m, 60)
    uptime_str = f"{h}h {m}m {s}s"
    
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory().percent
    active_chats = sum(1 for v in PLAYING.values() if v)
    total_chats = len(database.get_tracked_chats())
    
    # Database stats
    with database.get_db() as conn:
        users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        song_plays = conn.execute("SELECT SUM(play_count) FROM song_stats").fetchone()[0] or 0
        
    stats = (
        f"📊 <b>MusicVerse Bot Statistics</b>\n\n"
        f"⏱️ <b>System Uptime:</b> <code>{uptime_str}</code>\n"
        f"🖥️ <b>CPU Utilization:</b> <code>{cpu}%</code>\n"
        f"💾 <b>RAM Consumption:</b> <code>{ram}%</code>\n"
        f"🎵 <b>Active Call Streams:</b> <code>{active_chats}</code>\n"
        f"👥 <b>Tracked Group Chats:</b> <code>{total_chats}</code>\n"
        f"👤 <b>Registered Database Users:</b> <code>{users_count}</code>\n"
        f"💿 <b>Total Songs Streamed:</b> <code>{song_plays}</code>"
    )
    await message.reply_text(stats, parse_mode="html")

@bot.on_message(filters.command("mysongs"))
async def mysongs_command(client: Client, message: Message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    
    # Save user info to database
    database.ensure_user(user_id, message.from_user.username, first_name)
    
    songs = database.get_top_songs(user_id, limit=10)
    if not songs:
        return await message.reply_text("💿 <b>You haven't streamed any songs yet!</b> Start playing to view metrics.", parse_mode="html")
        
    text = f"🏆 <b>Top 10 Most Played Songs by {first_name}</b>\n\n"
    for idx, song in enumerate(songs, start=1):
        text += f"{idx}. 🎼 <b>{song['song_title']}</b> — Played <code>{song['play_count']}</code> times\n"
        
    await message.reply_text(text, parse_mode="html")

@bot.on_message(filters.command("profile"))
async def profile_command(client: Client, message: Message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    username = message.from_user.username
    
    # Save user info to database
    database.ensure_user(user_id, username, first_name)
    
    processing_msg = await message.reply_text("🎨 <b>Generating player card, please wait...</b>", parse_mode="html")
    
    # Fetch database stats
    profile = database.get_user_profile(user_id)
    if not profile:
        profile = {
            "user_id": user_id,
            "username": username or "N/A",
            "first_name": first_name,
            "total_listen_seconds": 0,
            "favorite_song": "None",
            "favorite_plays": 0
        }
        
    # Get avatar photo
    avatar_path = None
    try:
        user_obj = await client.get_users(user_id)
        if user_obj.photo and user_obj.photo.big_file_id:
            avatar_path = await client.download_media(user_obj.photo.big_file_id)
    except Exception as e:
        logging.error(f"Error fetching user photo: {e}")
        
    output_path = f"profile_{user_id}.jpg"
    
    try:
        # Run image generation in separate thread
        await asyncio.to_thread(
            generate_profile_card,
            avatar_path,
            user_id,
            username,
            first_name,
            profile["total_listen_seconds"],
            profile["favorite_song"],
            profile["favorite_plays"],
            output_path
        )
        
        # Send photo
        caption = (
            f"👤 <b>MusicVerse Player Profile</b>\n\n"
            f"🏷️ <b>Name Tag:</b> <a href='tg://user?id={user_id}'>{first_name}</a>\n"
            f"🆔 <b>Player ID:</b> <code>{user_id}</code>\n"
            f"⏱️ <b>Time Listened:</b> <code>{profile['total_listen_seconds'] / 3600:.2f} Hours</code>\n"
            f"💿 <b>Most Played Song:</b> <i>{profile['favorite_song']}</i> (<b>{profile['favorite_plays']} plays</b>)"
        )
        await message.reply_photo(photo=output_path, caption=caption, parse_mode="html")
        await processing_msg.delete()
        
    except Exception as e:
        await processing_msg.edit_text(f"❌ <b>Card Generation Failed:</b> {str(e)}", parse_mode="html")
        
    # Clean up files
    try:
        if avatar_path and os.path.exists(avatar_path):
            os.remove(avatar_path)
        if os.path.exists(output_path):
            os.remove(output_path)
    except:
        pass

@bot.on_message(filters.command("restart") & filters.user(OWNER_ID))
async def restart_command(client: Client, message: Message):
    caption = "🔄 <b>Rebooting MusicVerse System...</b>"
    photo_path = "assets/restart.png"
    if os.path.exists(photo_path):
        await message.reply_photo(photo=photo_path, caption=caption, parse_mode="html")
    else:
        await message.reply_text(text=caption, parse_mode="html")
        
    # Clean shutdown and execv restart
    logging.info("Restart command received. rebooting...")
    # Leave active calls
    for chat_id in list(PLAYING.keys()):
        try:
            await call_py.leave_call(chat_id)
        except:
            pass
            
    await bot.stop()
    await user.stop()
    os.execv(sys.executable, ['python'] + sys.argv)

# --- Group Join & PM Start Logging ---

@bot.on_chat_member_updated(group=2)
async def chat_member_updated_handler(client: Client, update):
    if not update.new_chat_member:
        return
        
    # If the bot itself was added to a group
    if update.new_chat_member.user.id == bot.me.id:
        chat_id = update.chat.id
        title = update.chat.title
        added_by_name = "Someone"
        added_by_id = 0
        
        # Track group in DB
        database.add_tracked_chat(chat_id, title)
        
        if update.from_user:
            added_by_name = update.from_user.first_name
            added_by_id = update.from_user.id
            
        logging.info(f"Bot added to group {title} ({chat_id}) by {added_by_name} ({added_by_id})")
        
        # Log to support group
        if SUPPORT_GROUP_ID:
            try:
                log_msg = (
                    "📥 <b>Bot Added to Group!</b>\n\n"
                    f"👥 <b>Group Name:</b> {title}\n"
                    f"🆔 <b>Group ID:</b> <code>{chat_id}</code>\n"
                    f"👤 <b>Added by:</b> {added_by_name} (<code>{added_by_id}</code>)"
                )
                await bot.send_message(SUPPORT_GROUP_ID, log_msg, parse_mode="html")
            except Exception as e:
                logging.error(f"Failed to log group addition: {e}")

# --- Callback Queries ---

@bot.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    
    # If starting help guide in private message
    if query.data == "help_callback":
        help_text = (
            "📚 <b>MusicVerse Help Guide</b>\n\n"
            "This bot plays music inside group voice chats. Add the bot to your group, "
            "make it an administrator (to read messages and manage voice chat), and start streaming!\n\n"
            "💬 <b>Basic Command:</b>\n"
            "• <code>/play [song name]</code> — Stream any song instantly in group VC."
        )
        return await query.edit_message_caption(caption=help_text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Start", callback_data="start_callback")]
        ]), parse_mode="html")
        
    if query.data == "start_callback":
        caption = (
            "☀️ <b>Welcome to MusicVerse</b> 🎵\n\n"
            "Elevate your Telegram group experience with high-fidelity, real-time music streaming in voice chats. "
            "<b>MusicVerse</b> is a feature-rich audio streaming client designed for communities, offering:\n\n"
            "🔹 <b>Seamless Playback:</b> Supports keywords, YouTube links, and audio files.\n"
            "🔹 <b>Queue Management:</b> Automated queuing with duration protection (max 1 hour).\n"
            "🔹 <b>Advanced Control:</b> Real-time pausing, resuming, skipping, and precise seeking.\n"
            "🔹 <b>Player Metrics:</b> Custom player profile cards and detailed top-played lists.\n"
            "🔹 <b>Admin Security:</b> Restrict bot access to authorized members or admins.\n\n"
            "ℹ️ <i>To get started, add me to your group and type /help to view all commands.</i>"
        )
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Add MusicVerse to Group", url=f"https://t.me/{bot.me.username}?startgroup=true")
            ],
            [
                InlineKeyboardButton("🛡️ Support Group", url="https://t.me/MusicVerseSupport"),
                InlineKeyboardButton("📚 Help Guide", callback_data="help_callback")
            ]
        ])
        return await query.edit_message_caption(caption=caption, reply_markup=markup, parse_mode="html")

    # Group playback control buttons
    if not await is_user_authorized(chat_id, user_id):
        return await query.answer("❌ You are not authorized to control playback in this chat.", show_alert=True)
        
    data = query.data
    if data == "pause":
        if chat_id in PLAYING and PLAYING[chat_id]:
            if PLAYING[chat_id]["is_paused"]:
                return await query.answer("⚠️ Player is already paused.", show_alert=True)
            track_listen_pause(chat_id)
            await call_py.pause_stream(chat_id)
            await query.answer("Playback Paused ⏸️")
        else:
            await query.answer("❌ Nothing is playing.", show_alert=True)
            
    elif data == "resume":
        if chat_id in PLAYING and PLAYING[chat_id]:
            if not PLAYING[chat_id]["is_paused"]:
                return await query.answer("⚠️ Player is already active.", show_alert=True)
            track_listen_resume(chat_id)
            await call_py.resume_stream(chat_id)
            await query.answer("Playback Resumed ▶️")
        else:
            await query.answer("❌ Nothing is playing.", show_alert=True)
            
    elif data == "skip":
        if chat_id in PLAYING and PLAYING[chat_id]:
            await query.answer("Skipped ⏭️")
            LOOP[chat_id] = 0
            await play_next(chat_id)
        else:
            await query.answer("❌ Nothing is playing.", show_alert=True)
            
    elif data == "end":
        await stop_playback(chat_id)
        await query.answer("Playback Ended ⏹️")
        await query.message.reply_text("⏹️ <b>Music playback ended</b> and queue cleared by user request.", parse_mode="html")
        
    elif data == "loop_toggle":
        curr = LOOP.get(chat_id, 0)
        if curr == 0:
            LOOP[chat_id] = -1
            await query.answer("🔂 Infinite Loop Enabled", show_alert=True)
        else:
            LOOP[chat_id] = 0
            await query.answer("🔄 Loop Disabled", show_alert=True)
            
    elif data == "queue_view":
        chat_queue = QUEUE.get(chat_id, [])
        text = "📋 <b>MusicVerse Playlist Queue</b>\n\n"
        if chat_id in PLAYING and PLAYING[chat_id]:
            text += f"▶️ <b>Now Playing:</b> {PLAYING[chat_id]['title']}\n\n"
        else:
            text += "⏸ <i>Player is idle.</i>\n\n"
            
        if not chat_queue:
            text += "📝 <i>No songs are currently queued.</i>"
        else:
            for idx, song in enumerate(chat_queue[:10], start=1):
                text += f"{idx}. 🎼 <b>{song['title']}</b> - <a href='tg://user?id={song['user_id']}'>{song['first_name']}</a>\n"
            if len(chat_queue) > 10:
                text += f"\n<i>...and {len(chat_queue) - 10} more.</i>"
        
        await bot.send_message(chat_id, text, parse_mode="html")
        await query.answer("Queue retrieved.")

# --- Startup & Main Execution ---

async def main():
    global call_py, user_id_self
    
    # Import pytgcalls and stream end events dynamically
    # to guarantee we catch DLL blocks and handle cleanly on startup
    try:
        from pytgcalls import PyTgCalls
        from pytgcalls.types import Update, StreamEnded
        
        # Define stream ended callback inside main
        @PyTgCalls.on_update()
        async def stream_handler(client, update):
            if isinstance(update, StreamEnded):
                chat_id = update.chat_id
                await play_next(chat_id)
    except Exception as e:
        logging.error(f"Failed to import/bind PyTgCalls due to OS limitations: {e}")
        print("\n" + "=" * 50)
        print("CRITICAL SYSTEM NOTE:")
        print("pytgcalls failed to import due to Windows Application Control restrictions.")
        print("This is normal for local testing under restricted permissions on Windows.")
        print("The bot will load commands and database functions, but streaming will be inactive.")
        print("To deploy fully, run this script on a standard Linux VPS or unrestricted system.")
        print("=" * 50 + "\n")

    print("Starting bot client...")
    await bot.start()
    
    # Cache bot's dialogs to prevent "Peer id invalid" updates errors
    try:
        async for dialog in bot.get_dialogs(limit=50):
            pass
        print("Bot dialogs cached successfully.")
    except Exception as e:
        logging.warning(f"Failed to cache bot dialogs: {e}")
    
    # Set bot commands list
    try:
        await bot.set_bot_commands([
            BotCommand("start", "Initialize start landing page"),
            BotCommand("help", "Display guide & commands list"),
            BotCommand("play", "Play a song in voice chat"),
            BotCommand("forceplay", "Admin force play immediately"),
            BotCommand("pause", "Pause current streaming"),
            BotCommand("resume", "Resume paused playback"),
            BotCommand("skip", "Skip current playing song"),
            BotCommand("loop", "Enable/Disable song repeat"),
            BotCommand("queue", "View upcoming songs"),
            BotCommand("end", "Stop playback & leave voice chat"),
            BotCommand("seek", "Fast-forward playback"),
            BotCommand("seekback", "Rewind playback"),
            BotCommand("auth", "Authorize a user to control bot"),
            BotCommand("unauth", "Revoke user authorization"),
            BotCommand("reload", "Reset admin list cache"),
            BotCommand("mysongs", "List your top played songs"),
            BotCommand("profile", "Display player music card")
        ])
        print("Bot commands set successfully.")
    except Exception as e:
        print(f"Failed to set bot commands: {e}")
        
    print("Starting user account client...")
    await user.start()
    
    # Cache user's dialogs to prevent "Peer id invalid" updates errors
    try:
        async for dialog in user.get_dialogs(limit=100):
            pass
        print("User account dialogs cached successfully.")
    except Exception as e:
        logging.warning(f"Failed to cache user dialogs: {e}")
    
    try:
        user_me = await user.get_me()
        user_id_self = user_me.id
        print(f"User account log-in success: {user_me.first_name} ({user_id_self})")
    except Exception as e:
        print(f"User account log-in failed: {e}")

    # Database Initialization
    database.init_db()

    # PyTgCalls service startup
    if 'PyTgCalls' in locals():
        try:
            call_py = PyTgCalls(user)
            print("Starting PyTgCalls service...")
            await call_py.start()
            print("PyTgCalls running successfully!")
        except Exception as e:
            print(f"Failed to run PyTgCalls service: {e}")

    print("MusicVerse Bot is now fully online!")
    await idle()
    
    print("Shutting down clients...")
    await bot.stop()
    await user.stop()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("Process exited.")
