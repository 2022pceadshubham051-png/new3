import os

# Your Telegram API Credentials
API_ID = os.getenv("API_ID", "23275523")  # Replace with your API ID
API_HASH = os.getenv("API_HASH", "5f470dfdbebf920fe36b6bb4e8cc9053")  # Replace with your API Hash
BOT_TOKEN = os.getenv("BOT_TOKEN", "7762184752:AAHzUPp6NCw3vh0_m6XQP_pWLhdm0Gltdrc")  # Replace with your Bot Token

# A Pyrogram String Session for the User account that will join the Voice Chat
SESSION_STRING = os.getenv("SESSION_STRING", "AQFjKAMALWTmeg8kSlEpadxQP9C8M3SiuH_45DgGQ5AcHao9_yWjWBzZRbY2qVXOdlminpjqll5qjGewMU_z5r53OpEUNAVOzd8c2F6ccLOQ_stAENBMEoqvPbBl81cx_rtrk5f1sRItJ_7GOIHuEMUrSmW-0QEBGL920VUqhbfdYiLpP03VPxz5ndfKbahJgQSzM_rOyAklDJhrnk7lKWKMpxcJEq68fetjIbpj6PchwE2t5UVJqTgo6T5qEdjE2AZzzeXv-dIducxhqCmgCxE12dwsrq6D2KAwOUZPqmBWS_Am5Kzm3lywVaRTSkwiJziCmk8qN6VceS6d0oThe64b8JAo4wAAAAIGs56sAA")
# Command prefix
PREFIX = "/"

# Bot Owner ID
OWNER_ID = int(os.getenv("OWNER_ID", "8702369452"))

# Support / Log Group Chat ID where new group additions & users are logged
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID", "-1003740536853"))

# Cookies file path for yt-dlp to bypass YouTube limits
YTDL_COOKIEFILE = os.getenv("YTDL_COOKIEFILE", "")
