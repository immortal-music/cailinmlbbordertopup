import json
import os
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ApplicationHandlerStop
)
from telegram.constants import ParseMode # Import ParseMode if using it explicitly elsewhere
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
    # Removed: clone_bots_col = db["clone_bots"]
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
        users_col.create_index([("orders.confirmed_at", 1)], background=True)
        users_col.create_index([("topups.approved_at", 1)], background=True)
        users_col.create_index([("restriction_status", 1)], background=True)

        auth_users_col.create_index([("_id", 1)], background=True) # Removed unique=True
        admins_col.create_index([("_id", 1)], background=True)    # Removed unique=True
        prices_col.create_index([("_id", 1)], background=True)    # Removed unique=True
        # Removed: clone_bots_col index
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
# Removed: clone_bot_apps = {}


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
    if not chat_id: logger.warning("is_bot_admin_in_group called with invalid chat_id."); return False
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
            '/d', '/m', '/y' # Reports are owner only anyway
            # Removed clone commands
        ]
        admin_callback_prefixes = ['topup_approve_', 'topup_reject_', 'order_confirm_', 'order_cancel_', 'register_approve_', 'register_reject_', 'report_'] # Removed clone callbacks

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
        raise ApplicationHandlerStop # Stop further processing

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

    # Middleware handles restriction. Check pending topup status.
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    try: # Upsert user info
        users_col.find_one_and_update(
            {"_id": user_id},
            {"$setOnInsert": {"balance": 0, "orders": [], "topups": [], "restriction_status": RESTRICTION_NONE}},
            {"$set": {"name": name, "username": username}}, upsert=True,
        )
    except PyMongoError as e:
        logger.error(f"DB Error during user upsert in /start for {user_id}: {e}")
        await update.message.reply_text("❌ Database error occurred. Please try again later.")
        return

    if user_id in pending_topups: del pending_topups[user_id] # Clear incomplete topup process

    clickable_name = f"[{name}](tg://user?id={user_id})"
    msg = ( f"👋 ***မင်္ဂလာပါ*** {clickable_name}!\n🆔 ***Telegram User ID:*** `{user_id}`\n\n"
            "💎 ***JB MLBB AUTO TOP UP BOT*** မှ ကြိုဆိုပါတယ်။\n\n***အသုံးပြုနိုင်တဲ့ command များ***:\n"
            "➤ /mmb gameid serverid amount\n➤ /balance - ဘယ်လောက်လက်ကျန်ရှိလဲ စစ်မယ်\n"
            "➤ /topup amount - ငွေဖြည့်မယ် (screenshot တင်ပါ)\n➤ /price - Diamond များရဲ့ ဈေးနှုန်းများ\n"
            "➤ /history - အော်ဒါမှတ်တမ်းကြည့်မယ်\n\n***📌 ဥပမာ***:\n`/mmb 123456789 12345 wp1`\n"
            "`/mmb 123456789 12345 86`\n\n***လိုအပ်တာရှိရင် Owner ကို ဆက်သွယ်နိုင်ပါတယ်။***" )
    try: # Send with profile photo
        user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        if user_photos.total_count > 0:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=user_photos.photos[0][0].file_id, caption=msg, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def mmb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Auth checked by filter, Restriction by middleware
    if not get_maintenance_status("orders"): await send_maintenance_message(update, "orders"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups:
        await update.message.reply_text("⏳ ***Topup လုပ်ငန်းစဉ် အရင်ပြီးဆုံးပါ! Screenshot တင်ပါ သို့မဟုတ် /cancel နှိပ်ပါ။***", parse_mode=ParseMode.MARKDOWN); return

    args = context.args
    if len(args) != 3:
        await update.message.reply_text("❌ Format မှား:\n`/mmb gameid serverid amount`\nဥပမာ:\n`/mmb 123 456 86`", parse_mode=ParseMode.MARKDOWN); return
    game_id, server_id, amount = args

    if not validate_game_id(game_id): await update.message.reply_text("❌ Game ID မှား (6-10 digits)။", parse_mode=ParseMode.MARKDOWN); return
    if not validate_server_id(server_id): await update.message.reply_text("❌ Server ID မှား (3-5 digits)။", parse_mode=ParseMode.MARKDOWN); return
    if is_banned_account(game_id):
        await update.message.reply_text(f"🚫 Account Ban ဖြစ်နေ:\n🎮 ID: `{game_id}`\n🌐 Server: `{server_id}`\n❌ Topup မရပါ။", parse_mode=ParseMode.MARKDOWN)
        # Notify admin about banned attempt (optional)
        admin_list = get_all_admin_ids()
        for admin_id in admin_list:
            try: await context.bot.send_message(admin_id, f"🚫 Banned Account Topup Attempt:\nUser: {update.effective_user.mention_markdown()} (`{user_id}`)\nGameID: `{game_id}`\nServer: `{server_id}`\nAmount: {amount}", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending banned acc notif to {admin_id}: {e}")
        return

    price = get_price(amount)
    if not price:
        await update.message.reply_text(f"❌ Diamond amount `{amount}` မရနိုင်ပါ။ /price နှိပ်ကြည့်ပါ။", parse_mode=ParseMode.MARKDOWN); return

    try:
        user_data = users_col.find_one({"_id": user_id}, {"balance": 1})
        user_balance = user_data.get("balance", 0) if user_data else 0
    except PyMongoError as e: logger.error(f"DB Error getting balance for {user_id} in /mmb: {e}"); await update.message.reply_text("❌ Database error occurred."); return

    if user_balance < price:
        keyboard = [[InlineKeyboardButton("💳 ငွေဖြည့်မယ်", callback_data="topup_button")]]
        await update.message.reply_text(f"❌ လက်ကျန်ငွေ မလုံလောက်ပါ!\n💰 လိုအပ်: {price:,} MMK\n💳 လက်ကျန်: {user_balance:,} MMK\n❗ လိုသေး: {price - user_balance:,} MMK", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)); return

    order_id = f"ORD{datetime.now().strftime('%y%m%d%H%M%S%f')[:-3]}{user_id[-2:]}" # Even more unique
    order = { "order_id": order_id, "game_id": game_id, "server_id": server_id, "amount": amount, "price": price,
              "status": STATUS_PENDING, "timestamp": datetime.now().isoformat(), "user_id": user_id, "chat_id": update.effective_chat.id }
    try:
        result = users_col.update_one({"_id": user_id}, {"$inc": {"balance": -price}, "$push": {"orders": order}})
        if not result.modified_count: logger.warning(f"Order update failed for user {user_id}."); await update.message.reply_text("❌ Order processing error. Try again."); return
        updated_user_data = users_col.find_one({"_id": user_id}, {"balance": 1})
        new_balance = updated_user_data.get("balance", user_balance - price)
    except PyMongoError as e: logger.error(f"DB Error processing order for {user_id}: {e}"); await update.message.reply_text("❌ DB error during order. Contact admin."); return

    keyboard = [[InlineKeyboardButton("✅ Confirm", callback_data=f"order_confirm_{order_id}"), InlineKeyboardButton("❌ Cancel", callback_data=f"order_cancel_{order_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    user_name = update.effective_user.mention_markdown() # Use mention

    admin_msg = (f"🔔 ***အော်ဒါအသစ်!***\n📝 ID: `{order_id}`\n👤 User: {user_name} (`{user_id}`)\n🎮 Game ID: `{game_id}`\n"
                 f"🌐 Server ID: `{server_id}`\n💎 Amount: {amount}\n💰 Price: {price:,} MMK\n⏰ Time: {datetime.now():%Y-%m-%d %H:%M:%S}\n📊 Status: ⏳ {STATUS_PENDING}")
    admin_list = get_all_admin_ids()
    for admin_id in admin_list:
        try: await context.bot.send_message(admin_id, admin_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except Exception as e: logger.warning(f"Failed sending order notif to admin {admin_id}: {e}")

    if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
        try:
            group_msg = (f"🛒 ***အော်ဒါအသစ်!***\n📝 ID: `{order_id}`\n👤 User: {user_name}\n🎮 Game ID: `{game_id}`\n"
                         f"🌐 Server ID: `{server_id}`\n💎 Amount: {amount}\n💰 Price: {price:,} MMK\n📊 Status: ⏳ {STATUS_PENDING}\n#NewOrder")
            await context.bot.send_message(ADMIN_GROUP_ID, group_msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed sending order notif to group {ADMIN_GROUP_ID}: {e}")

    await update.message.reply_text(
        f"✅ ***အော်ဒါ အောင်မြင်ပါပြီ!***\n📝 ID: `{order_id}`\n🎮 Game ID: `{game_id}`\n🌐 Server ID: `{server_id}`\n"
        f"💎 Diamond: {amount}\n💰 ကုန်ကျစရိတ်: {price:,} MMK\n💳 လက်ကျန်ငွေ: {new_balance:,} MMK\n📊 Status: ⏳ {STATUS_PENDING}\n\n"
        f"⚠️ Admin confirm လုပ်မှ diamonds ရပါမည်။", parse_mode=ParseMode.MARKDOWN)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Auth checked by filter, Restriction by middleware
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ Topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return

    try:
        user_data = users_col.find_one({"_id": user_id})
        if not user_data: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return

        balance = user_data.get("balance", 0)
        total_orders = len(user_data.get("orders", [])) # Maybe count in DB for large arrays?
        total_topups = len(user_data.get("topups", []))

        pending_topups_list = [t for t in user_data.get("topups", []) if t.get("status") == STATUS_PENDING]
        pending_topups_count = len(pending_topups_list)
        pending_amount = sum(t.get("amount", 0) for t in pending_topups_list)

        name = user_data.get('name', 'Unknown').replace('*','').replace('_','').replace('`','') # Basic sanitize
        username = user_data.get('username', 'None').replace('*','').replace('_','').replace('`','')

        status_msg = f"\n⏳ ***Pending Topups***: {pending_topups_count} ခု ({pending_amount:,} MMK)\n❗ ***Admin approve စောင့်ပါ။***" if pending_topups_count > 0 else ""
        keyboard = [[InlineKeyboardButton("💳 ငွေဖြည့်မယ်", callback_data="topup_button")]]
        balance_text = (f"💳 ***သင့် Account***\n\n💰 ***Balance***: `{balance:,} MMK`\n📦 Orders: {total_orders}\n"
                        f"💸 Topups: {total_topups}{status_msg}\n\n👤 Name: {name}\n🆔 Username: @{username}")

        try: # Send with profile photo
            user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
            if user_photos.total_count > 0:
                await context.bot.send_photo(update.effective_chat.id, user_photos.photos[0][0].file_id, caption=balance_text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            else: await update.message.reply_text(balance_text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception: await update.message.reply_text(balance_text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

    except PyMongoError as e: logger.error(f"DB Error getting balance for {user_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Auth checked by filter, Restriction by middleware
    if not get_maintenance_status("topups"): await send_maintenance_message(update, "topups"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ယခင် topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("❌ Format မှား:\n`/topup <amount>`\nဥပမာ: `/topup 5000`", parse_mode=ParseMode.MARKDOWN); return
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
    # Auth checked by filter, Restriction by middleware
    if user_id in pending_topups: await update.message.reply_text("⏳ Topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return

    custom_prices = load_prices()
    default_prices = { # Consider moving defaults to DB?
        "wp1": 6000, "wp2": 12000, "wp3": 18000, "wp4": 24000, "wp5": 30000, "wp6": 36000, "wp7": 42000, "wp8": 48000, "wp9": 54000, "wp10": 60000,
        "11": 950, "22": 1900, "33": 2850, "56": 4200, "86": 5100, "112": 8200, "172": 10200, "257": 15300, "343": 20400, "429": 25500, "514": 30600,
        "600": 35700, "706": 40800, "878": 51000, "963": 56100, "1049": 61200, "1135": 66300, "1412": 81600, "2195": 122400, "3688": 204000,
        "5532": 306000, "9288": 510000, "12976": 714000, "55": 3500, "165": 10000, "275": 16000, "565": 33000 }
    current_prices = {**default_prices, **custom_prices} # Custom overrides default

    price_msg = "💎 ***MLBB Diamond ဈေးနှုန်းများ***\n\n🎟️ ***Weekly Pass***:\n"
    for i in range(1, 11): price_msg += f"• wp{i} = {current_prices.get(f'wp{i}', 'N/A'):,} MMK\n"
    price_msg += "\n💎 ***Regular Diamonds***:\n"
    reg_dm = ["11", "22", "33", "56", "86", "112", "172", "257", "343", "429", "514", "600", "706", "878", "963", "1049", "1135", "1412", "2195", "3688", "5532", "9288", "12976"]
    for dm in reg_dm: price_msg += f"• {dm} = {current_prices.get(dm, 'N/A'):,} MMK\n"
    price_msg += "\n💎 ***2X Diamond Pass***:\n" # Assuming these are 2X pass amounts
    dbl_dm = ["55", "165", "275", "565"]
    for dm in dbl_dm: price_msg += f"• {dm} = {current_prices.get(dm, 'N/A'):,} MMK\n"

    other_customs = {k: v for k, v in custom_prices.items() if k not in default_prices}
    if other_customs:
        price_msg += "\n🔥 ***Special Items***:\n"
        for item, price in other_customs.items(): price_msg += f"• {item} = {price:,} MMK\n"

    price_msg += "\n\n***📝 အသုံးပြုရန်***:\n`/mmb gameid serverid amount`\nဥပမာ: `/mmb 123 456 86`"
    await update.message.reply_text(price_msg, parse_mode=ParseMode.MARKDOWN)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Auth checked by filter, Restriction by middleware
    if user_id in pending_topups:
        del pending_topups[user_id]
        await update.message.reply_text("✅ ***ငွေဖြည့်ခြင်း ပယ်ဖျက်ပါပြီ!*** /topup နှိပ်ပြီး ပြန်စနိုင်ပါသည်။", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("ℹ️ လက်ရှိ ငွေဖြည့်မှု လုပ်ငန်းစဉ် မရှိပါ။", parse_mode=ParseMode.MARKDOWN)


async def c_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No auth needed?, Restriction by middleware
    if not context.args: await update.message.reply_text("🧮 Calculator: `/c <expression>`\nဥပမာ: `/c 100*5+3`", parse_mode=ParseMode.MARKDOWN); return
    expression = ''.join(context.args).strip()
    # Basic validation to prevent arbitrary code execution
    allowed_chars = set("0123456789+-*/(). ")
    if not all(char in allowed_chars for char in expression):
        await update.message.reply_text("❌ Invalid characters. Use numbers, +, -, *, /, (, )."); return
    try:
        # Use a safer evaluation method if possible, or keep basic validation tight
        result = eval(expression.replace(' ', '')) # eval can be risky, ensure validation is strong
        await update.message.reply_text(f"🧮 Result:\n`{expression}` = ***{result:,}***", parse_mode=ParseMode.MARKDOWN)
    except ZeroDivisionError: await update.message.reply_text("❌ သုညဖြင့် စားလို့မရပါ။")
    except Exception as e: logger.warning(f"Calculator error for '{expression}': {e}"); await update.message.reply_text("❌ Expression မှားနေပါသည်။")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Auth checked by filter, Restriction by middleware
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ Topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return

    try:
        # Fetch only last 5 orders and topups using projection and slice
        user_data = users_col.find_one(
            {"_id": user_id},
            {"orders": {"$slice": -5}, "topups": {"$slice": -5}}
        )
        if not user_data: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return

        orders = user_data.get("orders", [])
        topups = user_data.get("topups", [])

        if not orders and not topups: await update.message.reply_text("📋 မှတ်တမ်း မရှိသေးပါ။"); return

        msg = "📋 ***သင်၏ မှတ်တမ်းများ***\n\n"
        if orders:
            msg += "🛒 Orders (နောက်ဆုံး ၅ ခု):\n"
            status_map = {STATUS_PENDING: "⏳", STATUS_CONFIRMED: "✅", STATUS_CANCELLED: "❌"}
            for order in reversed(orders): # Show newest first
                status = order.get("status", STATUS_PENDING)
                ts = datetime.fromisoformat(order.get("timestamp","")).strftime('%y-%m-%d %H:%M') if order.get("timestamp") else "N/A"
                msg += f"{status_map.get(status, '❓')} `{order.get('order_id', 'N/A')}` ({order.get('amount', '?')}💎/{order.get('price', 0):,}K) [{ts}]\n"
        if topups:
             msg += "\n💳 Topups (နောက်ဆုံး ၅ ခု):\n"
             status_map = {STATUS_PENDING: "⏳", STATUS_APPROVED: "✅", STATUS_REJECTED: "❌"}
             for topup in reversed(topups): # Show newest first
                 status = topup.get("status", STATUS_PENDING)
                 ts = datetime.fromisoformat(topup.get("timestamp","")).strftime('%y-%m-%d %H:%M') if topup.get("timestamp") else "N/A"
                 msg += f"{status_map.get(status, '❓')} {topup.get('amount', 0):,} MMK [{ts}]\n"

        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error getting history for {user_id}: {e}"); await update.message.reply_text("❌ Database error.")
    except Exception as e: logger.error(f"Error formatting history for {user_id}: {e}"); await update.message.reply_text("❌ Error displaying history.")


# --- Admin Commands ---

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user = update.effective_user
    admin_id = str(admin_user.id)
    admin_name = admin_user.first_name
    # Admin check done by filter

    if len(context.args) != 2: await update.message.reply_text("❌ Format: `/approve <user_id> <amount>`"); return
    target_user_id, amount_str = context.args
    try: amount = int(amount_str)
    except ValueError: await update.message.reply_text("❌ Amount ကို ဂဏန်းဖြင့် ထည့်ပါ။"); return

    try:
        # Find user and the specific pending topup
        user_doc = users_col.find_one(
            {"_id": target_user_id, "topups": {"$elemMatch": {"amount": amount, "status": STATUS_PENDING}}},
            {"_id": 1, "balance": 1, "topups.$": 1} # Get matching topup and balance
        )

        if not user_doc or not user_doc.get("topups"):
            await update.message.reply_text(f"❌ `{target_user_id}` ထံမှ `{amount}` MMK pending topup မတွေ့ပါ။"); return

        matched_topup = user_doc["topups"][0]
        topup_id_to_approve = matched_topup.get("topup_id", None) # Get the ID if it exists

        # Atomically update
        update_result = users_col.update_one(
            {"_id": target_user_id, "topups.topup_id": topup_id_to_approve, "topups.status": STATUS_PENDING}, # Ensure it's still pending by ID
            {
                "$set": {
                    "topups.$.status": STATUS_APPROVED,
                    "topups.$.approved_by": admin_name,
                    "topups.$.approved_at": datetime.now().isoformat(),
                    "restriction_status": RESTRICTION_NONE
                },
                "$inc": {"balance": amount}
            }
        )

        if update_result.matched_count == 0: # Check if update actually happened (race condition)
             await update.message.reply_text("⚠️ Topup ကို အခြား Admin တစ်ဦးမှ approve/reject လုပ်ပြီး ဖြစ်နိုင်ပါသည်။")
             return

        new_balance = user_doc.get("balance", 0) + amount

        # Notify user
        try:
            keyboard = [[InlineKeyboardButton("💎 Order တင်မယ်", url=f"https://t.me/{context.bot.username}?start=order")]]
            await context.bot.send_message(
                chat_id=int(target_user_id),
                text=(f"✅ ***ငွေဖြည့်မှု အတည်ပြုပါပြီ!*** 🎉\n\n💰 ပမာဏ: `{amount:,} MMK`\n💳 လက်ကျန်ငွေ: `{new_balance:,} MMK`\n"
                      f"👤 Approved by: {admin_name}\n⏰ အချိန်: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
                      f"🎉 ယခု diamonds ဝယ်ယူနိုင်ပါပြီ!\n🔓 Bot functions များ ပြန်သုံးနိုင်ပါပြီ!\n\n💎 Order တင်ရန်:\n`/mmb gameid serverid amount`"),
                parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e: logger.warning(f"Failed to notify user {target_user_id} of approval: {e}")

        # Confirm to admin
        await update.message.reply_text(f"✅ Approve အောင်မြင်!\n👤 User ID: `{target_user_id}`\n💰 Amount: `{amount:,} MMK`\n💳 New balance: `{new_balance:,} MMK`", parse_mode=ParseMode.MARKDOWN)

        # Notify other admins (Optional) / Group
        # ... (similar notification logic as in button_callback) ...

    except PyMongoError as e: logger.error(f"DB Error during approve for {target_user_id} by {admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = str(update.effective_user.id)
    # Admin check done by filter
    if len(context.args) != 2: await update.message.reply_text("❌ Format: `/deduct <user_id> <amount>`"); return
    target_user_id, amount_str = context.args
    try: amount = int(amount_str); assert amount > 0
    except (ValueError, AssertionError): await update.message.reply_text("❌ Amount မှားနေ (must be positive number)။"); return

    try:
        result = users_col.find_one_and_update(
            {"_id": target_user_id, "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount}},
            projection={"balance": 1}, return_document=ReturnDocument.AFTER
        )
        if result is None:
            user_exists = users_col.find_one({"_id": target_user_id}, {"balance": 1})
            if user_exists: await update.message.reply_text(f"❌ Balance မလုံလောက်! လက်ကျန်: {user_exists.get('balance', 0):,} MMK");
            else: await update.message.reply_text("❌ User မတွေ့ရှိပါ!")
            return
        new_balance = result.get("balance")
        # Notify user
        try:
            await context.bot.send_message(int(target_user_id),
                f"⚠️ ***လက်ကျန်ငွေ နှုတ်ခံရမှု***\n💰 ပမာဏ: `{amount:,} MMK`\n💳 လက်ကျန်: `{new_balance:,} MMK`\n⏰ အချိန်: {datetime.now():%Y-%m-%d %H:%M:%S}\n📞 Admin ကို ဆက်သွယ်ပါ။",
                parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed to notify user {target_user_id} of deduction: {e}")
        # Confirm to admin
        await update.message.reply_text(f"✅ Balance နှုတ်ပြီး!\n👤 User ID: `{target_user_id}`\n💰 နှုတ် Amount: `{amount:,} MMK`\n💳 လက်ကျန်: `{new_balance:,} MMK`", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error during deduct for {target_user_id} by {admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/done <user_id>`"); return
    target_user_id = int(context.args[0])
    try:
        await context.bot.send_message(target_user_id, "🙏 ဝယ်ယူအားပေးမှုအတွက် ကျေးဇူးတင်ပါတယ်။\n✅ Order Done! 🎉")
        await update.message.reply_text("✅ User ထံ message ပို့ပြီး။")
    except Exception as e: logger.warning(f"Failed to send /done msg to {target_user_id}: {e}"); await update.message.reply_text("❌ User ID မှားနေ သို့မဟုတ် Bot blocked ဖြစ်နေ။")


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
    if len(context.args) < 2 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/reply <user_id> <message>`"); return
    target_user_id, message = int(context.args[0]), " ".join(context.args[1:])
    try:
        await context.bot.send_message(target_user_id, f"✉️ ***Admin Reply:***\n\n{message}", parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("✅ Message ပို့ပြီး။")
    except Exception as e: logger.warning(f"Failed to send /reply msg to {target_user_id}: {e}"); await update.message.reply_text("❌ Message မပို့နိုင်ပါ။")


async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    if is_user_authorized(user_id): await update.message.reply_text("✅ သင် အသုံးပြုခွင့် ရပြီးသားပါ။ /start နှိပ်ပါ။"); return

    username = user.username or "-"
    name = f"{user.first_name} {user.last_name or ''}".strip()
    keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"register_approve_{user_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"register_reject_{user_id}")]]
    owner_msg = (f"📝 ***Registration Request***\n👤 Name: {user.mention_markdown()}\n🆔 ID: `{user_id}`\n📱 User: @{username}\n⏰ Time: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n Approve?")
    user_confirm_msg = (f"✅ ***Register တောင်းဆိုမှု ပို့ပြီး!***\n👤 Name: {name}\n🆔 ID: `{user_id}`\n⏳ Owner approve လုပ်သည်ထိ စောင့်ပါ။")

    # Send request to Owner (ADMIN_ID)
    try:
        user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        if user_photos.total_count > 0:
             await context.bot.send_photo(ADMIN_ID, user_photos.photos[0][0].file_id, caption=owner_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        else: await context.bot.send_message(ADMIN_ID, owner_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e: logger.error(f"Failed sending register req to owner {ADMIN_ID}: {e}")

    # Send confirmation to user
    try: await update.message.reply_text(user_confirm_msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.warning(f"Failed sending register confirm to {user_id}: {e}")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user = update.effective_user
    admin_id = str(admin_user.id)
    admin_name = admin_user.first_name
    # Admin check done by filter
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/ban <user_id>`"); return
    target_user_id = context.args[0]
    if int(target_user_id) == ADMIN_ID: await update.message.reply_text("❌ Owner ကို ban မရပါ။"); return

    try:
        result_auth = auth_users_col.delete_one({"_id": target_user_id})
        if result_auth.deleted_count == 0: await update.message.reply_text("ℹ️ User သည် authorize မလုပ်ထားပါ သို့မဟုတ် ban ပြီးသား။"); return
        set_user_restriction_status(target_user_id, RESTRICTION_NONE) # Clear restriction just in case

        user_doc = users_col.find_one({"_id": target_user_id}, {"name": 1})
        target_name = user_doc.get("name", "Unknown") if user_doc else "Unknown"

        # Notify user
        try: await context.bot.send_message(int(target_user_id), "🚫 Bot အသုံးပြုခွင့် ပိတ်ပင်ခံရမှု\nAdmin က သင့်ကို ban လုပ်လိုက်ပါပြီ။ Admin ကို ဆက်သွယ်ပါ။", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed sending ban notif to {target_user_id}: {e}")

        # Notify owner (if not the one banning)
        if int(admin_id) != ADMIN_ID:
            try: await context.bot.send_message(ADMIN_ID, f"🚫 User Ban by Admin:\nBanned: [{target_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\nBy: {admin_user.mention_markdown()}", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending ban notif to owner: {e}")

        # Notify group
        if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
            try: await context.bot.send_message(ADMIN_GROUP_ID, f"🚫 User Banned:\nUser: [{target_name}](tg://user?id={target_user_id})\nBy: {admin_name}\n#UserBanned", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending ban notif to group: {e}")

        await update.message.reply_text(f"✅ User Ban အောင်မြင်!\n👤 ID: `{target_user_id}`\n📊 Total authorized: {get_authorized_user_count()}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error during ban for {target_user_id} by {admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user = update.effective_user
    admin_id = str(admin_user.id)
    admin_name = admin_user.first_name
    # Admin check done by filter
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/unban <user_id>`"); return
    target_user_id = context.args[0]

    if is_user_authorized(target_user_id): await update.message.reply_text("ℹ️ User သည် authorize လုပ်ထားပြီးသား။"); return

    try:
        auth_users_col.update_one({"_id": target_user_id}, {"$set": {"authorized_at": datetime.now(), "unbanned_by": admin_id}}, upsert=True)
        set_user_restriction_status(target_user_id, RESTRICTION_NONE) # Ensure restriction removed

        user_doc = users_col.find_one({"_id": target_user_id}, {"name": 1})
        target_name = user_doc.get("name", "Unknown") if user_doc else "Unknown"

        # Notify user
        try: await context.bot.send_message(int(target_user_id), "🎉 *Bot အသုံးပြုခွင့် ပြန်ရပါပြီ!*\nAdmin က ban ဖြုတ်ပေးပါပြီ။ /start နှိပ်ပြီး ပြန်သုံးနိုင်ပါပြီ။", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed sending unban notif to {target_user_id}: {e}")

        # Notify owner (if not the one unbanning)
        if int(admin_id) != ADMIN_ID:
             try: await context.bot.send_message(ADMIN_ID, f"✅ User Unban by Admin:\nUnbanned: [{target_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\nBy: {admin_user.mention_markdown()}", parse_mode=ParseMode.MARKDOWN)
             except Exception as e: logger.warning(f"Failed sending unban notif to owner: {e}")

        # Notify group
        if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
            try: await context.bot.send_message(ADMIN_GROUP_ID, f"✅ User Unbanned:\nUser: [{target_name}](tg://user?id={target_user_id})\nBy: {admin_name}\n#UserUnbanned", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending unban notif to group: {e}")

        await update.message.reply_text(f"✅ User Unban အောင်မြင်!\n👤 ID: `{target_user_id}`\n📊 Total authorized: {get_authorized_user_count()}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error during unban for {target_user_id} by {admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def addadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner check done by filter
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/addadm <user_id>`"); return
    new_admin_id = int(context.args[0])
    if is_admin(new_admin_id): await update.message.reply_text("ℹ️ User သည် admin ဖြစ်ပြီးသား။"); return

    try:
        admins_col.update_one({"_id": new_admin_id}, {"$set": {"is_owner": False, "added_by": ADMIN_ID, "added_at": datetime.now()}}, upsert=True)
        # Notify new admin
        try: await context.bot.send_message(new_admin_id, "🎉 Admin ရာထူးရရှိမှု\nOwner က သင့်ကို Admin ခန့်အပ်ပါပြီ။ /adminhelp နှိပ်ကြည့်ပါ။")
        except Exception as e: logger.warning(f"Failed sending addadm notif to {new_admin_id}: {e}")
        # Owner confirmation
        await update.message.reply_text(f"✅ Admin ထပ်ထည့်ပြီး!\n👤 ID: `{new_admin_id}`\n📊 Total admins: {admins_col.count_documents({})}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error adding admin {new_admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def unadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner check done by filter
    if len(context.args) != 1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/unadm <user_id>`"); return
    target_admin_id = int(context.args[0])
    if target_admin_id == ADMIN_ID: await update.message.reply_text("❌ Owner ကို ဖြုတ်မရပါ။"); return

    try:
        result = admins_col.delete_one({"_id": target_admin_id})
        if result.deleted_count == 0: await update.message.reply_text("ℹ️ User သည် admin မဟုတ်ပါ။"); return
        # Notify removed admin
        try: await context.bot.send_message(target_admin_id, "⚠️ Admin ရာထူး ရုပ်သိမ်းခံရမှု\nOwner က သင့် admin ရာထူးကို ဖြုတ်လိုက်ပါပြီ။")
        except Exception as e: logger.warning(f"Failed sending unadm notif to {target_admin_id}: {e}")
        # Owner confirmation
        await update.message.reply_text(f"✅ Admin ဖြုတ်ပြီး!\n👤 ID: `{target_admin_id}`\n📊 Total admins: {admins_col.count_documents({})}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error removing admin {target_admin_id}: {e}"); await update.message.reply_text("❌ Database error.")


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
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
    # Admin check done by filter
    if len(context.args) != 2: await update.message.reply_text("❌ Format: `/setprice <item> <price>`"); return
    item, price_str = context.args[0], context.args[1]
    try: price = int(price_str); assert price >= 0
    except (ValueError, AssertionError): await update.message.reply_text("❌ Price မှားနေ (must be positive number)။"); return
    try:
        prices_col.update_one({"_id": item}, {"$set": {"price": price}}, upsert=True)
        await update.message.reply_text(f"✅ Price Updated!\n💎 Item: `{item}`\n💰 New Price: `{price:,} MMK`", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error setting price for {item}: {e}"); await update.message.reply_text("❌ Database error.")


async def removeprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
    if len(context.args) != 1: await update.message.reply_text("❌ Format: `/removeprice <item>`"); return
    item = context.args[0]
    try:
        result = prices_col.delete_one({"_id": item})
        if result.deleted_count == 0: await update.message.reply_text(f"❌ `{item}` မှာ custom price မရှိပါ။"); return
        await update.message.reply_text(f"✅ Custom Price Removed!\n💎 Item: `{item}`\n🔄 Default price ကို ပြန်သုံးပါမည်။", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error removing price for {item}: {e}"); await update.message.reply_text("❌ Database error.")


# --- Payment Info Commands ---
async def setwavenum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
    if len(context.args) != 1: await update.message.reply_text("❌ Format: `/setwavenum <number>`"); return
    new_number = context.args[0]
    if update_payment_info("wave_number", new_number):
        info = get_payment_info()
        await update.message.reply_text(f"✅ Wave Number Updated!\n📱 New: `{info['wave_number']}`\n👤 Name: {info['wave_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error updating.")

async def setkpaynum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
    if len(context.args) != 1: await update.message.reply_text("❌ Format: `/setkpaynum <number>`"); return
    new_number = context.args[0]
    if update_payment_info("kpay_number", new_number):
        info = get_payment_info()
        await update.message.reply_text(f"✅ KPay Number Updated!\n📱 New: `{info['kpay_number']}`\n👤 Name: {info['kpay_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error updating.")

async def setwavename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
    if not context.args: await update.message.reply_text("❌ Format: `/setwavename <name>`"); return
    new_name = " ".join(context.args)
    if update_payment_info("wave_name", new_name):
        info = get_payment_info()
        await update.message.reply_text(f"✅ Wave Name Updated!\n📱 Number: `{info['wave_number']}`\n👤 New Name: {info['wave_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error updating.")

async def setkpayname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
    if not context.args: await update.message.reply_text("❌ Format: `/setkpayname <name>`"); return
    new_name = " ".join(context.args)
    if update_payment_info("kpay_name", new_name):
        info = get_payment_info()
        await update.message.reply_text(f"✅ KPay Name Updated!\n📱 Number: `{info['kpay_number']}`\n👤 New Name: {info['kpay_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error updating.")

async def setkpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner check done by filter
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("❌ ပုံကို reply လုပ်ပြီး `/setkpayqr` သုံးပါ။"); return
    photo_file_id = update.message.reply_to_message.photo[-1].file_id
    if update_payment_info("kpay_image", photo_file_id):
        await update.message.reply_text("✅ KPay QR Code ထည့်သွင်းပြီးပါပြီ!")
    else: await update.message.reply_text("❌ Error setting KPay QR.")

async def removekpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner check done by filter
    current_info = get_payment_info()
    if not current_info.get("kpay_image"): await update.message.reply_text("ℹ️ KPay QR code မရှိသေးပါ။"); return
    if update_payment_info("kpay_image", None):
        await update.message.reply_text("✅ KPay QR Code ဖျက်ပြီးပါပြီ!")
    else: await update.message.reply_text("❌ Error removing KPay QR.")

async def setwaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner check done by filter
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("❌ ပုံကို reply လုပ်ပြီး `/setwaveqr` သုံးပါ။"); return
    photo_file_id = update.message.reply_to_message.photo[-1].file_id
    if update_payment_info("wave_image", photo_file_id):
        await update.message.reply_text("✅ Wave QR Code ထည့်သွင်းပြီးပါပြီ!")
    else: await update.message.reply_text("❌ Error setting Wave QR.")

async def removewaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Owner check done by filter
    current_info = get_payment_info()
    if not current_info.get("wave_image"): await update.message.reply_text("ℹ️ Wave QR code မရှိသေးပါ။"); return
    if update_payment_info("wave_image", None):
        await update.message.reply_text("✅ Wave QR Code ဖျက်ပြီးပါပြီ!")
    else: await update.message.reply_text("❌ Error removing Wave QR.")

async def send_to_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
    if not context.args: await update.message.reply_text("❌ Format: `/sendgroup <message>`"); return
    message = " ".join(context.args)
    if not ADMIN_GROUP_ID: await update.message.reply_text("❌ Admin Group ID is not set in env.py."); return
    try:
        await context.bot.send_message(ADMIN_GROUP_ID, f"📢 ***Admin Message***\n\n{message}", parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("✅ Group ထဲကို message ပို့ပြီး။")
    except Exception as e: logger.error(f"Failed to send to group {ADMIN_GROUP_ID}: {e}"); await update.message.reply_text(f"❌ Group ထဲကို message မပို့နိုင်ပါ။\nError: {e}")

async def testgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin check done by filter
    if not ADMIN_GROUP_ID: await update.message.reply_text("❌ Admin Group ID is not set in env.py."); return
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
    # Admin check done by filter
    user_id = str(update.effective_user.id)
    is_user_owner = is_owner(user_id)
    current_payment_info = get_payment_info() # Fetch latest

    help_msg = "🔧 ***Admin Commands*** 🔧\n\n"
    if is_user_owner:
        help_msg += ("👑 *Owner Only:*\n"
                     "/addadm <id> - Add Admin\n/unadm <id> - Remove Admin\n"
                     "/broadcast <msg> - Broadcast (Reply)\n"
                     "/setkpayqr - Set KPay QR (Reply)\n/removekpayqr - Remove KPay QR\n"
                     "/setwaveqr - Set Wave QR (Reply)\n/removewaveqr - Remove Wave QR\n"
                     # Removed clone commands
                     "/d [YYYY-MM-DD] [YYYY-MM-DD] - Daily Report\n"
                     "/m [YYYY-MM] [YYYY-MM] - Monthly Report\n"
                     "/y [YYYY] [YYYY] - Yearly Report\n\n")
    help_msg += ("💰 *Balance:*\n/approve <id> <amt> - Approve Topup\n/deduct <id> <amt> - Deduct Balance\n\n"
                 "💬 *Communication:*\n/reply <id> <msg> - Reply User\n/done <id> - Send Done Msg\n/sendgroup <msg> - Send to Admin Group\n\n"
                 "🔧 *Settings:*\n/maintenance <feat> <on|off> - Toggle Feature (orders/topups/general)\n"
                 "/setprice <item> <price> - Set Custom Price\n/removeprice <item> - Remove Custom Price\n"
                 "/setkpaynum <num>\n/setwavenum <num>\n/setkpayname <name>\n/setwavename <name>\n\n"
                 "🛡️ *User Management:*\n/ban <id>\n/unban <id>\n\n"
                 "ℹ️ *Info:*\n/testgroup - Check Admin Group\n/adminhelp - This help\n\n")
    help_msg += (f"📊 *Current Status:*\n"
                 f"• Orders: {'🟢' if bot_maintenance['orders'] else '🔴'}\n"
                 f"• Topups: {'🟢' if bot_maintenance['topups'] else '🔴'}\n"
                 f"• General: {'🟢' if bot_maintenance['general'] else '🔴'}\n"
                 f"• Authorized Users: {get_authorized_user_count()}\n\n"
                 f"💳 *Payment Info:*\n"
                 f"• KPay: {current_payment_info['kpay_number']} ({current_payment_info['kpay_name']}){' [QR Set]' if current_payment_info['kpay_image'] else ''}\n"
                 f"• Wave: {current_payment_info['wave_number']} ({current_payment_info['wave_name']}){' [QR Set]' if current_payment_info['wave_image'] else ''}")

    await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN)


# --- Message Handlers ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming photos, primarily for topup screenshots."""
    user = update.effective_user
    if not user: return # Should not happen
    user_id = str(user.id)
    if not is_user_authorized(user_id): logger.debug(f"Ignoring photo from unauthorized user {user_id}"); return

    # Check restriction status
    if get_user_restriction_status(user_id) == RESTRICTION_AWAITING_APPROVAL:
        await update.message.reply_text("⏳ Screenshot ပို့ပြီးပါပြီ။ Admin approve စောင့်ပါ။", parse_mode=ParseMode.MARKDOWN); return

    # Check if it's part of a topup process
    if user_id not in pending_topups:
        await update.message.reply_text("💡 ပုံ မပို့မီ `/topup amount` ကို အရင်သုံးပါ။", parse_mode=ParseMode.MARKDOWN); return

    if not is_payment_screenshot(update):
        await update.message.reply_text("❌ Payment screenshot (KPay/Wave) သာ လက်ခံပါတယ်။", parse_mode=ParseMode.MARKDOWN); return

    pending = pending_topups[user_id]
    amount, payment_method = pending["amount"], pending.get("payment_method", "Unknown")
    if payment_method == "Unknown": await update.message.reply_text("❌ Payment app (KPay/Wave) ကို အရင်ရွေးပါ။"); return

    # Set restriction in DB first
    if not set_user_restriction_status(user_id, RESTRICTION_AWAITING_APPROVAL):
        await update.message.reply_text("❌ User status update error. Contact admin."); return

    topup_id = f"TOP{datetime.now().strftime('%y%m%d%H%M%S%f')[:-3]}{user_id[-2:]}"
    user_name = user.mention_markdown()
    topup_request = { "topup_id": topup_id, "amount": amount, "payment_method": payment_method, "status": STATUS_PENDING,
                      "timestamp": datetime.now().isoformat(), "chat_id": update.effective_chat.id }

    try: # Save to DB
        users_col.update_one({"_id": user_id}, {"$push": {"topups": topup_request}}, upsert=True)
        del pending_topups[user_id] # Clear memory state only after DB success

        # Notify Admins/Group
        admin_msg = ( f"💳 ***ငွေဖြည့်တောင်းဆိုမှု***\n👤 User: {user_name} (`{user_id}`)\n💰 Amt: `{amount:,} MMK`\n"
                      f"📱 Via: {payment_method.upper()}\n🔖 ID: `{topup_id}`\n⏰ Time: {datetime.now():%H:%M:%S}\n📊 Status: ⏳ {STATUS_PENDING}" )
        keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"topup_approve_{topup_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"topup_reject_{topup_id}")]]
        admin_list = get_all_admin_ids()
        for admin_id in admin_list:
            try: await context.bot.send_photo(admin_id, update.message.photo[-1].file_id, caption=admin_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e: logger.warning(f"Failed sending topup photo to admin {admin_id}: {e}")
        if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
            try:
                group_caption = admin_msg + f"\n\nApprove: `/approve {user_id} {amount}`\n#TopupRequest"
                await context.bot.send_photo(ADMIN_GROUP_ID, update.message.photo[-1].file_id, caption=group_caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e: logger.warning(f"Failed sending topup photo to group {ADMIN_GROUP_ID}: {e}")

        # Reply to user
        await update.message.reply_text(
            f"✅ ***Screenshot လက်ခံပါပြီ!***\n💰 ပမာဏ: `{amount:,} MMK`\n⏰ Admin approve လုပ်သည်ထိ စောင့်ပါ။\n\n"
            f"🔒 ***သင်၏ အသုံးပြုမှုကို ယာယီ ကန့်သတ်ထားပါမည်။ Admin မှ စစ်ဆေးပြီးပါက ပြန်လည် အသုံးပြုနိုင်ပါမည်။***",
            parse_mode=ParseMode.MARKDOWN)

    except PyMongoError as e: logger.error(f"DB Error saving topup request for {user_id}: {e}"); set_user_restriction_status(user_id, RESTRICTION_NONE); await update.message.reply_text("❌ Database error. Topup မရ။ ပြန်ကြိုးစားပါ။")


async def handle_other_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles non-command, non-photo messages based on auth and restriction."""
    user = update.effective_user
    if not user: return # Should not happen
    user_id = str(user.id)

    # Unauthorized users get simple replies for text, ignored otherwise
    if not is_user_authorized(user_id):
        if update.message and update.message.text: await update.message.reply_text(simple_reply(update.message.text), parse_mode=ParseMode.MARKDOWN)
        return

    # Authorized but restricted users get a specific message
    if get_user_restriction_status(user_id) == RESTRICTION_AWAITING_APPROVAL:
        await update.message.reply_text("❌ ***အသုံးပြုမှု ကန့်သတ်ထားပါ!***\n🔒 Admin မှ topup စစ်ဆေးပြီးသည်ထိ စာများ/sticker များ ပို့၍မရပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    # Authorized, non-restricted users get simple replies for text, ignored otherwise
    if update.message and update.message.text:
        await update.message.reply_text(simple_reply(update.message.text), parse_mode=ParseMode.MARKDOWN)
    else: logger.debug(f"Ignoring non-text/photo message from authorized user {user_id}")


# --- Callback Query Handler ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user # User pressing the button
    user_id = str(user.id)
    admin_name = user.first_name
    await query.answer() # Acknowledge callback quickly

    data = query.data
    logger.info(f"Callback received: {data} from user {user_id}")

    # --- Payment method selection (topup_pay_) ---
    if data.startswith("topup_pay_"):
        # User must match the one who initiated topup
        target_user_id = str(query.message.chat_id) # Assuming button is in user's chat
        if user_id != target_user_id: logger.warning(f"User {user_id} tried to press topup_pay button for user {target_user_id}. Ignoring."); return

        if target_user_id not in pending_topups: await query.edit_message_text("❌ Topup process မရှိတော့ပါ။ /topup ပြန်စပါ။"); return
        parts = data.split("_"); payment_method, amount_str = parts[2], parts[3]
        amount = int(amount_str) # Amount check already done in topup_command

        pending_topups[target_user_id]["payment_method"] = payment_method
        info = get_payment_info() # Get current info from cache/DB
        pay_info = {}
        if payment_method == 'kpay': pay_info = {'name': "KBZ Pay", 'num': info['kpay_number'], 'acc': info['kpay_name'], 'qr': info.get('kpay_image')}
        elif payment_method == 'wave': pay_info = {'name': "Wave Money", 'num': info['wave_number'], 'acc': info['wave_name'], 'qr': info.get('wave_image')}
        else: await query.edit_message_text("❌ Invalid payment method selected."); return

        msg = (f"💳 ***ငွေဖြည့်ရန် ({pay_info['name']})***\n💰 Amount: `{amount:,} MMK`\n\n"
               f"📱 {pay_info['name']}\n📞 Number: `{pay_info['num']}`\n👤 Name: {pay_info['acc']}\n\n"
               f"⚠️ ***Important:*** ငွေလွှဲ Note/Remark တွင် သင်၏ {pay_info['name']} အကောင့်အမည်ကို ရေးပါ။ မရေးပါက ငြင်းပယ်ခံရနိုင်ပါသည်။\n\n"
               f"💡 ***ငွေလွှဲပြီးလျှင် screenshot ကို ဤ chat တွင် တင်ပေးပါ။***\n⏰ Admin စစ်ဆေးအတည်ပြုပါမည်။\n\nℹ️ ပယ်ဖျက်ရန် /cancel နှိပ်ပါ။")
        try: await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed to edit topup message: {e}") # Maybe message too old

        if pay_info.get('qr'):
            try: await query.message.reply_photo(pay_info['qr'], caption=f"👆 {pay_info['name']} QR Code\nNumber: `{pay_info['num']}`\nName: {pay_info['acc']}", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed to send QR photo {pay_info['qr']}: {e}")
        return

    # --- Registration request button ---
    elif data == "request_register":
        if is_user_authorized(user_id): await context.bot.send_message(user_id, "✅ သင် အသုံးပြုခွင့် ရပြီးသားပါ။ /start နှိပ်ပါ။"); return
        # Call register logic directly
        username = user.username or "-"; name = f"{user.first_name} {user.last_name or ''}".strip()
        keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"register_approve_{user_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"register_reject_{user_id}")]]
        owner_msg = (f"📝 ***Registration Request***\n👤 Name: {user.mention_markdown()}\n🆔 ID: `{user_id}`\n📱 User: @{username}\n⏰ Time: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n Approve?")
        try: # Send request to Owner (ADMIN_ID) with profile photo if possible
            # ... (send photo or message logic) ...
            await context.bot.send_message(ADMIN_ID, owner_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)) # Fallback if photo fails
            await query.edit_message_text(f"✅ ***Register တောင်းဆိုမှု ပို့ပြီး!***\n🆔 Your ID: `{user_id}`\n⏳ Owner approve လုပ်သည်ထိ စောင့်ပါ။", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Failed sending register req to owner {ADMIN_ID}: {e}")
            await context.bot.send_message(user_id, "❌ Register တောင်းဆိုမှု ပို့ရာတွင် အမှားဖြစ်နေပါသည်။ Owner ကို ဆက်သွယ်ပါ။")
        return

    # --- Admin action Callbacks (Check if user is admin) ---
    if not is_admin(user_id): logger.warning(f"Non-admin {user_id} tried admin callback {data}. Ignoring."); return

    # --- Registration approve ---
    if data.startswith("register_approve_"):
        target_user_id = data.split("_")[-1]
        if is_user_authorized(target_user_id): logger.info(f"User {target_user_id} already authorized."); try: await query.edit_message_reply_markup(reply_markup=None); except: pass; return
        try:
            auth_users_col.update_one({"_id": target_user_id}, {"$set": {"authorized_at": datetime.now(), "approved_by": user_id}}, upsert=True)
            set_user_restriction_status(target_user_id, RESTRICTION_NONE)
            # Edit original message
            try: await query.edit_message_text(query.message.text + f"\n\n✅ Approved by {admin_name}", parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed to edit register msg: {e}")
            # Notify user
            try: await context.bot.send_message(int(target_user_id), f"🎉 Registration Approved!\nAdmin က လက်ခံပါပြီ။ /start နှိပ်ပြီး သုံးနိုင်ပါပြီ!")
            except Exception as e: logger.warning(f"Failed sending register approval to {target_user_id}: {e}")
            # Notify group (optional)
            if ADMIN_GROUP_ID: #... send group notification ...
                 pass
        except PyMongoError as e: logger.error(f"DB Error approving registration for {target_user_id}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Registration reject ---
    elif data.startswith("register_reject_"):
        target_user_id = data.split("_")[-1]
        try: await query.edit_message_text(query.message.text + f"\n\n❌ Rejected by {admin_name}", parse_mode=ParseMode.MARKDOWN, reply_markup=None)
        except Exception as e: logger.warning(f"Failed to edit reject msg: {e}")
        # Notify user
        try: await context.bot.send_message(int(target_user_id), "❌ Registration Rejected\nAdmin က ငြင်းပယ်လိုက်ပါပြီ။ Admin ကို ဆက်သွယ်ပါ။")
        except Exception as e: logger.warning(f"Failed sending register rejection to {target_user_id}: {e}")
        # Notify group (optional)
        if ADMIN_GROUP_ID: #... send group notification ...
            pass
        return

    # --- Topup cancel (Pressed by user who initiated) ---
    # Handled above in non-admin section

    # --- Topup approve ---
    elif data.startswith("topup_approve_"):
        topup_id = data.split("_")[-1]
        try:
            result = users_col.find_one_and_update(
                {"topups.topup_id": topup_id, "topups.status": STATUS_PENDING},
                [{"$set": { # Pipeline update
                    "balance": {"$add": ["$balance", "$$amount_to_add"]},
                    "restriction_status": RESTRICTION_NONE,
                    "topups": {"$map": {"input": "$topups", "as": "t", "in": {"$cond": [ {"$eq": ["$$t.topup_id", topup_id]},
                                {"$mergeObjects": ["$$t", {"status": STATUS_APPROVED, "approved_by": admin_name, "approved_at": datetime.now().isoformat()}]}, "$$t" ]}}}
                }}],
                let={"amount_to_add": {"$let": {"vars": {"m": {"$first": {"$filter": {"input": "$topups", "as": "t", "cond": {"$eq": ["$$t.topup_id", topup_id]}}}}}, "in": "$$m.amount"}}},
                projection={"balance": 1, "_id": 1, "topups.$": 1}, return_document=ReturnDocument.BEFORE
            )
            if result is None: await context.bot.send_message(user_id, "⚠️ Topup ကို လုပ်ဆောင်ပြီးသား ဖြစ်နိုင်ပါသည်။"); try: await query.edit_message_reply_markup(reply_markup=None); except: pass; return

            target_user_id = result["_id"]; matched_topup = result["topups"][0]; amount = matched_topup["amount"]; old_balance = result.get("balance", 0); new_balance = old_balance + amount

            # Edit original message (photo caption or text)
            try:
                original_caption = query.message.caption or ""
                updated_caption = original_caption.replace(f"⏳ {STATUS_PENDING}", f"✅ {STATUS_APPROVED}") + f"\n\n✅ Approved by: {admin_name}"
                await query.edit_message_caption(caption=updated_caption, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed editing topup approve caption: {e}") # Maybe was text message

            # Notify user
            try:
                keyboard = [[InlineKeyboardButton("💎 Order တင်မယ်", url=f"https://t.me/{context.bot.username}?start=order")]]
                await context.bot.send_message(int(target_user_id), f"✅ ***Topup Approved!*** 🎉\n💰 Amount: `{amount:,} MMK`\n💳 Balance: `{new_balance:,} MMK`\n👤 By: {admin_name}\n\n🔓 Bot ပြန်သုံးနိုင်ပါပြီ!", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e: logger.warning(f"Failed sending topup approval to {target_user_id}: {e}")

            # Notify group
            if ADMIN_GROUP_ID: #... send group notification ...
                pass

        except PyMongoError as e: logger.error(f"DB Error approving topup {topup_id}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Topup reject ---
    elif data.startswith("topup_reject_"):
        topup_id = data.split("_")[-1]
        try:
            result = users_col.find_one_and_update(
                 {"topups.topup_id": topup_id, "topups.status": STATUS_PENDING},
                 {"$set": {"topups.$.status": STATUS_REJECTED, "topups.$.rejected_by": admin_name, "topups.$.rejected_at": datetime.now().isoformat(), "restriction_status": RESTRICTION_NONE}},
                 projection={"_id": 1, "topups.$": 1}
            )
            if result is None: await context.bot.send_message(user_id, "⚠️ Topup ကို လုပ်ဆောင်ပြီးသား ဖြစ်နိုင်ပါသည်။"); try: await query.edit_message_reply_markup(reply_markup=None); except: pass; return

            target_user_id = result["_id"]; amount = result["topups"][0].get("amount", 0)

            # Edit original message
            try:
                original_caption = query.message.caption or ""
                updated_caption = original_caption.replace(f"⏳ {STATUS_PENDING}", f"❌ {STATUS_REJECTED}") + f"\n\n❌ Rejected by: {admin_name}"
                await query.edit_message_caption(caption=updated_caption, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed editing topup reject caption: {e}")

            # Notify user
            try: await context.bot.send_message(int(target_user_id), f"❌ ***Topup Rejected!***\n💰 Amount: `{amount:,} MMK`\n👤 By: {admin_name}\n📞 အကြောင်းရင်းသိရန် Admin ကို ဆက်သွယ်ပါ။\n\n🔓 Bot ပြန်သုံးနိုင်ပါပြီ!", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending topup rejection to {target_user_id}: {e}")

            # Notify group
            if ADMIN_GROUP_ID: #... send group notification ...
                pass

        except PyMongoError as e: logger.error(f"DB Error rejecting topup {topup_id}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Order confirm ---
    elif data.startswith("order_confirm_"):
        order_id = data.split("_")[-1]
        try:
            result = users_col.find_one_and_update(
                {"orders.order_id": order_id, "orders.status": STATUS_PENDING},
                {"$set": {"orders.$.status": STATUS_CONFIRMED, "orders.$.confirmed_by": admin_name, "orders.$.confirmed_at": datetime.now().isoformat()}},
                projection={"_id": 1, "orders.$": 1}
            )
            if result is None: await context.bot.send_message(user_id, "⚠️ Order ကို လုပ်ဆောင်ပြီးသား ဖြစ်နိုင်ပါသည်။"); try: await query.edit_message_reply_markup(reply_markup=None); except: pass; return

            target_user_id = result["_id"]; order = result["orders"][0]

            # Edit original message
            try:
                 updated_text = query.message.text.replace(f"⏳ {STATUS_PENDING}", f"✅ {STATUS_CONFIRMED}") + f"\n\n✅ Confirmed by: {admin_name}"
                 await query.edit_message_text(updated_text, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed editing order confirm msg: {e}")

            # Notify user (in their original chat)
            try:
                chat_id_to_notify = order.get("chat_id", int(target_user_id)) # Prefer original chat
                user_mention = f"[{order.get('name', 'User')}](tg://user?id={target_user_id})" # Get name from order if stored, else fallback
                await context.bot.send_message(chat_id_to_notify, f"✅ ***Order Confirmed!***\n📝 ID: `{order_id}`\n👤 User: {user_mention}\n🎮 Game ID: `{order['game_id']}`\n💎 Amt: {order['amount']}\n📊 Status: ✅ {STATUS_CONFIRMED}\n\n💎 Diamonds ပို့ပြီး!", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending order confirmation to chat {chat_id_to_notify}: {e}")

            # Notify group
            if ADMIN_GROUP_ID: #... send group notification ...
                pass

        except PyMongoError as e: logger.error(f"DB Error confirming order {order_id}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Order cancel ---
    elif data.startswith("order_cancel_"):
        order_id = data.split("_")[-1]
        try:
            user_doc = users_col.find_one({"orders.order_id": order_id, "orders.status": STATUS_PENDING}, {"_id": 1, "orders.$": 1})
            if not user_doc or not user_doc.get("orders"): await context.bot.send_message(user_id, "⚠️ Order ကို လုပ်ဆောင်ပြီးသား ဖြစ်နိုင်ပါသည်။"); try: await query.edit_message_reply_markup(reply_markup=None); except: pass; return

            target_user_id = user_doc["_id"]; order = user_doc["orders"][0]; refund_amount = order.get("price", 0)
            if refund_amount <= 0: logger.error(f"Invalid refund amount for order {order_id}"); await context.bot.send_message(user_id, "❌ Order price error!"); return

            # Atomically update and refund
            users_col.update_one( {"_id": target_user_id, "orders.order_id": order_id},
                                 {"$set": {"orders.$.status": STATUS_CANCELLED, "orders.$.cancelled_by": admin_name, "orders.$.cancelled_at": datetime.now().isoformat()},
                                  "$inc": {"balance": refund_amount}} )

            # Edit original message
            try:
                updated_text = query.message.text.replace(f"⏳ {STATUS_PENDING}", f"❌ {STATUS_CANCELLED}") + f"\n\n❌ Cancelled by: {admin_name} (Refunded)"
                await query.edit_message_text(updated_text, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed editing order cancel msg: {e}")

            # Notify user
            try:
                 chat_id_to_notify = order.get("chat_id", int(target_user_id))
                 user_mention = f"[{order.get('name', 'User')}](tg://user?id={target_user_id})"
                 await context.bot.send_message(chat_id_to_notify, f"❌ ***Order Cancelled!***\n📝 ID: `{order_id}`\n👤 User: {user_mention}\n🎮 Game ID: `{order['game_id']}`\n📊 Status: ❌ {STATUS_CANCELLED}\n💰 Refunded: {refund_amount:,} MMK\n📞 Admin ကို ဆက်သွယ်ပါ။", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed sending order cancel notification to chat {chat_id_to_notify}: {e}")

            # Notify group
            if ADMIN_GROUP_ID: #... send group notification ...
                pass

        except PyMongoError as e: logger.error(f"DB Error cancelling order {order_id}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Report filter callbacks ---
    # These call the respective report commands, no direct logic here needed
    elif data.startswith("report_day_"): await daily_report_command(update, context); return
    elif data.startswith("report_month_"): await monthly_report_command(update, context); return
    elif data.startswith("report_year_"): await yearly_report_command(update, context); return

    # --- Other user buttons ---
    elif data == "copy_kpay":
        info = get_payment_info()
        await query.message.reply_text(f"📱 ***KBZ Pay***\n`{info['kpay_number']}`\n👤 {info['kpay_name']}", parse_mode=ParseMode.MARKDOWN); return
    elif data == "copy_wave":
        info = get_payment_info()
        await query.message.reply_text(f"📱 ***Wave Money***\n`{info['wave_number']}`\n👤 {info['wave_name']}", parse_mode=ParseMode.MARKDOWN); return
    elif data == "topup_button":
        # Show topup instructions and copy buttons
        info = get_payment_info()
        keyboard = [[InlineKeyboardButton("📱 Copy KPay Number", callback_data="copy_kpay")], [InlineKeyboardButton("📱 Copy Wave Number", callback_data="copy_wave")]]
        msg = ("💳 ***ငွေဖြည့်ရန်***\n1️⃣ `/topup amount` ရိုက်ပါ (e.g. `/topup 5000`)\n2️⃣ အောက်ပါအကောင့်သို့ ငွေလွှဲပါ:\n"
               f"   📱 KBZ Pay: `{info['kpay_number']}` ({info['kpay_name']})\n   📱 Wave Money: `{info['wave_number']}` ({info['wave_name']})\n"
               f"3️⃣ ငွေလွှဲ Screenshot ကို ဤ chat တွင် တင်ပါ\n⏰ Admin စစ်ဆေးပြီး approve လုပ်ပါမည်။")
        try: await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception: await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)) # Fallback if edit fails
        return

    # --- Fallback for unknown callbacks ---
    else: logger.warning(f"Unhandled callback data: {data}")


# --- Post Init Function (Removed clone bot startup) ---
async def post_init(application: Application):
    """Placeholder for any actions after application initialization."""
    logger.info("🚀 Main bot application initialized.")
    # You could add checks here, like ensuring owner ID is valid, etc.


# --- Main Function ---
def main():
    if not BOT_TOKEN: logger.critical("❌ BOT_TOKEN environment variable is missing!"); return

    application = ( Application.builder().token(BOT_TOKEN).post_init(post_init)
                    # Consider adding persistence=PicklePersistence(filepath='./bot_persistence') if needed for conversation handlers later
                    .build() )

    # --- Register Handlers ---
    # Middleware (Group 0) - Runs FIRST
    application.add_handler(CommandHandler(filters.ALL, check_restriction), group=0)
    application.add_handler(MessageHandler(filters.ALL, check_restriction), group=0)
    application.add_handler(CallbackQueryHandler(check_restriction), group=0)

    # User Commands & Handlers (Group 1) - Runs AFTER middleware
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("register", register_command)) # Allow all to register
    application.add_handler(CommandHandler("c", c_command)) # Allow all to calculate?

    auth_commands = ["mmb", "balance", "topup", "cancel", "price", "history"]
    for cmd in auth_commands: application.add_handler(CommandHandler(cmd, globals()[f"{cmd}_command"])) # Check auth inside or use filter

    # Admin Commands (Group 1)
    admin_commands = [ "approve", "deduct", "done", "reply", "ban", "unban", "sendgroup", "maintenance",
                       "testgroup", "setprice", "removeprice", "setwavenum", "setkpaynum", "setwavename",
                       "setkpayname", "adminhelp" ]
    owner_commands = [ "addadm", "unadm", "setkpayqr", "removekpayqr", "setwaveqr", "removewaveqr",
                       "broadcast", "d", "m", "y" ] # Removed clone commands

    for cmd in admin_commands: application.add_handler(CommandHandler(cmd, globals()[f"{cmd}_command"])) # Check admin inside or use filter
    for cmd in owner_commands: application.add_handler(CommandHandler(cmd, globals()[f"{cmd}_command"])) # Check owner inside or use filter


    # Callback Query Handler (Group 1)
    application.add_handler(CallbackQueryHandler(button_callback))

    # Message Handlers (Group 1)
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo)) # Check auth inside
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.PHOTO, handle_other_messages)) # Check auth inside


    # --- Start Bot ---
    logger.info("🤖 Bot starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("⚫ Bot stopped.")

if __name__ == "__main__":
    main()
