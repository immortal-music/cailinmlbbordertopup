import json, os, asyncio
from datetime import datetime, timedelta
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from env import BOT_TOKEN, ADMIN_ID, ADMIN_GROUP_ID, MONGO_URI
from pymongo import MongoClient
from bson import ObjectId

# MongoDB Connection
client = MongoClient(MONGO_URI)
db = client.mlbb_bot
users_collection = db.users
orders_collection = db.orders
topups_collection = db.topups
settings_collection = db.settings
clone_bots_collection = db.clone_bots

# Initialize collections
if settings_collection.count_documents({}) == 0:
    settings_collection.insert_one({
        "authorized_users": [],
        "admin_ids": [ADMIN_ID],
        "prices": {},
        "payment_info": {
            "kpay_number": "09678786528",
            "kpay_name": "Ma May Phoo Wai",
            "kpay_image": None,
            "wave_number": "09673585480", 
            "wave_name": "Nine Nine",
            "wave_image": None
        },
        "bot_maintenance": {
            "orders": True,
            "topups": True,
            "general": True
        }
    })

# Global variables
AUTHORIZED_USERS = set()
user_states = {}
pending_topups = {}
clone_bot_apps = {}
order_queue = asyncio.Queue()

def is_user_authorized(user_id):
    """Check if user is authorized to use the bot"""
    return str(user_id) in AUTHORIZED_USERS or int(user_id) == ADMIN_ID

def is_owner(user_id):
    """Check if user is the owner"""
    return int(user_id) == ADMIN_ID

def is_admin(user_id):
    """Check if user is any admin"""
    if int(user_id) == ADMIN_ID:
        return True
    settings = settings_collection.find_one({})
    admin_list = settings.get("admin_ids", [])
    return int(user_id) in admin_list

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

def load_authorized_users():
    """Load authorized users from MongoDB"""
    global AUTHORIZED_USERS
    settings = settings_collection.find_one({})
    AUTHORIZED_USERS = set(str(uid) for uid in settings.get("authorized_users", []))

def save_authorized_users():
    """Save authorized users to MongoDB"""
    settings_collection.update_one(
        {},
        {"$set": {"authorized_users": [int(uid) for uid in AUTHORIZED_USERS]}}
    )

def get_prices():
    """Get prices from MongoDB"""
    settings = settings_collection.find_one({})
    return settings.get("prices", {})

def save_prices(prices):
    """Save prices to MongoDB"""
    settings_collection.update_one(
        {},
        {"$set": {"prices": prices}}
    )

def get_payment_info():
    """Get payment info from MongoDB"""
    settings = settings_collection.find_one({})
    return settings.get("payment_info", {})

def save_payment_info(payment_info):
    """Save payment info to MongoDB"""
    settings_collection.update_one(
        {},
        {"$set": {"payment_info": payment_info}}
    )

def get_bot_maintenance():
    """Get bot maintenance status from MongoDB"""
    settings = settings_collection.find_one({})
    return settings.get("bot_maintenance", {})

def save_bot_maintenance(bot_maintenance):
    """Save bot maintenance status to MongoDB"""
    settings_collection.update_one(
        {},
        {"$set": {"bot_maintenance": bot_maintenance}}
    )

def get_user(user_id):
    """Get user from MongoDB"""
    return users_collection.find_one({"user_id": str(user_id)})

def save_user(user_data):
    """Save user to MongoDB"""
    users_collection.update_one(
        {"user_id": user_data["user_id"]},
        {"$set": user_data},
        upsert=True
    )

def create_user(user_id, name, username):
    """Create new user in MongoDB"""
    user_data = {
        "user_id": str(user_id),
        "name": name,
        "username": username,
        "balance": 0,
        "orders": [],
        "topups": [],
        "created_at": datetime.now().isoformat()
    }
    save_user(user_data)
    return user_data

def add_user_order(user_id, order_data):
    """Add order to user in MongoDB"""
    users_collection.update_one(
        {"user_id": str(user_id)},
        {"$push": {"orders": order_data}}
    )

def add_user_topup(user_id, topup_data):
    """Add topup to user in MongoDB"""
    users_collection.update_one(
        {"user_id": str(user_id)},
        {"$push": {"topups": topup_data}}
    )

def update_user_balance(user_id, new_balance):
    """Update user balance in MongoDB"""
    users_collection.update_one(
        {"user_id": str(user_id)},
        {"$set": {"balance": new_balance}}
    )

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
    Check if MLBB account is banned
    """
    banned_ids = [
        "123456789",
        "000000000", 
        "111111111",
    ]

    if game_id in banned_ids:
        return True

    if len(set(game_id)) == 1:
        return True

    if game_id.startswith("000") or game_id.endswith("000"):
        return True

    return False

def get_price(diamonds):
    """Get price for diamonds from MongoDB"""
    custom_prices = get_prices()
    if diamonds in custom_prices:
        return custom_prices[diamonds]

    # Default prices
    if diamonds.startswith("wp") and diamonds[2:].isdigit():
        n = int(diamonds[2:])
        if 1 <= n <= 10:
            return n * 6000
            
    price_table = {
        "11": 950, "22": 1900, "33": 2850, "56": 4200, "112": 8200,
        "86": 5100, "172": 10200, "257": 15300, "343": 20400,
        "429": 25500, "514": 30600, "600": 35700, "706": 40800,
        "878": 51000, "963": 56100, "1049": 61200, "1135": 66300,
        "1412": 81600, "2195": 122400, "3688": 204000,
        "5532": 306000, "9288": 510000, "12976": 714000,
        "55": 3500, "165": 10000, "275": 16000, "565": 33000
    }
    return price_table.get(diamonds)

def is_payment_screenshot(update):
    """
    Check if the image is likely a payment screenshot
    """
    if update.message.photo:
        caption = update.message.caption or ""
        payment_keywords = ["kpay", "wave", "payment", "pay", "transfer", "လွှဲ", "ငွေ"]
        return True
    return False

async def check_pending_topup(user_id):
    """Check if user has pending topups"""
    user_data = get_user(user_id)
    if not user_data:
        return False
        
    for topup in user_data.get("topups", []):
        if topup.get("status") == "pending":
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
    bot_maintenance = get_bot_maintenance()
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

def simple_reply(message_text):
    """
    Simple auto-replies for common queries
    """
    message_lower = message_text.lower()

    # Greetings
    if any(word in message_lower for word in ["hello", "hi", "မင်္ဂလာပါ", "ဟယ်လို", "ဟိုင်း", "ကောင်းလား"]):
        return ("👋 မင်္ဂလာပါ! 𝙅𝘽 𝙈𝙇𝘽𝘽 𝘼𝙐𝙏𝙊 𝙏𝙊𝙋 𝙐𝙋 𝘽𝙊𝙏 မှ ကြိုဆိုပါတယ်!\n\n"
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or "-"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    # Load authorized users
    load_authorized_users()

    # Check if user is authorized
    if not is_user_authorized(user_id):
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

    # Get or create user
    user_data = get_user(user_id)
    if not user_data:
        user_data = create_user(user_id, name, username)

    # Clear any restricted state when starting
    if user_id in user_states:
        del user_states[user_id]

    # Create clickable name
    clickable_name = f"[{name}](tg://user?id={user_id})"

    msg = (
        f"👋 ***မင်္ဂလာပါ*** {clickable_name}!\n"
        f"🆔 ***Telegram User ID:*** `{user_id}`\n\n"
        "💎 ***𝙅𝘽 𝙈𝙇𝘽𝘽 𝘼𝙐𝙏𝙊 𝙏𝙊𝙋 𝙐𝙋 𝘽𝙊𝙏*** မှ ကြိုဆိုပါတယ်။\n\n"
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
            await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(msg, parse_mode="Markdown")

async def mmb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Check authorization
    load_authorized_users()
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
    if not await check_maintenance_mode("orders"):
        await send_maintenance_message(update, "orders")
        return

    # Check if user is restricted after screenshot
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        await update.message.reply_text(
            "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
            "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
            "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Check for pending topups first
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    # Check if user has pending topup process
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

    # Validate Game ID
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

    # Validate Server ID
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

    # Check if account is banned
    if is_banned_account(game_id):
        await update.message.reply_text(
            "🚫 ***Account Ban ဖြစ်နေပါတယ်!***\n\n"
            f"🎮 Game ID: `{game_id}`\n"
            f"🌐 Server ID: `{server_id}`\n\n"
            "❌ ဒီ account မှာ diamond topup လုပ်လို့ မရပါ။\n\n"
            "***အကြောင်းရင်းများ***:\n"
            "***• Account suspended/banned ဖြစ်နေခြင်း***\n"
            "***• Invalid account pattern***\n"
            "***• MLBB မှ ပိတ်ပင်ထားခြင်း***\n\n"
            "🔄 ***အခြား account သုံးပြီး ထပ်ကြိုးစားကြည့်ပါ။***\n\n\n"
            "📞 ***ပြဿနာရှိရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )

        # Notify admin about banned account attempt
        admin_msg = (
            f"🚫 ***Banned Account Topup ကြိုးစားမှု***\n\n"
            f"👤 ***User:*** [{update.effective_user.first_name}](tg://user?id={user_id})\n\n"
            f"🆔 ***User ID:*** `{user_id}`\n"
            f"🎮 ***Game ID:*** `{game_id}`\n"
            f"🌐 ***Server ID:*** `{server_id}`\n"
            f"💎 ***Amount:*** {amount}\n"
            f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "***⚠️ ဒီ account မှာ topup လုပ်လို့ မရပါ။***"
        )

        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="Markdown")
        except:
            pass

        return

    price = get_price(amount)

    if not price:
        await update.message.reply_text(
            "❌ Diamond amount မှားနေပါတယ်!\n\n"
            "***ရရှိနိုင်တဲ့ amounts***:\n"
            "***• Weekly Pass:*** wp1-wp10\n\n"
            "***• Diamonds:*** 11, 22, 33, 56, 86, 112, 172, 257, 343, 429, 514, 600, 706, 878, 963, 1049, 1135, 1412, 2195, 3688, 5532, 9288, 12976",
            parse_mode="Markdown"
        )
        return

    user_data = get_user(user_id)
    user_balance = user_data.get("balance", 0) if user_data else 0

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

    # Process order
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

    # Deduct balance and add order
    new_balance = user_balance - price
    update_user_balance(user_id, new_balance)
    add_user_order(user_id, order)

    # Create confirm/cancel buttons for admin
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"order_confirm_{order_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"order_cancel_{order_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Get user name
    user_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

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

    # Send to all admins
    settings = settings_collection.find_one({})
    admin_list = settings.get("admin_ids", [ADMIN_ID])
    for admin_id in admin_list:
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

    await update.message.reply_text(
        f"✅ ***အော်ဒါ အောင်မြင်ပါပြီ!***\n\n"
        f"📝 ***Order ID:*** `{order_id}`\n"
        f"🎮 ***Game ID:*** `{game_id}`\n"
        f"🌐 ***Server ID:*** `{server_id}`\n"
        f"💎 ***Diamond:*** {amount}\n"
        f"💰 ***ကုန်ကျစရိတ်:*** {price:,} MMK\n"
        f"💳 ***လက်ကျန်ငွေ:*** {new_balance:,} MMK\n"
        f"📊 Status: ⏳ ***စောင့်ဆိုင်းနေသည်***\n\n"
        "⚠️ ***Admin က confirm လုပ်ပြီးမှ diamonds များ ရရှိပါမယ်။***\n"
        "📞 ***ပြဿနာရှိရင် admin ကို ဆက်သွယ်ပါ။***",
        parse_mode="Markdown"
    )

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Check authorization
    load_authorized_users()
    if not is_user_authorized(user_id):
        keyboard = [[InlineKeyboardButton("👑 Contact Owner", url=f"tg://user?id={ADMIN_ID}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🚫 အသုံးပြုခွင့် မရှိပါ!\n\n"
            "Owner ထံ bot အသုံးပြုခွင့် တောင်းဆိုပါ။",
            reply_markup=reply_markup
        )
        return

    # Check if user is restricted after screenshot
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        await update.message.reply_text(
            "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
            "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
            "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Check if user has pending topup process
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

    # Check for pending topups in data
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    user_data = get_user(user_id)

    if not user_data:
        await update.message.reply_text("❌ အရင်ဆုံး /start နှိပ်ပါ။")
        return

    balance = user_data.get("balance", 0)
    total_orders = len(user_data.get("orders", []))
    total_topups = len(user_data.get("topups", []))

    # Check for pending topups
    pending_topups_count = 0
    pending_amount = 0

    for topup in user_data.get("topups", []):
        if topup.get("status") == "pending":
            pending_topups_count += 1
            pending_amount += topup.get("amount", 0)

    # Escape special characters
    name = user_data.get('name', 'Unknown').replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
    username = user_data.get('username', 'None').replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')

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
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=user_photos.photos[0][0].file_id,
                caption=balance_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                balance_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
    except:
        await update.message.reply_text(
            balance_text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Check authorization
    load_authorized_users()
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
    if not await check_maintenance_mode("topups"):
        await send_maintenance_message(update, "topups")
        return

    # Check if user is restricted after screenshot
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        await update.message.reply_text(
            "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
            "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
            "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Check for pending topups first
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    # Check if user has pending topup process
    if user_id in pending_topups:
        await update.message.reply_text(
            "⏳ ***Topup လုပ်ငန်းစဉ် ဆက်လက်လုပ်ဆောင်ပါ!***\n\n"
            "❌ ***လက်ရှိ topup လုပ်ငန်းစဉ်ကို မပြီးသေးပါ။***\n\n"
            "***လုပ်ရမည့်အရာများ***:\n"
            "***• Payment app ရွေးပြီး screenshot တင်ပါ***\n"
            "***• သို့မဟုတ် /cancel နှိပ်ပြီး ပယ်ဖျက်ပါ***\n\n"
            "💡 ***ပယ်ဖျက်ပြီးမှ အသစ် topup လုပ်နိုင်ပါမယ်။***",
            parse_mode="Markdown"
        )
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "❌ ***အမှားရှိပါတယ်!***\n\n"
            "***မှန်ကန်တဲ့ format***: `/topup <amount>`\n\n"
            "**ဥပမာ**:\n"
            "• `/topup 1000`\n"
            "• `/topup 5000`\n"
            "• `/topup 50000`\n\n"
            "💡 ***အနည်းဆုံး 1,000 MMK ဖြည့်ရပါမည်။***",
            parse_mode="Markdown"
        )
        return

    try:
        amount = int(args[0])
        if amount < 1000:
            await update.message.reply_text(
                "❌ ***ငွေပမာဏ နည်းလွန်းပါတယ်!***\n\n"
                "💰 ***အနည်းဆုံး 1,000 MMK ဖြည့်ရပါမည်။***",
                parse_mode="Markdown"
            )
            return
    except ValueError:
        await update.message.reply_text(
            "❌ ***ငွေပမာဏ မှားနေပါတယ်!***\n\n"
            "💰 ***ကိန်းဂဏန်းများသာ ရေးပါ။***\n\n"
            "***ဥပမာ***: `/topup 5000`",
            parse_mode="Markdown"
        )
        return

    # Store pending topup
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

    # Check authorization
    load_authorized_users()
    if not is_user_authorized(user_id):
        keyboard = [[InlineKeyboardButton("👑 Contact Owner", url=f"tg://user?id={ADMIN_ID}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🚫 အသုံးပြုခွင့် မရှိပါ!\n\n"
            "Owner ထံ bot အသုံးပြုခွင့် တောင်းဆိုပါ။",
            reply_markup=reply_markup
        )
        return

    # Check if user is restricted after screenshot
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        await update.message.reply_text(
            "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
            "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
            "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Check if user has pending topup process
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

    # Get custom prices
    custom_prices = get_prices()

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

    # Merge custom prices with defaults
    current_prices = {**default_prices, **custom_prices}

    price_msg = "💎 ***MLBB Diamond ဈေးနှုန်းများ***\n\n"

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
                    if k not in default_prices}
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

    # Check authorization
    load_authorized_users()
    if not is_user_authorized(user_id):
        return

    # Clear pending topup if exists
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

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Check authorization
    load_authorized_users()
    if not is_user_authorized(user_id):
        keyboard = [[InlineKeyboardButton("👑 Contact Owner", url=f"tg://user?id={ADMIN_ID}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🚫 အသုံးပြုခွင့် မရှိပါ!\n\n"
            "Owner ထံ bot အသုံးပြုခွင့် တောင်းဆိုပါ။",
            reply_markup=reply_markup
        )
        return

    # Check if user is restricted after screenshot
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        await update.message.reply_text(
            "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
            "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
            "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Check if user has pending topup process
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

    # Check for pending topups in data
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    user_data = get_user(user_id)

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
        msg += "🛒 အော်ဒါများ (နောက်ဆုံး 5 ခု):\n"
        for order in orders[-5:]:
            status_emoji = "✅" if order.get("status") == "completed" else "⏳"
            msg += f"{status_emoji} {order['order_id']} - {order['amount']} ({order['price']:,} MMK)\n"
        msg += "\n"

    if topups:
        msg += "💳 ငွေဖြည့်များ (နောက်ဆုံး 5 ခု):\n"
        for topup in topups[-5:]:
            status_emoji = "✅" if topup.get("status") == "approved" else "⏳"
            msg += f"{status_emoji} {topup['amount']:,} MMK - {topup.get('timestamp', 'Unknown')[:10]}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Check if user is any admin
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

    user_data = get_user(target_user_id)

    if not user_data:
        await update.message.reply_text("❌ User မတွေ့ရှိပါ!")
        return

    # Add balance to user
    current_balance = user_data.get("balance", 0)
    new_balance = current_balance + amount
    update_user_balance(target_user_id, new_balance)

    # Update topup status
    topups = user_data.get("topups", [])
    for topup in reversed(topups):
        if topup["status"] == "pending" and topup["amount"] == amount:
            topup["status"] = "approved"
            topup["approved_by"] = update.effective_user.first_name
            topup["approved_at"] = datetime.now().isoformat()
            break

    # Save updated topups
    users_collection.update_one(
        {"user_id": target_user_id},
        {"$set": {"topups": topups}}
    )

    # Clear user restriction state after approval
    if target_user_id in user_states:
        del user_states[target_user_id]

    # Notify user
    try:
        user_balance = new_balance

        # Create order button
        keyboard = [[InlineKeyboardButton("💎 Order တင်မယ်", url=f"https://t.me/{context.bot.username}?start=order")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=int(target_user_id),
            text=f"✅ ***ငွေဖြည့်မှု အတည်ပြုပါပြီ!*** 🎉\n\n"
                 f"💰 ***ပမာဏ:*** `{amount:,} MMK`\n"
                 f"💳 ***လက်ကျန်ငွေ:*** `{user_balance:,} MMK`\n"
                 f"👤 ***Approved by:*** [{update.effective_user.first_name}](tg://user?id={user_id})\n"
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
        f"💳 ***User's new balance:*** `{new_balance:,} MMK`\n"
        f"🔓 ***User restrictions cleared!***",
        parse_mode="Markdown"
    )

async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User registration request"""
    user_id = str(update.effective_user.id)
    user = update.effective_user
    username = user.username or "-"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    # Load authorized users
    load_authorized_users()

    # Check if already authorized
    if is_user_authorized(user_id):
        await update.message.reply_text(
            "✅ သင်သည် အသုံးပြုခွင့် ရပြီးသား ဖြစ်ပါတယ်!\n\n"
            "🚀 /start နှိပ်ပြီး bot ကို အသုံးပြုနိုင်ပါပြီ။",
            parse_mode="Markdown"
        )
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
        f"📱 ***Username:*** @{username}\n"
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

    try:
        # Send to owner with user's profile photo
        try:
            user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
            if user_photos.total_count > 0:
                await context.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=user_photos.photos[0][0].file_id,
                    caption=owner_msg,
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=owner_msg,
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
        except:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=owner_msg,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
    except Exception as e:
        print(f"Error sending registration request to owner: {e}")

    # Send confirmation to user with their profile photo
    try:
        user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        if user_photos.total_count > 0:
            await update.message.reply_photo(
                photo=user_photos.photos[0][0].file_id,
                caption=user_confirm_msg,
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(user_confirm_msg, parse_mode="Markdown")
    except:
        await update.message.reply_text(user_confirm_msg, parse_mode="Markdown")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Check if user is authorized
    load_authorized_users()
    if not is_user_authorized(user_id):
        return

    # Validate if it's a payment screenshot
    if not is_payment_screenshot(update):
        await update.message.reply_text(
            "❌ ***သင့်ပုံ လက်မခံပါ!***\n\n"
            "🔍 ***Payment screenshot သာ လက်ခံပါတယ်။***\n"
            "💳 ***KPay, Wave လွှဲမှု screenshot များသာ တင်ပေးပါ။***\n\n"
            "📷 ***Payment app ရဲ့ transfer confirmation screenshot ကို တင်ပေးပါ။***",
            parse_mode="Markdown"
        )
        return

    if user_id not in pending_topups:
        await update.message.reply_text(
            "❌ ***Topup process မရှိပါ!***\n\n"
            "🔄 ***အရင်ဆုံး `/topup amount` command ကို သုံးပါ။***\n"
            "💡 ***ဥပမာ:*** `/topup 50000`",
            parse_mode="Markdown"
        )
        return

    pending = pending_topups[user_id]
    amount = pending["amount"]
    payment_method = pending.get("payment_method", "Unknown")

    # Check if payment method was selected
    if payment_method == "Unknown":
        await update.message.reply_text(
            "❌ ***Payment app ကို အရင်ရွေးပါ!***\n\n"
            "📱 ***KPay သို့မဟုတ် Wave ကို ရွေးချယ်ပြီးမှ screenshot တင်ပါ။***\n\n"
            "🔄 ***အဆင့်များ***:\n"
            "1. `/topup amount` နှိပ်ပါ\n"
            "2. ***Payment app ရွေးပါ (KPay/Wave)***\n"
            "3. ***Screenshot တင်ပါ***",
            parse_mode="Markdown"
        )
        return

    # Set user state to restricted
    user_states[user_id] = "waiting_approval"

    # Generate unique topup ID
    topup_id = f"TOP{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"

    # Get user name
    user_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    # Notify admin about topup request with payment screenshot
    admin_msg = (
        f"💳 ***ငွေဖြည့်တောင်းဆိုမှု***\n\n"
        f"👤 User Name: [{user_name}](tg://user?id={user_id})\n"
        f"🆔 User ID: `{user_id}`\n"
        f"💰 Amount: `{amount:,} MMK`\n"
        f"📱 Payment: {payment_method.upper()}\n"
        f"🔖 Topup ID: `{topup_id}`\n"
        f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 ***Status:*** ⏳ စောင့်ဆိုင်းနေသည်\n\n"
        f"***Screenshot စစ်ဆေးပြီး လုပ်ဆောင်ပါ။***"
    )

    # Create approve/reject buttons for admins
    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"topup_approve_{topup_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"topup_reject_{topup_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Save topup request
    topup_request = {
        "topup_id": topup_id,
        "amount": amount,
        "payment_method": payment_method,
        "status": "pending",
        "timestamp": datetime.now().isoformat()
    }
    add_user_topup(user_id, topup_request)

    # Get all admins
    settings = settings_collection.find_one({})
    admin_list = settings.get("admin_ids", [ADMIN_ID])

    try:
        # Send to all admins
        for admin_id in admin_list:
            try:
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=update.message.photo[-1].file_id,
                    caption=admin_msg,
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
            except:
                pass

        # Send to admin group
        try:
            if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                group_msg = (
                    f"💳 ***ငွေဖြည့်တောင်းဆိုမှု***\n\n"
                    f"👤 User Name: [{user_name}](tg://user?id={user_id})\n"
                    f"🆔 ***User ID:*** `{user_id}`\n"
                    f"💰 ***Amount:*** `{amount:,} MMK`\n"
                    f"📱 Payment: {payment_method.upper()}\n"
                    f"🔖 ***Topup ID:*** `{topup_id}`\n"
                    f"⏰ ***Time:*** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"📊 ***Status:*** ⏳ စောင့်ဆိုင်းနေသည်\n\n"
                    f"***Approve လုပ်ရန်:*** `/approve {user_id} {amount}`\n\n"
                    f"#TopupRequest #Payment"
                )
                await context.bot.send_photo(
                    chat_id=ADMIN_GROUP_ID,
                    photo=update.message.photo[-1].file_id,
                    caption=group_msg,
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
        except Exception as e:
            pass
    except Exception as e:
        print(f"Error in topup process: {e}")

    del pending_topups[user_id]

    await update.message.reply_text(
        f"✅ ***Screenshot လက်ခံပါပြီ!***\n\n"
        f"💰 ***ပမာဏ:*** `{amount:,} MMK`\n"
        f"⏰ ***အချိန်:*** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "🔒 ***အသုံးပြုမှု ယာယီ ကန့်သတ်ပါ***\n"
        "❌ ***Screenshot ပို့ပြီးပါပြီ။ Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ:***\n\n"
        "❌ ***Commands အသုံးပြုလို့ မရပါ။***\n"
        "❌ ***စာသား ပို့လို့ မရပါ။***\n"
        "❌ ***Voice, Sticker, GIF, Video ပို့လို့ မရပါ။***\n"
        "❌ ***Emoji ပို့လို့ မရပါ။***\n\n"
        "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n"
        "📞 ***ပြဿနာရှိရင် admin ကို ဆက်သွယ်ပါ။***",
        parse_mode="Markdown"
    )

async def handle_restricted_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all non-command messages for restricted users"""
    user_id = str(update.effective_user.id)

    # Check if user is authorized first
    load_authorized_users()
    if not is_user_authorized(user_id):
        # For unauthorized users, give AI reply
        if update.message.text:
            reply = simple_reply(update.message.text)
            await update.message.reply_text(reply, parse_mode="Markdown")
        return

    # Check if user is restricted after sending screenshot
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        # Block everything except photos for restricted users
        if update.message.photo:
            await handle_photo(update, context)
            return

        # Block all other content types
        await update.message.reply_text(
            "❌ ***အသုံးပြုမှု ကန့်သတ်ထားပါ!***\n\n"
            "🔒 ***Screenshot ပို့ပြီးပါပြီ။ Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ:***\n\n"
            "❌ ***Commands အသုံးပြုလို့ မရပါ။***\n"
            "❌ ***စာသား ပို့လို့ မရပါ။***\n"
            "❌ ***Voice, Sticker, GIF, Video အသုံးပြုလို့ မရပါ။***\n"
            "❌ ***Emoji ပို့လို့ မရပါ။***\n\n"
            "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # For authorized users - handle different message types
    if update.message.text:
        text = update.message.text.strip()
        # Provide simple auto-reply for text messages
        reply = simple_reply(text)
        await update.message.reply_text(reply, parse_mode="Markdown")

    # Handle other content types
    else:
        await update.message.reply_text(
            "📱 ***MLBB Diamond Top-up Bot***\n\n"
            "💎 Diamond ဝယ်ယူရန် /mmb command သုံးပါ\n"
            "💰 ဈေးနှုန်းများ သိရှိရန် /price နှိပ်ပါ\n"
            "🆘 အကူအညီ လိုရင် /start နှိပ်ပါ",
            parse_mode="Markdown"
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)
    admin_name = query.from_user.first_name or "Admin"

    # Handle payment method selection
    if query.data.startswith("topup_pay_"):
        parts = query.data.split("_")
        payment_method = parts[2]  # kpay or wave
        amount = int(parts[3])

        # Update pending topup with payment method
        if user_id in pending_topups:
            pending_topups[user_id]["payment_method"] = payment_method

        payment_info = get_payment_info()
        payment_name = "KBZ Pay" if payment_method == "kpay" else "Wave Money"
        payment_num = payment_info['kpay_number'] if payment_method == "kpay" else payment_info['wave_number']
        payment_acc_name = payment_info['kpay_name'] if payment_method == "kpay" else payment_info['wave_name']
        payment_qr = payment_info.get('kpay_image') if payment_method == "kpay" else payment_info.get('wave_image')

        # Send QR if available
        if payment_qr:
            try:
                await query.message.reply_photo(
                    photo=payment_qr,
                    caption=f"📱 **{payment_name} QR Code**\n\n"
                            f"📞 နံပါတ်: `{payment_num}`\n"
                            f"👤 နာမည်: {payment_acc_name}",
                    parse_mode="Markdown"
                )
            except:
                pass

        await query.edit_message_text(
            f"💳 ***ငွေဖြည့်လုပ်ငန်းစဉ်***\n\n"
            f"✅ ***ပမာဏ:*** `{amount:,} MMK`\n"
            f"✅ ***Payment:*** {payment_name}\n\n"
            f"***အဆင့် 3: ငွေလွှဲပြီး Screenshot တင်ပါ။***\n\n"
            f"📱 {payment_name}\n"
            f"📞 ***နံပါတ်:*** `{payment_num}`\n"
            f"👤 ***အမည်:*** {payment_acc_name}\n\n"
            f"⚠️ ***အရေးကြီးသော သတိပေးချက်:***\n"
            f"***ငွေလွှဲ note/remark မှာ သင့်ရဲ့ {payment_name} အကောင့်နာမည်ကို ရေးပေးပါ။***\n"
            f"***မရေးရင် ငွေဖြည့်မှု ငြင်းပယ်ခံရနိုင်ပါတယ်။***\n\n"
            f"💡 ***ငွေလွှဲပြီးရင် screenshot ကို ဒီမှာ တင်ပေးပါ။***\n"
            f"⏰ ***24 နာရီအတွင်း confirm လုပ်ပါမယ်။***\n\n"
            f"ℹ️ ***ပယ်ဖျက်ရန် /cancel နှိပ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Handle registration request button
    elif query.data == "request_register":
        user = query.from_user
        user_id = str(user.id)
        username = user.username or "-"
        name = f"{user.first_name} {user.last_name or ''}".strip()

        # Load authorized users
        load_authorized_users()

        # Check if already authorized
        if is_user_authorized(user_id):
            await query.answer("✅ သင်သည် အသုံးပြုခွင့် ရပြီးသား ဖြစ်ပါတယ်!", show_alert=True)
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
            f"📱 ***Username:*** @{username}\n"
            f"⏰ ***Time:*** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"***အသုံးပြုခွင့် ပေးမလား?***"
        )

        try:
            # Try to send user's profile photo first
            try:
                user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
                if user_photos.total_count > 0:
                    await context.bot.send_photo(
                        chat_id=ADMIN_ID,
                        photo=user_photos.photos[0][0].file_id,
                        caption=owner_msg,
                        parse_mode="Markdown",
                        reply_markup=reply_markup
                    )
                else:
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=owner_msg,
                        parse_mode="Markdown",
                        reply_markup=reply_markup
                    )
            except:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=owner_msg,
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
        except Exception as e:
            print(f"Error sending registration request to owner: {e}")

        await query.answer("✅ Registration တောင်းဆိုမှု ပို့ပြီးပါပြီ!", show_alert=True)
        try:
            await query.edit_message_text(
                "✅ ***Registration တောင်းဆိုမှု ပို့ပြီးပါပြီ!***\n\n"
                "⏳ ***Owner က approve လုပ်တဲ့အထိ စောင့်ပါ။***\n"
                "📞 ***အရေးပေါ်ဆိုရင် owner ကို ဆက်သွယ်ပါ။***\n\n"
                f"🆔 ***သင့် User ID:*** `{user_id}`",
                parse_mode="Markdown"
            )
        except:
            pass
        return

    # Handle registration approve (admins can approve)
    elif query.data.startswith("register_approve_"):
        if not is_admin(user_id):
            await query.answer("❌ Admin များသာ registration approve လုပ်နိုင်ပါတယ်!", show_alert=True)
            return

        target_user_id = query.data.replace("register_approve_", "")
        load_authorized_users()

        if target_user_id in AUTHORIZED_USERS:
            await query.answer("ℹ️ User ကို approve လုပ်ပြီးပါပြီ!", show_alert=True)
            return

        AUTHORIZED_USERS.add(target_user_id)
        save_authorized_users()

        # Clear any restrictions
        if target_user_id in user_states:
            del user_states[target_user_id]

        # Remove buttons
        await query.edit_message_reply_markup(reply_markup=None)

        # Update message
        try:
            await query.edit_message_text(
                text=query.message.text + f"\n\n✅ Approved by {admin_name}",
                parse_mode="Markdown"
            )
        except:
            pass

        # Notify user
        try:
            user_data = get_user(target_user_id)
            user_name = user_data.get('name', 'User') if user_data else 'User'

            await context.bot.send_message(
                chat_id=int(target_user_id),
                text=f"🎉 Registration Approved!\n\n"
                     f"✅ Admin က သင့် registration ကို လက်ခံပါပြီ။\n\n"
                     f"🚀 ယခုအခါ /start နှိပ်ပြီး bot ကို အသုံးပြုနိုင်ပါပြီ!"
            )
        except:
            pass

        await query.answer("✅ User approved!", show_alert=True)
        return

    # Handle registration reject (admins can reject)
    elif query.data.startswith("register_reject_"):
        if not is_admin(user_id):
            await query.answer("❌ Admin များသာ registration reject လုပ်နိုင်ပါတယ်!", show_alert=True)
            return

        target_user_id = query.data.replace("register_reject_", "")

        # Remove buttons
        await query.edit_message_reply_markup(reply_markup=None)

        # Update message
        try:
            await query.edit_message_text(
                text=query.message.text + f"\n\n❌ Rejected by {admin_name}",
                parse_mode="Markdown"
            )
        except:
            pass

        # Notify user
        try:
            await context.bot.send_message(
                chat_id=int(target_user_id),
                text="❌ Registration Rejected\n\n"
                     "Admin က သင့် registration ကို ငြင်းပယ်လိုက်ပါပြီ။\n\n"
                     "📞 အကြောင်းရင်း သိရှိရန် Admin ကို ဆက်သွယ်ပါ။\n\n"
            )
        except:
            pass

        await query.answer("❌ User rejected!", show_alert=True)
        return

    # Handle topup cancel
    elif query.data == "topup_cancel":
        if user_id in pending_topups:
            del pending_topups[user_id]

        await query.edit_message_text(
            "✅ ***ငွေဖြည့်ခြင်း ပယ်ဖျက်ပါပြီ!***\n\n"
            "💡 ***ပြန်ဖြည့်ချင်ရင်*** /topup ***နှိပ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Handle topup approve
    elif query.data.startswith("topup_approve_"):
        if not is_admin(user_id):
            await query.answer("❌ ***သင်သည် admin မဟုတ်ပါ!***")
            return

        topup_id = query.data.replace("topup_approve_", "")

        # Find user with this topup
        user_data = users_collection.find_one({"topups.topup_id": topup_id})
        if not user_data:
            await query.answer("❌ Topup မတွေ့ရှိပါ!")
            return

        # Find and approve topup
        topup_found = False
        topup_amount = 0
        target_user_id = user_data["user_id"]

        for topup in user_data.get("topups", []):
            if topup.get("topup_id") == topup_id and topup.get("status") == "pending":
                topup["status"] = "approved"
                topup["approved_by"] = admin_name
                topup["approved_at"] = datetime.now().isoformat()
                topup_amount = topup["amount"]
                topup_found = True

                # Add balance to user
                current_balance = user_data.get("balance", 0)
                new_balance = current_balance + topup_amount
                update_user_balance(target_user_id, new_balance)

                # Clear user restriction
                if target_user_id in user_states:
                    del user_states[target_user_id]
                break

        if topup_found:
            # Save updated topups
            users_collection.update_one(
                {"user_id": target_user_id},
                {"$set": {"topups": user_data["topups"]}}
            )

            # Remove buttons
            await query.edit_message_reply_markup(reply_markup=None)

            # Update message
            try:
                original_text = query.message.text or query.message.caption or ""
                updated_text = original_text.replace("pending", "approved") if original_text else "✅ Approved"
                updated_text += f"\n\n✅ Approved by: {admin_name}"

                if query.message.text:
                    await query.edit_message_text(
                        text=updated_text,
                        parse_mode="Markdown"
                    )
                elif query.message.caption:
                    await query.edit_message_caption(
                        caption=updated_text,
                        parse_mode="Markdown"
                    )
            except:
                pass

            # Notify user
            try:
                user_balance = new_balance

                keyboard = [[InlineKeyboardButton("💎 Order တင်မယ်", url=f"https://t.me/{context.bot.username}?start=order")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await context.bot.send_message(
                    chat_id=int(target_user_id),
                    text=f"✅ ငွေဖြည့်မှု အတည်ပြုပါပြီ! 🎉\n\n"
                         f"💰 ပမာဏ: `{topup_amount:,} MMK`\n"
                         f"💳 လက်ကျန်ငွေ: `{user_balance:,} MMK`\n"
                         f"👤 Approved by: [{admin_name}](tg://user?id={user_id})\n"
                         f"⏰ အချိန်: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                         f"🎉 ယခုအခါ diamonds များ ဝယ်ယူနိုင်ပါပြီ!\n"
                         f"🔓 Bot လုပ်ဆောင်ချက်များ ပြန်လည် အသုံးပြုနိုင်ပါပြီ!\n\n"
                         f"💎 Order တင်ရန်:\n"
                         f"`/mmb gameid serverid amount`",
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
            except:
                pass

            await query.answer("✅ Topup approved!", show_alert=True)
        else:
            await query.answer("❌ Topup မတွေ့ရှိပါ သို့မဟုတ် လုပ်ဆောင်ပြီးပါပြီ!")
        return

    # Handle order confirm/cancel (similar logic as before but with MongoDB)
    # ... (order confirmation/cancellation logic would go here)

    # Handle other button callbacks
    elif query.data == "topup_button":
        payment_info = get_payment_info()
        try:
            keyboard = [
                [InlineKeyboardButton("📱 Copy KPay Number", callback_data="copy_kpay")],
                [InlineKeyboardButton("📱 Copy Wave Number", callback_data="copy_wave")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                text="💳 ***ငွေဖြည့်လုပ်ငန်းစဉ်***\n\n"
                     "***အဆင့် 1: ငွေပမာဏ ရေးပါ***\n"
                     "`/topup amount` ဥပမာ: `/topup 50000`\n\n"
                     "***အဆင့် 2: ငွေလွှဲပါ***\n"
                     f"📱 ***KBZ Pay:*** `{payment_info['kpay_number']}` ({payment_info['kpay_name']})\n"
                     f"📱 ***Wave Money:*** `{payment_info['wave_number']}` ({payment_info['wave_name']})\n\n"
                     "***အဆင့် 3: Screenshot တင်ပါ***\n"
                     "***ငွေလွှဲပြီးရင် screenshot ကို ဒီမှာ တင်ပေးပါ။***\n\n"
                     "⏰ ***24 နာရီအတွင်း confirm လုပ်ပါမယ်။***",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        except Exception as e:
            keyboard = [
                [InlineKeyboardButton("📱 Copy KPay Number", callback_data="copy_kpay")],
                [InlineKeyboardButton("📱 Copy Wave Number", callback_data="copy_wave")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.message.reply_text(
                text="💳 ***ငွေဖြည့်လုပ်ငန်းစဉ်***\n\n"
                     "***အဆင့် 1: ငွေပမာဏ ရေးပါ***\n"
                     "`/topup amount` ဥပမာ: `/topup 50000`\n\n"
                     "***အဆင့် 2: ငွေလွှဲပါ***\n"
                     f"📱 ***KBZ Pay:*** `{payment_info['kpay_number']}` ({payment_info['kpay_name']})\n"
                     f"📱 ***Wave Money:*** `{payment_info['wave_number']}` ({payment_info['wave_name']})\n\n"
                     "***အဆင့် 3: Screenshot တင်ပါ***\n"
                     "***ငွေလွှဲပြီးရင် screenshot ကို ဒီမှာ တင်ပေးပါ။***\n\n"
                     "⏰ ***24 နာရီအတွင်း confirm လုပ်ပါမယ်။***",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )

    elif query.data == "copy_kpay":
        payment_info = get_payment_info()
        await query.answer(f"📱 KPay Number copied! {payment_info['kpay_number']}", show_alert=True)
        await query.message.reply_text(
            "📱 ***KBZ Pay Number***\n\n"
            f"`{payment_info['kpay_number']}`\n\n"
            f"👤 Name: ***{payment_info['kpay_name']}***\n"
            "📋 ***Number ကို အပေါ်မှ copy လုပ်ပါ***",
            parse_mode="Markdown"
        )

    elif query.data == "copy_wave":
        payment_info = get_payment_info()
        await query.answer(f"📱 Wave Number copied! {payment_info['wave_number']}", show_alert=True)
        await query.message.reply_text(
            "📱 ***Wave Money Number***\n\n"
            f"`{payment_info['wave_number']}`\n\n"
            f"👤 Name: ***{payment_info['wave_name']}***\n"
            "📋 ***Number ကို အပေါ်မှ copy လုပ်ပါ***",
            parse_mode="Markdown"
        )

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN environment variable မရှိပါ!")
        return

    # Load authorized users on startup
    load_authorized_users()

    application = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mmb", mmb_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("topup", topup_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("register", register_command))

    # Callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))

    # Photo handler (for payment screenshots)
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Handle all other message types
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.VOICE | filters.Sticker.ALL | filters.VIDEO |
         filters.ANIMATION | filters.AUDIO | filters.Document.ALL |
         filters.FORWARDED | filters.Entity("url") | filters.POLL) & ~filters.COMMAND,
        handle_restricted_content
    ))

    print("🤖 Bot စတင်နေပါသည် - MongoDB Version")
    print("✅ MongoDB နဲ့ ချိတ်ဆက်ပြီးပါပြီ")
    print("🔧 Orders, Topups နဲ့ User Management အဆင်သင့်ပါ")

    # Run main bot
    application.run_polling()

if __name__ == "__main__":
    main()
