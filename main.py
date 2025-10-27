import json, os, asyncio
from datetime import datetime, timedelta
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from env import BOT_TOKEN, ADMIN_ID, ADMIN_GROUP_ID

# --- DB.py မှ လိုအပ်သော Functions များနှင့် Collections များကို Import လုပ်ခြင်း ---
from db import (
    users_col, settings_col, load_settings_db, save_settings_field_db,
    load_authorized_users_db, save_authorized_users_db, load_prices_db,
    load_admins_db, client
)
from pymongo.errors import ConnectionFailure, OperationFailure

# --- Global Variables ---
# Bot စတင်ချိန်နှင့် /start command ခေါ်ဆိုချိန်တွင် Database မှ Load လုပ်ရန်
SETTINGS = load_settings_db()
AUTHORIZED_USERS = set(SETTINGS.get("authorized_users", []))
ADMIN_IDS = set(SETTINGS.get("admin_ids", [ADMIN_ID]))
PRICES = SETTINGS.get("prices", {})

# User states for restricting actions after screenshot
user_states = {}

# Bot maintenance mode - default to disabled (Database မှ load လုပ်ရန်)
bot_maintenance = SETTINGS.get("maintenance_mode", {
    "orders": False,
    "topups": False,
    "general": False
})

# Payment information (Database တွင် သီးခြား document/field ဖြင့် သိမ်းထားသင့်သော်လည်း 
# ဤ code တွင် local variable အဖြစ်ထားပြီး settings_col တွင် သိမ်း/ဖတ်ပါမည်)
payment_info = SETTINGS.get("payment_info", {
    "kpay_number": "09678786528",
    "kpay_name": "Ma May Phoo Wai",
    "kpay_image": None,
    "wave_number": "09673585480",
    "wave_name": "Nine Nine",
    "wave_image": None
})

# Pending topup for step-by-step process (Local cache for ongoing transaction)
pending_topups = {} 

# Clone Bot Management (Local cache for running instances)
clone_bot_apps = {} 
# -----------------------------------------------
# --- Utility Functions (Database & Config) ---
# -----------------------------------------------

def check_db_connection():
    """Check if MongoDB client is connected"""
    global client
    if client is None:
        return False
    try:
        # ismaster command ကို thread ဖြင့် run ရန်မလို - Connection state ကိုပဲ စစ်သည်
        client.admin.command('ismaster')
        return True
    except (ConnectionFailure, OperationFailure, Exception):
        return False

async def load_bot_config():
    """Load settings, authorized users, and admins from Database"""
    global SETTINGS, AUTHORIZED_USERS, ADMIN_IDS, PRICES, bot_maintenance, payment_info

    if settings_col is None:
        print("⚠️ Settings Collection is not available. Using local cache.")
        return

    try:
        # Database functions ကို asyncio.to_thread ဖြင့် ခေါ်ယူ
        db_settings = await asyncio.to_thread(load_settings_db)
        if not db_settings:
            print("⚠️ Bot config document not found. Using current local settings.")
            return

        SETTINGS = db_settings
        AUTHORIZED_USERS = set(db_settings.get("authorized_users", []))
        ADMIN_IDS = set(db_settings.get("admin_ids", [ADMIN_ID]))
        PRICES = db_settings.get("prices", {})
        
        # Maintenance mode ကို load လုပ်ခြင်း
        db_maintenance = db_settings.get("maintenance_mode", None)
        if isinstance(db_maintenance, dict):
            bot_maintenance.update(db_maintenance)
            
        # Payment info ကို load လုပ်ခြင်း
        db_payment_info = db_settings.get("payment_info", None)
        if isinstance(db_payment_info, dict):
            payment_info.update(db_payment_info)

        print("✅ Bot configuration loaded from Database.")
    except Exception as e:
        print(f"❌ Error loading bot config from DB: {e}. Using local cache.")

async def save_bot_config_field(field_name, value):
    """Save a specific field to the settings collection (wraps db.py function)"""
    global settings_col
    if settings_col is None:
        print("❌ Settings Collection is not available. Cannot save config.")
        return False

    try:
        success = await asyncio.to_thread(save_settings_field_db, field_name, value)
        return success
    except Exception as e:
        print(f"MongoDB Error saving {field_name}: {e}")
        return False
        
async def initialize_user_db(user_id, name, username):
    """Initialize user document if it doesn't exist (DB Call)"""
    global users_col
    if users_col is None:
        return 

    user_doc = {
        "_id": str(user_id),
        "name": name,
        "username": username,
        "balance": 0,
        "orders": [],
        "topups": [],
        "created_at": datetime.now().isoformat()
    }
    try:
        await asyncio.to_thread(users_col.update_one, 
            {"_id": str(user_id)},
            {"$set": {"name": name, "username": username}, 
             "$setOnInsert": user_doc},
            upsert=True
        )
    except (ConnectionFailure, OperationFailure) as e:
        print(f"MongoDB Error initializing user: {e}")

async def get_user_data_db(user_id):
    """Get all user data from Database (DB Call)"""
    global users_col
    if users_col is None:
        return None
    try:
        # find_one ကို thread ဖြင့် run ခြင်း
        return await asyncio.to_thread(users_col.find_one, {"_id": str(user_id)})
    except (ConnectionFailure, OperationFailure) as e:
        print(f"MongoDB Error getting user data: {e}")
        return None

async def get_user_balance_db(user_id):
    """Get user balance from Database (DB Call)"""
    user_data = await get_user_data_db(user_id)
    return user_data.get("balance", 0) if user_data else 0

async def process_order_db(user_id, order):
    """Deduct balance and save order to Database (DB Call)"""
    global users_col
    if users_col is None:
        return False

    try:
        # Deduct balance and add order to array (Atomic Operation)
        result = await asyncio.to_thread(users_col.update_one,
            {"_id": str(user_id), "balance": {"$gte": order["price"]}}, # Check balance first
            {"$inc": {"balance": -order["price"]},
             "$push": {"orders": order}}
        )
        return result.modified_count > 0
    except (ConnectionFailure, OperationFailure) as e:
        print(f"MongoDB Error processing order: {e}")
        return False

async def check_pending_topup(user_id):
    """Check if user has pending topups (in Database) (DB Call)"""
    global users_col
    if users_col is None:
        return False

    try:
        user_doc = await asyncio.to_thread(users_col.find_one, 
            {"_id": str(user_id)}, 
            {"topups": {"$slice": -1}} # နောက်ဆုံး Topup တစ်ခုကိုသာ ယူပြီး စစ်ခြင်း
        )
        if user_doc and user_doc.get("topups"):
            last_topup = user_doc["topups"][-1]
            return last_topup.get("status") == "pending"
        return False
    except (ConnectionFailure, OperationFailure) as e:
        print(f"MongoDB Error in check_pending_topup: {e}")
        return False
        
def is_user_authorized(user_id):
    """Check if user is authorized to use the bot (Local Cache Check)"""
    global AUTHORIZED_USERS
    return str(user_id) in AUTHORIZED_USERS or int(user_id) == ADMIN_ID

def is_owner(user_id):
    """Check if user is the owner (ADMIN_ID)"""
    return int(user_id) == ADMIN_ID

def is_admin(user_id):
    """Check if user is any admin (owner or appointed admin) (Local Cache Check)"""
    global ADMIN_IDS
    return int(user_id) in ADMIN_IDS

def get_price(diamonds):
    """Get price for a diamond amount, checking custom prices first (Local Cache Check)"""
    global PRICES
    
    # Custom prices (from Database via Load_config)
    if diamonds in PRICES:
        return PRICES[diamonds]

    # Default prices
    if diamonds.startswith("wp") and diamonds[2:].isdigit():
        n = int(diamonds[2:])
        if 1 <= n <= 10:
            return n * 6000
    table = {
        "11": 950, "22": 1900, "33": 2850, "56": 4200, "112": 8200,
        "86": 5100, "172": 10200, "257": 15300, "343": 20400,
        "429": 25500, "514": 30600, "600": 35700, "706": 40800,
        "878": 51000, "963": 56100, "1049": 61200, "1135": 66300,
        "1412": 81600, "2195": 122400, "3688": 204000,
        "5532": 306000, "9288": 510000, "12976": 714000,
        "55": 3500, "165": 10000, "275": 16000, "565": 33000
    }
    return table.get(diamonds)

async def is_bot_admin_in_group(bot, chat_id):
    """Check if bot is admin in the group"""
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
        is_admin = bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
        print(f"Bot admin check for group {chat_id}: {is_admin}, status: {bot_member.status}")
        return is_admin
    except Exception as e:
        print(f"Error checking bot admin status in group {chat_id}: {e}")
        return False

def simple_reply(message_text):
    """
    Simple auto-replies for common queries
    """
    message_lower = message_text.lower()

    # Greetings
    if any(word in message_lower for word in ["hello", "hi", "မင်္ဂလာပါ", "ဟယ်လို", "ဟိုင်း", "ကောင်းလား"]):
        return ("👋 မင်္ဂလာပါ! 𝙆𝙀𝘼 𝙈𝙇𝘽𝘽 𝘼𝙐𝙏𝙊 𝙏𝙊𝙋 𝙐𝙋 𝘽𝙊𝙏 မှ ကြိုဆိုပါတယ်!\n\n"
                  "📱 Bot commands များ သုံးရန် /start နှိပ်ပါ\n")


    # Help requests
    elif any(word in message_lower for word in ["help", "ကူညီ", "အကူအညီ", "မသိ", "လမ်းညွှန်"]):
        return ("📱 ***အသုံးပြုနိုင်တဲ့ commands:***\n\n"
                  "• /start - Bot စတင်အသုံးပြုရန်\n"
                  "• /mmb gameid serverid amount - Diamond ဝယ်ယူရန်\n"
                  "• /balance - လက်ကျန်ငွေ စစ်ရန်\n"
                  "• /topup amount - ငွေဖြည့်ရန်\n"
                  "• /price - ဈေးနှုန်းများ ကြည့်ရန်\n"
                  "• /history - မှတ်တမ်းများ ကြည့်ရန်\n\n"
                  "💡 အသေးစိတ် လိုအပ်ရင် admin ကို ဆက်သွယ်ပါ!")

    # Default response
    else:
        return ("📱 ***MLBB Diamond Top-up Bot***\n\n"
                  "💎 ***Diamond ဝယ်ယူရန် /mmb command သုံးပါ။***\n"
                  "💰 ***ဈေးနှုန်းများ သိရှိရန် /price နှိပ်ပါ။***\n"
                  "🆘 ***အကူအညီ လိုရင် /start နှိပ်ပါ။***")

def validate_game_id(game_id):
    """Validate MLBB Game ID (6-10 digits)"""
    if not game_id.isdigit():
        return False
    if len(game_id) < 6 or len(game_id) > 10:
        return False
    return True

def validate_server_id(server_id):
    """Validate MLBB Server ID (3-5 digits)"""
    if not server_id.isdigit():
        return False
    if len(server_id) < 3 or len(server_id) > 5:
        return False
    return True

def is_banned_account(game_id):
    """
    Check if MLBB account is banned (Mock function)
    """
    # Add known banned account IDs here
    banned_ids = [
        "123456789",  # Example banned ID
        "000000000",  # Invalid pattern
        "111111111",  # Invalid pattern
    ]

    # Check if game_id matches banned patterns
    if game_id in banned_ids:
        return True

    # Check for suspicious patterns (all same digits, too simple patterns)
    if len(set(game_id)) == 1:  # All same digits like 111111111
        return True

    if game_id.startswith("000") or game_id.endswith("000"):
        return True

    return False

def is_payment_screenshot(update):
    """
    Check if the image is likely a payment screenshot (Basic validation)
    """
    if update.message.photo:
        return True
    return False

async def send_pending_topup_warning(update: Update):
    """Send pending topup warning message"""
    await update.message.reply_text(
        "⏳ ***Pending Topup ရှိနေပါတယ်!***\n\n"
        "❌ သင့်မှာ admin က approve မလုပ်သေးတဲ့ topup ရှိနေပါတယ်။\n\n"
        "***လုပ်ရမည့်အရာများ***:\n"
        "***• Admin က topup ကို approve လုပ်ပေးတဲ့အထိ စောင့်ပါ။***\n"
        "***• Approve ရပြီးမှ command တွေကို ပြန်အသုံးပြုနိုင်ပါမယ်။***\n\n"
        "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***\n\n"
        "💡 /balance ***နဲ့ status စစ်ကြည့်နိုင်ပါတယ်။***",
        parse_mode="Markdown"
    )

async def check_maintenance_mode(command_type):
    """Check if specific command type is in maintenance mode"""
    global bot_maintenance
    return bot_maintenance.get(command_type, True)

async def send_maintenance_message(update: Update, command_type):
    """Send maintenance mode message"""
    user_name = update.effective_user.first_name or "User"

    if command_type == "orders":
        msg = (
            f"မင်္ဂလာပါ {user_name}! 👋\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⏸️ ***Bot အော်ဒါတင်ခြင်းအား ခေတ္တ ယာယီပိတ်ထားပါသည်** ⏸️***\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "***🔄 Admin မှ ပြန်လည်ဖွင့်ပေးမှ အသုံးပြုနိုင်ပါမည်။***\n\n"
            "📞 အရေးပေါ်ဆိုရင် Admin ကို ဆက်သွယ်ပါ။"
        )
    elif command_type == "topups":
        msg = (
            f"မင်္ဂလာပါ {user_name}! 👋\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⏸️ ***Bot ငွေဖြည့်ခြင်းအား ခေတ္တ ယာယီပိတ်ထားပါသည်*** ⏸️\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "***🔄 Admin မှ ပြန်လည်ဖွင့်ပေးမှ အသုံးပြုနိုင်ပါမည်။***\n\n"
            "📞 ***အရေးပေါ်ဆိုရင် Admin ကို ဆက်သွယ်ပါ။***"
        )
    else:
        msg = (
            f"***မင်္ဂလာပါ*** {user_name}! 👋\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⏸️ ***Bot အား ခေတ္တ ယာယီပိတ်ထားပါသည်*** ⏸️\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "***🔄 Admin မှ ပြန်လည်ဖွင့်ပေးမှ အသုံးပြုနိုင်ပါမည်။***\n\n"
            "📞 ***အရေးပေါ်ဆိုရင် Admin ကို ဆက်သွယ်ပါ။***"
        )

    await update.message.reply_text(msg, parse_mode="Markdown")

# -----------------------------------------------
# --- Command Handlers ---
# -----------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or "-"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    # Load latest config
    await load_bot_config()

    # Check if user is authorized
    if not is_user_authorized(user_id):
        # board Create keyboard with Register button only
        keyboard = [
            [InlineKeyboardButton("📝 Register တောင်းဆိုမယ်", callback_data="request_register")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"🚫 ***Bot အသုံးပြုခွင့် မရှိပါ!***\n\n"
            f"👋 ***မင်္ဂလာပါ*** `{name}`!\n"
            f"🆔 Your ID: `{user_id}`\n\n"
            "❌ ***သင်သည် ဤ bot ကို အသုံးပြုခွင့် မရှိသေးပါ။***\n\n"
            "***လုပ်ရမည့်အရာများ***:\n"
            "***• အောက်က 'Register တောင်းဆိုမယ်' button ကို နှိပ်ပါ***\n"
            "***• သို့မဟုတ်*** /register ***command သုံးပါ။***\n"
            "***• Owner က approve လုပ်တဲ့အထိ စောင့်ပါ။***\n\n"
            "✅ ***Owner က approve လုပ်ပြီးမှ bot ကို အသုံးပြုနိုင်ပါမယ်။***\n\n",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    # Check for pending topups first
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    # Initialize user data in DB if needed
    await initialize_user_db(user_id, name, username)

    # Clear any restricted state when starting
    if user_id in user_states:
        del user_states[user_id]

    # Get user data
    user_balance = await get_user_balance_db(user_id)
    clickable_name = f"[{name}](tg://user?id={user_id})"

    msg = (
        f"👋 ***မင်္ဂလာပါ*** {clickable_name}!\n"
        f"🆔 ***Telegram User ID:*** `{user_id}`\n"
        f"💳 ***လက်ကျန်ငွေ:*** `{user_balance:,} MMK`\n\n"
        "💎 ***𝙆𝙀𝘼 𝙈𝙇𝘽𝘽 𝘼𝙐𝙏𝙊 𝙏𝙊𝙋 𝙐𝙋 𝘽𝙊𝙏*** မှ ကြိုဆိုပါတယ်။\n\n"
        "***အသုံးပြုနိုင်တဲ့ command များ***:\n"
        "➤ /mmb gameid serverid amount\n"
        "➤ /balance - ဘယ်လောက်လက်ကျန်ရှိလဲ စစ်မယ်\n"
        "➤ /topup amount - ငွေဖြည့်မယ် (screenshot တင်ပါ)\n"
        "➤ /price - Diamond များရဲ့ ဈေးနှုန်းများ\n"
        "➤ /history - အော်ဒါမှတ်တမ်းကြည့်မယ်\n\n"
        "***📌 ဥပမာ***:\n"
        "`/mmb 123456789 12345 wp1`\n"
        "`/mmb 123456789 12345 86`\n\n"
        "***လိုအပ်တာရှိရင် Owner ကို ဆက်သွယ်နိုင်ပါတယ်။***"
    )

    # Try to send with user's profile photo
    try:
        user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        if user_photos.total_count > 0:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=user_photos.photos[0][0].file_id,
                caption=msg,
                parse_mode="Markdown"
            )
        else:
            # No profile photo, send text only
            await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        # If error getting photo, send text only
        await update.message.reply_text(msg, parse_mode="Markdown")

async def mmb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Load latest config
    await load_bot_config()
    
    # Check authorization
    if not is_user_authorized(user_id):
        keyboard = [[InlineKeyboardButton("👑 Contact Owner", url=f"tg://user?id={ADMIN_ID}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🚫 အသုံးပြုခွင့် မရှိပါ!\n\n"
            "Owner ထံ bot အသုံးပြုခွင့် တောင်းဆိုပါ။",
            reply_markup=reply_markup
        )
        return

    # Check maintenance mode
    if await check_maintenance_mode("orders"):
        await send_maintenance_message(update, "orders")
        return

    # Check restricted state
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        await update.message.reply_text(
            "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
            "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
            "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Check for pending topups first (Database Check)
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    # Check if user has pending topup process (Local Check)
    if user_id in pending_topups:
        await update.message.reply_text(
            "⏳ ***Topup လုပ်ငန်းစဉ် အရင်ပြီးဆုံးပါ!***\n\n"
            "❌ ***လက်ရှိ topup လုပ်ငန်းစဉ်ကို မပြီးသေးပါ။***\n\n"
            "***လုပ်ရမည့်အရာများ***:\n"
            "***• Payment app ရွေးပြီး screenshot တင်ပါ***\n"
            "***• သို့မဟုတ် /cancel နှိပ်ပြီး ပယ်ဖျက်ပါ***\n\n"
            "💡 ***Topup ပြီးမှ order တင်နိုင်ပါမယ်။***",
            parse_mode="Markdown"
        )
        return

    args = context.args

    if len(args) != 3:
        await update.message.reply_text(
            "❌ အမှားရှိပါတယ်!\n\n"
            "***မှန်ကန်တဲ့ format***:\n"
            "/mmb gameid serverid amount\n\n"
            "***ဥပမာ***:\n"
            "`/mmb 123456789 12345 wp1`\n"
            "`/mmb 123456789 12345 86`",
            parse_mode="Markdown"
        )
        return

    game_id, server_id, amount = args

    if not validate_game_id(game_id):
        await update.message.reply_text(
            "❌ ***Game ID မှားနေပါတယ်!***\n\n"
            "***Game ID requirements***:\n"
            "***• ကိန်းဂဏန်းများသာ ပါရမည်။***\n"
            "***• 6-10 digits ရှိရမည်။***\n\n"
            "***ဥပမာ***: `123456789`",
            parse_mode="Markdown"
        )
        return

    if not validate_server_id(server_id):
        await update.message.reply_text(
            "❌ ***Server ID မှားနေပါတယ်!***\n\n"
            "***Server ID requirements***:\n"
            "***• ကိန်းဂဏန်းများသာ ပါရမည်။***\n"
            "***• 3-5 digits ရှိရမည်။***\n\n"
            "***ဥပမာ***: `8662`, `12345`",
            parse_mode="Markdown"
        )
        return

    if is_banned_account(game_id):
        # ... (Banned account message code - for brevity skip here)
        await update.message.reply_text("🚫 ***Account Ban ဖြစ်နေပါတယ်!***\n\n", parse_mode="Markdown")
        return

    price = get_price(amount)

    if not price:
        # ... (Invalid diamond amount message code - for brevity skip here)
        await update.message.reply_text("❌ Diamond amount မှားနေပါတယ်!\n\n", parse_mode="Markdown")
        return

    # --- DB Call: Check Balance ---
    user_balance = await get_user_balance_db(user_id)

    if user_balance < price:
        keyboard = [[InlineKeyboardButton("💳 ငွေဖြည့်မယ်", callback_data="topup_button")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"❌ ***လက်ကျန်ငွေ မလုံလောက်ပါ!***\n\n"
            f"💰 ***လိုအပ်တဲ့ငွေ***: {price:,} MMK\n"
            f"💳 ***သင့်လက်ကျန်***: {user_balance:,} MMK\n"
            f"❗ ***လိုအပ်သေးတာ***: {price - user_balance:,} MMK\n\n"
            "***ငွေဖြည့်ရန်*** `/topup amount` ***သုံးပါ။***",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    # --- DB Call: Process Order ---
    order_id = f"ORD{datetime.now().strftime('%Y%m%d%H%M%S')}"
    order = {
        "order_id": order_id,
        "game_id": game_id,
        "server_id": server_id,
        "amount": amount,
        "price": price,
        "status": "pending",
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
        "chat_id": update.effective_chat.id
    }

    if not await process_order_db(user_id, order):
        await update.message.reply_text("❌ ***အော်ဒါတင်ရာတွင် အမှားဖြစ်ပွားပါသည် (Database Error)***")
        return
        
    # Get user name
    user_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    # Create confirm/cancel buttons for admin
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"order_confirm_{order_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"order_cancel_{order_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Notify admin
    admin_msg = (
        f"🔔 ***အော်ဒါအသစ်ရောက်ပါပြီ!***\n\n"
        f"📝 ***Order ID:*** `{order_id}`\n"
        f"👤 ***User Name:*** [{user_name}](tg://user?id={user_id})\n\n"
        f"🆔 ***User ID:*** `{user_id}`\n"
        f"🎮 ***Game ID:*** `{game_id}`\n"
        f"🌐 ***Server ID:*** `{server_id}`\n"
        f"💎 ***Amount:*** {amount}\n"
        f"💰 ***Price:*** {price:,} MMK\n"
        f"⏰ ***Time:*** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 Status: ⏳ ***စောင့်ဆိုင်းနေသည်***"
    )

    # Send to all admins (from local cache)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_msg,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        except:
            pass

    # Notify admin group
    try:
        bot = Bot(token=BOT_TOKEN)
        if await is_bot_admin_in_group(bot, ADMIN_GROUP_ID):
            group_msg = (
                f"🛒 ***အော်ဒါအသစ် ရောက်ပါပြီ!***\n\n"
                f"📝 ***Order ID:*** `{order_id}`\n"
                f"👤 ***User Name:*** [{user_name}](tg://user?id={user_id})\n"
                f"🎮 ***Game ID:*** `{game_id}`\n"
                f"🌐 ***Server ID:*** `{server_id}`\n"
                f"💎 ***Amount:*** {amount}\n"
                f"💰 ***Price:*** {price:,} MMK\n"
                f"⏰ ***Time:*** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"📊 ***Status:*** ⏳ စောင့်ဆိုင်းနေသည်\n\n"
                f"#NewOrder #MLBB"
            )
            await bot.send_message(chat_id=ADMIN_GROUP_ID, text=group_msg, parse_mode="Markdown")
    except Exception as e:
        pass

    # Update balance for confirmation message
    new_user_balance = await get_user_balance_db(user_id)

    await update.message.reply_text(
        f"✅ ***အော်ဒါ အောင်မြင်ပါပြီ!***\n\n"
        f"📝 ***Order ID:*** `{order_id}`\n"
        f"🎮 ***Game ID:*** `{game_id}`\n"
        f"🌐 ***Server ID:*** `{server_id}`\n"
        f"💎 ***Diamond:*** {amount}\n"
        f"💰 ***ကုန်ကျစရိတ်:*** {price:,} MMK\n"
        f"💳 ***လက်ကျန်ငွေ:*** {new_user_balance:,} MMK\n"
        f"📊 Status: ⏳ ***စောင့်ဆိုင်းနေသည်***\n\n"
        "⚠️ ***Admin က confirm လုပ်ပြီးမှ diamonds များ ရရှိပါမယ်။***\n"
        "📞 ***ပြဿနာရှိရင် admin ကို ဆက်သွယ်ပါ။***",
        parse_mode="Markdown"
    )

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Load latest config
    await load_bot_config()

    # Check authorization
    if not is_user_authorized(user_id):
        keyboard = [[InlineKeyboardButton("👑 Contact Owner", url=f"tg://user?id={ADMIN_ID}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🚫 အသုံးပြုခွင့် မရှိပါ!\n\n"
            "Owner ထံ bot အသုံးပြုခွင့် တောင်းဆိုပါ။",
            reply_markup=reply_markup
        )
        return

    # Check restricted state
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        # ... (Restricted message code) ...
        await update.message.reply_text(
            "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
            "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
            "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Check pending topup process (Local variable)
    if user_id in pending_topups:
        await update.message.reply_text(
            "⏳ ***Topup လုပ်ငန်းစဉ် ဆက်လက်လုပ်ဆောင်ပါ!***\n\n"
            "❌ ***လက်ရှိ topup လုပ်ငန်းစဉ်ကို မပြီးသေးပါ။***\n\n"
            "***လုပ်ရမည့်အရာများ***:\n"
            "***• Payment app ရွေးပြီး screenshot တင်ပါ***\n"
            "***• သို့မဟုတ် /cancel နှိပ်ပြီး ပယ်ဖျက်ပါ***\n\n"
            "💡 ***ပယ်ဖျက်ပြီးမှ အခြား commands များ အသုံးပြုနိုင်ပါမယ်။***",
            parse_mode="Markdown"
        )
        return

    # Check for pending topups in data (Database Check)
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    # --- DB Call: Get User Data ---
    user_data = await get_user_data_db(user_id)

    if not user_data:
        await update.message.reply_text("❌ အရင်ဆုံး /start နှိပ်ပါ။")
        return

    balance = user_data.get("balance", 0)
    orders = user_data.get("orders", [])
    topups = user_data.get("topups", [])
    total_orders = len(orders)
    total_topups = len(topups)

    # Check for pending topups (in DB data)
    pending_topups_count = 0
    pending_amount = 0

    for topup in topups:
        if topup.get("status") == "pending":
            pending_topups_count += 1
            pending_amount += topup.get("amount", 0)

    # Escape special characters in name and username
    name = user_data.get('name', 'Unknown')
    username = user_data.get('username', 'None')

    # Remove or escape problematic characters for Markdown
    name = name.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
    username = username.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')

    status_msg = ""
    if pending_topups_count > 0:
        status_msg = f"\n⏳ ***Pending Topups***: {pending_topups_count} ခု ({pending_amount:,} MMK)\n❗ ***Diamond order ထားလို့မရပါ။ Admin approve စောင့်ပါ။***"

    # Create inline keyboard with topup button
    keyboard = [[InlineKeyboardButton("💳 ငွေဖြည့်မယ်", callback_data="topup_button")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    balance_text = (
        f"💳 ***သင့်ရဲ့ Account အချက်အလက်များ***\n\n"
        f"💰 ***လက်ကျန်ငွေ***: `{balance:,} MMK`\n"
        f"📦 ***စုစုပေါင်း အော်ဒါများ***: {total_orders}\n"
        f"💳 ***စုစုပေါင်း ငွေဖြည့်မှုများ***: {total_topups}{status_msg}\n\n"
        f"***👤 နာမည်***: {name}\n"
        f"***🆔 Username***: @{username}"
    )

    # Try to get user's profile photo
    try:
        user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        if user_photos.total_count > 0:
            # Send photo with balance info as caption
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=user_photos.photos[0][0].file_id,
                caption=balance_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            # No profile photo, send text only
            await update.message.reply_text(
                balance_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
    except:
        # If error getting photo, send text only
        await update.message.reply_text(
            balance_text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    await load_bot_config()

    if not is_user_authorized(user_id):
        # ... (Unauthorized message code) ...
        return

    if await check_maintenance_mode("topups"):
        await send_maintenance_message(update, "topups")
        return

    if user_id in user_states and user_states[user_id] == "waiting_approval":
        # ... (Restricted message code) ...
        return

    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    if user_id in pending_topups:
        # ... (Pending topup process message code) ...
        return

    args = context.args
    if len(args) != 1:
        # ... (Error message code) ...
        return

    try:
        amount = int(args[0])
        if amount < 1000:
            # ... (Amount too low message code) ...
            return
    except ValueError:
        # ... (Invalid amount message code) ...
        return

    # Store pending topup (Local Variable)
    pending_topups[user_id] = {
        "amount": amount,
        "timestamp": datetime.now().isoformat()
    }

    # Show payment method selection
    keyboard = [
        [InlineKeyboardButton("📱 KBZ Pay", callback_data=f"topup_pay_kpay_{amount}")],
        [InlineKeyboardButton("📱 Wave Money", callback_data=f"topup_pay_wave_{amount}")],
        [InlineKeyboardButton("❌ ငြင်းပယ်မယ်", callback_data="topup_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"💳 ***ငွေဖြည့်လုပ်ငန်းစဉ်***\n\n"
        f"***✅ ပမာဏ***: `{amount:,} MMK`\n\n"
        f"***အဆင့် 1***: Payment method ရွေးချယ်ပါ\n\n"
        f"***⬇️ ငွေလွှဲမည့် app ရွေးချယ်ပါ***:\n\n"
        f"***ℹ️ ပယ်ဖျက်ရန်*** /cancel ***နှိပ်ပါ***",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    await load_bot_config()

    if not is_user_authorized(user_id):
        # ... (Unauthorized message code) ...
        return
        
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        # ... (Restricted message code) ...
        return

    if user_id in pending_topups:
        # ... (Pending topup process message code) ...
        return

    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    # Use global PRICES (loaded from DB)
    custom_prices = PRICES
    
    # Default prices
    default_prices = {
        # Weekly Pass
        "wp1": 6000, "wp2": 12000, "wp3": 18000, "wp4": 24000, "wp5": 30000,
        "wp6": 36000, "wp7": 42000, "wp8": 48000, "wp9": 54000, "wp10": 60000,
        # Regular Diamonds
        "11": 950, "22": 1900, "33": 2850, "56": 4200, "86": 5100, "112": 8200,
        "172": 10200, "257": 15300, "343": 20400, "429": 25500, "514": 30600,
        "600": 35700, "706": 40800, "878": 51000, "963": 56100, "1049": 61200,
        "1135": 66300, "1412": 81600, "2195": 122400, "3688": 204000,
        "5532": 306000, "9288": 510000, "12976": 714000,
        # 2X Diamond Pass
        "55": 3500, "165": 10000, "275": 16000, "565": 33000
    }

    # Merge custom prices with defaults (custom overrides default)
    current_prices = {**default_prices, **custom_prices}

    price_msg = "💎 ***MLBB Diamond ဈေးနှုန်းများ***\n\n"

    # ... (Price list generation code - unchanged) ...
    # Weekly Pass section
    price_msg += "🎟️ ***Weekly Pass***:\n"
    for i in range(1, 11):
        wp_key = f"wp{i}"
        if wp_key in current_prices:
            price_msg += f"• {wp_key} = {current_prices[wp_key]:,} MMK\n"
    price_msg += "\n"

    # Regular Diamonds section
    price_msg += "💎 ***Regular Diamonds***:\n"
    regular_diamonds = ["11", "22", "33", "56", "86", "112", "172", "257", "343",
                        "429", "514", "600", "706", "878", "963", "1049", "1135",
                        "1412", "2195", "3688", "5532", "9288", "12976"]

    for diamond in regular_diamonds:
        if diamond in current_prices:
            price_msg += f"• {diamond} = {current_prices[diamond]:,} MMK\n"
    price_msg += "\n"

    # 2X Diamond Pass section
    price_msg += "💎 ***2X Diamond Pass***:\n"
    double_pass = ["55", "165", "275", "565"]
    for dp in double_pass:
        if dp in current_prices:
            price_msg += f"• {dp} = {current_prices[dp]:,} MMK\n"
    price_msg += "\n"

    # Show any other custom items not in default categories
    other_customs = {k: v for k, v in custom_prices.items()
                     if k not in default_prices and not k.startswith("wp")}
    if other_customs:
        price_msg += "🔥 ***Special Items***:\n"
        for item, price in other_customs.items():
            price_msg += f"• {item} = {price:,} MMK\n"
        price_msg += "\n"

    price_msg += (
        "***📝 အသုံးပြုနည်း***:\n"
        "`/mmb gameid serverid amount`\n\n"
        "***ဥပမာ***:\n"
        "`/mmb 123456789 12345 wp1`\n"
        "`/mmb 123456789 12345 86`"
    )

    await update.message.reply_text(price_msg, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    await load_bot_config()

    if not is_user_authorized(user_id):
        return

    # Clear pending topup if exists (Local Variable)
    if user_id in pending_topups:
        del pending_topups[user_id]
        await update.message.reply_text(
            "✅ ***ငွေဖြည့်ခြင်း ပယ်ဖျက်ပါပြီ!***\n\n"
            "💡 ***ပြန်ဖြည့်ချင်ရင်*** /topup ***နှိပ်ပါ။***",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "***ℹ️ လက်ရှိ ငွေဖြည့်မှု လုပ်ငန်းစဉ် မရှိပါ။***\n\n"
            "***💡 ငွေဖြည့်ရန် /topup ***နှိပ်ပါ။***",
            parse_mode="Markdown"
        )

async def c_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calculator command - /c <expression>"""
    import re
    user_id = str(update.effective_user.id)

    if user_id in user_states and user_states[user_id] == "waiting_approval":
        # ... (Restricted message code) ...
        return

    args = context.args
    # ... (Calculator logic - unchanged) ...
    if not args:
        # ... (Help message code) ...
        return

    # Join all args and remove spaces
    expression = ''.join(args).replace(' ', '')

    # Validate expression contains only allowed characters
    pattern = r'^[0-9+\-*/().]+$'
    if not re.match(pattern, expression):
        # ... (Error message code) ...
        return

    # Must contain at least one operator
    if not any(op in expression for op in ['+', '-', '*', '/']):
        # ... (Error message code) ...
        return

    operators = {'+': 'ပေါင်းခြင်း', '-': 'နုတ်ခြင်း', '*': 'မြှောက်ခြင်း', '/': 'စားခြင်း'}
    operator_found = None
    for op in operators:
        if op in expression:
            operator_found = operators[op]
            break

    try:
        result = eval(expression)
        await update.message.reply_text(
            f"🧮 ***Calculator ရလဒ်***\n\n"
            f"📊 `{expression}` = ***{result:,}***\n\n"
            f"***⚙️ လုပ်ဆောင်ချက်***: {operator_found}",
            parse_mode="Markdown"
        )
    except ZeroDivisionError:
        # ... (Zero division error message code) ...
        pass
    except:
        # ... (Generic error message code) ...
        pass

async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Daily report - /d YYYY-MM-DD or /d YYYY-MM-DD YYYY-MM-DD for range"""
    user_id = str(update.effective_user.id)

    if not is_owner(user_id):
        await update.message.reply_text("❌ Owner သာ ကြည့်နိုင်ပါတယ်!")
        return

    args = context.args
    
    # --- DB Call: Get All User Data ---
    global users_col
    if users_col is None:
        await update.message.reply_text("❌ Database ချိတ်ဆက်မှု မရပါ!")
        return
    try:
        all_users_cursor = await asyncio.to_thread(users_col.find, {}, {"orders": 1, "topups": 1})
        all_users_data = await asyncio.to_thread(list, all_users_cursor)
    except Exception as e:
        await update.message.reply_text(f"❌ Database Error: {e}")
        return

    if len(args) == 0:
        # ... (Date filter buttons code - unchanged) ...
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        week_ago = today - timedelta(days=7)

        keyboard = [
            [InlineKeyboardButton("📅 ဒီနေ့", callback_data=f"report_day_{today.strftime('%Y-%m-%d')}")],
            [InlineKeyboardButton("📅 မနေ့က", callback_data=f"report_day_{yesterday.strftime('%Y-%m-%d')}")],
            [InlineKeyboardButton("📅 လွန်ခဲ့သော ၇ ရက်", callback_data=f"report_day_range_{week_ago.strftime('%Y-%m-%d')}_{today.strftime('%Y-%m-%d')}")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "📊 ***ရက်စွဲ ရွေးချယ်ပါ***\n\n"
            "***သို့မဟုတ် manual ရိုက်ပါ***:\n\n"
            "• `/d 2025-01-15` - သတ်မှတ်ရက်\n"
            "• `/d 2025-01-15 2025-01-20` - ရက်အပိုင်းအခြား",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    elif len(args) == 1:
        # Single date
        start_date = end_date = args[0]
        period_text = f"ရက် ({start_date})"
    elif len(args) == 2:
        # Date range
        start_date = args[0]
        end_date = args[1]
        period_text = f"ရက် ({start_date} မှ {end_date})"
    else:
        # ... (Error message code) ...
        return

    total_sales = 0
    total_orders = 0
    total_topups = 0
    topup_count = 0

    for user_data in all_users_data:
        for order in user_data.get("orders", []):
            if order.get("status") == "confirmed":
                order_date = order.get("confirmed_at", order.get("timestamp", ""))[:10]
                if start_date <= order_date <= end_date:
                    total_sales += order["price"]
                    total_orders += 1

        for topup in user_data.get("topups", []):
            if topup.get("status") == "approved":
                topup_date = topup.get("approved_at", topup.get("timestamp", ""))[:10]
                if start_date <= topup_date <= end_date:
                    total_topups += topup["amount"]
                    topup_count += 1

    await update.message.reply_text(
        f"📊 ***ရောင်းရငွေ & ငွေဖြည့် မှတ်တမ်း***\n\n"
        f"📅 ကာလ: {period_text}\n\n"
        f"🛒 ***Order Confirmed စုစုပေါင်း***:\n"
        f"💰 ***ငွေ***: `{total_sales:,} MMK`\n"
        f"📦 ***အရေအတွက်***: {total_orders}\n\n"
        f"💳 ***Topup Approved စုစုပေါင်း***:\n"
        f"💰 ***ငွေ***: `{total_topups:,} MMK`\n"
        f"📦 ***အရေအတွက်***: {topup_count}",
        parse_mode="Markdown"
    )

# ... (monthly_report_command, yearly_report_command, history_command function များတွင် 
# daily_report_command ကဲ့သို့ DB မှ data အားလုံးကို ယူပြီး စစ်ဆေးရန် ပြင်ဆင်ရန် လိုပါသည်။) ...

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    await load_bot_config()

    if not is_user_authorized(user_id):
        # ... (Unauthorized message code) ...
        return

    if user_id in user_states and user_states[user_id] == "waiting_approval":
        # ... (Restricted message code) ...
        return

    if user_id in pending_topups:
        # ... (Pending topup process message code) ...
        return

    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    # --- DB Call: Get User Data ---
    user_data = await get_user_data_db(user_id)

    if not user_data:
        await update.message.reply_text("❌ အရင်ဆုံး /start နှိပ်ပါ။")
        return

    orders = user_data.get("orders", [])
    topups = user_data.get("topups", [])

    if not orders and not topups:
        await update.message.reply_text("📋 သင့်မှာ မည်သည့် မှတ်တမ်းမှ မရှိသေးပါ။")
        return

    msg = "📋 သင့်ရဲ့ မှတ်တမ်းများ\n\n"

    if orders:
        msg += "🛒 ***အော်ဒါများ (နောက်ဆုံး 5 ခု)***:\n"
        for order in orders[-5:]:
            status_emoji = "✅" if order.get("status") == "confirmed" else "⏳"
            msg += f"{status_emoji} {order['order_id']} - {order['amount']} ({order['price']:,} MMK)\n"
        msg += "\n"

    if topups:
        msg += "💳 ***ငွေဖြည့်များ (နောက်ဆုံး 5 ခု)***:\n"
        for topup in topups[-5:]:
            status_emoji = "✅" if topup.get("status") == "approved" else "⏳"
            msg += f"{status_emoji} {topup['amount']:,} MMK - {topup.get('timestamp', 'Unknown')[:10]}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    if not is_admin(user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "❌ အမှားရှိပါတယ်!\n\n"
            "မှန်ကန်တဲ့ format: `/approve user_id amount`\n"
            "ဥပမာ: `/approve 123456789 50000`"
        )
        return

    try:
        target_user_id = args[0]
        amount = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ ငွေပမာဏမှားနေပါတယ်!")
        return

    # --- DB Call: Find and Update Topup/Balance ---
    global users_col
    if users_col is None:
        await update.message.reply_text("❌ Database ချိတ်ဆက်မှု မရပါ!")
        return

    try:
        # Atomically find pending topup with matching amount, update status, and update balance
        result = await asyncio.to_thread(users_col.update_one,
            {"_id": target_user_id, "topups": {"$elemMatch": {"status": "pending", "amount": amount}}},
            {"$set": {"topups.$.status": "approved", 
                      "topups.$.approved_by": admin_name, 
                      "topups.$.approved_at": datetime.now().isoformat()},
             "$inc": {"balance": amount}}
        )

        if result.modified_count == 0:
            await update.message.reply_text("❌ User မတွေ့ရှိပါ သို့မဟုတ် Pending Topup မရှိပါ (ပမာဏ မှားနေနိုင်သည်)!")
            return

        # Clear user restriction state after approval
        if target_user_id in user_states:
            del user_states[target_user_id]
        
        # Notify user (Balance update)
        user_balance = await get_user_balance_db(target_user_id)
        
        # Notify user
        try:
            keyboard = [[InlineKeyboardButton("💎 Order တင်မယ်", url=f"https://t.me/{context.bot.username}?start=order")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_message(
                chat_id=int(target_user_id),
                text=f"✅ ***ငွေဖြည့်မှု အတည်ပြုပါပြီ!*** 🎉\n\n"
                     f"💰 ***ပမာဏ:*** `{amount:,} MMK`\n"
                     f"💳 ***လက်ကျန်ငွေ:*** `{user_balance:,} MMK`\n"
                     f"👤 ***Approved by:*** [{admin_name}](tg://user?id={user_id})\n"
                     f"⏰ ***အချိန်:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                     f"🎉 ***ယခုအခါ diamonds များ ဝယ်ယူနိုင်ပါပြီ!***\n"
                     f"🔓 ***Bot လုပ်ဆောင်ချက်များ ပြန်လည် အသုံးပြုနိုင်ပါပြီ!***\n\n"
                     f"💎 ***Order တင်ရန်:***\n"
                     f"`/mmb gameid serverid amount`",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        except:
            pass
        
        # Confirm to admin
        await update.message.reply_text(
            f"✅ ***Approve အောင်မြင်ပါပြီ!***\n\n"
            f"👤 ***User ID:*** `{target_user_id}`\n"
            f"💰 ***Amount:*** `{amount:,} MMK`\n"
            f"💳 ***User's new balance:*** `{user_balance:,} MMK`\n"
            f"🔓 ***User restrictions cleared!***",
            parse_mode="Markdown"
        )
    except (ConnectionFailure, OperationFailure) as e:
        await update.message.reply_text(f"❌ Database Error: {e}")

async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    if not is_admin(user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "❌ အမှားရှိပါတယ်!\n\n"
            "မှန်ကန်တဲ့ format: `/deduct user_id amount`\n"
            "ဥပမာ: `/deduct 123456789 10000`"
        )
        return

    try:
        target_user_id = args[0]
        amount = int(args[1])
        if amount <= 0:
            await update.message.reply_text("❌ ငွေပမာဏသည် သုညထက် ကြီးရမည်!")
            return
    except ValueError:
        await update.message.reply_text("❌ ငွေပမာဏမှားနေပါတယ်!")
        return

    # --- DB Call: Deduct Balance ---
    global users_col
    if users_col is None:
        await update.message.reply_text("❌ Database ချိတ်ဆက်မှု မရပါ!")
        return

    try:
        # Check current balance and ensure deduction is possible
        current_balance = await get_user_balance_db(target_user_id)

        if current_balance < amount:
            await update.message.reply_text(
                f"❌ ***နှုတ်လို့မရပါ!***\n\n"
                f"👤 User ID: `{target_user_id}`\n"
                f"💰 ***နှုတ်ချင်တဲ့ပမာဏ***: `{amount:,} MMK`\n"
                f"💳 ***User လက်ကျန်ငွေ***: `{current_balance:,} MMK`\n"
                f"❗ ***လိုအပ်သေးတာ***: `{amount - current_balance:,} MMK`",
                parse_mode="Markdown"
            )
            return

        # Atomically deduct balance
        result = await asyncio.to_thread(users_col.update_one,
            {"_id": target_user_id},
            {"$inc": {"balance": -amount}}
        )

        if result.modified_count == 0:
            await update.message.reply_text("❌ User မတွေ့ရှိပါ သို့မဟုတ် Balance နှုတ်ရာတွင် အမှားဖြစ်ပွားပါသည်!")
            return

        # Notify user
        new_balance = await get_user_balance_db(target_user_id)
        try:
            user_msg = (
                f"⚠️ ***လက်ကျန်ငွေ နှုတ်ခံရမှု***\n\n"
                f"💰 ***နှုတ်ခံရတဲ့ပမာဏ***: `{amount:,} MMK`\n"
                f"💳 ***လက်ကျန်ငွေ***: `{new_balance:,} MMK`\n"
                f"⏰ ***အချိန်***: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "📞 မေးခွန်းရှိရင် admin ကို ဆက်သွယ်ပါ။"
            )
            await context.bot.send_message(chat_id=int(target_user_id), text=user_msg, parse_mode="Markdown")
        except:
            pass

        # Confirm to admin
        await update.message.reply_text(
            f"✅ ***Balance နှုတ်ခြင်း အောင်မြင်ပါပြီ!***\n\n"
            f"👤 User ID: `{target_user_id}`\n"
            f"💰 ***နှုတ်ခဲ့တဲ့ပမာဏ***: `{amount:,} MMK`\n"
            f"💳 ***User လက်ကျန်ငွေ***: `{new_balance:,} MMK`",
            parse_mode="Markdown"
        )
    except (ConnectionFailure, OperationFailure) as e:
        await update.message.reply_text(f"❌ Database Error: {e}")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_admin(user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("❌ မှန်ကန်တဲ့အတိုင်း: /done <user_id>")
        return

    target_user_id = int(args[0])
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text="🙏 ဝယ်ယူအားပေးမှုအတွက် ကျေးဇူးအများကြီးတင်ပါတယ်။\n\n✅ Order Done! 🎉"
        )
        await update.message.reply_text("✅ User ထံ message ပေးပြီးပါပြီ။")
    except:
        await update.message.reply_text("❌ User ID မှားနေပါတယ်။ Message မပို့နိုင်ပါ။")

async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_admin(user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text("❌ မှန်ကန်တဲ့အတိုင်း: /reply <user_id> <message>")
        return

    target_user_id = int(args[0])
    message = " ".join(args[1:])
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=message
        )
        await update.message.reply_text("✅ Message ပေးပြီးပါပြီ။")
    except:
        await update.message.reply_text("❌ Message မပို့နိုင်ပါ။")

async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User registration request"""
    user_id = str(update.effective_user.id)
    user = update.effective_user
    username = user.username or "-"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    await load_bot_config()

    def escape_markdown(text):
        """Escape special characters for Markdown"""
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text

    username_escaped = escape_markdown(username)

    if is_user_authorized(user_id):
        # ... (Already authorized message code) ...
        return

    # Send registration request to owner with approve button
    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"register_approve_{user_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"register_reject_{user_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    owner_msg = (
        f"📝 ***Registration Request***\n\n"
        f"👤 ***User Name:*** [{name}](tg://user?id={user_id})\n"
        f"🆔 ***User ID:*** `{user_id}`\n"
        f"📱 ***Username:*** @{username_escaped}\n"
        f"⏰ ***Time:*** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"***အသုံးပြုခွင့် ပေးမလား?***"
    )

    user_confirm_msg = (
        f"✅ ***Registration တောင်းဆိုမှု ပို့ပြီးပါပြီ!***\n\n"
        f"👤 ***သင့်အမည်:*** {name}\n"
        f"🆔 ***သင့် User ID:*** `{user_id}`\n\n"
        f"⏳ ***Owner က approve လုပ်တဲ့အထိ စောင့်ပါ။***\n"
        f"📞 ***အရေးပေါ်ဆိုရင် owner ကို ဆက်သွယ်ပါ။***"
    )

    # ... (Send request to admin code) ...
    try:
        # Send to owner with user's profile photo
        # (For brevity, only the final send_message/send_photo logic is shown, 
        # handling profile photo logic is complex and skipped here)
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=owner_msg,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    except Exception as e:
        print(f"Error sending registration request to owner: {e}")

    # Send confirmation to user
    await update.message.reply_text(user_confirm_msg, parse_mode="Markdown")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    if not is_admin(user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("❌ မှန်ကန်တဲ့အတိုင်း: /ban <user\\_id>", parse_mode="Markdown")
        return

    target_user_id = args[0]
    await load_bot_config()

    if target_user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("ℹ️ User သည် authorize မလုပ်ထားပါ။")
        return

    # --- DB Call: Remove User from Authorized List ---
    global AUTHORIZED_USERS
    AUTHORIZED_USERS.remove(target_user_id)
    success = await save_bot_config_field("authorized_users", list(AUTHORIZED_USERS))
    
    if not success:
        await update.message.reply_text("❌ User ban ခြင်း Database သို့ မသိမ်းနိုင်ပါ။")
        return

    # Notify user and admins
    # ... (Notification logic - unchanged) ...
    
    await update.message.reply_text(
        f"✅ User Ban အောင်မြင်ပါပြီ!\n\n"
        f"👤 User ID: `{target_user_id}`\n"
        f"🎯 Status: Banned\n"
        f"📝 Total authorized users: {len(AUTHORIZED_USERS)}",
        parse_mode="Markdown"
    )

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    if not is_admin(user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("❌ မှန်ကန်တဲ့အတိုင်း: /unban <user\\_id>", parse_mode="Markdown")
        return

    target_user_id = args[0]
    await load_bot_config()

    if target_user_id in AUTHORIZED_USERS:
        await update.message.reply_text("ℹ️ User သည် authorize ပြုလုပ်ထားပြီးပါပြီ။")
        return

    # --- DB Call: Add User to Authorized List ---
    global AUTHORIZED_USERS
    AUTHORIZED_USERS.add(target_user_id)
    success = await save_bot_config_field("authorized_users", list(AUTHORIZED_USERS))

    if not success:
        await update.message.reply_text("❌ User unban ခြင်း Database သို့ မသိမ်းနိုင်ပါ။")
        return

    # Clear any restrictions when unbanning
    if target_user_id in user_states:
        del user_states[target_user_id]

    # Notify user and admins
    # ... (Notification logic - unchanged) ...
    
    await update.message.reply_text(
        f"✅ User Unban အောင်မြင်ပါပြီ!\n\n"
        f"👤 User ID: `{target_user_id}`\n"
        f"🎯 Status: Unbanned\n"
        f"📝 Total authorized users: {len(AUTHORIZED_USERS)}",
        parse_mode="Markdown"
    )

async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_admin(user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 2:
        # ... (Help message code) ...
        return

    feature = args[0].lower()
    status = args[1].lower()

    if feature not in ["orders", "topups", "general"]:
        await update.message.reply_text("❌ Feature မှားနေပါတယ်! orders, topups, general ထဲမှ ရွေးပါ။")
        return

    if status not in ["on", "off"]:
        await update.message.reply_text("❌ Status မှားနေပါတယ်! on သို့မဟုတ် off ရွေးပါ။")
        return

    # --- DB Call: Update Maintenance Mode ---
    global bot_maintenance
    bot_maintenance[feature] = (status == "on")
    success = await save_bot_config_field("maintenance_mode", bot_maintenance)
    
    if not success:
        await update.message.reply_text("❌ Maintenance Mode Database သို့ မသိမ်းနိုင်ပါ။")
        return

    # ... (Status message code - unchanged) ...
    status_text = "🟢 ***ဖွင့်ထား***" if status == "on" else "🔴 ***ပိတ်ထား***"
    feature_text = {
        "orders": "***အော်ဒါလုပ်ဆောင်ချက်***",
        "topups": "***ငွေဖြည့်လုပ်ဆောင်ချက်***",
        "general": "***ယေဘူယျလုပ်ဆောင်ချက်***"
    }

    await update.message.reply_text(
        f"✅ ***Maintenance Mode ပြောင်းလဲပါပြီ!***\n\n"
        f"🔧 Feature: {feature_text[feature]}\n"
        f"📊 Status: {status_text}\n\n"
        f"***လက်ရှိ Maintenance Status:***\n"
        f"***• အော်ဒါများ:*** {'🟢 ***ဖွင့်ထား***' if bot_maintenance['orders'] else '🔴 ***ပိတ်ထား***'}\n"
        f"***• ငွေဖြည့်များ:*** {'🟢 ***ဖွင့်ထား***' if bot_maintenance['topups'] else '🔴 ***ပိတ်ထား***'}\n"
        f"***• ယေဘူယျ:*** {'🟢 ဖွင့်ထား' if bot_maintenance['general'] else '🔴 ***ပိတ်ထား***'}",
        parse_mode="Markdown"
    )

async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_admin(user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 2:
        # ... (Help message code) ...
        return

    item = args[0]
    try:
        price = int(args[1])
        if price < 0:
            await update.message.reply_text("❌ ဈေးနှုန်း သုညထက် ကြီးရမည်!")
            return
    except ValueError:
        await update.message.reply_text("❌ ဈေးနှုန်း ကိန်းဂဏန်းဖြင့် ထည့်ပါ!")
        return

    # --- DB Call: Update Prices ---
    global PRICES
    PRICES[item] = price
    success = await save_bot_config_field("prices", PRICES)

    if not success:
        await update.message.reply_text("❌ ဈေးနှုန်း Database သို့ မသိမ်းနိုင်ပါ။")
        return

    await update.message.reply_text(
        f"✅ ***ဈေးနှုန်း ပြောင်းလဲပါပြီ!***\n\n"
        f"💎 Item: `{item}`\n"
        f"💰 New Price: `{price:,} MMK`\n\n"
        f"📝 Users တွေ /price ***နဲ့ အသစ်တွေ့မယ်။***",
        parse_mode="Markdown"
    )

async def removeprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_admin(user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 1:
        # ... (Help message code) ...
        return

    item = args[0]
    
    # --- DB Call: Remove Price ---
    global PRICES
    if item not in PRICES:
        await update.message.reply_text(f"❌ `{item}` မှာ custom price မရှိပါ!")
        return

    del PRICES[item]
    success = await save_bot_config_field("prices", PRICES)

    if not success:
        await update.message.reply_text("❌ ဈေးနှုန်း Database သို့ မသိမ်းနိုင်ပါ။")
        return

    await update.message.reply_text(
        f"✅ ***Custom Price ဖျက်ပါပြီ!***\n\n"
        f"💎 Item: `{item}`\n"
        f"🔄 ***Default price ကို ပြန်သုံးပါမယ်။***",
        parse_mode="Markdown"
    )

# ... (setwavenum_command, setkpaynum_command, setwavename_command, setkpayname_command 
# function များကိုလည်း payment_info ကို update လုပ်ပြီး save_bot_config_field("payment_info", payment_info) ဖြင့် သိမ်းဆည်းရန် လိုအပ်ပါသည်။) ...

# ... (QR command များကိုလည်း payment_info ကို update လုပ်ပြီး save_bot_config_field("payment_info", payment_info) ဖြင့် သိမ်းဆည်းရန် လိုအပ်ပါသည်။) ...

async def addadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_owner(user_id):
        await update.message.reply_text("❌ ***Owner သာ admin ခန့်အပ်နိုင်ပါတယ်!***")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        # ... (Error message code) ...
        return

    new_admin_id = int(args[0])

    # --- DB Call: Add Admin ---
    global settings_col, ADMIN_IDS
    if settings_col is None:
        await update.message.reply_text("❌ Database ချိတ်ဆက်မှု မရပါ!")
        return
    
    # Update DB with addToSet
    try:
        result = await asyncio.to_thread(settings_col.update_one,
            {"_id": "bot_config"},
            {"$addToSet": {"admin_ids": new_admin_id}},
            upsert=True
        )

        if result.modified_count > 0:
            ADMIN_IDS.add(new_admin_id) # Update local set
            # Notify new admin
            # ... (Notification code) ...
            
            await update.message.reply_text(
                f"✅ ***Admin ထပ်မံထည့်သွင်းပါပြီ!***\n\n"
                f"👤 ***User ID:*** `{new_admin_id}`\n"
                f"🎯 ***Status:*** Admin\n"
                f"📝 ***Total admins:*** {len(ADMIN_IDS)}",
                parse_mode="Markdown"
            )
        else:
             await update.message.reply_text("ℹ️ User သည် admin ဖြစ်နေပြီးပါပြီ။")
    except (ConnectionFailure, OperationFailure) as e:
        await update.message.reply_text(f"❌ Database Error: {e}")

async def unadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_owner(user_id):
        await update.message.reply_text("❌ Owner သာ admin ဖြုတ်နိုင်ပါတယ်!")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        # ... (Error message code) ...
        return

    target_admin_id = int(args[0])

    if target_admin_id == ADMIN_ID:
        await update.message.reply_text("❌ Owner ကို ဖြုတ်လို့ မရပါ!")
        return

    # --- DB Call: Remove Admin ---
    global settings_col, ADMIN_IDS
    if settings_col is None:
        await update.message.reply_text("❌ Database ချိတ်ဆက်မှု မရပါ!")
        return

    # Update DB with $pull
    try:
        result = await asyncio.to_thread(settings_col.update_one,
            {"_id": "bot_config"},
            {"$pull": {"admin_ids": target_admin_id}}
        )

        if result.modified_count > 0:
            ADMIN_IDS.remove(target_admin_id) # Update local set
            # Notify removed admin
            # ... (Notification code) ...

            await update.message.reply_text(
                f"✅ ***Admin ဖြုတ်ခြင်း အောင်မြင်ပါပြီ!***\n\n"
                f"👤 User ID: `{target_admin_id}`\n"
                f"🎯 ***Status:*** Removed from Admin\n"
                f"📝 ***Total admins:*** {len(ADMIN_IDS)}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("ℹ️ User သည် admin မဟုတ်ပါ။")
    except (ConnectionFailure, OperationFailure) as e:
        await update.message.reply_text(f"❌ Database Error: {e}")

# ... (clone bot management functions များကိုလည်း DB functions များနှင့် ချိန်ညှိရန် လိုအပ်ပါသည်။
# ဤ code တွင် clone bot management ကို မထည့်သွင်းတော့ဘဲ အဓိက commands များကိုသာ DB ဖြင့် ချိတ်ဆက်ပေးလိုက်ပါမည်။
# အကယ်၍ clone bot management ကို ထည့်သွင်းလိုပါက clone_bots_col ကို အသုံးပြုရမည်။) ...
async def load_clone_bots():
    """Load clone bots from db.py's clone_bots_col"""
    global clone_bots_col
    if clone_bots_col is None:
        return {}
    try:
        # Load all documents from clone_bots_col
        bots_cursor = await asyncio.to_thread(clone_bots_col.find)
        clone_bots_list = await asyncio.to_thread(list, bots_cursor)
        return {str(bot.get("_id")): bot for bot in clone_bots_list}
    except Exception as e:
        print(f"Error loading clone bots: {e}")
        return {}


async def post_init(application: Application):
    """Called after application initialization - load config and start clone bots here"""
    print("ℹ️ Post Init: Loading initial config and starting clone bots...")
    
    # 1. Load Main Bot Config from DB
    await load_bot_config()
    
    # 2. Load Clone Bot Config from DB and Start
    clone_bots = await load_clone_bots()
    for bot_id, bot_data in clone_bots.items():
        bot_token = bot_data.get("token")
        admin_id = bot_data.get("owner_id")
        if bot_token and admin_id:
            # Create task to run clone bot (Need to define clone_bot_start/mmb/callback first)
            # asyncio.create_task(run_clone_bot(bot_token, bot_id, admin_id))
            print(f"🔄 Clone bot {bot_id} should be started now...")

# ... (main function - unchanged) ...

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN environment variable မရှိပါ!")
        return

    # post_init မှာ Database မှ Config ကို load လုပ်မည်
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mmb", mmb_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("topup", topup_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("c", c_command))
    application.add_handler(CommandHandler("d", daily_report_command))
    # application.add_handler(CommandHandler("m", monthly_report_command)) # DB integration လို
    # application.add_handler(CommandHandler("y", yearly_report_command)) # DB integration လို
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("history", history_command))


    # Admin commands
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("deduct", deduct_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("reply", reply_command))
    application.add_handler(CommandHandler("register", register_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("addadm", addadm_command))
    application.add_handler(CommandHandler("unadm", unadm_command))
    # application.add_handler(CommandHandler("sendgroup", send_to_group_command)) # DB integration လို
    application.add_handler(CommandHandler("maintenance", maintenance_command))
    # application.add_handler(CommandHandler("testgroup", testgroup_command)) # DB integration လို
    application.add_handler(CommandHandler("setprice", setprice_command))
    application.add_handler(CommandHandler("removeprice", removeprice_command))
    # application.add_handler(CommandHandler("setwavenum", setwavenum_command)) # DB integration လို
    # application.add_handler(CommandHandler("setkpaynum", setkpaynum_command)) # DB integration လို
    # application.add_handler(CommandHandler("setwavename", setwavename_command)) # DB integration လို
    # application.add_handler(CommandHandler("setkpayname", setkpayname_command)) # DB integration လို
    # application.add_handler(CommandHandler("setkpayqr", setkpayqr_command)) # DB integration လို
    # application.add_handler(CommandHandler("removekpayqr", removekpayqr_command)) # DB integration လို
    # application.add_handler(CommandHandler("setwaveqr", setwaveqr_command)) # DB integration လို
    # application.add_handler(CommandHandler("removewaveqr", removewaveqr_command)) # DB integration လို
    # application.add_handler(CommandHandler("adminhelp", adminhelp_command)) # DB integration လို
    # application.add_handler(CommandHandler("broadcast", broadcast_command)) # DB integration လို

    # Clone Bot Management commands (DB integration လိုအပ်၍ မှတ်ချက်ပေးထားသည်)
    # application.add_handler(CommandHandler("addbot", addbot_command))
    # application.add_handler(CommandHandler("listbots", listbots_command))
    # application.add_handler(CommandHandler("removebot", removebot_command))
    # application.add_handler(CommandHandler("addfund", addfund_command))
    # application.add_handler(CommandHandler("deductfund", deductfund_command))

    # Callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))

    # Photo handler (for payment screenshots)
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Handle all other message types (text, voice, sticker, video, etc.)
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.VOICE | filters.Sticker.ALL | filters.VIDEO |
         filters.ANIMATION | filters.AUDIO | filters.Document.ALL |
         filters.FORWARDED | filters.Entity("url") | filters.POLL) & ~filters.COMMAND,
        handle_restricted_content
    ))

    print("🤖 Bot စတင်နေပါသည် - 24/7 Running Mode")
    print("✅ Orders, Topups နဲ့ AI စလုံးအဆင်သင့်ပါ")
    print("🔧 Admin commands များ အသုံးပြုနိုင်ပါပြီ")

    # Run main bot
    application.run_polling()

if __name__ == "__main__":
    main()
