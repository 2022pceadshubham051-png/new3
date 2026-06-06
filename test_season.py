import asyncio
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
from pyrogram import Client
from config import API_ID, API_HASH, SESSION_STRING

async def main():
    app = Client("in_memory_test", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, in_memory=True)
    try:
        await app.start()
        print("SESSION_VALID")
        await app.stop()
    except Exception as e:
        print(f"SESSION_INVALID: {e}")

asyncio.run(main())
