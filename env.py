import os
from dotenv import load_dotenv

# .env file ထဲက variables တွေကို local မှာ run ရင် load လုပ်မယ်
# Render ပေါ်မှာ run ရင် .env file မရှိတဲ့အတွက် ဒီ function က ဘာမှမလုပ်ပါဘူး
load_dotenv()

# Environment variables တွေကို os.environ ကနေ ဖတ်မယ်
# Render ကနေ ထည့်ပေးလိုက်တဲ့ variables တွေက ဒီနေရာကနေ ဝင်လာမှာပါ
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID_STR = os.environ.get("ADMIN_ID")
ADMIN_GROUP_ID_STR = os.environ.get("ADMIN_GROUP_ID")
MONGO_URI = os.environ.get("MONGO_URI")

# --- Variables Validation ---
ADMIN_ID = 0
if ADMIN_ID_STR and ADMIN_ID_STR.isdigit():
    ADMIN_ID = int(ADMIN_ID_STR)
else:
    print("⚠️ WARNING: ADMIN_ID is not set or not a number in environment variables.")

ADMIN_GROUP_ID = 0
if ADMIN_GROUP_ID_STR:
    try:
        ADMIN_GROUP_ID = int(ADMIN_GROUP_ID_STR)
    except ValueError:
        print(f"⚠️ WARNING: ADMIN_GROUP_ID '{ADMIN_GROUP_ID_STR}' is not a valid number.")
else:
    print("⚠️ WARNING: ADMIN_GROUP_ID is not set in environment variables.")


if not BOT_TOKEN:
    print("❌ FATAL ERROR: BOT_TOKEN is not set in environment variables.")
    # (Optional) raise ValueError("BOT_TOKEN not found in environment")
if not MONGO_URI:
    print("❌ FATAL ERROR: MONGO_URI is not set in environment variables.")
    # (Optional) raise ValueError("MONGO_URI not found in environment")
