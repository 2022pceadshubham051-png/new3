import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from pytgcalls.types import MediaStream
from yt_dlp import YoutubeDL

# We import call_py from main so we can use it to join calls
from main import call_py

# yt-dlp configuration to extract audio stream
ydl_opts = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
}

@Client.on_message(filters.command("play") & filters.group)
async def play_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /play <song name or youtube link>")
    
    query = " ".join(message.command[1:])
    processing_msg = await message.reply_text("🔎 Searching...")

    try:
        def extract_info():
            with YoutubeDL(ydl_opts) as ydl:
                search_query = query if query.startswith("http") else f"ytsearch:{query}"
                info = ydl.extract_info(search_query, download=False)
                if 'entries' in info:
                    return info['entries'][0]
                return info

        # Run yt-dlp in a separate thread so it doesn't block the async loop
        info = await asyncio.to_thread(extract_info)
        audio_url = info['url']
        title = info.get('title', 'Unknown Title')
        
        await processing_msg.edit_text(f"🎵 Joining Voice Chat...\n**{title}**")
        
        # Join voice chat and stream the audio url
        await call_py.play(
            message.chat.id,
            MediaStream(audio_url)
        )
        await processing_msg.edit_text(f"▶️ Now playing: **{title}**")
        
    except Exception as e:
        await processing_msg.edit_text(f"❌ Error: {str(e)}")

@Client.on_message(filters.command("stop") & filters.group)
async def stop_command(client: Client, message: Message):
    try:
        await call_py.leave_call(message.chat.id)
        await message.reply_text("⏹️ Stopped playback and left the voice chat.")
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}\n\nMake sure I'm in a voice chat.")
