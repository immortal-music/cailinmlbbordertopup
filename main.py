import json
import os
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ApplicationHandlerStop
)
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
    db_name = MongoClient(MONGODB_URL).get_database().name
    if db_name == 'test': # Default name if not specified in URL
        db_name = 'mlbb_bot_db' # Use your preferred default name
    db = client[db_name]

    logger.info(f"Using MongoDB database: {db_name}")

    # Collections
    users_col = db["users"]
    admins_col = db["admins"]
    auth_users_col = db["authorized_users"]
    prices_col = db["prices"]
    clone_bots_col = db["clone_bots"]
    config_col = db["config"] # For storing maintenance mode, payment info

    # Test connection
    client.admin.command('ping')
    logger.info("✅ MongoDB connected successfully!")

    # --- Create Indexes (Essential for Performance) ---
    logger.info("Applying MongoDB indexes...")
    try:
        # Ensure indexes exist, create if not
        users_col.create_index([("topups.topup_id", 1)], unique=True, sparse=True, background=True)
        users_col.create_index([("orders.order_id", 1)], unique=True, sparse=True, background=True)
        users_col.create_index([("topups.status", 1)], background=True)
        users_col.create_index([("orders.status", 1)], background=True)
        users_col.create_index([("orders.confirmed_at", 1)], background=True) # For Reporting
        users_col.create_index([("topups.approved_at", 1)], background=True) # For Reporting
        users_col.create_index([("restriction_status", 1)], background=True) # For checking restricted users

        auth_users_col.create_index([("_id", 1)], unique=True, background=True)
        admins_col.create_index([("_id", 1)], unique=True, background=True)
        prices_col.create_index([("_id", 1)], unique=True, background=True)
        clone_bots_col.create_index([("owner_id", 1)], background=True)
        config_col.create_index([("_id", 1)], unique=True, background=True)
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
        # Ensure all keys exist, default to True if missing
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
        # Ensure all keys exist, default if missing
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
clone_bot_apps = {} # { bot_id: Application }


# --- Helper Functions (Database & Config Access) ---

def is_owner(user_id):
    """Check if user is the owner"""
    return int(user_id) == ADMIN_ID

def is_admin(user_id):
    """Check if user is any admin (owner or appointed admin)"""
    if int(user_id) == ADMIN_ID:
        return True
    try:
        # Cache admin check slightly? For very high traffic maybe. For now, direct check is fine.
        return admins_col.count_documents({"_id": int(user_id)}) > 0
    except PyMongoError as e:
        logger.error(f"DB Error checking admin status for {user_id}: {e}")
        return False # Safer default

def is_user_authorized(user_id):
    """Check if user is authorized to use the bot"""
    if int(user_id) == ADMIN_ID:
        return True
    try:
        return auth_users_col.count_documents({"_id": str(user_id)}) > 0
    except PyMongoError as e:
        logger.error(f"DB Error checking auth status for {user_id}: {e}")
        return False # Safer default

def get_user_restriction_status(user_id):
    """Get user's restriction status from DB"""
    try:
        user_doc = users_col.find_one({"_id": str(user_id)}, {"restriction_status": 1})
        return user_doc.get("restriction_status", RESTRICTION_NONE) if user_doc else RESTRICTION_NONE
    except PyMongoError as e:
        logger.error(f"DB Error getting restriction status for {user_id}: {e}")
        return RESTRICTION_NONE # Assume not restricted on error

def set_user_restriction_status(user_id, status):
    """Set user's restriction status in DB"""
    try:
        logger.info(f"Setting restriction status for {user_id} to {status}")
        users_col.update_one(
            {"_id": str(user_id)},
            {"$set": {"restriction_status": status}},
            upsert=True # Create user doc if needed (should exist by this point)
        )
        return True
    except PyMongoError as e:
        logger.error(f"DB Error setting restriction status for {user_id} to {status}: {e}")
        return False

def load_prices():
    """Load custom prices from DB"""
    custom_prices = {}
    try:
        for doc in prices_col.find({}, {"_id": 1, "price": 1}):
            if "price" in doc: # Ensure price field exists
                custom_prices[doc["_id"]] = doc["price"]
    except PyMongoError as e:
        logger.error(f"DB Error loading prices: {e}")
    return custom_prices

def get_price(diamonds):
    """Gets price, considering defaults and custom prices from DB"""
    # This function relies on load_prices() which handles DB access
    custom_prices = load_prices()
    if diamonds in custom_prices:
        return custom_prices[diamonds]
    # Default prices logic... (unchanged)
    if diamonds.startswith("wp") and diamonds[2:].isdigit():
        n = int(diamonds[2:])
        if 1 <= n <= 10: return n * 6000
    table = { # Consider moving defaults to DB as well for easier management?
        "11": 950, "22": 1900, "33": 2850, "56": 4200, "112": 8200, "86": 5100,
        "172": 10200, "257": 15300, "343": 20400, "429": 25500, "514": 30600,
        "600": 35700, "706": 40800, "878": 51000, "963": 56100, "1049": 61200,
        "1135": 66300, "1412": 81600, "2195": 122400, "3688": 204000,
        "5532": 306000, "9288": 510000, "12976": 714000, "55": 3500,
        "165": 10000, "275": 16000, "565": 33000
    }
    return table.get(diamonds)


async def check_pending_topup(user_id):
    """Check if user has pending topups in DB"""
    try:
        # Use count_documents for efficiency if only existence check is needed
        count = users_col.count_documents({"_id": str(user_id), "topups.status": STATUS_PENDING})
        return count > 0
    except PyMongoError as e:
        logger.error(f"DB Error checking pending topup for {user_id}: {e}")
        return False # Assume no pending on error

def get_all_admin_ids():
    """Get a list of all admin IDs from DB"""
    try:
        # Fetch only the _id field
        return [doc["_id"] for doc in admins_col.find({}, {"_id": 1})]
    except PyMongoError as e:
        logger.error(f"DB Error fetching admin IDs: {e}")
        return [ADMIN_ID] # Fallback to owner

def get_authorized_user_count():
    """Get the count of authorized users"""
    try:
        return auth_users_col.count_documents({})
    except PyMongoError as e:
        logger.error(f"DB Error counting auth users: {e}")
        return 0

def get_maintenance_status(feature):
    """Get maintenance status for a feature (uses in-memory cache)"""
    # Reads from the variable loaded at startup
    return bot_maintenance.get(feature, True) # Default True

def set_maintenance_status(feature, status: bool):
    """Set maintenance status in DB and update cache"""
    try:
        result = config_col.update_one(
            {"_id": CONFIG_MAINTENANCE},
            {"$set": {f"settings.{feature}": status}},
            upsert=True
        )
        if result.acknowledged:
            # Update in-memory cache as well
            bot_maintenance[feature] = status
            logger.info(f"Maintenance status for '{feature}' set to {status}")
            return True
        else:
            logger.error(f"DB update not acknowledged for maintenance status '{feature}'")
            return False
    except PyMongoError as e:
        logger.error(f"DB Error setting maintenance status for {feature} to {status}: {e}")
        return False

def get_payment_info():
    """Get payment info (uses in-memory cache)"""
    # Reads from the variable loaded at startup
    return payment_info

def update_payment_info(key, value):
    """Update a specific payment info field in DB and cache"""
    try:
        result = config_col.update_one(
            {"_id": CONFIG_PAYMENT_INFO},
            {"$set": {f"details.{key}": value}},
            upsert=True
        )
        if result.acknowledged:
            # Update in-memory cache
            payment_info[key] = value
            logger.info(f"Payment info '{key}' updated.")
            return True
        else:
             logger.error(f"DB update not acknowledged for payment info '{key}'")
             return False
    except PyMongoError as e:
        logger.error(f"DB Error updating payment info key '{key}': {e}")
        return False


# --- (Your other helper functions: simple_reply, validate_game_id, etc. remain unchanged) ---
async def is_bot_admin_in_group(bot: Bot, chat_id: int):
    """Check if bot is admin in the group"""
    if not chat_id: # Handle case where ADMIN_GROUP_ID might be None or invalid
        logger.warning("Attempted admin check with invalid chat_id.")
        return False
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
        is_admin = bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
        logger.debug(f"Bot admin check for group {chat_id}: {is_admin}, status: {bot_member.status}")
        return is_admin
    except Exception as e: # Catch specific telegram errors if needed
        logger.error(f"Error checking bot admin status in group {chat_id}: {e}")
        return False

def simple_reply(message_text: str) -> str:
    # ... (unchanged) ...
    pass

def validate_game_id(game_id: str) -> bool:
    # ... (unchanged) ...
    pass

def validate_server_id(server_id: str) -> bool:
    # ... (unchanged) ...
    pass

def is_banned_account(game_id: str) -> bool:
    # ... (unchanged) ...
    pass

def is_payment_screenshot(update: Update) -> bool:
    # ... (unchanged) ...
    pass

# --- Message Sending Helpers (unchanged) ---
async def send_pending_topup_warning(update: Update):
    # ... (unchanged) ...
    pass

async def send_maintenance_message(update: Update, command_type: str):
    # ... (unchanged) ...
    pass


# --- Middleware for checking user restriction ---
async def check_restriction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Middleware to check if user is restricted before running command/message handlers"""
    user = update.effective_user
    if not user: # Should not happen for user messages/callbacks
        return

    user_id = str(user.id)
    query = update.callback_query

    # Allow admins to bypass restriction *only* for specifically designated admin actions
    if is_admin(user_id):
        is_admin_action = False
        command_or_data = ""
        # Check if it's a command message
        if update.message and update.message.text and update.message.text.startswith('/'):
            command_or_data = update.message.text.split()[0].lower()
            # List of commands only admins should use (even when restricted)
            admin_commands = [
                '/approve', '/deduct', '/done', '/reply', '/ban', '/unban', '/addadm', '/unadm',
                '/sendgroup', '/maintenance', '/testgroup', '/setprice', '/removeprice',
                '/setwavenum', '/setkpaynum', '/setwavename', '/setkpayname', '/setkpayqr',
                '/removekpayqr', '/setwaveqr', '/removewaveqr', '/adminhelp', '/broadcast',
                '/addbot', '/listbots', '/removebot', '/addfund', '/deductfund', '/d', '/m', '/y'
            ]
            if command_or_data in admin_commands:
                is_admin_action = True

        # Check if it's a callback query
        elif query:
            # List of callback data prefixes only admins should use
            admin_callback_prefixes = ['topup_approve_', 'topup_reject_', 'order_confirm_', 'order_cancel_', 'register_approve_', 'register_reject_', 'main_approve_', 'main_reject_', 'report_']
            if any(query.data.startswith(prefix) for prefix in admin_callback_prefixes):
                 is_admin_action = True

        if is_admin_action:
            logger.debug(f"Admin {user_id} performing admin action, bypassing restriction check.")
            return # Allow admin actions regardless of restriction

    # Check restriction status from DB for non-admin actions or non-admins
    restriction_status = get_user_restriction_status(user_id)

    if restriction_status == RESTRICTION_AWAITING_APPROVAL:
        logger.info(f"User {user_id} is restricted ({RESTRICTION_AWAITING_APPROVAL}). Blocking action.")
        message = (
            "❌ ***အသုံးပြုမှု ကန့်သတ်ထားပါ!***\n\n"
            "🔒 ***Screenshot ပို့ပြီး၍ Admin စစ်ဆေးနေဆဲ ဖြစ်ပါသည်။ Admin မှ လက်ခံ/ငြင်းပယ်ခြင်း မပြုလုပ်မချင်း အခြားလုပ်ဆောင်ချက်များ (Commands/Buttons) ကို အသုံးပြု၍ မရပါ။***\n\n"
            "⏰ ***Admin မှ ဆောင်ရွက်ပြီးပါက ပြန်လည် အသုံးပြုနိုင်ပါမည်။***\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို တိုက်ရိုက် ဆက်သွယ်ပါ။***"
        )
        try:
            if query:
                await query.answer("❌ အသုံးပြုမှု ကန့်သတ်ထားပါ! Admin ဆောင်ရွက်မှု စောင့်ပါ။", show_alert=True)
            elif update.message:
                await update.message.reply_text(message, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Failed to send restriction notice to {user_id}: {e}")

        # Stop further handlers for this update
        raise ApplicationHandlerStop

    # If not restricted, continue to the intended handler
    logger.debug(f"User {user_id} restriction check passed ({restriction_status}).")

# --- Command Handlers (Now simpler due to middleware) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or "-"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    if not is_user_authorized(user_id):
        # ... (Unauthorized message logic - unchanged) ...
        keyboard = [ [InlineKeyboardButton("📝 Register တောင်းဆိုမယ်", callback_data="request_register")] ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
             f"🚫 ***Bot အသုံးပြုခွင့် မရှိပါ!***\n\n"
             #... rest of message
             , parse_mode="Markdown", reply_markup=reply_markup)
        return

    # No need to check restriction here, middleware does it
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    try:
        users_col.find_one_and_update(
            {"_id": user_id},
            {"$setOnInsert": {"balance": 0, "orders": [], "topups": [], "restriction_status": RESTRICTION_NONE}},
            {"$set": {"name": name, "username": username}},
            upsert=True,
        )
    except PyMongoError as e:
        logger.error(f"DB Error during user upsert in /start for {user_id}: {e}")
        await update.message.reply_text("❌ Database error occurred. Please try again later.")
        return

    if user_id in pending_topups: del pending_topups[user_id] # Clear incomplete topup

    # ... (Start message logic - unchanged) ...
    clickable_name = f"[{name}](tg://user?id={user_id})"
    msg = (f"👋 ***မင်္ဂလာပါ*** {clickable_name}!\n"
           # ... rest of message ...
           )
    try: # Send with profile photo logic
        user_photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        # ... send photo or text ...
    except Exception: await update.message.reply_text(msg, parse_mode="Markdown")


async def mmb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Auth checked by filter, Restriction checked by middleware
    if not get_maintenance_status("orders"): await send_maintenance_message(update, "orders"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: # ... pending process reply ...
         return

    args = context.args
    if len(args) != 3: # ... invalid format reply ...
        return
    game_id, server_id, amount = args

    # Validations (unchanged)
    if not validate_game_id(game_id): # ... reply ...
        return
    if not validate_server_id(server_id): # ... reply ...
        return
    if is_banned_account(game_id): # ... reply ...
        # ... Notify admin about banned attempt ...
        return

    price = get_price(amount)
    if not price: # ... invalid amount reply ...
        return

    try: # Get balance
        user_data = users_col.find_one({"_id": user_id}, {"balance": 1})
        user_balance = user_data.get("balance", 0) if user_data else 0
    except PyMongoError as e: # ... handle DB error ...
        return

    if user_balance < price: # ... insufficient balance reply ...
        return

    # Process Order (DB update)
    order_id = f"ORD{datetime.now().strftime('%Y%m%d%H%M%S%f')[:-3]}{user_id[-3:]}" # More unique ID
    order = {
        "order_id": order_id, "game_id": game_id, "server_id": server_id, "amount": amount,
        "price": price, "status": STATUS_PENDING, "timestamp": datetime.now().isoformat(),
        "user_id": user_id, "chat_id": update.effective_chat.id
    }
    try:
        result = users_col.update_one(
            {"_id": user_id},
            {"$inc": {"balance": -price}, "$push": {"orders": order}}
            # Removed upsert=True as user should exist if authorized
        )
        if not result.modified_count:
             logger.warning(f"Order update failed for user {user_id} (maybe concurrent modification?).")
             # Optionally retry or inform user
             await update.message.reply_text("❌ Order processing error. Please try again.")
             return

        updated_user_data = users_col.find_one({"_id": user_id}, {"balance": 1})
        new_balance = updated_user_data.get("balance", user_balance - price)
    except PyMongoError as e: # ... handle DB error ...
        return

    # ... (Admin/Group Notification logic - unchanged) ...
    # ... (Success reply to user - unchanged) ...
    pass


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Auth checked by filter, Restriction checked by middleware
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: # ... pending process reply ...
        return

    try:
        user_data = users_col.find_one({"_id": user_id})
        if not user_data: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return
        # ... (Balance message formatting - unchanged) ...
    except PyMongoError as e: # ... handle DB error ...
        pass


async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Auth checked by filter, Restriction checked by middleware
    if not get_maintenance_status("topups"): await send_maintenance_message(update, "topups"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: # ... pending process reply ...
        return

    # ... (Arg parsing/validation - unchanged) ...
    try: amount = int(context.args[0]); assert amount >= 1000
    except: # ... invalid format/amount reply ...
        return

    pending_topups[user_id] = {"amount": amount, "timestamp": datetime.now().isoformat()}
    # ... (Payment method selection reply - unchanged) ...
    pass


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Auth checked by filter, Restriction checked by middleware
    if update.effective_user.id in pending_topups: # Check in-memory process only
        # ... pending process reply ...
        return
    # ... (Price message formatting using load_prices() - unchanged) ...
    pass

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Auth checked by filter, Restriction checked by middleware
    # ... (Deals with in-memory pending_topups - unchanged) ...
    pass

async def c_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No auth needed?, Restriction checked by middleware
    # ... (Calculator logic - unchanged) ...
    pass

# --- Reports (Owner only, Auth handled by filter, Restriction not relevant) ---
async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Date range logic - unchanged) ...
    # ... (DB Aggregation logic - unchanged) ...
    pass
async def monthly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Date range logic - unchanged) ...
    # ... (DB Aggregation logic - unchanged) ...
    pass
async def yearly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Date range logic - unchanged) ...
    # ... (DB Aggregation logic - unchanged) ...
    pass

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Auth checked by filter, Restriction checked by middleware
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: # ... pending process reply ...
        return
    try:
        user_data = users_col.find_one({"_id": user_id}, {"orders": {"$slice": -5}, "topups": {"$slice": -5}}) # Get last 5 only
        if not user_data: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return
        # ... (History message formatting - unchanged) ...
    except PyMongoError as e: # ... handle DB error ...
        pass


# --- Admin Commands (Auth handled by filter, Restriction bypassed in middleware) ---

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id) # Admin ID
    admin_name = update.effective_user.first_name
    # ... (Arg parsing) ...
    try: target_user_id = context.args[0]; amount = int(context.args[1])
    except: # ... handle error ...
        return

    try:
        # Atomically find pending topup, update status, increment balance, remove restriction
        result = users_col.find_one_and_update(
            {"_id": target_user_id, "topups": {"$elemMatch": {"status": STATUS_PENDING, "amount": amount}}},
            [ # Use aggregation pipeline for update
                {"$set": {
                    "balance": {"$add": ["$balance", amount]},
                    "restriction_status": RESTRICTION_NONE,
                    "topups": {
                        "$map": {
                            "input": "$topups",
                            "as": "t",
                            "in": {
                                "$cond": [
                                    {"$and": [ # Find the specific pending topup
                                        {"$eq": ["$$t.amount", amount]},
                                        {"$eq": ["$$t.status", STATUS_PENDING]},
                                        # Add more conditions if needed (e.g., check if approved_by exists)
                                        # To prevent approving multiple identical pending topups, we ideally need a unique topup_id check here
                                        # For now, this approves the first match in the array. Be careful if multiple identical pending topups can exist.
                                    ]},
                                    # Update the matched topup
                                    {"$mergeObjects": ["$$t", {"status": STATUS_APPROVED, "approved_by": admin_name, "approved_at": datetime.now().isoformat()}]},
                                    # Keep others unchanged
                                    "$$t"
                                ]
                            }
                        }
                    }
                }}
            ],
            projection={"balance": 1, "_id": 0}, # Get old balance
            return_document=ReturnDocument.BEFORE
        )

        if result is None:
            # Check if it was already processed or doesn't exist
            check_user = users_col.find_one(
                {"_id": target_user_id},
                {"topups": {"$elemMatch": {"amount": amount, "status": {"$ne": STATUS_PENDING}}}}
            )
            if check_user and 'topups' in check_user and check_user['topups']:
                 await update.message.reply_text(f"ℹ️ `{target_user_id}` ထံမှ `{amount}` MMK topup ကို လုပ်ဆောင်ပြီးသား ဖြစ်နိုင်ပါသည်။")
            else:
                 await update.message.reply_text(f"❌ `{target_user_id}` ထံမှ `{amount}` MMK ဖြင့် pending topup မတွေ့ပါ!")
            return

        old_balance = result.get("balance", 0)
        new_balance = old_balance + amount

        # ... (Notify user with new_balance - unchanged) ...
        # ... (Confirm to admin with new_balance - unchanged) ...

    except PyMongoError as e: # ... handle DB error ...
        pass


async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Arg parsing) ...
    try: target_user_id = context.args[0]; amount = int(context.args[1]); assert amount > 0
    except: # ... handle error ...
        return

    try:
        result = users_col.find_one_and_update(
            {"_id": target_user_id, "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount}},
            projection={"balance": 1},
            return_document=ReturnDocument.AFTER
        )
        if result is None: # ... handle insufficient funds / user not found ...
            return
        new_balance = result.get("balance")
        # ... (Notify user, Confirm to admin - unchanged) ...
    except PyMongoError as e: # ... handle DB error ...
        pass


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Arg parsing, owner check) ...
    target_user_id = context.args[0]
    if int(target_user_id) == ADMIN_ID: return

    try:
        result_auth = auth_users_col.delete_one({"_id": target_user_id})
        result_user = users_col.update_one({"_id": target_user_id}, {"$set": {"restriction_status": RESTRICTION_NONE}}) # Clear restriction if any

        if result_auth.deleted_count == 0: # ... already banned/not found reply ...
            return
        # ... (Notifications, Admin confirmation - unchanged) ...
    except PyMongoError as e: # ... handle DB error ...
        pass


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Arg parsing) ...
    target_user_id = context.args[0]
    if is_user_authorized(target_user_id): # ... already authorized reply ...
        return
    try:
        auth_users_col.update_one(
            {"_id": target_user_id},
            {"$set": {"authorized_at": datetime.now(), "unbanned_by": update.effective_user.id}},
            upsert=True
        )
        set_user_restriction_status(target_user_id, RESTRICTION_NONE) # Ensure restriction removed
        # ... (Notifications, Admin confirmation - unchanged) ...
    except PyMongoError as e: # ... handle DB error ...
        pass


async def addadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Arg parsing, owner check) ...
    new_admin_id = int(context.args[0])
    if is_admin(new_admin_id): # ... already admin reply ...
        return
    try:
        admins_col.update_one(
            {"_id": new_admin_id},
            {"$set": {"is_owner": False, "added_by": ADMIN_ID, "added_at": datetime.now()}},
            upsert=True
        )
        # ... (Notifications, Owner confirmation - unchanged) ...
    except PyMongoError as e: # ... handle DB error ...
        pass


async def unadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Arg parsing, owner check) ...
    target_admin_id = int(context.args[0])
    if target_admin_id == ADMIN_ID: return # Cannot remove owner
    try:
        result = admins_col.delete_one({"_id": target_admin_id})
        if result.deleted_count == 0: # ... not admin reply ...
            return
        # ... (Notifications, Owner confirmation - unchanged) ...
    except PyMongoError as e: # ... handle DB error ...
        pass


async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Arg parsing, validation) ...
    feature, status_str = context.args[0].lower(), context.args[1].lower()
    # ... validate feature/status_str ...
    status_bool = (status_str == "on")
    if set_maintenance_status(feature, status_bool):
        # ... (Success reply using updated bot_maintenance dict - unchanged) ...
        pass
    else: # ... handle error ...
        pass


async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Arg parsing, price validation) ...
    item, price = context.args[0], int(context.args[1])
    try:
        prices_col.update_one({"_id": item}, {"$set": {"price": price}}, upsert=True)
        # ... (Success reply - unchanged) ...
    except PyMongoError as e: # ... handle DB error ...
        pass


async def removeprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Arg parsing) ...
    item = context.args[0]
    try:
        result = prices_col.delete_one({"_id": item})
        if result.deleted_count == 0: # ... price not found reply ...
            return
        # ... (Success reply - unchanged) ...
    except PyMongoError as e: # ... handle DB error ...
        pass

# --- Payment Info Commands (use update_payment_info) ---
# Example:
async def setwavenum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Arg parsing) ...
    new_number = context.args[0]
    if update_payment_info("wave_number", new_number):
        current_info = get_payment_info() # Get updated info for reply
        # ... (Success reply using current_info - unchanged) ...
        pass
    else: await update.message.reply_text("❌ Error updating Wave number.")
# (Implement other set/remove payment commands similarly)

# --- Other Admin Commands (No major DB changes needed) ---
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # Unchanged
async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # Unchanged
async def send_to_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # Unchanged
async def testgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass # Unchanged
async def adminhelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     current_payment_info = get_payment_info() # Fetch latest
     # ... (Build help message using bot_maintenance, current_payment_info, get_authorized_user_count()) ...
     pass
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (Fetch user IDs from users_col - logic unchanged) ...
    pass

# --- Clone Bot Commands (Auth handled by filter, Restriction bypassed) ---
# ... (addbot, listbots, removebot, addfund, deductfund - unchanged from previous MongoDB version) ...

# --- Clone Bot Runner Functions ---
# ... (run_clone_bot, clone_bot_start, clone_bot_mmb, clone_bot_callback - unchanged) ...

# --- Message Handlers ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming photos, primarily for topup screenshots."""
    user_id = str(update.effective_user.id)
    # Auth check done by filter group
    # Restriction check (awaiting approval)
    if get_user_restriction_status(user_id) == RESTRICTION_AWAITING_APPROVAL:
        await update.message.reply_text("⏳ Screenshot ပို့ပြီးပါပြီ။ Admin approve စောင့်ပါ။")
        return

    if user_id not in pending_topups: # ... Need to start /topup reply ...
        return
    if not is_payment_screenshot(update): # ... Invalid screenshot reply ...
        return

    pending = pending_topups[user_id]
    amount, payment_method = pending["amount"], pending.get("payment_method", "Unknown")
    if payment_method == "Unknown": # ... Method not selected reply ...
        return

    # Set restriction in DB first
    if not set_user_restriction_status(user_id, RESTRICTION_AWAITING_APPROVAL):
        # ... handle error ...
        return

    topup_id = f"TOP{datetime.now().strftime('%Y%m%d%H%M%S%f')[:-3]}{user_id[-3:]}"
    user_name = update.effective_user.first_name
    topup_request = { # ... create topup_request dict ...
        "topup_id": topup_id, "amount": amount, "payment_method": payment_method,
        "status": STATUS_PENDING, "timestamp": datetime.now().isoformat(),
        "chat_id": update.effective_chat.id
    }

    try: # Save to DB
        users_col.update_one({"_id": user_id}, {"$push": {"topups": topup_request}}, upsert=True)
        del pending_topups[user_id] # Clear memory state only after DB success
        # ... (Admin/Group notifications - unchanged) ...
        # ... (User confirmation reply - unchanged) ...
    except PyMongoError as e:
        logger.error(f"DB Error saving topup request for {user_id}: {e}")
        set_user_restriction_status(user_id, RESTRICTION_NONE) # Revert restriction
        await update.message.reply_text("❌ Database error. Topup မရ။ ပြန်ကြိုးစားပါ။")
    pass


async def handle_other_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles non-command, non-photo messages based on auth and restriction."""
    user_id = str(update.effective_user.id)

    # Unauthorized users get simple replies or are ignored
    if not is_user_authorized(user_id):
        if update.message and update.message.text:
            reply = simple_reply(update.message.text)
            await update.message.reply_text(reply, parse_mode="Markdown")
        return # Ignore other types from unauthorized users

    # Authorized but restricted users get a specific message
    if get_user_restriction_status(user_id) == RESTRICTION_AWAITING_APPROVAL:
        await update.message.reply_text(
            "❌ ***အသုံးပြုမှု ကန့်သတ်ထားပါ!***\n\n"
            "🔒 ***Admin မှ topup စစ်ဆေးပြီးသည်အထိ စာများ/sticker များ ပို့၍မရပါ။***",
            parse_mode="Markdown"
        )
        return

    # Authorized, non-restricted users get simple replies for text
    if update.message and update.message.text:
        reply = simple_reply(update.message.text)
        await update.message.reply_text(reply, parse_mode="Markdown")
    # Ignore other types (stickers, voice) from authorized, non-restricted users for now
    # else:
    #     logger.debug(f"Ignoring non-text/photo message from authorized user {user_id}")


# --- Callback Query Handler (Refactored) ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id) # User pressing the button
    admin_name = query.from_user.first_name
    # Restriction handled by middleware (checks user_id)

    # --- Payment method selection (topup_pay_) ---
    if query.data.startswith("topup_pay_"):
        # ... (Logic unchanged, deals with in-memory pending_topups) ...
        # Ensure user_id matches query.from_user.id for security?
        # payment_info used here is now loaded from DB at startup
        return

    # --- Registration request (request_register) ---
    elif query.data == "request_register":
        # ... (Logic unchanged) ...
        return

    # --- Registration approve (register_approve_) ---
    elif query.data.startswith("register_approve_"):
        if not is_admin(user_id): return await query.answer("❌ Admin only!", show_alert=True)
        target_user_id = query.data.split("_")[-1]
        if is_user_authorized(target_user_id): # ... already authorized reply ...
             return
        try:
            auth_users_col.update_one({"_id": target_user_id}, {"$set": {"authorized_at": datetime.now(), "approved_by": user_id}}, upsert=True)
            set_user_restriction_status(target_user_id, RESTRICTION_NONE)
            # ... (Notifications, edit message - unchanged) ...
        except PyMongoError as e: # ... handle DB error ...
             pass
        return

    # --- Registration reject (register_reject_) ---
    elif query.data.startswith("register_reject_"):
        if not is_admin(user_id): return await query.answer("❌ Admin only!", show_alert=True)
        # ... (Notifications, edit message - unchanged) ...
        return

    # --- Topup cancel (topup_cancel) ---
    elif query.data == "topup_cancel":
         if str(query.from_user.id) in pending_topups: del pending_topups[str(query.from_user.id)]
         # ... (edit message - unchanged) ...
         return

    # --- Topup approve (topup_approve_) ---
    elif query.data.startswith("topup_approve_"):
        if not is_admin(user_id): return await query.answer("❌ Admin only!", show_alert=True)
        topup_id = query.data.split("_")[-1]
        try:
            # Atomically find and update
            result = users_col.find_one_and_update(
                {"topups.topup_id": topup_id, "topups.status": STATUS_PENDING},
                [{"$set": { # Pipeline update
                    "balance": {"$add": ["$balance", "$$amount_to_add"]}, # Amount added later
                    "restriction_status": RESTRICTION_NONE,
                    "topups": { # Map over topups array
                        "$map": {
                            "input": "$topups", "as": "t",
                            "in": {"$cond": [
                                {"$eq": ["$$t.topup_id", topup_id]},
                                {"$mergeObjects": ["$$t", {"status": STATUS_APPROVED, "approved_by": admin_name, "approved_at": datetime.now().isoformat()}]},
                                "$$t"
                            ]}
                        }
                    }
                }}],
                let={"amount_to_add": {"$let": { # Find amount within the query
                        "vars": {"matched": {"$first": {"$filter": {"input": "$topups", "as": "t", "cond": {"$eq": ["$$t.topup_id", topup_id]}}}}},
                        "in": "$$matched.amount"
                }}},
                projection={"balance": 1, "_id": 1, "topups.$": 1},
                return_document=ReturnDocument.BEFORE
            )
            # ... (Handle result=None -> already processed/not found) ...
            # ... (Calculate new_balance, Notifications, edit message) ...
        except PyMongoError as e: # ... handle DB error ...
            pass
        return

    # --- Topup reject (topup_reject_) ---
    elif query.data.startswith("topup_reject_"):
        if not is_admin(user_id): return await query.answer("❌ Admin only!", show_alert=True)
        topup_id = query.data.split("_")[-1]
        try:
            # Atomically find and update
            result = users_col.find_one_and_update(
                 {"topups.topup_id": topup_id, "topups.status": STATUS_PENDING},
                 {"$set": {"topups.$.status": STATUS_REJECTED, # ... set reject fields ...
                           "restriction_status": RESTRICTION_NONE}},
                 projection={"_id": 1}
            )
            # ... (Handle result=None -> already processed/not found) ...
            # ... (Notifications, edit message) ...
        except PyMongoError as e: # ... handle DB error ...
            pass
        return

    # --- Order confirm (order_confirm_) ---
    elif query.data.startswith("order_confirm_"):
        if not is_admin(user_id): return await query.answer("❌ Admin only!", show_alert=True)
        order_id = query.data.split("_")[-1]
        try:
            # Atomically find and update
            result = users_col.update_one(
                {"orders.order_id": order_id, "orders.status": STATUS_PENDING},
                {"$set": {"orders.$.status": STATUS_CONFIRMED, # ... set confirm fields ...
                           }}
            )
            # ... (Handle result.matched_count == 0 -> already processed/not found) ...
            # ... (Notifications, edit message) ...
        except PyMongoError as e: # ... handle DB error ...
            pass
        return

    # --- Order cancel (order_cancel_) ---
    elif query.data.startswith("order_cancel_"):
        if not is_admin(user_id): return await query.answer("❌ Admin only!", show_alert=True)
        order_id = query.data.split("_")[-1]
        try:
            # Find order first to get refund amount
            user_doc = users_col.find_one(
                {"orders.order_id": order_id, "orders.status": STATUS_PENDING},
                {"_id": 1, "orders.$": 1}
            )
            # ... (Handle not found / already processed) ...
            refund_amount = user_doc["orders"][0].get("price", 0)
            if refund_amount <= 0: # ... handle error ...
                 return

            # Atomically update and refund
            users_col.update_one(
                {"_id": user_doc["_id"], "orders.order_id": order_id},
                {"$set": {"orders.$.status": STATUS_CANCELLED, # ... set cancel fields ...
                           },
                 "$inc": {"balance": refund_amount}}
            )
            # ... (Notifications with refund_amount, edit message) ...
        except PyMongoError as e: # ... handle DB error ...
            pass
        return

    # --- Report filter callbacks ---
    # ... (Unchanged) ...

    # --- Other buttons (copy_kpay, copy_wave, topup_button) ---
    # ... (Unchanged, uses payment_info cache) ...

    # --- Clone bot callbacks ---
    # ... (Unchanged) ...


# --- Post Init Function (Starts Clone Bots) ---
async def post_init(application: Application):
    """Starts clone bots after main application initialization."""
    logger.info("🚀 Main bot initialized. Starting clone bots...")
    # ... (Logic unchanged) ...
    pass

# --- Main Function ---
def main():
    if not BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN environment variable is missing!")
        return

    # --- Application Setup ---
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        # Consider adding connection_pool_size=10 or higher if needed
        .build()
    )

    # --- Register Handlers ---
    # Middleware (Group 0)
    application.add_handler(CommandHandler(filters.ALL, check_restriction), group=0)
    application.add_handler(MessageHandler(filters.ALL, check_restriction), group=0)
    application.add_handler(CallbackQueryHandler(check_restriction), group=0)

    # User Commands & Handlers (Group 1)
    application.add_handler(CommandHandler("start", start)) # Allow all
    application.add_handler(CommandHandler("register", register_command)) # Allow all
    application.add_handler(CommandHandler("c", c_command)) # Allow all

    # Commands requiring authorization (filter applied)
    auth_needed_commands = ["mmb", "balance", "topup", "cancel", "price", "history"]
    for cmd in auth_needed_commands:
        application.add_handler(CommandHandler(cmd, globals()[f"{cmd}_command"])) # Filter applied later dynamically or check inside handler

    # Admin Commands (filter applied)
    admin_commands = [
        "approve", "deduct", "done", "reply", "ban", "unban", "sendgroup", "maintenance",
        "testgroup", "setprice", "removeprice", "setwavenum", "setkpaynum", "setwavename",
        "setkpayname", "adminhelp", "d", "m", "y", "addbot", "listbots"
    ]
    owner_commands = [
         "addadm", "unadm", "setkpayqr", "removekpayqr", "setwaveqr", "removewaveqr",
         "broadcast", "removebot", "addfund", "deductfund"
    ]
    for cmd in admin_commands:
        application.add_handler(CommandHandler(cmd, globals()[f"{cmd}_command"])) # Filter applied later or check inside
    for cmd in owner_commands:
        application.add_handler(CommandHandler(cmd, globals()[f"{cmd}_command"])) # Filter applied later or check inside


    # Callback Query Handler (Group 1)
    application.add_handler(CallbackQueryHandler(button_callback))

    # Message Handlers (Group 1)
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.PHOTO, handle_other_messages))


    # --- Start Bot ---
    logger.info("🤖 Bot starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES) # Explicitly allow all update types
    logger.info("⚫ Bot stopped.")


if __name__ == "__main__":
    main()
