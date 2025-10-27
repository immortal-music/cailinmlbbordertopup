import json
import os
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ApplicationHandlerStop
)
from telegram.constants import ParseMode # Import ParseMode
from pymongo import MongoClient, UpdateOne, ReturnDocument
from pymongo.errors import ConnectionFailure, PyMongoError

# --- Environment Variables ---
try:
    from env import BOT_TOKEN, ADMIN_ID, ADMIN_GROUP_ID, MONGODB_URL
except ImportError:
    print("❌ Error: env.py file not found or required variables (BOT_TOKEN, ADMIN_ID, ADMIN_GROUP_ID, MONGODB_URL) are missing.")
    exit(1)

# --- Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO # Change to logging.DEBUG for more detailed logs
)
logger = logging.getLogger(__name__)

# --- Constants ---
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"

RESTRICTION_NONE = "none"
RESTRICTION_AWAITING_APPROVAL = "awaiting_topup_approval"

CONFIG_MAINTENANCE = "maintenance_mode"
CONFIG_PAYMENT_INFO = "payment_info"

# --- MongoDB Connection Setup ---
try:
    if not MONGODB_URL:
        raise ValueError("MONGODB_URL is not set in env.py")

    client = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=5000, appname="MLBBTopUpBot")
    # Extract database name from connection string if provided, otherwise use default
    db_name_from_uri = MongoClient(MONGODB_URL).get_database().name
    db_name = db_name_from_uri if db_name_from_uri != 'test' else 'mlbb_bot_db' # Use your preferred default name
    db = client[db_name]

    logger.info(f"Using MongoDB database: {db_name}")

    # Collections
    users_col = db["users"]
    admins_col = db["admins"]
    auth_users_col = db["authorized_users"]
    prices_col = db["prices"]
    config_col = db["config"]

    # Test connection
    client.admin.command('ping')
    logger.info("✅ MongoDB connected successfully!")

    # --- Create Indexes (Essential for Performance) ---
    logger.info("Applying MongoDB indexes...")
    try:
        # Ensure indexes exist, create if not (removed unique=True for _id)
        users_col.create_index([("topups.topup_id", 1)], unique=True, sparse=True, background=True)
        users_col.create_index([("orders.order_id", 1)], unique=True, sparse=True, background=True)
        users_col.create_index([("topups.status", 1)], background=True)
        users_col.create_index([("orders.status", 1)], background=True)
        users_col.create_index([("orders.confirmed_at", 1)], background=True) # For Reporting
        users_col.create_index([("topups.approved_at", 1)], background=True) # For Reporting
        users_col.create_index([("restriction_status", 1)], background=True) # For checking restricted users

        auth_users_col.create_index([("_id", 1)], background=True) # Removed unique=True
        admins_col.create_index([("_id", 1)], background=True)    # Removed unique=True
        prices_col.create_index([("_id", 1)], background=True)    # Removed unique=True
        config_col.create_index([("_id", 1)], background=True)    # Removed unique=True
        logger.info("✅ MongoDB indexes checked/applied.")
    except PyMongoError as index_e:
        logger.warning(f"⚠️ Could not apply all MongoDB indexes: {index_e}. Performance might be affected.")


    # --- Initialize Config from DB (or set defaults) ---
    # Maintenance Mode
    maintenance_doc = config_col.find_one({"_id": CONFIG_MAINTENANCE})
    if not maintenance_doc:
        bot_maintenance = {"orders": True, "topups": True, "general": True}
        config_col.insert_one({"_id": CONFIG_MAINTENANCE, "settings": bot_maintenance})
        logger.info("Initialized default maintenance settings in DB.")
    else:
        bot_maintenance = {
            "orders": maintenance_doc.get("settings", {}).get("orders", True),
            "topups": maintenance_doc.get("settings", {}).get("topups", True),
            "general": maintenance_doc.get("settings", {}).get("general", True),
        }
        logger.info(f"Loaded maintenance settings from DB: {bot_maintenance}")

    # Payment Info
    payment_doc = config_col.find_one({"_id": CONFIG_PAYMENT_INFO})
    default_payment_info = {
            "kpay_number": "Not Set", "kpay_name": "Not Set", "kpay_image": None,
            "wave_number": "Not Set", "wave_name": "Not Set", "wave_image": None
        }
    if not payment_doc:
        payment_info = default_payment_info
        config_col.insert_one({"_id": CONFIG_PAYMENT_INFO, "details": payment_info})
        logger.info("Initialized default payment info in DB.")
    else:
        db_details = payment_doc.get("details", {})
        payment_info = {key: db_details.get(key, default_payment_info[key]) for key in default_payment_info}
        logger.info(f"Loaded payment info from DB (Numbers: KPay={payment_info['kpay_number']}, Wave={payment_info['wave_number']})")


    # Ensure owner is always an admin and authorized
    admins_col.update_one({"_id": ADMIN_ID}, {"$set": {"is_owner": True}}, upsert=True)
    auth_users_col.update_one({"_id": str(ADMIN_ID)}, {"$set": {"authorized_at": datetime.now()}}, upsert=True)

except ConnectionFailure:
    logger.critical("❌ MongoDB connection failed. Check your MONGODB_URL and network access.")
    exit(1)
except Exception as e:
    logger.critical(f"❌ An error occurred during MongoDB setup: {e}", exc_info=True)
    exit(1)


# --- In-Memory State (For multi-step processes like topup) ---
pending_topups = {} # { user_id: {"amount": int, "payment_method": str, ...} }


# --- Helper Functions (Database & Config Access) ---

def is_owner(user_id):
    return int(user_id) == ADMIN_ID

def is_admin(user_id):
    if int(user_id) == ADMIN_ID: return True
    try: return admins_col.count_documents({"_id": int(user_id)}) > 0
    except PyMongoError as e: logger.error(f"DB Error checking admin status for {user_id}: {e}"); return False

def is_user_authorized(user_id):
    if int(user_id) == ADMIN_ID: return True
    try: return auth_users_col.count_documents({"_id": str(user_id)}) > 0
    except PyMongoError as e: logger.error(f"DB Error checking auth status for {user_id}: {e}"); return False

def get_user_restriction_status(user_id):
    try:
        user_doc = users_col.find_one({"_id": str(user_id)}, {"restriction_status": 1})
        return user_doc.get("restriction_status", RESTRICTION_NONE) if user_doc else RESTRICTION_NONE
    except PyMongoError as e: logger.error(f"DB Error getting restriction status for {user_id}: {e}"); return RESTRICTION_NONE

def set_user_restriction_status(user_id, status):
    try:
        logger.info(f"Setting restriction status for {user_id} to {status}")
        users_col.update_one({"_id": str(user_id)}, {"$set": {"restriction_status": status}}, upsert=True)
        return True
    except PyMongoError as e: logger.error(f"DB Error setting restriction status for {user_id} to {status}: {e}"); return False

def load_prices():
    custom_prices = {}
    try:
        for doc in prices_col.find({}, {"_id": 1, "price": 1}):
            if "price" in doc: custom_prices[doc["_id"]] = doc["price"]
    except PyMongoError as e: logger.error(f"DB Error loading prices: {e}")
    return custom_prices

def get_price(diamonds):
    custom_prices = load_prices()
    if diamonds in custom_prices: return custom_prices[diamonds]
    if diamonds.startswith("wp") and diamonds[2:].isdigit():
        n = int(diamonds[2:]);
        if 1 <= n <= 10: return n * 6000
    table = { # Consider moving defaults to DB?
        "11": 950, "22": 1900, "33": 2850, "56": 4200, "112": 8200, "86": 5100,
        "172": 10200, "257": 15300, "343": 20400, "429": 25500, "514": 30600,
        "600": 35700, "706": 40800, "878": 51000, "963": 56100, "1049": 61200,
        "1135": 66300, "1412": 81600, "2195": 122400, "3688": 204000,
        "5532": 306000, "9288": 510000, "12976": 714000, "55": 3500,
        "165": 10000, "275": 16000, "565": 33000 }
    return table.get(diamonds)

async def check_pending_topup(user_id):
    try: return users_col.count_documents({"_id": str(user_id), "topups.status": STATUS_PENDING}) > 0
    except PyMongoError as e: logger.error(f"DB Error checking pending topup for {user_id}: {e}"); return False

def get_all_admin_ids():
    try: return [doc["_id"] for doc in admins_col.find({}, {"_id": 1})]
    except PyMongoError as e: logger.error(f"DB Error fetching admin IDs: {e}"); return [ADMIN_ID]

def get_authorized_user_count():
    try: return auth_users_col.count_documents({})
    except PyMongoError as e: logger.error(f"DB Error counting auth users: {e}"); return 0

def get_maintenance_status(feature): return bot_maintenance.get(feature, True)

def set_maintenance_status(feature, status: bool):
    try:
        result = config_col.update_one({"_id": CONFIG_MAINTENANCE}, {"$set": {f"settings.{feature}": status}}, upsert=True)
        if result.acknowledged: bot_maintenance[feature] = status; logger.info(f"Maintenance status for '{feature}' set to {status}"); return True
        else: logger.error(f"DB update not acknowledged for maintenance status '{feature}'"); return False
    except PyMongoError as e: logger.error(f"DB Error setting maintenance status for {feature} to {status}: {e}"); return False

def get_payment_info(): return payment_info

def update_payment_info(key, value):
    try:
        result = config_col.update_one({"_id": CONFIG_PAYMENT_INFO}, {"$set": {f"details.{key}": value}}, upsert=True)
        if result.acknowledged: payment_info[key] = value; logger.info(f"Payment info '{key}' updated."); return True
        else: logger.error(f"DB update not acknowledged for payment info '{key}'"); return False
    except PyMongoError as e: logger.error(f"DB Error updating payment info key '{key}': {e}"); return False

# --- Other Helper Functions ---
async def is_bot_admin_in_group(bot: Bot, chat_id: int):
    if not chat_id or not isinstance(chat_id, int) or chat_id == 0: logger.warning(f"is_bot_admin_in_group called with invalid chat_id: {chat_id}."); return False
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
        is_admin = bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
        logger.debug(f"Bot admin check for group {chat_id}: {is_admin}, status: {bot_member.status}")
        return is_admin
    except Exception as e: logger.error(f"Error checking bot admin status in group {chat_id}: {e}"); return False

def simple_reply(message_text: str) -> str:
    message_lower = message_text.lower()
    greetings = ["hello", "hi", "မင်္ဂလာပါ", "ဟယ်လို", "ဟိုင်း", "ကောင်းလား"]
    help_words = ["help", "ကူညီ", "အကူအညီ", "မသိ", "လမ်းညွှန်"]
    if any(word in message_lower for word in greetings):
        return ("👋 မင်္ဂလာပါ! JB MLBB AUTO TOP UP BOT မှ ကြိုဆိုပါတယ်!\n\n"
                "📱 Bot commands များ သုံးရန် /start နှိပ်ပါ\n")
    elif any(word in message_lower for word in help_words):
        return ("📱 ***အသုံးပြုနိုင်တဲ့ commands:***\n\n"
                "• /start - Bot စတင်အသုံးပြုရန်\n"
                "• /mmb gameid serverid amount - Diamond ဝယ်ယူရန်\n"
                "• /balance - လက်ကျန်ငွေ စစ်ရန်\n"
                "• /topup amount - ငွေဖြည့်ရန်\n"
                "• /price - ဈေးနှုန်းများ ကြည့်ရန်\n"
                "• /history - မှတ်တမ်းများ ကြည့်ရန်\n\n"
                "💡 အသေးစိတ် လိုအပ်ရင် admin ကို ဆက်သွယ်ပါ!")
    else:
        return ("📱 ***MLBB Diamond Top-up Bot***\n\n"
                "💎 ***Diamond ဝယ်ယူရန် /mmb command သုံးပါ။***\n"
                "💰 ***ဈေးနှုန်းများ သိရှိရန် /price နှိပ်ပါ။***\n"
                "🆘 ***အကူအညီ လိုရင် /start နှိပ်ပါ။***")

def validate_game_id(game_id: str) -> bool:
    return game_id.isdigit() and 6 <= len(game_id) <= 10

def validate_server_id(server_id: str) -> bool:
    return server_id.isdigit() and 3 <= len(server_id) <= 5

def is_banned_account(game_id: str) -> bool: # Basic check
    banned_ids = ["123456789", "000000000", "111111111"]
    if game_id in banned_ids: return True
    if len(set(game_id)) == 1: return True # All same digits
    if game_id.startswith("000") or game_id.endswith("000"): return True
    return False

def is_payment_screenshot(update: Update) -> bool: # Basic check
    return update.message and update.message.photo


# --- Message Sending Helpers ---
async def send_pending_topup_warning(update: Update):
    await update.effective_message.reply_text(
        "⏳ ***Pending Topup ရှိနေပါတယ်!***\n\n"
        "❌ သင့်မှာ admin က approve မလုပ်သေးတဲ့ topup ရှိနေပါတယ်။\n"
        "Admin က approve လုပ်ပေးတဲ့အထိ စောင့်ပါ။ Approve ရပြီးမှ commands တွေကို ပြန်အသုံးပြုနိုင်ပါမယ်။\n\n"
        "📞 အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။\n"
        "💡 /balance နဲ့ status စစ်ကြည့်နိုင်ပါတယ်။",
        parse_mode=ParseMode.MARKDOWN
    )

async def send_maintenance_message(update: Update, command_type: str):
    user_name = update.effective_user.first_name or "User"
    feature_text = { "orders": "အော်ဒါတင်ခြင်း", "topups": "ငွေဖြည့်ခြင်း", "general": "Bot" }.get(command_type, "Bot")
    msg = ( f"👋 မင်္ဂလာပါ {user_name}!\n\n"
            f"⏸️ ***{feature_text}အား ခေတ္တ ယာယီပိတ်ထားပါသည်*** ⏸️\n"
            "🔄 Admin မှ ပြန်လည်ဖွင့်ပေးမှ အသုံးပြုနိုင်ပါမည်။\n\n"
            "📞 အရေးပေါ်ဆိုရင် Admin ကို ဆက်သွယ်ပါ။" )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# --- Middleware for checking user restriction ---
async def check_restriction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return

    user_id = str(user.id)
    query = update.callback_query

    # Allow admins to bypass restriction for specific admin actions
    if is_admin(user_id):
        is_admin_action = False
        command_or_data = ""
        admin_commands = [
            '/approve', '/deduct', '/done', '/reply', '/ban', '/unban', '/addadm', '/unadm',
            '/sendgroup', '/maintenance', '/testgroup', '/setprice', '/removeprice',
            '/setwavenum', '/setkpaynum', '/setwavename', '/setkpayname', '/setkpayqr',
            '/removekpayqr', '/setwaveqr', '/removewaveqr', '/adminhelp', '/broadcast',
            '/d', '/m', '/y'
        ]
        admin_callback_prefixes = ['topup_approve_', 'topup_reject_', 'order_confirm_', 'order_cancel_', 'register_approve_', 'register_reject_', 'report_']

        if update.message and update.message.text and update.message.text.startswith('/'):
            command_or_data = update.message.text.split()[0].lower()
            if command_or_data in admin_commands: is_admin_action = True
        elif query and any(query.data.startswith(prefix) for prefix in admin_callback_prefixes):
            is_admin_action = True

        if is_admin_action: logger.debug(f"Admin {user_id} performing admin action, bypassing restriction check."); return

    # Check restriction status from DB for non-admin actions or non-admins
    restriction_status = get_user_restriction_status(user_id)

    if restriction_status == RESTRICTION_AWAITING_APPROVAL:
        logger.info(f"User {user_id} is restricted ({RESTRICTION_AWAITING_APPROVAL}). Blocking action.")
        message = ( "❌ ***အသုံးပြုမှု ကန့်သတ်ထားပါ!***\n\n"
                    "🔒 ***Screenshot ပို့ပြီး၍ Admin စစ်ဆေးနေဆဲ ဖြစ်ပါသည်။ Admin မှ လက်ခံ/ငြင်းပယ်ခြင်း မပြုလုပ်မချင်း အခြားလုပ်ဆောင်ချက်များ (Commands/Buttons) ကို အသုံးပြု၍ မရပါ။***\n\n"
                    "⏰ ***Admin မှ ဆောင်ရွက်ပြီးပါက ပြန်လည် အသုံးပြုနိုင်ပါမည်။***\n"
                    "📞 ***အရေးပေါ်ဆိုရင် admin ကို တိုက်ရိုက် ဆက်သွယ်ပါ။***" )
        try:
            if query: await query.answer("❌ အသုံးပြုမှု ကန့်သတ်ထားပါ! Admin ဆောင်ရွက်မှု စောင့်ပါ။", show_alert=True)
            elif update.message: await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed to send restriction notice to {user_id}: {e}")
        raise ApplicationHandlerStop

    logger.debug(f"User {user_id} restriction check passed ({restriction_status}).")


# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or "-"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    if not is_user_authorized(user_id):
        keyboard = [[InlineKeyboardButton("📝 Register တောင်းဆိုမယ်", callback_data="request_register")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
             f"🚫 ***Bot အသုံးပြုခွင့် မရှိပါ!***\n\n👋 ***မင်္ဂလာပါ*** `{name}`!\n🆔 Your ID: `{user_id}`\n\n"
             "❌ ***သင်သည် ဤ bot ကို အသုံးပြုခွင့် မရှိသေးပါ။***\n\n***လုပ်ရမည့်အရာများ***:\n"
             "***• အောက်က 'Register တောင်းဆိုမယ်' button ကို နှိပ်ပါ***\n***• သို့မဟုတ်*** /register ***command သုံးပါ။***\n"
             "***• Owner က approve လုပ်တဲ့အထိ စောင့်ပါ။***\n\n✅ ***Owner က approve လုပ်ပြီးမှ bot ကို အသုံးပြုနိုင်ပါမယ်။***",
             parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        return

    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return

    try:
        users_col.find_one_and_update(
            {"_id": user_id},
            {"$setOnInsert": {"balance": 0, "orders": [], "topups": [], "restriction_status": RESTRICTION_NONE}},
            {"$set": {"name": name, "username": username}}, upsert=True,
        )
    except PyMongoError as e: logger.error(f"DB Error during user upsert in /start for {user_id}: {e}"); await update.message.reply_text("❌ Database error."); return

    if user_id in pending_topups: del pending_topups[user_id]

    clickable_name = f"[{name}](tg://user?id={user_id})"
    msg = ( f"👋 ***မင်္ဂလာပါ*** {clickable_name}!\n🆔 ***Telegram User ID:*** `{user_id}`\n\n"
            "💎 ***JB MLBB AUTO TOP UP BOT*** မှ ကြိုဆိုပါတယ်။\n\n***အသုံးပြုနိုင်တဲ့ command များ***:\n"
            "➤ /mmb gameid serverid amount\n➤ /balance - ဘယ်လောက်လက်ကျန်ရှိလဲ စစ်မယ်\n"
            "➤ /topup amount - ငွေဖြည့်မယ် (screenshot တင်ပါ)\n➤ /price - Diamond များရဲ့ ဈေးနှုန်းများ\n"
            "➤ /history - အော်ဒါမှတ်တမ်းကြည့်မယ်\n\n***📌 ဥပမာ***:\n`/mmb 123456789 12345 wp1`\n"
            "`/mmb 123456789 12345 86`\n\n***လိုအပ်တာရှိရင် Owner ကို ဆက်သွယ်နိုင်ပါတယ်။***" )
    try:
        user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        photo_id = user_photos.photos[0][0].file_id if user_photos.total_count > 0 else None
        if photo_id: await context.bot.send_photo(update.effective_chat.id, photo_id, caption=msg, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.warning(f"Error getting/sending profile photo for {user_id}: {e}"); await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def mmb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not get_maintenance_status("orders"): await send_maintenance_message(update, "orders"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ***Topup လုပ်ငန်းစဉ် အရင်ပြီးဆုံးပါ! Screenshot တင်ပါ သို့မဟုတ် /cancel နှိပ်ပါ။***", parse_mode=ParseMode.MARKDOWN); return

    args = context.args
    if len(args) != 3: await update.message.reply_text("❌ Format မှား:\n`/mmb gameid serverid amount`\nဥပမာ:\n`/mmb 123 456 86`", parse_mode=ParseMode.MARKDOWN); return
    game_id, server_id, amount = args

    if not validate_game_id(game_id): await update.message.reply_text("❌ Game ID မှား (6-10 digits)။", parse_mode=ParseMode.MARKDOWN); return
    if not validate_server_id(server_id): await update.message.reply_text("❌ Server ID မှား (3-5 digits)။", parse_mode=ParseMode.MARKDOWN); return
    if is_banned_account(game_id):
        await update.message.reply_text(f"🚫 Account Ban ဖြစ်နေ:\n🎮 ID: `{game_id}`\n🌐 Server: `{server_id}`\n❌ Topup မရပါ။", parse_mode=ParseMode.MARKDOWN)
        admin_list = get_all_admin_ids()
        for admin_id in admin_list:
            try: await context.bot.send_message(admin_id, f"🚫 Banned Account Topup Attempt:\nUser: {update.effective_user.mention_markdown()} (`{user_id}`)\nGameID: `{game_id}`\nServer: `{server_id}`\nAmount: {amount}", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending banned acc notif to {admin_id}: {e}")
        return

    price = get_price(amount)
    if not price: await update.message.reply_text(f"❌ Diamond amount `{amount}` မရနိုင်ပါ။ /price နှိပ်ကြည့်ပါ။", parse_mode=ParseMode.MARKDOWN); return

    try:
        user_data = users_col.find_one({"_id": user_id}, {"balance": 1})
        user_balance = user_data.get("balance", 0) if user_data else 0
    except PyMongoError as e: logger.error(f"DB Error getting balance for {user_id} in /mmb: {e}"); await update.message.reply_text("❌ Database error."); return

    if user_balance < price:
        keyboard = [[InlineKeyboardButton("💳 ငွေဖြည့်မယ်", callback_data="topup_button")]]
        await update.message.reply_text(f"❌ လက်ကျန်ငွေ မလုံလောက်ပါ!\n💰 လိုအပ်: {price:,} MMK\n💳 လက်ကျန်: {user_balance:,} MMK\n❗ လိုသေး: {price - user_balance:,} MMK", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)); return

    order_id = f"ORD{datetime.now().strftime('%y%m%d%H%M%S%f')[:-3]}{user_id[-2:]}"
    order = { "order_id": order_id, "game_id": game_id, "server_id": server_id, "amount": amount, "price": price,
              "status": STATUS_PENDING, "timestamp": datetime.now().isoformat(), "user_id": user_id, "chat_id": update.effective_chat.id,
              "user_name": update.effective_user.first_name }
    try:
        result = users_col.update_one({"_id": user_id}, {"$inc": {"balance": -price}, "$push": {"orders": order}})
        if not result.modified_count: logger.warning(f"Order update failed for user {user_id}."); await update.message.reply_text("❌ Order processing error."); return
        updated_user_data = users_col.find_one({"_id": user_id}, {"balance": 1})
        new_balance = updated_user_data.get("balance", user_balance - price)
    except PyMongoError as e: logger.error(f"DB Error processing order for {user_id}: {e}"); await update.message.reply_text("❌ DB error during order."); return

    keyboard = [[InlineKeyboardButton("✅ Confirm", callback_data=f"order_confirm_{order_id}"), InlineKeyboardButton("❌ Cancel", callback_data=f"order_cancel_{order_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    user_mention = update.effective_user.mention_markdown()

    admin_msg = (f"🔔 ***အော်ဒါအသစ်!***\n📝 ID: `{order_id}`\n👤 User: {user_mention} (`{user_id}`)\n🎮 Game ID: `{game_id}`\n"
                 f"🌐 Server ID: `{server_id}`\n💎 Amount: {amount}\n💰 Price: {price:,} MMK\n⏰ Time: {datetime.now():%Y-%m-%d %H:%M:%S}\n📊 Status: ⏳ {STATUS_PENDING}")
    admin_list = get_all_admin_ids()
    for admin_id in admin_list:
        try: await context.bot.send_message(admin_id, admin_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except Exception as e: logger.warning(f"Failed sending order notif to admin {admin_id}: {e}")

    if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
        try:
            group_msg = (f"🛒 ***အော်ဒါအသစ်!***\n📝 ID: `{order_id}`\n👤 User: {user_mention}\n🎮 Game ID: `{game_id}`\n"
                         f"🌐 Server ID: `{server_id}`\n💎 Amount: {amount}\n💰 Price: {price:,} MMK\n📊 Status: ⏳ {STATUS_PENDING}\n#NewOrder")
            await context.bot.send_message(ADMIN_GROUP_ID, group_msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed sending order notif to group {ADMIN_GROUP_ID}: {e}")

    await update.message.reply_text(
        f"✅ ***အော်ဒါ အောင်မြင်ပါပြီ!***\n📝 ID: `{order_id}`\n🎮 Game ID: `{game_id}`\n🌐 Server ID: `{server_id}`\n"
        f"💎 Diamond: {amount}\n💰 ကုန်ကျစရိတ်: {price:,} MMK\n💳 လက်ကျန်ငွေ: {new_balance:,} MMK\n📊 Status: ⏳ {STATUS_PENDING}\n\n"
        f"⚠️ Admin confirm လုပ်မှ diamonds ရပါမည်။", parse_mode=ParseMode.MARKDOWN)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ Topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return

    try:
        user_data = users_col.find_one({"_id": user_id})
        if not user_data: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return

        balance = user_data.get("balance", 0)
        total_orders = len(user_data.get("orders", []))
        total_topups = len(user_data.get("topups", []))
        pending_topups_list = [t for t in user_data.get("topups", []) if t.get("status") == STATUS_PENDING]
        pending_topups_count = len(pending_topups_list)
        pending_amount = sum(t.get("amount", 0) for t in pending_topups_list)

        name = user_data.get('name', 'Unknown').replace('*','').replace('_','').replace('`','')
        username = user_data.get('username', 'None').replace('*','').replace('_','').replace('`','')

        status_msg = f"\n⏳ ***Pending Topups***: {pending_topups_count} ခု ({pending_amount:,} MMK)\n❗ ***Admin approve စောင့်ပါ။***" if pending_topups_count > 0 else ""
        keyboard = [[InlineKeyboardButton("💳 ငွေဖြည့်မယ်", callback_data="topup_button")]]
        balance_text = (f"💳 ***သင့် Account***\n\n💰 ***Balance***: `{balance:,} MMK`\n📦 Orders: {total_orders}\n"
                        f"💸 Topups: {total_topups}{status_msg}\n\n👤 Name: {name}\n🆔 Username: @{username}")

        try:
            user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
            photo_id = user_photos.photos[0][0].file_id if user_photos.total_count > 0 else None
            markup = InlineKeyboardMarkup(keyboard)
            if photo_id: await context.bot.send_photo(update.effective_chat.id, photo_id, caption=balance_text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            else: await update.message.reply_text(balance_text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except Exception as e: logger.warning(f"Error sending balance with photo: {e}"); await update.message.reply_text(balance_text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

    except PyMongoError as e: logger.error(f"DB Error getting balance for {user_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not get_maintenance_status("topups"): await send_maintenance_message(update, "topups"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ယခင် topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return

    if not context.args or len(context.args) != 1: await update.message.reply_text("❌ Format မှား:\n`/topup <amount>`\nဥပမာ: `/topup 5000`", parse_mode=ParseMode.MARKDOWN); return
    try:
        amount = int(context.args[0])
        if amount < 1000: await update.message.reply_text("❌ အနည်းဆုံး 1,000 MMK ဖြည့်ပါ။", parse_mode=ParseMode.MARKDOWN); return
    except ValueError: await update.message.reply_text("❌ Amount ကို ဂဏန်းဖြင့်သာ ထည့်ပါ။", parse_mode=ParseMode.MARKDOWN); return

    pending_topups[user_id] = {"amount": amount, "timestamp": datetime.now().isoformat()}
    keyboard = [[InlineKeyboardButton("📱 KBZ Pay", callback_data=f"topup_pay_kpay_{amount}")],
                [InlineKeyboardButton("📱 Wave Money", callback_data=f"topup_pay_wave_{amount}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="topup_cancel")]]
    await update.message.reply_text(f"💳 ***ငွေဖြည့်ရန်***\n💰 Amount: `{amount:,} MMK`\n\n⬇️ Payment method ရွေးပါ:",
                                   parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in pending_topups: await update.message.reply_text("⏳ Topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return

    custom_prices = load_prices()
    default_prices = { "wp1": 6000, "wp2": 12000, "wp3": 18000, "wp4": 24000, "wp5": 30000, "wp6": 36000, "wp7": 42000, "wp8": 48000, "wp9": 54000, "wp10": 60000,
        "11": 950, "22": 1900, "33": 2850, "56": 4200, "86": 5100, "112": 8200, "172": 10200, "257": 15300, "343": 20400, "429": 25500, "514": 30600,
        "600": 35700, "706": 40800, "878": 51000, "963": 56100, "1049": 61200, "1135": 66300, "1412": 81600, "2195": 122400, "3688": 204000,
        "5532": 306000, "9288": 510000, "12976": 714000, "55": 3500, "165": 10000, "275": 16000, "565": 33000 }
    current_prices = {**default_prices, **custom_prices}

    price_msg = "💎 ***MLBB Diamond ဈေးနှုန်းများ***\n\n🎟️ ***Weekly Pass***:\n"
    for i in range(1, 11): price_msg += f"• wp{i} = {current_prices.get(f'wp{i}', 'N/A'):,} MMK\n"
    price_msg += "\n💎 ***Regular Diamonds***:\n"
    reg_dm = ["11", "22", "33", "56", "86", "112", "172", "257", "343", "429", "514", "600", "706", "878", "963", "1049", "1135", "1412", "2195", "3688", "5532", "9288", "12976"]
    for dm in reg_dm: price_msg += f"• {dm} = {current_prices.get(dm, 'N/A'):,} MMK\n"
    price_msg += "\n💎 ***2X Diamond Pass***:\n"
    dbl_dm = ["55", "165", "275", "565"]
    for dm in dbl_dm: price_msg += f"• {dm} = {current_prices.get(dm, 'N/A'):,} MMK\n"

    other_customs = {k: v for k, v in custom_prices.items() if k not in default_prices}
    if other_customs:
        price_msg += "\n🔥 ***Special Items***:\n"
        for item, price in sorted(other_customs.items()): price_msg += f"• {item} = {price:,} MMK\n"

    price_msg += "\n\n***📝 အသုံးပြုရန်***:\n`/mmb gameid serverid amount`\nဥပမာ: `/mmb 123 456 86`"
    await update.message.reply_text(price_msg, parse_mode=ParseMode.MARKDOWN)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in pending_topups:
        del pending_topups[user_id]
        await update.message.reply_text("✅ ***ငွေဖြည့်ခြင်း ပယ်ဖျက်ပါပြီ!*** /topup နှိပ်ပြီး ပြန်စနိုင်ပါသည်။", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("ℹ️ လက်ရှိ ငွေဖြည့်မှု လုပ်ငန်းစဉ် မရှိပါ။", parse_mode=ParseMode.MARKDOWN)


async def c_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("🧮 Calculator: `/c <expression>`\nဥပမာ: `/c 100*5+3`", parse_mode=ParseMode.MARKDOWN); return
    expression = ''.join(context.args).strip()
    allowed_chars = set("0123456789+-*/(). ")
    if not all(char in allowed_chars for char in expression): await update.message.reply_text("❌ Invalid characters."); return
    try:
        result = eval(expression.replace(' ', ''))
        await update.message.reply_text(f"🧮 Result:\n`{expression}` = ***{result:,}***", parse_mode=ParseMode.MARKDOWN)
    except ZeroDivisionError: await update.message.reply_text("❌ သုညဖြင့် စားလို့မရပါ။")
    except Exception as e: logger.warning(f"Calculator error for '{expression}': {e}"); await update.message.reply_text("❌ Expression မှားနေပါသည်။")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ Topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return

    try:
        user_data = users_col.find_one({"_id": user_id}, {"orders": {"$slice": -5}, "topups": {"$slice": -5}})
        if not user_data: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return
        orders = user_data.get("orders", [])
        topups = user_data.get("topups", [])
        if not orders and not topups: await update.message.reply_text("📋 မှတ်တမ်း မရှိသေးပါ။"); return

        msg = "📋 ***သင်၏ မှတ်တမ်းများ***\n\n"
        if orders:
            msg += "🛒 Orders (နောက်ဆုံး ၅ ခု):\n"
            status_map = {STATUS_PENDING: "⏳", STATUS_CONFIRMED: "✅", STATUS_CANCELLED: "❌"}
            for order in reversed(orders):
                status = order.get("status", STATUS_PENDING)
                ts_str = order.get("timestamp", "")
                ts = datetime.fromisoformat(ts_str).strftime('%y-%m-%d %H:%M') if ts_str else "N/A"
                msg += f"{status_map.get(status, '❓')} `{order.get('order_id', 'N/A')}` ({order.get('amount', '?')}💎/{order.get('price', 0):,}K) [{ts}]\n"
        if topups:
             msg += "\n💳 Topups (နောက်ဆုံး ၅ ခု):\n"
             status_map = {STATUS_PENDING: "⏳", STATUS_APPROVED: "✅", STATUS_REJECTED: "❌"}
             for topup in reversed(topups):
                 status = topup.get("status", STATUS_PENDING)
                 ts_str = topup.get("timestamp", "")
                 ts = datetime.fromisoformat(ts_str).strftime('%y-%m-%d %H:%M') if ts_str else "N/A"
                 msg += f"{status_map.get(status, '❓')} {topup.get('amount', 0):,} MMK [{ts}]\n"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error getting history for {user_id}: {e}"); await update.message.reply_text("❌ Database error.")
    except Exception as e: logger.error(f"Error formatting history for {user_id}: {e}"); await update.message.reply_text("❌ Error displaying history.")


# --- Admin Commands ---

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user = update.effective_user
    admin_id = str(admin_user.id)
    admin_name = admin_user.first_name
    if not is_admin(admin_id): return # Redundant if filter is used, but safe

    if len(context.args) != 2: await update.message.reply_text("❌ Format: `/approve <user_id> <amount>`"); return
    target_user_id, amount_str = context.args
    try: amount = int(amount_str)
    except ValueError: await update.message.reply_text("❌ Amount ကို ဂဏန်းဖြင့် ထည့်ပါ။"); return

    try:
        # Find user and the specific pending topup to get its ID
        user_doc = users_col.find_one(
            {"_id": target_user_id, "topups": {"$elemMatch": {"amount": amount, "status": STATUS_PENDING}}},
            {"topups.$": 1} # Get only the matched topup element
        )

        if not user_doc or not user_doc.get("topups"):
            await update.message.reply_text(f"❌ `{target_user_id}` ထံမှ `{amount}` MMK pending topup မတွေ့ပါ။"); return

        topup_id_to_approve = user_doc["topups"][0].get("topup_id") # Get the actual ID

        # Atomically update using the found topup_id
        result = users_col.find_one_and_update(
            {"_id": target_user_id, "topups.topup_id": topup_id_to_approve, "topups.status": STATUS_PENDING}, # Match by ID and ensure still pending
            {"$set": {
                    "topups.$.status": STATUS_APPROVED, "topups.$.approved_by": admin_name,
                    "topups.$.approved_at": datetime.now().isoformat(), "restriction_status": RESTRICTION_NONE },
             "$inc": {"balance": amount} },
            projection={"balance": 1}, return_document=ReturnDocument.BEFORE # Get old balance
        )

        if result is None: # Update didn't happen (race condition or ID mismatch)
            await update.message.reply_text("⚠️ Topup ကို အခြား Admin လုပ်ဆောင်သွားပြီး/မတွေ့ ဖြစ်နိုင်ပါသည်။")
            return

        old_balance = result.get("balance", 0)
        new_balance = old_balance + amount

        # Notify user
        try:
            keyboard = [[InlineKeyboardButton("💎 Order တင်မယ်", url=f"https://t.me/{context.bot.username}?start=order")]]
            await context.bot.send_message( int(target_user_id),
                (f"✅ ***ငွေဖြည့်မှု အတည်ပြုပါပြီ!*** 🎉\n\n💰 ပမာဏ: `{amount:,} MMK`\n💳 လက်ကျန်ငွေ: `{new_balance:,} MMK`\n"
                 f"👤 Approved by: {admin_name}\n⏰ အချိန်: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
                 f"🎉 ယခု diamonds ဝယ်ယူနိုင်ပါပြီ!\n🔓 Bot functions များ ပြန်သုံးနိုင်ပါပြီ!\n\n💎 Order တင်ရန်:\n`/mmb gameid serverid amount`"),
                parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e: logger.warning(f"Failed to notify user {target_user_id} of approval: {e}")

        await update.message.reply_text(f"✅ Approve အောင်မြင်!\n👤 User ID: `{target_user_id}`\n💰 Amount: `{amount:,} MMK`\n💳 New balance: `{new_balance:,} MMK`", parse_mode=ParseMode.MARKDOWN)

        # Notify other admins/group (optional)

    except PyMongoError as e: logger.error(f"DB Error during approve for {target_user_id} by {admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = str(update.effective_user.id)
    if not is_admin(admin_id): return
    if len(context.args) != 2: await update.message.reply_text("❌ Format: `/deduct <user_id> <amount>`"); return
    target_user_id, amount_str = context.args
    try: amount = int(amount_str); assert amount > 0
    except (ValueError, AssertionError): await update.message.reply_text("❌ Amount မှားနေ (must be positive number)။"); return

    try:
        result = users_col.find_one_and_update(
            {"_id": target_user_id, "balance": {"$gte": amount}}, {"$inc": {"balance": -amount}},
            projection={"balance": 1}, return_document=ReturnDocument.AFTER
        )
        if result is None:
            user_exists = users_col.find_one({"_id": target_user_id}, {"balance": 1})
            if user_exists: await update.message.reply_text(f"❌ Balance မလုံလောက်! လက်ကျန်: {user_exists.get('balance', 0):,} MMK");
            else: await update.message.reply_text("❌ User မတွေ့ရှိပါ!")
            return
        new_balance = result.get("balance")
        try: # Notify user
            await context.bot.send_message(int(target_user_id),
                f"⚠️ ***လက်ကျန်ငွေ နှုတ်ခံရမှု***\n💰 ပမာဏ: `{amount:,} MMK`\n💳 လက်ကျန်: `{new_balance:,} MMK`\n⏰ {datetime.now():%Y-%m-%d %H:%M:%S}\n📞 Admin ကို ဆက်သွယ်ပါ။",
                parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed to notify user {target_user_id} of deduction: {e}")
        await update.message.reply_text(f"✅ Balance နှုတ်ပြီး!\n👤 ID: `{target_user_id}`\n💰 နှုတ် Amount: `{amount:,} MMK`\n💳 လက်ကျန်: `{new_balance:,} MMK`", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error during deduct for {target_user_id} by {admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/done <user_id>`"); return
    target_user_id = int(context.args[0])
    try:
        await context.bot.send_message(target_user_id, "🙏 ဝယ်ယူအားပေးမှုအတွက် ကျေးဇူးတင်ပါတယ်။\n✅ Order Done! 🎉")
        await update.message.reply_text("✅ User ထံ message ပို့ပြီး။")
    except Exception as e: logger.warning(f"Failed to send /done msg to {target_user_id}: {e}"); await update.message.reply_text("❌ User ID မှားနေ/Bot blocked ဖြစ်နေ။")


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) < 2 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/reply <user_id> <message>`"); return
    target_user_id, message = int(context.args[0]), " ".join(context.args[1:])
    try:
        await context.bot.send_message(target_user_id, f"✉️ ***Admin Reply:***\n\n{message}", parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("✅ Message ပို့ပြီး။")
    except Exception as e: logger.warning(f"Failed to send /reply msg to {target_user_id}: {e}"); await update.message.reply_text("❌ Message မပို့နိုင်ပါ။")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user = update.effective_user; admin_id = str(admin_user.id); admin_name = admin_user.first_name
    if not is_admin(admin_id): return
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/ban <user_id>`"); return
    target_user_id = context.args[0]
    if int(target_user_id) == ADMIN_ID: await update.message.reply_text("❌ Owner ကို ban မရပါ။"); return

    try:
        result_auth = auth_users_col.delete_one({"_id": target_user_id})
        if result_auth.deleted_count == 0: await update.message.reply_text("ℹ️ User သည် authorize မလုပ်ထားပါ/ban ပြီးသား။"); return
        set_user_restriction_status(target_user_id, RESTRICTION_NONE)

        user_doc = users_col.find_one({"_id": target_user_id}, {"name": 1})
        target_name = user_doc.get("name", "Unknown") if user_doc else "Unknown"

        try: await context.bot.send_message(int(target_user_id), "🚫 Bot အသုံးပြုခွင့် ပိတ်ပင်ခံရမှု\nAdmin က သင့်ကို ban လုပ်လိုက်ပါပြီ။", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed sending ban notif to {target_user_id}: {e}")
        if int(admin_id) != ADMIN_ID:
            try: await context.bot.send_message(ADMIN_ID, f"🚫 User Ban by Admin:\nBanned: [{target_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\nBy: {admin_user.mention_markdown()}", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending ban notif to owner: {e}")
        if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
            try: await context.bot.send_message(ADMIN_GROUP_ID, f"🚫 User Banned:\nUser: [{target_name}](tg://user?id={target_user_id})\nBy: {admin_name}\n#UserBanned", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending ban notif to group: {e}")

        await update.message.reply_text(f"✅ User Ban အောင်မြင်!\n👤 ID: `{target_user_id}`\n📊 Total authorized: {get_authorized_user_count()}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error during ban for {target_user_id} by {admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user = update.effective_user; admin_id = str(admin_user.id); admin_name = admin_user.first_name
    if not is_admin(admin_id): return
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/unban <user_id>`"); return
    target_user_id = context.args[0]
    if is_user_authorized(target_user_id): await update.message.reply_text("ℹ️ User သည် authorize လုပ်ထားပြီးသား။"); return

    try:
        auth_users_col.update_one({"_id": target_user_id}, {"$set": {"authorized_at": datetime.now(), "unbanned_by": admin_id}}, upsert=True)
        set_user_restriction_status(target_user_id, RESTRICTION_NONE)

        user_doc = users_col.find_one({"_id": target_user_id}, {"name": 1})
        target_name = user_doc.get("name", "Unknown") if user_doc else "Unknown"

        try: await context.bot.send_message(int(target_user_id), "🎉 *Bot အသုံးပြုခွင့် ပြန်ရပါပြီ!*\nAdmin က ban ဖြုတ်ပေးပါပြီ။ /start နှိပ်ပါ။", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed sending unban notif to {target_user_id}: {e}")
        if int(admin_id) != ADMIN_ID:
             try: await context.bot.send_message(ADMIN_ID, f"✅ User Unban by Admin:\nUnbanned: [{target_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\nBy: {admin_user.mention_markdown()}", parse_mode=ParseMode.MARKDOWN)
             except Exception as e: logger.warning(f"Failed sending unban notif to owner: {e}")
        if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
            try: await context.bot.send_message(ADMIN_GROUP_ID, f"✅ User Unbanned:\nUser: [{target_name}](tg://user?id={target_user_id})\nBy: {admin_name}\n#UserUnbanned", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending unban notif to group: {e}")

        await update.message.reply_text(f"✅ User Unban အောင်မြင်!\n👤 ID: `{target_user_id}`\n📊 Total authorized: {get_authorized_user_count()}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error during unban for {target_user_id} by {admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def addadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/addadm <user_id>`"); return
    new_admin_id = int(context.args[0])
    if is_admin(new_admin_id): await update.message.reply_text("ℹ️ User သည် admin ဖြစ်ပြီးသား။"); return

    try:
        admins_col.update_one({"_id": new_admin_id}, {"$set": {"is_owner": False, "added_by": ADMIN_ID, "added_at": datetime.now()}}, upsert=True)
        try: await context.bot.send_message(new_admin_id, "🎉 Admin ရာထူးရရှိမှု\nOwner က သင့်ကို Admin ခန့်အပ်ပါပြီ။ /adminhelp နှိပ်ကြည့်ပါ။")
        except Exception as e: logger.warning(f"Failed sending addadm notif to {new_admin_id}: {e}")
        await update.message.reply_text(f"✅ Admin ထပ်ထည့်ပြီး!\n👤 ID: `{new_admin_id}`\n📊 Total admins: {admins_col.count_documents({})}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error adding admin {new_admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def unadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/unadm <user_id>`"); return
    target_admin_id = int(context.args[0])
    if target_admin_id == ADMIN_ID: await update.message.reply_text("❌ Owner ကို ဖြုတ်မရပါ။"); return

    try:
        result = admins_col.delete_one({"_id": target_admin_id})
        if result.deleted_count == 0: await update.message.reply_text("ℹ️ User သည် admin မဟုတ်ပါ။"); return
        try: await context.bot.send_message(target_admin_id, "⚠️ Admin ရာထူး ရုပ်သိမ်းခံရမှု\nOwner က သင့် admin ရာထူးကို ဖြုတ်လိုက်ပါပြီ။")
        except Exception as e: logger.warning(f"Failed sending unadm notif to {target_admin_id}: {e}")
        await update.message.reply_text(f"✅ Admin ဖြုတ်ပြီး!\n👤 ID: `{target_admin_id}`\n📊 Total admins: {admins_col.count_documents({})}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error removing admin {target_admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2: await update.message.reply_text("❌ Format: `/maintenance <orders|topups|general> <on|off>`"); return
    feature, status_str = context.args[0].lower(), context.args[1].lower()
    if feature not in ["orders", "topups", "general"] or status_str not in ["on", "off"]: await update.message.reply_text("❌ Invalid feature or status."); return
    status_bool = (status_str == "on")

    if set_maintenance_status(feature, status_bool):
        status_text = "🟢 Enabled" if status_bool else "🔴 Disabled"
        feature_text = {"orders": "Orders", "topups": "Topups", "general": "General"}.get(feature)
        current_status = "\n".join([f"• {f.capitalize()}: {'🟢' if bot_maintenance[f] else '🔴'}" for f in bot_maintenance])
        await update.message.reply_text(f"✅ Maintenance Mode Updated!\n🔧 Feature: {feature_text}\n📊 Status: {status_text}\n\n***Current Status:***\n{current_status}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error updating maintenance mode in DB.")


async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2: await update.message.reply_text("❌ Format: `/setprice <item> <price>`"); return
    item, price_str = context.args[0], context.args[1]
    try: price = int(price_str); assert price >= 0
    except (ValueError, AssertionError): await update.message.reply_text("❌ Price မှားနေ (must be non-negative number)။"); return
    try:
        prices_col.update_one({"_id": item}, {"$set": {"price": price}}, upsert=True)
        await update.message.reply_text(f"✅ Price Updated!\n💎 Item: `{item}`\n💰 New Price: `{price:,} MMK`", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error setting price for {item}: {e}"); await update.message.reply_text("❌ Database error.")


async def removeprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1: await update.message.reply_text("❌ Format: `/removeprice <item>`"); return
    item = context.args[0]
    try:
        result = prices_col.delete_one({"_id": item})
        if result.deleted_count == 0: await update.message.reply_text(f"❌ `{item}` မှာ custom price မရှိပါ။"); return
        await update.message.reply_text(f"✅ Custom Price Removed!\n💎 Item: `{item}`\n🔄 Default price ကို ပြန်သုံးပါမည်။", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error removing price for {item}: {e}"); await update.message.reply_text("❌ Database error.")


async def setwavenum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1: await update.message.reply_text("❌ Format: `/setwavenum <number>`"); return
    new_number = context.args[0]
    if update_payment_info("wave_number", new_number):
        info = get_payment_info()
        await update.message.reply_text(f"✅ Wave Number Updated!\n📱 New: `{info['wave_number']}`\n👤 Name: {info['wave_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error updating.")

async def setkpaynum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1: await update.message.reply_text("❌ Format: `/setkpaynum <number>`"); return
    new_number = context.args[0]
    if update_payment_info("kpay_number", new_number):
        info = get_payment_info()
        await update.message.reply_text(f"✅ KPay Number Updated!\n📱 New: `{info['kpay_number']}`\n👤 Name: {info['kpay_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error updating.")

async def setwavename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ Format: `/setwavename <name>`"); return
    new_name = " ".join(context.args)
    if update_payment_info("wave_name", new_name):
        info = get_payment_info()
        await update.message.reply_text(f"✅ Wave Name Updated!\n📱 Number: `{info['wave_number']}`\n👤 New Name: {info['wave_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error updating.")

async def setkpayname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ Format: `/setkpayname <name>`"); return
    new_name = " ".join(context.args)
    if update_payment_info("kpay_name", new_name):
        info = get_payment_info()
        await update.message.reply_text(f"✅ KPay Name Updated!\n📱 Number: `{info['kpay_number']}`\n👤 New Name: {info['kpay_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error updating.")

async def setkpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not update.message.reply_to_message or not update.message.reply_to_message.photo: await update.message.reply_text("❌ ပုံကို reply လုပ်ပြီး `/setkpayqr` သုံးပါ။"); return
    photo_file_id = update.message.reply_to_message.photo[-1].file_id
    if update_payment_info("kpay_image", photo_file_id): await update.message.reply_text("✅ KPay QR Code ထည့်သွင်းပြီးပါပြီ!")
    else: await update.message.reply_text("❌ Error setting KPay QR.")

async def removekpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    current_info = get_payment_info()
    if not current_info.get("kpay_image"): await update.message.reply_text("ℹ️ KPay QR code မရှိသေးပါ။"); return
    if update_payment_info("kpay_image", None): await update.message.reply_text("✅ KPay QR Code ဖျက်ပြီးပါပြီ!")
    else: await update.message.reply_text("❌ Error removing KPay QR.")

async def setwaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not update.message.reply_to_message or not update.message.reply_to_message.photo: await update.message.reply_text("❌ ပုံကို reply လုပ်ပြီး `/setwaveqr` သုံးပါ။"); return
    photo_file_id = update.message.reply_to_message.photo[-1].file_id
    if update_payment_info("wave_image", photo_file_id): await update.message.reply_text("✅ Wave QR Code ထည့်သွင်းပြီးပါပြီ!")
    else: await update.message.reply_text("❌ Error setting Wave QR.")

async def removewaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    current_info = get_payment_info()
    if not current_info.get("wave_image"): await update.message.reply_text("ℹ️ Wave QR code မရှိသေးပါ။"); return
    if update_payment_info("wave_image", None): await update.message.reply_text("✅ Wave QR Code ဖျက်ပြီးပါပြီ!")
    else: await update.message.reply_text("❌ Error removing Wave QR.")

async def send_to_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ Format: `/sendgroup <message>`"); return
    message = " ".join(context.args)
    if not ADMIN_GROUP_ID: await update.message.reply_text("❌ Admin Group ID is not set."); return
    try:
        await context.bot.send_message(ADMIN_GROUP_ID, f"📢 ***Admin Message***\n\n{message}", parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("✅ Group ထဲကို message ပို့ပြီး။")
    except Exception as e: logger.error(f"Failed to send to group {ADMIN_GROUP_ID}: {e}"); await update.message.reply_text(f"❌ Group ထဲကို message မပို့နိုင်ပါ။\nError: {e}")

async def testgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ADMIN_GROUP_ID: await update.message.reply_text("❌ Admin Group ID is not set."); return
    is_admin_in_group = await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID)
    status_text = "Admin ✅" if is_admin_in_group else "Not Admin ❌"
    try:
        if is_admin_in_group:
            await context.bot.send_message(ADMIN_GROUP_ID, f"✅ **Test Notification**\nBot ကနေ group ထဲကို message ပို့နိုင်ပါပြီ!\n⏰ {datetime.now():%Y-%m-%d %H:%M:%S}", parse_mode=ParseMode.MARKDOWN)
            await update.message.reply_text(f"✅ **Group Test OK!**\n📱 Group ID: `{ADMIN_GROUP_ID}`\n🤖 Bot Status: {status_text}\n📨 Test message ပို့ပြီး။", parse_mode=ParseMode.MARKDOWN)
        else:
             await update.message.reply_text(f"❌ **Group Connection Failed!**\n📱 Group ID: `{ADMIN_GROUP_ID}`\n🤖 Bot Status: {status_text}\n\n**ပြင်ဆင်ရန်:**\n1️⃣ Group မှာ bot ကို add လုပ်ပါ\n2️⃣ Bot ကို Administrator လုပ်ပါ\n3️⃣ 'Post Messages' permission ပေးပါ", parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error during /testgroup: {e}"); await update.message.reply_text(f"❌ Error sending test message: {e}")


async def adminhelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    user_id = str(update.effective_user.id)
    is_user_owner = is_owner(user_id)
    current_payment_info = get_payment_info()

    help_msg = "🔧 ***Admin Commands*** 🔧\n\n"
    if is_user_owner:
        help_msg += ("👑 *Owner Only:*\n"
                     "/addadm <id>\n/unadm <id>\n"
                     "/broadcast (Reply)\n"
                     "/setkpayqr (Reply)\n/removekpayqr\n"
                     "/setwaveqr (Reply)\n/removewaveqr\n"
                     "/d /m /y (Reports)\n\n")
                     # Removed clone commands
    help_msg += ("💰 *Balance:*\n/approve <id> <amt>\n/deduct <id> <amt>\n\n"
                 "💬 *Comm:*\n/reply <id> <msg>\n/done <id>\n/sendgroup <msg>\n\n"
                 "🔧 *Settings:*\n/maintenance <feat> <on|off>\n"
                 "/setprice <item> <price>\n/removeprice <item>\n"
                 "/setkpaynum <num>\n/setwavenum <num>\n/setkpayname <name>\n/setwavename <name>\n\n"
                 "🛡️ *Users:*\n/ban <id>\n/unban <id>\n\n"
                 "ℹ️ *Info:*\n/testgroup\n/adminhelp\n\n")
    help_msg += (f"📊 *Status:*\n"
                 f"• Orders: {'🟢' if bot_maintenance['orders'] else '🔴'}\n"
                 f"• Topups: {'🟢' if bot_maintenance['topups'] else '🔴'}\n"
                 f"• General: {'🟢' if bot_maintenance['general'] else '🔴'}\n"
                 f"• Auth Users: {get_authorized_user_count()}\n\n"
                 f"💳 *Payment:*\n"
                 f"• KPay: {current_payment_info['kpay_number']} ({current_payment_info['kpay_name']}){'[QR]' if current_payment_info['kpay_image'] else ''}\n"
                 f"• Wave: {current_payment_info['wave_number']} ({current_payment_info['wave_name']}){'[QR]' if current_payment_info['wave_image'] else ''}")

    await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): await update.message.reply_text("❌ Owner only!"); return
    if not update.message.reply_to_message: await update.message.reply_text("❌ Message ကို reply လုပ်ပြီးမှ သုံးပါ။"); return
    # Args determine target (user, gp, or both) - Your original logic was fine here
    args = context.args
    send_to_users = "user" in args or not args # Default to users if no arg
    send_to_groups = "gp" in args

    replied_msg = update.message.reply_to_message
    user_success = user_fail = group_success = group_fail = 0

    try:
        user_ids_cursor = users_col.find({}, {"_id": 1}) # Get cursor for users
        user_ids = [doc["_id"] for doc in user_ids_cursor] # Fetch all user IDs

        # Find unique group IDs (inefficient for large data, consider storing separately or better aggregation)
        group_ids = set()
        # group_pipeline = [{"$match": {"orders.chat_id": {"$lt": 0}}}, {"$group": {"_id": "$orders.chat_id"}}] # Example
        # for doc in users_col.aggregate(group_pipeline): group_ids.add(doc["_id"])
        # Add similar logic for topups if chat_id is stored there

        # --- Broadcast Logic ---
        if replied_msg.text:
            msg_text = replied_msg.text; msg_entities = replied_msg.entities
            if send_to_users:
                for uid in user_ids:
                    try: await context.bot.send_message(int(uid), msg_text, entities=msg_entities); user_success += 1
                    except Exception as e: logger.warning(f"Broadcast text fail user {uid}: {e}"); user_fail += 1
                    await asyncio.sleep(0.05) # Rate limit
            if send_to_groups:
                 for gid in group_ids:
                    try: await context.bot.send_message(gid, msg_text, entities=msg_entities); group_success += 1
                    except Exception as e: logger.warning(f"Broadcast text fail group {gid}: {e}"); group_fail += 1
                    await asyncio.sleep(0.05)

        elif replied_msg.photo:
            photo_id = replied_msg.photo[-1].file_id; caption = replied_msg.caption; caption_entities = replied_msg.caption_entities
            if send_to_users:
                for uid in user_ids:
                    try: await context.bot.send_photo(int(uid), photo_id, caption=caption, caption_entities=caption_entities); user_success += 1
                    except Exception as e: logger.warning(f"Broadcast photo fail user {uid}: {e}"); user_fail += 1
                    await asyncio.sleep(0.05)
            if send_to_groups:
                 for gid in group_ids:
                    try: await context.bot.send_photo(gid, photo_id, caption=caption, caption_entities=caption_entities); group_success += 1
                    except Exception as e: logger.warning(f"Broadcast photo fail group {gid}: {e}"); group_fail += 1
                    await asyncio.sleep(0.05)
        # Add other message types (video, document etc.) if needed
        else: await update.message.reply_text("❌ Text/Photo သာ broadcast လုပ်နိုင်ပါသည်။"); return

        # --- Report ---
        report = f"✅ Broadcast Done!\n\n"
        if send_to_users: report += f"👥 Users: {user_success} sent, {user_fail} failed.\n"
        if send_to_groups: report += f"🏢 Groups: {group_success} sent, {group_fail} failed.\n"
        await update.message.reply_text(report)

    except PyMongoError as e: logger.error(f"DB Error during broadcast: {e}"); await update.message.reply_text("❌ DB error fetching targets.")
    except Exception as e: logger.error(f"General Error during broadcast: {e}", exc_info=True); await update.message.reply_text(f"❌ Broadcast error: {e}")


async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    args = context.args; start_date = end_date = period_text = None
    today = datetime.now()
    if not args: # Show buttons if no args
        yesterday = today - timedelta(days=1); week_ago = today - timedelta(days=7)
        keyboard = [ [InlineKeyboardButton("📅 ဒီနေ့", callback_data=f"report_day_{today.strftime('%Y-%m-%d')}")],
                     [InlineKeyboardButton("📅 မနေ့က", callback_data=f"report_day_{yesterday.strftime('%Y-%m-%d')}")],
                     [InlineKeyboardButton("📅 လွန်ခဲ့သော ၇ ရက်", callback_data=f"report_day_range_{week_ago.strftime('%Y-%m-%d')}_{today.strftime('%Y-%m-%d')}")] ]
        await update.message.reply_text("📊 ***ရက်စွဲ ရွေးပါ သို့မဟုတ် manual ရိုက်ပါ***\n`/d YYYY-MM-DD`\n`/d YYYY-MM-DD YYYY-MM-DD`", reply_markup=InlineKeyboardMarkup(keyboard)); return
    elif len(args) == 1 and "_" not in args[0]: # Manual single date
        try: start_date = end_date = datetime.strptime(args[0], '%Y-%m-%d').strftime('%Y-%m-%d'); period_text = f"ရက် ({start_date})"
        except ValueError: await update.message.reply_text("❌ Date format မှား (YYYY-MM-DD)။"); return
    elif len(args) == 2: # Manual date range
        try: start_date = datetime.strptime(args[0], '%Y-%m-%d').strftime('%Y-%m-%d'); end_date = datetime.strptime(args[1], '%Y-%m-%d').strftime('%Y-%m-%d'); period_text = f"ရက် ({start_date} မှ {end_date})"
        except ValueError: await update.message.reply_text("❌ Date format မှား (YYYY-MM-DD)။"); return
    elif len(args) == 1 and args[0].startswith("range_"): # From button callback
         parts = args[0].split('_'); start_date, end_date = parts[1], parts[2]; period_text = f"ရက် ({start_date} မှ {end_date})"
    elif len(args) == 1: # Single date from button callback
         start_date = end_date = args[0]; period_text = f"ရက် ({start_date})"
    else: await update.message.reply_text("❌ Format မှား။ /d"); return

    try: # DB Aggregation
        start_dt_iso = f"{start_date}T00:00:00.000Z" # Assuming UTC storage or adjust timezone
        end_dt_iso = f"{end_date}T23:59:59.999Z"
        sales_pipeline = [ {"$unwind": "$orders"}, {"$match": {"orders.status": STATUS_CONFIRMED, "orders.confirmed_at": {"$gte": start_dt_iso, "$lte": end_dt_iso}}},
                           {"$group": {"_id": None, "total_sales": {"$sum": "$orders.price"}, "total_orders": {"$sum": 1}}} ]
        topup_pipeline = [ {"$unwind": "$topups"}, {"$match": {"topups.status": STATUS_APPROVED, "topups.approved_at": {"$gte": start_dt_iso, "$lte": end_dt_iso}}},
                           {"$group": {"_id": None, "total_topups": {"$sum": "$topups.amount"}, "topup_count": {"$sum": 1}}} ]
        sales_result = list(users_col.aggregate(sales_pipeline)); topup_result = list(users_col.aggregate(topup_pipeline))
        total_sales = sales_result[0]["total_sales"] if sales_result else 0; total_orders = sales_result[0]["total_orders"] if sales_result else 0
        total_topups = topup_result[0]["total_topups"] if topup_result else 0; topup_count = topup_result[0]["topup_count"] if topup_result else 0

        msg = ( f"📊 ***Daily Report***\n📅 ကာလ: {period_text}\n\n🛒 Orders:\n💰 Sales: `{total_sales:,} MMK`\n📦 Count: {total_orders}\n\n"
                f"💳 Topups:\n💰 Amount: `{total_topups:,} MMK`\n📦 Count: {topup_count}" )
        if update.callback_query: await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error during daily report: {e}"); await update.effective_message.reply_text("❌ DB error generating report.")
    except Exception as e: logger.error(f"Error generating daily report: {e}", exc_info=True); await update.effective_message.reply_text(f"❌ Error: {e}")


async def monthly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    args = context.args; start_month = end_month = period_text = None
    today = datetime.now()
    if not args:
        this_month = today.strftime("%Y-%m"); last_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"); three_months_ago = (today.replace(day=1) - timedelta(days=90)).strftime("%Y-%m")
        keyboard = [ [InlineKeyboardButton("📅 ဒီလ", callback_data=f"report_month_{this_month}")], [InlineKeyboardButton("📅 ပြီးခဲ့သောလ", callback_data=f"report_month_{last_month}")],
                     [InlineKeyboardButton("📅 လွန်ခဲ့သော ၃ လ", callback_data=f"report_month_range_{three_months_ago}_{this_month}")] ]
        await update.message.reply_text("📊 ***လ ရွေးပါ သို့မဟုတ် manual ရိုက်ပါ***\n`/m YYYY-MM`\n`/m YYYY-MM YYYY-MM`", reply_markup=InlineKeyboardMarkup(keyboard)); return
    # ... (Similar logic as daily_report for parsing args/callback data for YYYY-MM) ...
    elif len(args) == 1 and "_" not in args[0]: # Manual single month YYYY-MM
        try: start_month = end_month = datetime.strptime(args[0], '%Y-%m').strftime('%Y-%m'); period_text = f"လ ({start_month})"
        except ValueError: await update.message.reply_text("❌ Format မှား (YYYY-MM)။"); return
    # ... (Add logic for range, callback single, callback range) ...
    else: await update.message.reply_text("❌ Format မှား။ /m"); return

    try: # DB Aggregation
        start_dt_obj = datetime.strptime(f"{start_month}-01", '%Y-%m-%d')
        end_year, end_mon = map(int, end_month.split('-'))
        # Calculate end of the end_month
        if end_mon == 12: end_dt_obj = datetime(end_year + 1, 1, 1) - timedelta(microseconds=1)
        else: end_dt_obj = datetime(end_year, end_mon + 1, 1) - timedelta(microseconds=1)

        start_dt_iso = start_dt_obj.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        end_dt_iso = end_dt_obj.strftime('%Y-%m-%dT%H:%M:%S.%fZ')

        sales_pipeline = [ {"$unwind": "$orders"}, {"$match": {"orders.status": STATUS_CONFIRMED, "orders.confirmed_at": {"$gte": start_dt_iso, "$lte": end_dt_iso}}},
                           {"$group": {"_id": None, "total_sales": {"$sum": "$orders.price"}, "total_orders": {"$sum": 1}}} ]
        topup_pipeline = [ {"$unwind": "$topups"}, {"$match": {"topups.status": STATUS_APPROVED, "topups.approved_at": {"$gte": start_dt_iso, "$lte": end_dt_iso}}},
                           {"$group": {"_id": None, "total_topups": {"$sum": "$topups.amount"}, "topup_count": {"$sum": 1}}} ]
        sales_result = list(users_col.aggregate(sales_pipeline)); topup_result = list(users_col.aggregate(topup_pipeline))
        # ... (Get totals from results) ...
        total_sales=0; total_orders=0; total_topups=0; topup_count=0; # Placeholder
        if sales_result: total_sales = sales_result[0].get("total_sales", 0); total_orders = sales_result[0].get("total_orders", 0)
        if topup_result: total_topups = topup_result[0].get("total_topups", 0); topup_count = topup_result[0].get("topup_count", 0)

        msg = ( f"📊 ***Monthly Report***\n📅 ကာလ: {period_text}\n\n🛒 Orders:\n💰 Sales: `{total_sales:,} MMK`\n📦 Count: {total_orders}\n\n"
                f"💳 Topups:\n💰 Amount: `{total_topups:,} MMK`\n📦 Count: {topup_count}" )
        if update.callback_query: await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error during monthly report: {e}"); await update.effective_message.reply_text("❌ DB error.")
    except Exception as e: logger.error(f"Error generating monthly report: {e}", exc_info=True); await update.effective_message.reply_text(f"❌ Error: {e}")


async def yearly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    args = context.args; start_year = end_year = period_text = None
    today = datetime.now()
    if not args:
        this_year = today.strftime("%Y"); last_year = str(int(this_year) - 1)
        keyboard = [ [InlineKeyboardButton("📅 ဒီနှစ်", callback_data=f"report_year_{this_year}")], [InlineKeyboardButton("📅 မနှစ်က", callback_data=f"report_year_{last_year}")],
                     [InlineKeyboardButton("📅 ၂ နှစ်စလုံး", callback_data=f"report_year_range_{last_year}_{this_year}")] ]
        await update.message.reply_text("📊 ***နှစ် ရွေးပါ သို့မဟုတ် manual ရိုက်ပါ***\n`/y YYYY`\n`/y YYYY YYYY`", reply_markup=InlineKeyboardMarkup(keyboard)); return
    # ... (Similar logic as daily_report for parsing args/callback data for YYYY) ...
    elif len(args) == 1 and "_" not in args[0] and args[0].isdigit() and len(args[0])==4: # Manual single year
        start_year = end_year = args[0]; period_text = f"နှစ် ({start_year})"
    # ... (Add logic for range, callback single, callback range) ...
    else: await update.message.reply_text("❌ Format မှား။ /y"); return

    try: # DB Aggregation
        start_dt_iso = f"{start_year}-01-01T00:00:00.000Z"
        end_dt_iso = f"{end_year}-12-31T23:59:59.999Z"
        sales_pipeline = [ {"$unwind": "$orders"}, {"$match": {"orders.status": STATUS_CONFIRMED, "orders.confirmed_at": {"$gte": start_dt_iso, "$lte": end_dt_iso}}},
                           {"$group": {"_id": None, "total_sales": {"$sum": "$orders.price"}, "total_orders": {"$sum": 1}}} ]
        topup_pipeline = [ {"$unwind": "$topups"}, {"$match": {"topups.status": STATUS_APPROVED, "topups.approved_at": {"$gte": start_dt_iso, "$lte": end_dt_iso}}},
                           {"$group": {"_id": None, "total_topups": {"$sum": "$topups.amount"}, "topup_count": {"$sum": 1}}} ]
        sales_result = list(users_col.aggregate(sales_pipeline)); topup_result = list(users_col.aggregate(topup_pipeline))
        # ... (Get totals from results) ...
        total_sales=0; total_orders=0; total_topups=0; topup_count=0; # Placeholder
        if sales_result: total_sales = sales_result[0].get("total_sales", 0); total_orders = sales_result[0].get("total_orders", 0)
        if topup_result: total_topups = topup_result[0].get("total_topups", 0); topup_count = topup_result[0].get("topup_count", 0)

        msg = ( f"📊 ***Yearly Report***\n📅 ကာလ: {period_text}\n\n🛒 Orders:\n💰 Sales: `{total_sales:,} MMK`\n📦 Count: {total_orders}\n\n"
                f"💳 Topups:\n💰 Amount: `{total_topups:,} MMK`\n📦 Count: {topup_count}" )
        if update.callback_query: await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error during yearly report: {e}"); await update.effective_message.reply_text("❌ DB error.")
    except Exception as e: logger.error(f"Error generating yearly report: {e}", exc_info=True); await update.effective_message.reply_text(f"❌ Error: {e}")


# --- Message Handlers ---
# (handle_photo, handle_other_messages - Copied from previous full answer)

# --- Callback Query Handler ---
# (button_callback - Copied from previous full answer with SyntaxError fix)

# --- Post Init Function ---
async def post_init(application: Application):
    logger.info("🚀 Main bot application initialized.")


# --- Main Function ---
def main():
    if not BOT_TOKEN: logger.critical("❌ BOT_TOKEN environment variable is missing!"); return

    application = ( Application.builder().token(BOT_TOKEN).post_init(post_init).build() )

    # --- Register Handlers ---
    # Middleware (Group 0) - Runs FIRST
    # **Important:** This setup runs check_restriction for ALL updates.
    # We rely on the logic inside check_restriction to allow admin actions.
    
    application.add_handler(MessageHandler(filters.ALL, check_restriction), group=0)
    application.add_handler(CallbackQueryHandler(check_restriction), group=0)

    # User Commands & Handlers (Group 1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("register", register_command))
    application.add_handler(CommandHandler("c", c_command))
    application.add_handler(CommandHandler("mmb", mmb_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("topup", topup_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("history", history_command))

    # Admin Commands (Group 1)
    admin_commands_list = [
        "approve", "deduct", "done", "reply", "ban", "unban", "sendgroup", "maintenance",
        "testgroup", "setprice", "removeprice", "setwavenum", "setkpaynum", "setwavename",
        "setkpayname", "adminhelp", "d", "m", "y", "addadm", "unadm", "setkpayqr",
        "removekpayqr", "setwaveqr", "removewaveqr", "broadcast"
        # Removed clone commands
    ]
    for cmd in admin_commands_list:
        # Check authorization inside the handler function itself
        application.add_handler(CommandHandler(cmd, globals()[f"{cmd}_command"]))

    # Callback Query Handler (Group 1)
    application.add_handler(CallbackQueryHandler(button_callback))

    # Message Handlers (Group 1)
    # Check authorization inside the handler
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.PHOTO, handle_other_messages))

    # --- Start Bot ---
    logger.info("🤖 Bot starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("⚫ Bot stopped.")

if __name__ == "__main__":
    main()
