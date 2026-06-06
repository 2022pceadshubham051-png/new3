import asyncio
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
from pyrogram import Client

API_ID = "23275523"
API_HASH = "5f470dfdbebf920fe36b6bb4e8cc9053"

async def main():
    print("Starting Session Generator...")
    print("Apna phone number (country code ke sath jaise +91...) enter karein jab pucha jaye.")
    
    app = Client("my_account", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    
    await app.start()
    string_session = await app.export_session_string()
    
    print("\n" + "=" * 50)
    print(">> YAHAN HAI AAPKA STRING SESSION <<")
    print("=" * 50 + "\n")
    print(string_session)
    print("\n" + "=" * 50)
    print("Upar wale code ko copy karein aur usko config.py file me SESSION_STRING ki jagah dal dein.")
    
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
