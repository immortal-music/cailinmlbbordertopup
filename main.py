# --- main.py ---
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
    db_name_from_uri = MongoClient(MONGODB_URL).get_database().name
    db_name = db_name_from_uri if db_name_from_uri != 'test' else 'mlbb_bot_db'
    db = client[db_name]

    logger.info(f"Using MongoDB database: {db_name}")

    # Collections
    users_col = db["users"]
    admins_col = db["admins"]
    auth_users_col = db["authorized_users"]
    prices_col = db["prices"]
    config_col = db["config"]

    client.admin.command('ping')
    logger.info("✅ MongoDB connected successfully!")

    # --- Create Indexes ---
    logger.info("Applying MongoDB indexes...")
    try:
        users_col.create_index([("topups.topup_id", 1)], unique=True, sparse=True, background=True)
        users_col.create_index([("orders.order_id", 1)], unique=True, sparse=True, background=True)
        users_col.create_index([("topups.status", 1)], background=True)
        users_col.create_index([("orders.status", 1)], background=True)
        users_col.create_index([("orders.confirmed_at", 1)], background=True)
        users_col.create_index([("topups.approved_at", 1)], background=True)
        users_col.create_index([("restriction_status", 1)], background=True)
        auth_users_col.create_index([("_id", 1)], background=True)
        admins_col.create_index([("_id", 1)], background=True)
        prices_col.create_index([("_id", 1)], background=True)
        config_col.create_index([("_id", 1)], background=True)
        logger.info("✅ MongoDB indexes checked/applied.")
    except PyMongoError as index_e: logger.warning(f"⚠️ Could not apply all MongoDB indexes: {index_e}.")


    # --- Initialize Config from DB ---
    maintenance_doc = config_col.find_one({"_id": CONFIG_MAINTENANCE})
    if not maintenance_doc:
        bot_maintenance = {"orders": True, "topups": True, "general": True}
        config_col.insert_one({"_id": CONFIG_MAINTENANCE, "settings": bot_maintenance})
        logger.info("Initialized default maintenance settings in DB.")
    else:
        bot_maintenance = {k: maintenance_doc.get("settings", {}).get(k, True) for k in ["orders", "topups", "general"]}
        logger.info(f"Loaded maintenance settings from DB: {bot_maintenance}")

    payment_doc = config_col.find_one({"_id": CONFIG_PAYMENT_INFO})
    default_payment_info = {"kpay_number": "Not Set", "kpay_name": "Not Set", "kpay_image": None, "wave_number": "Not Set", "wave_name": "Not Set", "wave_image": None}
    if not payment_doc:
        payment_info = default_payment_info
        config_col.insert_one({"_id": CONFIG_PAYMENT_INFO, "details": payment_info})
        logger.info("Initialized default payment info in DB.")
    else:
        db_details = payment_doc.get("details", {})
        payment_info = {key: db_details.get(key, default_payment_info[key]) for key in default_payment_info}
        logger.info(f"Loaded payment info from DB (Numbers: KPay={payment_info['kpay_number']}, Wave={payment_info['wave_number']})")

    admins_col.update_one({"_id": ADMIN_ID}, {"$set": {"is_owner": True}}, upsert=True)
    auth_users_col.update_one({"_id": str(ADMIN_ID)}, {"$set": {"authorized_at": datetime.now()}}, upsert=True)

except ConnectionFailure: logger.critical("❌ MongoDB connection failed."); exit(1)
except Exception as e: logger.critical(f"❌ MongoDB setup error: {e}", exc_info=True); exit(1)


# --- In-Memory State ---
pending_topups = {}

# --- Helper Functions ---
def is_owner(user_id): return int(user_id) == ADMIN_ID
def is_admin(user_id):
    if int(user_id) == ADMIN_ID: return True
    try: return admins_col.count_documents({"_id": int(user_id)}) > 0
    except PyMongoError as e: logger.error(f"DB Error checking admin status for {user_id}: {e}"); return False
def is_user_authorized(user_id):
    if int(user_id) == ADMIN_ID: return True
    try: return auth_users_col.count_documents({"_id": str(user_id)}) > 0
    except PyMongoError as e: logger.error(f"DB Error checking auth status for {user_id}: {e}"); return False
def get_user_restriction_status(user_id):
    try: user_doc = users_col.find_one({"_id": str(user_id)}, {"restriction_status": 1}); return user_doc.get("restriction_status", RESTRICTION_NONE) if user_doc else RESTRICTION_NONE
    except PyMongoError as e: logger.error(f"DB Error get restriction status {user_id}: {e}"); return RESTRICTION_NONE
def set_user_restriction_status(user_id, status):
    try: logger.info(f"Setting restriction {user_id} to {status}"); users_col.update_one({"_id": str(user_id)}, {"$set": {"restriction_status": status}}, upsert=True); return True
    except PyMongoError as e: logger.error(f"DB Error set restriction status {user_id} to {status}: {e}"); return False
def load_prices():
    prices = {};
    try:
        for doc in prices_col.find({}, {"_id": 1, "price": 1}):
             if "price" in doc: prices[doc["_id"]] = doc["price"]
    except PyMongoError as e: logger.error(f"DB Error loading prices: {e}")
    return prices
def get_price(diamonds):
    custom = load_prices();
    if diamonds in custom: return custom[diamonds]
    if diamonds.startswith("wp") and diamonds[2:].isdigit(): n = int(diamonds[2:]); return n * 6000 if 1 <= n <= 10 else None
    table = {"11": 950, "22": 1900, "33": 2850, "56": 4200, "112": 8200, "86": 5100,"172": 10200, "257": 15300, "343": 20400, "429": 25500, "514": 30600,"600": 35700, "706": 40800, "878": 51000, "963": 56100, "1049": 61200,"1135": 66300, "1412": 81600, "2195": 122400, "3688": 204000,"5532": 306000, "9288": 510000, "12976": 714000, "55": 3500,"165": 10000, "275": 16000, "565": 33000 }
    return table.get(diamonds)
async def check_pending_topup(user_id):
    try: return users_col.count_documents({"_id": str(user_id), "topups.status": STATUS_PENDING}) > 0
    except PyMongoError as e: logger.error(f"DB Error checking pending topup {user_id}: {e}"); return False
def get_all_admin_ids():
    try: return [doc["_id"] for doc in admins_col.find({}, {"_id": 1})]
    except PyMongoError as e: logger.error(f"DB Error fetching admin IDs: {e}"); return [ADMIN_ID]
def get_authorized_user_count():
    try: return auth_users_col.count_documents({})
    except PyMongoError as e: logger.error(f"DB Error counting auth users: {e}"); return 0
def get_maintenance_status(feature): return bot_maintenance.get(feature, True)
def set_maintenance_status(feature, status: bool):
    try:
        r = config_col.update_one({"_id": CONFIG_MAINTENANCE}, {"$set": {f"settings.{feature}": status}}, upsert=True)
        if r.acknowledged: bot_maintenance[feature] = status; logger.info(f"Maintenance '{feature}' set to {status}"); return True
        else: logger.error(f"DB update ack failed for maintenance '{feature}'"); return False
    except PyMongoError as e: logger.error(f"DB Error set maintenance {feature} to {status}: {e}"); return False
def get_payment_info(): return payment_info
def update_payment_info(key, value):
    try:
        r = config_col.update_one({"_id": CONFIG_PAYMENT_INFO}, {"$set": {f"details.{key}": value}}, upsert=True)
        if r.acknowledged: payment_info[key] = value; logger.info(f"Payment info '{key}' updated."); return True
        else: logger.error(f"DB update ack failed for payment info '{key}'"); return False
    except PyMongoError as e: logger.error(f"DB Error update payment key '{key}': {e}"); return False
async def is_bot_admin_in_group(bot: Bot, chat_id: int):
    if not chat_id or not isinstance(chat_id, int) or chat_id == 0: logger.warning(f"is_bot_admin_in_group invalid chat_id: {chat_id}."); return False
    try: me = await bot.get_me(); bm = await bot.get_chat_member(chat_id, me.id); is_admin = bm.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]; logger.debug(f"Bot admin check group {chat_id}: {is_admin}, status: {bm.status}"); return is_admin
    except Exception as e: logger.error(f"Error check bot admin status group {chat_id}: {e}"); return False
def simple_reply(msg: str) -> str:
    ml = msg.lower(); g = ["hello","hi","မင်္ဂလာပါ","ဟယ်လို","ဟိုင်း","ကောင်းလား"]; h = ["help","ကူညီ","အကူအညီ","မသိ","လမ်းညွှန်"]
    if any(w in ml for w in g): return ("👋 မင်္ဂလာပါ! JB MLBB AUTO TOP UP BOT မှ ကြိုဆိုပါတယ်!\n\n📱 /start နှိပ်ပါ\n")
    elif any(w in ml for w in h): return ("📱 ***Commands:***\n\n• /start\n• /mmb gameid serverid amount\n• /balance\n• /topup amount\n• /price\n• /history\n\n💡 Admin ကို ဆက်သွယ်ပါ။")
    else: return ("📱 ***MLBB Diamond Bot***\n\n💎 /mmb\n💰 /price\n🆘 /start")
def validate_game_id(gid: str) -> bool: return gid.isdigit() and 6 <= len(gid) <= 10
def validate_server_id(sid: str) -> bool: return sid.isdigit() and 3 <= len(sid) <= 5
def is_banned_account(gid: str) -> bool: b=["123456789","000000000","111111111"]; return gid in b or len(set(gid))==1 or gid.startswith("000") or gid.endswith("000")
def is_payment_screenshot(upd: Update) -> bool: return upd.message and upd.message.photo

# --- Message Sending Helpers ---
async def send_pending_topup_warning(update: Update): await update.effective_message.reply_text("⏳ ***Pending Topup ရှိနေ!***\n\n❌ Admin approve မလုပ်မချင်း စောင့်ပါ။\n📞 Admin ကို ဆက်သွယ်ပါ။\n💡 /balance", parse_mode=ParseMode.MARKDOWN)
async def send_maintenance_message(update: Update, cmd_type: str): ftxt = {"orders":"အော်ဒါ","topups":"ငွေဖြည့်","general":"Bot"}.get(cmd_type,"Bot"); uname = update.effective_user.first_name or "User"; msg=f"👋 {uname}!\n\n⏸️ ***{ftxt}အား ခေတ္တပိတ်ထားပါသည်*** ⏸️\n🔄 Admin ဖွင့်မှ သုံးနိုင်မည်။\n📞 Admin ကို ဆက်သွယ်ပါ။"; await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# --- Middleware ---
async def check_restriction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user;
    if not user: return
    user_id = str(user.id); query = update.callback_query
    if is_admin(user_id):
        is_admin_action = False; cmd_or_data = "";
        admin_cmds = ['/approve','/deduct','/done','/reply','/ban','/unban','/addadm','/unadm','/sendgroup','/maintenance','/testgroup','/setprice','/removeprice','/setwavenum','/setkpaynum','/setwavename','/setkpayname','/setkpayqr','/removekpayqr','/setwaveqr','/removewaveqr','/adminhelp','/broadcast','/d','/m','/y']
        admin_cb_prefixes = ['topup_approve_','topup_reject_','order_confirm_','order_cancel_','register_approve_','register_reject_','report_']
        if update.message and update.message.text and update.message.text.startswith('/'): cmd_or_data = update.message.text.split()[0].lower(); is_admin_action = cmd_or_data in admin_cmds
        elif query and any(query.data.startswith(p) for p in admin_cb_prefixes): is_admin_action = True
        if is_admin_action: logger.debug(f"Admin {user_id} action, bypassing restriction."); return
    restriction = get_user_restriction_status(user_id)
    if restriction == RESTRICTION_AWAITING_APPROVAL:
        logger.info(f"User {user_id} restricted. Blocking."); msg = ("❌ ***အသုံးပြုမှု ကန့်သတ်ထား!***\n\n🔒 Admin စစ်ဆေးနေဆဲ။ Approve/Reject မလုပ်မချင်း အခြား Commands/Buttons သုံးမရပါ။\n\n⏰ ဆောင်ရွက်ပြီးပါက ပြန်သုံးနိုင်မည်။\n📞 Admin ကို ဆက်သွယ်ပါ။")
        try:
            if query: await query.answer("❌ အသုံးပြုမှု ကန့်သတ်ထား! Admin စောင့်ပါ။", show_alert=True)
            elif update.message: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed sending restriction notice to {user_id}: {e}")
        raise ApplicationHandlerStop
    logger.debug(f"User {user_id} restriction check passed ({restriction}).")


# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = str(user.id); username = user.username or "-"; name = f"{user.first_name} {user.last_name or ''}".strip()
    if not is_user_authorized(user_id):
        kbd = [[InlineKeyboardButton("📝 Register တောင်းဆိုမယ်", callback_data="request_register")]]; markup = InlineKeyboardMarkup(kbd)
        await update.message.reply_text(f"🚫 ***Bot အသုံးပြုခွင့် မရှိပါ!***\n\n👋 `{name}`!\n🆔 ID: `{user_id}`\n\n❌ အသုံးပြုခွင့် တောင်းဆိုရန် Button နှိပ်ပါ သို့မဟုတ် /register ။", parse_mode=ParseMode.MARKDOWN, reply_markup=markup); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    try: users_col.find_one_and_update({"_id": user_id}, {"$setOnInsert": {"balance": 0,"orders": [],"topups": [],"restriction_status": RESTRICTION_NONE}}, {"$set": {"name": name,"username": username}}, upsert=True,)
    except PyMongoError as e: logger.error(f"DB Error upsert user {user_id}: {e}"); await update.message.reply_text("❌ DB error."); return
    if user_id in pending_topups: del pending_topups[user_id]
    cname = f"[{name}](tg://user?id={user_id})"; msg = (f"👋 ***မင်္ဂလာပါ*** {cname}!\n🆔 ID: `{user_id}`\n\n💎 ***JB MLBB BOT***\n\n➤ /mmb gameid serverid amount\n➤ /balance\n➤ /topup amount\n➤ /price\n➤ /history\n\n📌 ဥပမာ:\n`/mmb 123 456 86`")
    try: photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1); pid = photos.photos[0][0].file_id if photos.total_count > 0 else None
        if pid: await context.bot.send_photo(update.effective_chat.id, pid, caption=msg, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.warning(f"Error get/send photo {user_id}: {e}"); await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def mmb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = str(user.id)
    if not get_maintenance_status("orders"): await send_maintenance_message(update, "orders"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ***Topup အရင်ပြီးဆုံးပါ! Screenshot တင်/ /cancel ။***", parse_mode=ParseMode.MARKDOWN); return
    args = context.args
    if len(args)!=3: await update.message.reply_text("❌ Format: `/mmb gameid serverid amount`", parse_mode=ParseMode.MARKDOWN); return
    gid, sid, amt = args
    if not validate_game_id(gid): await update.message.reply_text("❌ Game ID မှား။"); return
    if not validate_server_id(sid): await update.message.reply_text("❌ Server ID မှား။"); return
    if is_banned_account(gid): await update.message.reply_text(f"🚫 Account Ban:\nID: `{gid}`\nServer: `{sid}`\n❌ Topup မရ။", parse_mode=ParseMode.MARKDOWN); return # Notify admin omitted for brevity
    price = get_price(amt)
    if not price: await update.message.reply_text(f"❌ Amount `{amt}` မရ။ /price ကြည့်ပါ။"); return
    try: udata = users_col.find_one({"_id": user_id}, {"balance": 1}); ubal = udata.get("balance", 0) if udata else 0
    except PyMongoError as e: logger.error(f"DB Error get balance {user_id}: {e}"); await update.message.reply_text("❌ DB error."); return
    if ubal < price: kbd=[[InlineKeyboardButton("💳 ငွေဖြည့်မယ်", callback_data="topup_button")]]; await update.message.reply_text(f"❌ Balance မလောက်!\n💰 လိုအပ်: {price:,} MMK\n💳 လက်ကျန်: {ubal:,} MMK", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kbd)); return
    oid = f"ORD{datetime.now().strftime('%y%m%d%H%M%S%f')[:-3]}{user_id[-2:]}"; order = {"order_id":oid,"game_id":gid,"server_id":sid,"amount":amt,"price":price,"status":STATUS_PENDING,"timestamp":datetime.now().isoformat(),"user_id":user_id,"chat_id":update.effective_chat.id,"user_name":user.first_name}
    try:
        r = users_col.update_one({"_id": user_id}, {"$inc": {"balance": -price}, "$push": {"orders": order}})
        if not r.modified_count: await update.message.reply_text("❌ Order error. Try again."); return
        udata_new = users_col.find_one({"_id": user_id}, {"balance": 1}); nbal = udata_new.get("balance", ubal - price)
    except PyMongoError as e: logger.error(f"DB Error process order {user_id}: {e}"); await update.message.reply_text("❌ DB error order."); return
    kbd=[[InlineKeyboardButton("✅ Confirm", callback_data=f"order_confirm_{oid}"), InlineKeyboardButton("❌ Cancel", callback_data=f"order_cancel_{oid}")]]; markup=InlineKeyboardMarkup(kbd); umention = user.mention_markdown()
    amsg = (f"🔔 ***Order!***\n📝 ID: `{oid}`\n👤 User: {umention} (`{user_id}`)\n🎮 ID: `{gid}`\n🌐 Server: `{sid}`\n💎 Amt: {amt}\n💰 Price: {price:,} MMK\n⏰ {datetime.now():%H:%M:%S}\n📊 ⏳ {STATUS_PENDING}")
    aids = get_all_admin_ids()
    for aid in aids:
        try: await context.bot.send_message(aid, amsg, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except Exception as e: logger.warning(f"Failed sending order notif admin {aid}: {e}")
    if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
        try: gmsg=(f"🛒 ***Order!***\n📝 ID: `{oid}`\n👤 User: {umention}\n🎮 ID: `{gid}`\n🌐 Server: `{sid}`\n💎 Amt: {amt}\n💰 Price: {price:,} MMK\n📊 ⏳ {STATUS_PENDING}\n#NewOrder"); await context.bot.send_message(ADMIN_GROUP_ID, gmsg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed sending order notif group {ADMIN_GROUP_ID}: {e}")
    await update.message.reply_text(f"✅ ***Order OK!***\n📝 ID: `{oid}`\n🎮 ID: `{gid}`\n🌐 Server: `{sid}`\n💎 Diamond: {amt}\n💰 Cost: {price:,} MMK\n💳 Balance: {nbal:,} MMK\n📊 ⏳ {STATUS_PENDING}\n\n⚠️ Admin confirm လုပ်မှ ရမည်။", parse_mode=ParseMode.MARKDOWN)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ Topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return
    try:
        udata = users_col.find_one({"_id": user_id})
        if not udata: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return
        bal=udata.get("balance",0); ocount=len(udata.get("orders",[])); tcount=len(udata.get("topups",[])); ptlist=[t for t in udata.get("topups",[]) if t.get("status")==STATUS_PENDING]; ptcount=len(ptlist); pamt=sum(t.get("amount",0) for t in ptlist)
        name=udata.get('name','?').replace('*','').replace('_','').replace('`',''); uname=udata.get('username','None').replace('*','').replace('_','').replace('`','')
        smsg=f"\n⏳ ***Pending Topups***: {ptcount} ({pamt:,} MMK)\n❗ ***Admin approve စောင့်ပါ။***" if ptcount > 0 else ""; kbd=[[InlineKeyboardButton("💳 ငွေဖြည့်မယ်", callback_data="topup_button")]]; btxt=(f"💳 ***Account***\n\n💰 ***Balance***: `{bal:,} MMK`\n📦 Orders: {ocount}\n💸 Topups: {tcount}{smsg}\n\n👤 Name: {name}\n🆔 User: @{uname}")
        try: photos=await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1); pid=photos.photos[0][0].file_id if photos.total_count>0 else None; markup=InlineKeyboardMarkup(kbd)
            if pid: await context.bot.send_photo(update.effective_chat.id, pid, caption=btxt, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            else: await update.message.reply_text(btxt, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except Exception as e: logger.warning(f"Error sending bal w/ photo: {e}"); await update.message.reply_text(btxt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kbd))
    except PyMongoError as e: logger.error(f"DB Error get balance {user_id}: {e}"); await update.message.reply_text("❌ DB error.")


async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not get_maintenance_status("topups"): await send_maintenance_message(update, "topups"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ယခင် topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return
    if not context.args or len(context.args)!=1: await update.message.reply_text("❌ Format: `/topup <amount>`\nဥပမာ: `/topup 5000`"); return
    try: amount=int(context.args[0]); assert amount>=1000
    except (ValueError, AssertionError): await update.message.reply_text("❌ Amount မှား (>= 1000)။"); return
    pending_topups[user_id] = {"amount": amount, "timestamp": datetime.now().isoformat()}
    kbd=[[InlineKeyboardButton("📱 KBZ Pay", callback_data=f"topup_pay_kpay_{amount}")], [InlineKeyboardButton("📱 Wave Money", callback_data=f"topup_pay_wave_{amount}")], [InlineKeyboardButton("❌ Cancel", callback_data="topup_cancel")]]; markup=InlineKeyboardMarkup(kbd)
    await update.message.reply_text(f"💳 ***ငွေဖြည့်ရန်***\n💰 Amount: `{amount:,} MMK`\n\n⬇️ Payment method ရွေးပါ:", parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in pending_topups: await update.message.reply_text("⏳ Topup process ကို အရင်ပြီးအောင်လုပ်ပါ...", parse_mode=ParseMode.MARKDOWN); return
    custom = load_prices(); default = {"wp1":6000,"wp2":12000,"wp3":18000,"wp4":24000,"wp5":30000,"wp6":36000,"wp7":42000,"wp8":48000,"wp9":54000,"wp10":60000,"11":950,"22":1900,"33":2850,"56":4200,"86":5100,"112":8200,"172":10200,"257":15300,"343":20400,"429":25500,"514":30600,"600":35700,"706":40800,"878":51000,"963":56100,"1049":61200,"1135":66300,"1412":81600,"2195":122400,"3688":204000,"5532":306000,"9288":510000,"12976":714000,"55":3500,"165":10000,"275":16000,"565":33000}; current={**default,**custom}
    pmsg="💎 ***MLBB ဈေးနှုန်းများ***\n\n🎟️ ***WP***:\n"; [pmsg := pmsg + f"• wp{i} = {current.get(f'wp{i}','N/A'):,} MMK\n" for i in range(1,11)]; pmsg+="\n💎 ***Regular DM***:\n"; reg=["11","22","33","56","86","112","172","257","343","429","514","600","706","878","963","1049","1135","1412","2195","3688","5532","9288","12976"]; [pmsg := pmsg + f"• {d} = {current.get(d,'N/A'):,} MMK\n" for d in reg]; pmsg+="\n💎 ***2X Pass***:\n"; dbl=["55","165","275","565"]; [pmsg := pmsg + f"• {d} = {current.get(d,'N/A'):,} MMK\n" for d in dbl]
    other={k:v for k,v in custom.items() if k not in default};
    if other: pmsg+="\n🔥 ***Special***:\n"; [pmsg := pmsg + f"• {i} = {p:,} MMK\n" for i,p in sorted(other.items())]
    pmsg+="\n\n***📝 Usage***:\n`/mmb gameid serverid amount`"; await update.message.reply_text(pmsg, parse_mode=ParseMode.MARKDOWN)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in pending_topups: del pending_topups[user_id]; await update.message.reply_text("✅ ***ငွေဖြည့်ခြင်း ပယ်ဖျက်ပါပြီ!***", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("ℹ️ လက်ရှိ ငွေဖြည့်မှု မရှိပါ။", parse_mode=ParseMode.MARKDOWN)


async def c_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("🧮 `/c <expression>`"); return
    expr = ''.join(context.args).strip(); allowed = set("0123456789+-*/(). ");
    if not all(c in allowed for c in expr): await update.message.reply_text("❌ Invalid chars."); return
    try: result = eval(expr.replace(' ','')); await update.message.reply_text(f"🧮 Result:\n`{expr}` = ***{result:,}***", parse_mode=ParseMode.MARKDOWN)
    except ZeroDivisionError: await update.message.reply_text("❌ Div by zero.")
    except Exception as e: logger.warning(f"Calc err '{expr}': {e}"); await update.message.reply_text("❌ Expression မှား။")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ Topup အရင်ပြီးအောင်လုပ်ပါ..."); return
    try:
        udata = users_col.find_one({"_id": user_id}, {"orders": {"$slice": -5}, "topups": {"$slice": -5}})
        if not udata: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return
        orders = udata.get("orders", []); topups = udata.get("topups", [])
        if not orders and not topups: await update.message.reply_text("📋 မှတ်တမ်း မရှိပါ။"); return
        msg = "📋 ***မှတ်တမ်းများ***\n\n"; smap_o = {STATUS_PENDING:"⏳", STATUS_CONFIRMED:"✅", STATUS_CANCELLED:"❌"}
        if orders: msg += "🛒 Orders (Last 5):\n"; [msg := msg + f"{smap_o.get(o.get('status', '?'), '?')} `{o.get('order_id', 'N/A')}` ({o.get('amount','?')}💎/{o.get('price',0):,}K) [{datetime.fromisoformat(o['timestamp']).strftime('%y-%m-%d %H:%M') if o.get('timestamp') else 'N/A'}]\n" for o in reversed(orders)]
        smap_t = {STATUS_PENDING:"⏳", STATUS_APPROVED:"✅", STATUS_REJECTED:"❌"}
        if topups: msg += "\n💳 Topups (Last 5):\n"; [msg := msg + f"{smap_t.get(t.get('status', '?'), '?')} {t.get('amount', 0):,} MMK [{datetime.fromisoformat(t['timestamp']).strftime('%y-%m-%d %H:%M') if t.get('timestamp') else 'N/A'}]\n" for t in reversed(topups)]
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error get history {user_id}: {e}"); await update.message.reply_text("❌ DB error.")
    except Exception as e: logger.error(f"Error format history {user_id}: {e}"); await update.message.reply_text("❌ Error display history.")

# --- START Admin Commands ---
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user = update.effective_user; admin_id = str(admin_user.id); admin_name = admin_user.first_name
    if not is_admin(admin_id): return
    if len(context.args) != 2: await update.message.reply_text("❌ Format: `/approve <user_id> <amount>`"); return
    target_user_id, amount_str = context.args
    try: amount = int(amount_str)
    except ValueError: await update.message.reply_text("❌ Amount ကို ဂဏန်းဖြင့် ထည့်ပါ။"); return
    try:
        user_doc = users_col.find_one({"_id": target_user_id, "topups": {"$elemMatch": {"amount": amount, "status": STATUS_PENDING}}}, {"topups.$": 1})
        if not user_doc or not user_doc.get("topups"): await update.message.reply_text(f"❌ `{target_user_id}` ထံမှ `{amount}` MMK pending topup မတွေ့ပါ။"); return
        topup_id_to_approve = user_doc["topups"][0].get("topup_id")
        result = users_col.find_one_and_update(
            {"_id": target_user_id, "topups.topup_id": topup_id_to_approve, "topups.status": STATUS_PENDING},
            {"$set": {"topups.$.status": STATUS_APPROVED, "topups.$.approved_by": admin_name, "topups.$.approved_at": datetime.now().isoformat(), "restriction_status": RESTRICTION_NONE}, "$inc": {"balance": amount}},
            projection={"balance": 1}, return_document=ReturnDocument.BEFORE)
        if result is None: await update.message.reply_text("⚠️ Topup ကို အခြား Admin လုပ်ဆောင်သွားပြီး/မတွေ့ ဖြစ်နိုင်ပါသည်။"); return
        old_balance = result.get("balance", 0); new_balance = old_balance + amount
        try:
            kbd = [[InlineKeyboardButton("💎 Order တင်မယ်", url=f"https://t.me/{context.bot.username}?start=order")]]
            await context.bot.send_message(int(target_user_id), (f"✅ ***Topup Approved!*** 🎉\n💰 Amount: `{amount:,} MMK`\n💳 Balance: `{new_balance:,} MMK`\n👤 By: {admin_name}\n\n🔓 Bot ပြန်သုံးနိုင်ပါပြီ!"), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kbd))
        except Exception as e: logger.warning(f"Failed notify user {target_user_id} approve: {e}")
        await update.message.reply_text(f"✅ Approve OK!\n👤 ID: `{target_user_id}`\n💰 Amt: `{amount:,} MMK`\n💳 New bal: `{new_balance:,} MMK`", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error approve {target_user_id}: {e}"); await update.message.reply_text("❌ DB error.")

async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = str(update.effective_user.id);
    if not is_admin(admin_id): return
    if len(context.args) != 2: await update.message.reply_text("❌ Format: `/deduct <user_id> <amount>`"); return
    target_user_id, amount_str = context.args
    try: amount = int(amount_str); assert amount > 0
    except (ValueError, AssertionError): await update.message.reply_text("❌ Amount မှား။"); return
    try:
        result = users_col.find_one_and_update({"_id": target_user_id, "balance": {"$gte": amount}}, {"$inc": {"balance": -amount}}, projection={"balance": 1}, return_document=ReturnDocument.AFTER)
        if result is None: uexists = users_col.find_one({"_id": target_user_id}, {"balance": 1}); await update.message.reply_text(f"❌ Balance မလောက်! ({uexists.get('balance',0):,} MMK)" if uexists else "❌ User မတွေ့!"); return
        nbal = result.get("balance")
        try: await context.bot.send_message(int(target_user_id), f"⚠️ ***Balance နှုတ်ခံရမှု***\n💰 Amount: `{amount:,} MMK`\n💳 Balance: `{nbal:,} MMK`", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed notify user {target_user_id} deduct: {e}")
        await update.message.reply_text(f"✅ Deduct OK!\n👤 ID: `{target_user_id}`\n💰 Amt: `{amount:,} MMK`\n💳 Balance: `{nbal:,} MMK`", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error deduct {target_user_id}: {e}"); await update.message.reply_text("❌ DB error.")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/done <user_id>`"); return
    tid = int(context.args[0])
    try: await context.bot.send_message(tid, "🙏 ဝယ်ယူမှု ကျေးဇူးပါ။\n✅ Order Done! 🎉"); await update.message.reply_text("✅ Done msg sent.")
    except Exception as e: logger.warning(f"Failed send done {tid}: {e}"); await update.message.reply_text("❌ User ID မှား/Bot blocked.")

async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)<2 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/reply <user_id> <message>`"); return
    tid, msg = int(context.args[0]), " ".join(context.args[1:])
    try: await context.bot.send_message(tid, f"✉️ ***Admin Reply:***\n\n{msg}", parse_mode=ParseMode.MARKDOWN); await update.message.reply_text("✅ Reply sent.")
    except Exception as e: logger.warning(f"Failed send reply {tid}: {e}"); await update.message.reply_text("❌ Msg မပို့နိုင်ပါ။")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user=update.effective_user; aid=str(admin_user.id); aname=admin_user.first_name
    if not is_admin(aid): return
    if len(context.args)!=1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/ban <user_id>`"); return
    tid = context.args[0]
    if int(tid)==ADMIN_ID: await update.message.reply_text("❌ Owner ကို ban မရ။"); return
    try:
        ra = auth_users_col.delete_one({"_id": tid})
        if ra.deleted_count==0: await update.message.reply_text("ℹ️ User authorized မလုပ်ထား/ban ပြီးသား။"); return
        set_user_restriction_status(tid, RESTRICTION_NONE)
        udoc=users_col.find_one({"_id": tid}, {"name": 1}); tname=udoc.get("name","?") if udoc else "?"
        try: await context.bot.send_message(int(tid), "🚫 Bot အသုံးပြုခွင့် ပိတ်ပင်ခံရမှု\nAdmin က ban လုပ်လိုက်ပါပြီ။", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed ban notif user {tid}: {e}")
        if int(aid)!=ADMIN_ID: try: await context.bot.send_message(ADMIN_ID, f"🚫 User Ban by Admin:\nBanned: [{tname}](tg://user?id={tid}) (`{tid}`)\nBy: {admin_user.mention_markdown()}", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed ban notif owner: {e}")
        if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID): try: await context.bot.send_message(ADMIN_GROUP_ID, f"🚫 User Banned:\nUser: [{tname}](tg://user?id={tid})\nBy: {aname}\n#UserBanned", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed ban notif group: {e}")
        await update.message.reply_text(f"✅ User Ban OK!\n👤 ID: `{tid}`\n📊 Total: {get_authorized_user_count()}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error ban {tid}: {e}"); await update.message.reply_text("❌ DB error.")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user=update.effective_user; aid=str(admin_user.id); aname=admin_user.first_name
    if not is_admin(aid): return
    if len(context.args)!=1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/unban <user_id>`"); return
    tid = context.args[0]
    if is_user_authorized(tid): await update.message.reply_text("ℹ️ User authorized လုပ်ထားပြီးသား။"); return
    try:
        auth_users_col.update_one({"_id": tid}, {"$set": {"authorized_at": datetime.now(), "unbanned_by": aid}}, upsert=True)
        set_user_restriction_status(tid, RESTRICTION_NONE)
        udoc=users_col.find_one({"_id": tid}, {"name": 1}); tname=udoc.get("name","?") if udoc else "?"
        try: await context.bot.send_message(int(tid), "🎉 *Bot အသုံးပြုခွင့် ပြန်ရပါပြီ!*\nAdmin က ban ဖြုတ်ပေးပါပြီ။ /start နှိပ်ပါ။", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed unban notif user {tid}: {e}")
        if int(aid)!=ADMIN_ID: try: await context.bot.send_message(ADMIN_ID, f"✅ User Unban by Admin:\nUnbanned: [{tname}](tg://user?id={tid}) (`{tid}`)\nBy: {admin_user.mention_markdown()}", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed unban notif owner: {e}")
        if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID): try: await context.bot.send_message(ADMIN_GROUP_ID, f"✅ User Unbanned:\nUser: [{tname}](tg://user?id={tid})\nBy: {aname}\n#UserUnbanned", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed unban notif group: {e}")
        await update.message.reply_text(f"✅ User Unban OK!\n👤 ID: `{tid}`\n📊 Total: {get_authorized_user_count()}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error unban {tid}: {e}"); await update.message.reply_text("❌ DB error.")

async def addadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if len(context.args)!=1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/addadm <user_id>`"); return
    nid = int(context.args[0])
    if is_admin(nid): await update.message.reply_text("ℹ️ User သည် admin ဖြစ်ပြီးသား။"); return
    try: admins_col.update_one({"_id": nid}, {"$set": {"is_owner": False,"added_by": ADMIN_ID,"added_at": datetime.now()}}, upsert=True)
        try: await context.bot.send_message(nid, "🎉 Admin ရာထူးရ!\nOwner က Admin ခန့်အပ်ပါပြီ။ /adminhelp ကြည့်ပါ။")
        except Exception as e: logger.warning(f"Failed addadm notif {nid}: {e}")
        await update.message.reply_text(f"✅ Admin Added!\n👤 ID: `{nid}`\n📊 Total: {admins_col.count_documents({})}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error add admin {nid}: {e}"); await update.message.reply_text("❌ DB error.")

async def unadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if len(context.args)!=1 or not context.args[0].isdigit(): await update.message.reply_text("❌ Format: `/unadm <user_id>`"); return
    tid = int(context.args[0])
    if tid==ADMIN_ID: await update.message.reply_text("❌ Owner ကို ဖြုတ်မရ။"); return
    try: r=admins_col.delete_one({"_id": tid});
        if r.deleted_count==0: await update.message.reply_text("ℹ️ User admin မဟုတ်ပါ။"); return
        try: await context.bot.send_message(tid, "⚠️ Admin ရာထူး ရုပ်သိမ်း!\nOwner က ဖြုတ်လိုက်ပါပြီ။")
        except Exception as e: logger.warning(f"Failed unadm notif {tid}: {e}")
        await update.message.reply_text(f"✅ Admin Removed!\n👤 ID: `{tid}`\n📊 Total: {admins_col.count_documents({})}", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error remove admin {tid}: {e}"); await update.message.reply_text("❌ DB error.")

async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=2: await update.message.reply_text("❌ Format: `/maintenance <orders|topups|general> <on|off>`"); return
    feat, sstr = context.args[0].lower(), context.args[1].lower()
    if feat not in ["orders","topups","general"] or sstr not in ["on","off"]: await update.message.reply_text("❌ Invalid args."); return
    sbool = (sstr == "on")
    if set_maintenance_status(feat, sbool): stxt="🟢 Enabled" if sbool else "🔴 Disabled"; ftxt={"orders":"Orders","topups":"Topups","general":"General"}.get(feat); cstat="\n".join([f"• {f.capitalize()}: {'🟢' if bot_maintenance[f] else '🔴'}" for f in bot_maintenance]); await update.message.reply_text(f"✅ Maint Updated!\n🔧 Feat: {ftxt}\n📊 Status: {stxt}\n\n***Current:***\n{cstat}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ DB Error updating maint.")

async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=2: await update.message.reply_text("❌ Format: `/setprice <item> <price>`"); return
    item, pstr = context.args[0], context.args[1]
    try: price=int(pstr); assert price>=0
    except (ValueError,AssertionError): await update.message.reply_text("❌ Price မှား။"); return
    try: prices_col.update_one({"_id": item}, {"$set": {"price": price}}, upsert=True); await update.message.reply_text(f"✅ Price Updated!\n💎 Item: `{item}`\n💰 New: `{price:,} MMK`", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error set price {item}: {e}"); await update.message.reply_text("❌ DB error.")

async def removeprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=1: await update.message.reply_text("❌ Format: `/removeprice <item>`"); return
    item = context.args[0]
    try: r=prices_col.delete_one({"_id": item});
        if r.deleted_count==0: await update.message.reply_text(f"❌ `{item}` မှာ custom price မရှိ။"); return
        await update.message.reply_text(f"✅ Custom Price Removed!\n💎 Item: `{item}`\n🔄 Default ပြန်သုံးမည်။", parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error remove price {item}: {e}"); await update.message.reply_text("❌ DB error.")

async def setwavenum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=1: await update.message.reply_text("❌ Format: `/setwavenum <number>`"); return
    num = context.args[0]
    if update_payment_info("wave_number", num): info=get_payment_info(); await update.message.reply_text(f"✅ Wave Num Updated!\n📱 New: `{info['wave_number']}`\n👤 Name: {info['wave_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error update.")
async def setkpaynum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=1: await update.message.reply_text("❌ Format: `/setkpaynum <number>`"); return
    num = context.args[0]
    if update_payment_info("kpay_number", num): info=get_payment_info(); await update.message.reply_text(f"✅ KPay Num Updated!\n📱 New: `{info['kpay_number']}`\n👤 Name: {info['kpay_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error update.")
async def setwavename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ Format: `/setwavename <name>`"); return
    name = " ".join(context.args)
    if update_payment_info("wave_name", name): info=get_payment_info(); await update.message.reply_text(f"✅ Wave Name Updated!\n📱 Num: `{info['wave_number']}`\n👤 New: {info['wave_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error update.")
async def setkpayname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ Format: `/setkpayname <name>`"); return
    name = " ".join(context.args)
    if update_payment_info("kpay_name", name): info=get_payment_info(); await update.message.reply_text(f"✅ KPay Name Updated!\n📱 Num: `{info['kpay_number']}`\n👤 New: {info['kpay_name']}", parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("❌ Error update.")
async def setkpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not update.message.reply_to_message or not update.message.reply_to_message.photo: await update.message.reply_text("❌ ပုံကို reply `/setkpayqr`"); return
    fid = update.message.reply_to_message.photo[-1].file_id
    if update_payment_info("kpay_image", fid): await update.message.reply_text("✅ KPay QR ထည့်ပြီး!")
    else: await update.message.reply_text("❌ Error set KPay QR.")
async def removekpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    info = get_payment_info();
    if not info.get("kpay_image"): await update.message.reply_text("ℹ️ KPay QR မရှိပါ။"); return
    if update_payment_info("kpay_image", None): await update.message.reply_text("✅ KPay QR ဖျက်ပြီး!")
    else: await update.message.reply_text("❌ Error remove KPay QR.")
async def setwaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    if not update.message.reply_to_message or not update.message.reply_to_message.photo: await update.message.reply_text("❌ ပုံကို reply `/setwaveqr`"); return
    fid = update.message.reply_to_message.photo[-1].file_id
    if update_payment_info("wave_image", fid): await update.message.reply_text("✅ Wave QR ထည့်ပြီး!")
    else: await update.message.reply_text("❌ Error set Wave QR.")
async def removewaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    info = get_payment_info();
    if not info.get("wave_image"): await update.message.reply_text("ℹ️ Wave QR မရှိပါ။"); return
    if update_payment_info("wave_image", None): await update.message.reply_text("✅ Wave QR ဖျက်ပြီး!")
    else: await update.message.reply_text("❌ Error remove Wave QR.")

async def send_to_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args: await update.message.reply_text("❌ Format: `/sendgroup <message>`"); return
    msg = " ".join(context.args); gid = ADMIN_GROUP_ID
    if not gid: await update.message.reply_text("❌ Admin Group ID not set."); return
    try: await context.bot.send_message(gid, f"📢 ***Admin Msg***\n\n{msg}", parse_mode=ParseMode.MARKDOWN); await update.message.reply_text("✅ Group msg sent.")
    except Exception as e: logger.error(f"Failed send group {gid}: {e}"); await update.message.reply_text(f"❌ Group msg fail: {e}")

async def testgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    gid = ADMIN_GROUP_ID;
    if not gid: await update.message.reply_text("❌ Admin Group ID not set."); return
    is_admin_grp = await is_bot_admin_in_group(context.bot, gid); stxt = "Admin ✅" if is_admin_grp else "Not Admin ❌"
    try:
        if is_admin_grp: await context.bot.send_message(gid, f"✅ **Test**\nBot msg ပို့နိုင်!\n⏰ {datetime.now():%H:%M:%S}", parse_mode=ParseMode.MARKDOWN); await update.message.reply_text(f"✅ **Group Test OK!**\n📱 ID: `{gid}`\n🤖 Status: {stxt}\n📨 Test msg ပို့ပြီး。", parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(f"❌ **Group Fail!**\n📱 ID: `{gid}`\n🤖 Status: {stxt}\n\n**Fix:**\n1️⃣ Add bot to group\n2️⃣ Make bot Admin\n3️⃣ Give 'Post Messages'", parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error /testgroup: {e}"); await update.message.reply_text(f"❌ Error test msg: {e}")

async def adminhelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    uid=str(update.effective_user.id); is_owner_user=is_owner(uid); info=get_payment_info()
    msg="🔧 ***Admin Cmds*** 🔧\n\n";
    if is_owner_user: msg+=("👑 *Owner:*\n/addadm <id>\n/unadm <id>\n/broadcast (Reply)\n/set..qr (Reply)\n/remove..qr\n/d /m /y (Reports)\n\n")
    msg+=("💰 *Bal:*\n/approve <id> <amt>\n/deduct <id> <amt>\n\n💬 *Comm:*\n/reply <id> <msg>\n/done <id>\n/sendgroup <msg>\n\n"
          "🔧 *Settings:*\n/maintenance <feat> <on|off>\n/setprice <item> <price>\n/removeprice <item>\n/set..num <num>\n/set..name <name>\n\n"
          "🛡️ *Users:*\n/ban <id>\n/unban <id>\n\nℹ️ *Info:*\n/testgroup\n/adminhelp\n\n")
    msg+=(f"📊 *Status:*\n{''.join([f'• {f.capitalize()}: {"🟢" if bot_maintenance[f] else "🔴"}\\n' for f in bot_maintenance])}"
           f"• Auth Users: {get_authorized_user_count()}\n\n💳 *Payment:*\n"
           f"• KPay: {info['kpay_number']} ({info['kpay_name']}){'[QR]' if info['kpay_image'] else ''}\n"
           f"• Wave: {info['wave_number']} ({info['wave_name']}){'[QR]' if info['wave_image'] else ''}")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): await update.message.reply_text("❌ Owner only!"); return
    if not update.message.reply_to_message: await update.message.reply_text("❌ Message ကို reply လုပ်ပါ။"); return
    args=context.args; send_u="user" in args or not args; send_g="gp" in args; rmsg=update.message.reply_to_message; us=uf=gs=gf=0
    try: uids_cursor=users_col.find({},{"_id":1}); uids=[d["_id"] for d in uids_cursor]
        # Group ID finding logic might be inefficient - consider alternatives
        gids=set(); # ... logic to find group IDs from orders/topups ...
        if rmsg.text: txt=rmsg.text; ent=rmsg.entities
            if send_u:
                for uid in uids:
                    try: await context.bot.send_message(int(uid),txt,entities=ent); us+=1
                    except Exception as e: logger.warning(f"BCast Txt fail user {uid}: {e}"); uf+=1
                    await asyncio.sleep(0.05)
            if send_g: # ... broadcast to groups ...
                pass
        elif rmsg.photo: pid=rmsg.photo[-1].file_id; cap=rmsg.caption; cap_ent=rmsg.caption_entities
            if send_u:
                for uid in uids:
                    try: await context.bot.send_photo(int(uid),pid,caption=cap,caption_entities=cap_ent); us+=1
                    except Exception as e: logger.warning(f"BCast Pic fail user {uid}: {e}"); uf+=1
                    await asyncio.sleep(0.05)
            if send_g: # ... broadcast to groups ...
                pass
        else: await update.message.reply_text("❌ Text/Photo သာ။"); return
        report=f"✅ Broadcast Done!\n";
        if send_u: report+=f"👥 Users: {us} sent, {uf} fail.\n"
        if send_g: report+=f"🏢 Groups: {gs} sent, {gf} fail.\n"
        await update.message.reply_text(report)
    except PyMongoError as e: logger.error(f"DB Error broadcast: {e}"); await update.message.reply_text("❌ DB error targets.")
    except Exception as e: logger.error(f"General Error broadcast: {e}", exc_info=True); await update.message.reply_text(f"❌ Error: {e}")

async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    args = context.args; sdate = edate = ptxt = None; today = datetime.now()
    if not args and not update.callback_query: # Show buttons if no args and not callback
        yday = today - timedelta(days=1); wago = today - timedelta(days=7)
        kbd = [ [InlineKeyboardButton("📅 Today", callback_data=f"report_day_{today.strftime('%Y-%m-%d')}")],
                [InlineKeyboardButton("📅 Yesterday", callback_data=f"report_day_{yday.strftime('%Y-%m-%d')}")],
                [InlineKeyboardButton("📅 Last 7 Days", callback_data=f"report_day_range_{wago.strftime('%Y-%m-%d')}_{today.strftime('%Y-%m-%d')}")] ]
        await update.message.reply_text("📊 Select Date or Type:\n`/d YYYY-MM-DD`\n`/d YYYY-MM-DD YYYY-MM-DD`", reply_markup=InlineKeyboardMarkup(kbd)); return
    # Parse args or callback data
    query_data = update.callback_query.data if update.callback_query else None
    input_args = query_data.replace('report_day_','') if query_data and query_data.startswith('report_day_') else ' '.join(args)
    parts = input_args.split('_') if query_data else args
    try:
        if len(parts) == 1 and '-' in parts[0]: sdate = edate = datetime.strptime(parts[0], '%Y-%m-%d').strftime('%Y-%m-%d'); ptxt = f"({sdate})"
        elif len(parts) == 2 and parts[0] == 'range': sdate=parts[1]; edate=parts[2]; ptxt = f"({sdate} to {edate})"
        elif len(parts) == 2 and '-' in parts[0] and '-' in parts[1]: sdate=parts[0]; edate=parts[1]; ptxt = f"({sdate} to {edate})"
        else: raise ValueError("Invalid format")
    except ValueError: await update.effective_message.reply_text("❌ Date format error (YYYY-MM-DD)."); return

    try: # DB Aggregation
        s_iso = f"{sdate}T00:00:00.000Z"; e_iso = f"{edate}T23:59:59.999Z"
        spipe=[{"$unwind":"$orders"},{"$match":{"orders.status":STATUS_CONFIRMED,"orders.confirmed_at":{"$gte":s_iso,"$lte":e_iso}}},{"$group":{"_id":None,"ts":{"$sum":"$orders.price"},"to":{"$sum":1}}}];
        tpipe=[{"$unwind":"$topups"},{"$match":{"topups.status":STATUS_APPROVED,"topups.approved_at":{"$gte":s_iso,"$lte":e_iso}}},{"$group":{"_id":None,"tt":{"$sum":"$topups.amount"},"tc":{"$sum":1}}}];
        sres=list(users_col.aggregate(spipe)); tres=list(users_col.aggregate(tpipe));
        ts=sres[0]["ts"] if sres else 0; to=sres[0]["to"] if sres else 0; tt=tres[0]["tt"] if tres else 0; tc=tres[0]["tc"] if tres else 0
        msg = (f"📊 ***Report***\n📅 Period: {ptxt}\n\n🛒 Orders:\n💰 Sales: `{ts:,} MMK`\n📦 Count: {to}\n\n💳 Topups:\n💰 Amount: `{tt:,} MMK`\n📦 Count: {tc}")
        if update.callback_query: await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error daily report: {e}"); await update.effective_message.reply_text("❌ DB error report.")
    except Exception as e: logger.error(f"Error daily report: {e}"); await update.effective_message.reply_text(f"❌ Error: {e}")

async def monthly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    args = context.args; smonth=emonth=ptxt=None; today=datetime.now()
    if not args and not update.callback_query:
        tmon=today.strftime("%Y-%m"); lmon=(today.replace(day=1)-timedelta(days=1)).strftime("%Y-%m"); ago3=(today.replace(day=1)-timedelta(days=90)).strftime("%Y-%m")
        kbd=[[InlineKeyboardButton("📅 This Month", callback_data=f"report_month_{tmon}")], [InlineKeyboardButton("📅 Last Month", callback_data=f"report_month_{lmon}")], [InlineKeyboardButton("📅 Last 3 Months", callback_data=f"report_month_range_{ago3}_{tmon}")]];
        await update.message.reply_text("📊 Select Month or Type:\n`/m YYYY-MM`\n`/m YYYY-MM YYYY-MM`", reply_markup=InlineKeyboardMarkup(kbd)); return
    # Parse args or callback data (similar to daily, but for YYYY-MM)
    # ... (Parsing logic needed here) ...
    # Placeholder parsing:
    try:
        if len(args)==1 and '-' in args[0]: smonth = emonth = datetime.strptime(args[0], '%Y-%m').strftime('%Y-%m'); ptxt=f"({smonth})"
        # Add logic for range, callback single, callback range based on YYYY-MM
        else: raise ValueError("Invalid format")
    except ValueError: await update.effective_message.reply_text("❌ Month format error (YYYY-MM)."); return

    try:
        sdt_obj = datetime.strptime(f"{smonth}-01", '%Y-%m-%d'); eyr, emo = map(int, emonth.split('-'));
        if emo==12: edt_obj = datetime(eyr+1, 1, 1) - timedelta(microseconds=1)
        else: edt_obj = datetime(eyr, emo+1, 1) - timedelta(microseconds=1)
        s_iso = sdt_obj.strftime('%Y-%m-%dT%H:%M:%S.%fZ'); e_iso = edt_obj.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        spipe=[{"$unwind":"$orders"},{"$match":{"orders.status":STATUS_CONFIRMED,"orders.confirmed_at":{"$gte":s_iso,"$lte":e_iso}}},{"$group":{"_id":None,"ts":{"$sum":"$orders.price"},"to":{"$sum":1}}}];
        tpipe=[{"$unwind":"$topups"},{"$match":{"topups.status":STATUS_APPROVED,"topups.approved_at":{"$gte":s_iso,"$lte":e_iso}}},{"$group":{"_id":None,"tt":{"$sum":"$topups.amount"},"tc":{"$sum":1}}}];
        sres=list(users_col.aggregate(spipe)); tres=list(users_col.aggregate(tpipe));
        ts=sres[0]["ts"] if sres else 0; to=sres[0]["to"] if sres else 0; tt=tres[0]["tt"] if tres else 0; tc=tres[0]["tc"] if tres else 0
        msg=(f"📊 ***Report***\n📅 Period: {ptxt}\n\n🛒 Orders:\n💰 Sales: `{ts:,} MMK`\n📦 Count: {to}\n\n💳 Topups:\n💰 Amount: `{tt:,} MMK`\n📦 Count: {tc}")
        if update.callback_query: await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error monthly report: {e}"); await update.effective_message.reply_text("❌ DB error report.")
    except Exception as e: logger.error(f"Error monthly report: {e}"); await update.effective_message.reply_text(f"❌ Error: {e}")


async def yearly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    args = context.args; syr=eyr=ptxt=None; today=datetime.now()
    if not args and not update.callback_query:
        tyr=today.strftime("%Y"); lyr=str(int(tyr)-1)
        kbd=[[InlineKeyboardButton("📅 This Year", callback_data=f"report_year_{tyr}")], [InlineKeyboardButton("📅 Last Year", callback_data=f"report_year_{lyr}")], [InlineKeyboardButton("📅 Both Years", callback_data=f"report_year_range_{lyr}_{tyr}")]];
        await update.message.reply_text("📊 Select Year or Type:\n`/y YYYY`\n`/y YYYY YYYY`", reply_markup=InlineKeyboardMarkup(kbd)); return
    # Parse args or callback data (similar to daily, but for YYYY)
    # ... (Parsing logic needed here) ...
    # Placeholder parsing:
    try:
        if len(args)==1 and args[0].isdigit() and len(args[0])==4: syr = eyr = args[0]; ptxt=f"({syr})"
        # Add logic for range, callback single, callback range based on YYYY
        else: raise ValueError("Invalid format")
    except ValueError: await update.effective_message.reply_text("❌ Year format error (YYYY)."); return

    try:
        s_iso = f"{syr}-01-01T00:00:00.000Z"; e_iso = f"{eyr}-12-31T23:59:59.999Z"
        spipe=[{"$unwind":"$orders"},{"$match":{"orders.status":STATUS_CONFIRMED,"orders.confirmed_at":{"$gte":s_iso,"$lte":e_iso}}},{"$group":{"_id":None,"ts":{"$sum":"$orders.price"},"to":{"$sum":1}}}];
        tpipe=[{"$unwind":"$topups"},{"$match":{"topups.status":STATUS_APPROVED,"topups.approved_at":{"$gte":s_iso,"$lte":e_iso}}},{"$group":{"_id":None,"tt":{"$sum":"$topups.amount"},"tc":{"$sum":1}}}];
        sres=list(users_col.aggregate(spipe)); tres=list(users_col.aggregate(tpipe));
        ts=sres[0]["ts"] if sres else 0; to=sres[0]["to"] if sres else 0; tt=tres[0]["tt"] if tres else 0; tc=tres[0]["tc"] if tres else 0
        msg=(f"📊 ***Report***\n📅 Period: {ptxt}\n\n🛒 Orders:\n💰 Sales: `{ts:,} MMK`\n📦 Count: {to}\n\n💳 Topups:\n💰 Amount: `{tt:,} MMK`\n📦 Count: {tc}")
        if update.callback_query: await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except PyMongoError as e: logger.error(f"DB Error yearly report: {e}"); await update.effective_message.reply_text("❌ DB error report.")
    except Exception as e: logger.error(f"Error yearly report: {e}"); await update.effective_message.reply_text(f"❌ Error: {e}")
# --- END Admin Commands ---


# --- Message Handlers ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = str(user.id)
    if not is_user_authorized(user_id): return
    if get_user_restriction_status(user_id) == RESTRICTION_AWAITING_APPROVAL: await update.message.reply_text("⏳ Screenshot ပို့ပြီးပါပြီ။ Admin approve စောင့်ပါ။"); return
    if user_id not in pending_topups: await update.message.reply_text("💡 ပုံ မပို့မီ `/topup amount` ကို အရင်သုံးပါ။"); return
    if not is_payment_screenshot(update): await update.message.reply_text("❌ Payment screenshot (KPay/Wave) သာ လက်ခံပါတယ်။"); return

    pending = pending_topups[user_id]
    amount, payment_method = pending["amount"], pending.get("payment_method", "Unknown")
    if payment_method == "Unknown": await update.message.reply_text("❌ Payment app (KPay/Wave) ကို အရင်ရွေးပါ။"); return

    if not set_user_restriction_status(user_id, RESTRICTION_AWAITING_APPROVAL): await update.message.reply_text("❌ User status update error."); return

    topup_id = f"TOP{datetime.now().strftime('%y%m%d%H%M%S%f')[:-3]}{user_id[-2:]}"
    user_mention = user.mention_markdown()
    topup_request = { "topup_id": topup_id, "amount": amount, "payment_method": payment_method, "status": STATUS_PENDING,
                      "timestamp": datetime.now().isoformat(), "chat_id": update.effective_chat.id, "user_name": user.first_name }
    try:
        users_col.update_one({"_id": user_id}, {"$push": {"topups": topup_request}}, upsert=True)
        del pending_topups[user_id]

        admin_msg = ( f"💳 ***ငွေဖြည့်တောင်းဆိုမှု***\n👤 User: {user_mention} (`{user_id}`)\n💰 Amt: `{amount:,} MMK`\n"
                      f"📱 Via: {payment_method.upper()}\n🔖 ID: `{topup_id}`\n⏰ Time: {datetime.now():%H:%M:%S}\n📊 Status: ⏳ {STATUS_PENDING}" )
        keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"topup_approve_{topup_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"topup_reject_{topup_id}")]]
        markup = InlineKeyboardMarkup(keyboard)
        admin_list = get_all_admin_ids()
        photo_file_id = update.message.photo[-1].file_id
        for admin_id in admin_list:
            try: await context.bot.send_photo(admin_id, photo_file_id, caption=admin_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            except Exception as e: logger.warning(f"Failed sending topup photo admin {admin_id}: {e}")
        if ADMIN_GROUP_ID and await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
            try: gcap = admin_msg + f"\n\nApprove: `/approve {user_id} {amount}`\n#TopupRequest"; await context.bot.send_photo(ADMIN_GROUP_ID, photo_file_id, caption=gcap, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            except Exception as e: logger.warning(f"Failed sending topup photo group {ADMIN_GROUP_ID}: {e}")

        await update.message.reply_text(f"✅ ***Screenshot လက်ခံပါပြီ!***\n💰 ပမာဏ: `{amount:,} MMK`\n⏰ Admin approve လုပ်သည်ထိ စောင့်ပါ။\n\n🔒 ***သင်၏ အသုံးပြုမှုကို ယာယီ ကန့်သတ်ထားပါမည်။***", parse_mode=ParseMode.MARKDOWN)

    except PyMongoError as e: logger.error(f"DB Error saving topup req {user_id}: {e}"); set_user_restriction_status(user_id, RESTRICTION_NONE); await update.message.reply_text("❌ DB error. Topup မရ။ ပြန်ကြိုးစားပါ။")


async def handle_other_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = str(user.id)
    if not is_user_authorized(user_id):
        if update.message and update.message.text: await update.message.reply_text(simple_reply(update.message.text), parse_mode=ParseMode.MARKDOWN)
        return
    if get_user_restriction_status(user_id) == RESTRICTION_AWAITING_APPROVAL:
        await update.message.reply_text("❌ ***အသုံးပြုမှု ကန့်သတ်ထား!***\n🔒 Admin မှ topup စစ်ဆေးပြီးထိ စာ/sticker ပို့မရပါ။", parse_mode=ParseMode.MARKDOWN); return
    if update.message and update.message.text: await update.message.reply_text(simple_reply(update.message.text), parse_mode=ParseMode.MARKDOWN)
    else: logger.debug(f"Ignoring non-text/photo from authorized user {user_id}")


# --- Callback Query Handler ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; user = query.from_user; user_id = str(user.id); admin_name = user.first_name
    await query.answer()
    data = query.data
    logger.info(f"Callback: {data} from user {user_id}")

    # --- Payment method selection ---
    if data.startswith("topup_pay_"):
        target_user_id = str(query.message.chat_id)
        if user_id != target_user_id: logger.warning(f"User {user_id} pressed topup_pay for {target_user_id}. Ignoring."); return
        if target_user_id not in pending_topups: await query.edit_message_text("❌ Topup process မရှိတော့ပါ။ /topup ပြန်စပါ။"); return
        parts = data.split("_"); pmethod, amount_str = parts[2], parts[3]; amount = int(amount_str)
        pending_topups[target_user_id]["payment_method"] = pmethod
        info = get_payment_info(); pinfo = {}
        if pmethod=='kpay': pinfo={'name':"KBZ Pay",'num':info['kpay_number'],'acc':info['kpay_name'],'qr':info.get('kpay_image')}
        elif pmethod=='wave': pinfo={'name':"Wave Money",'num':info['wave_number'],'acc':info['wave_name'],'qr':info.get('wave_image')}
        else: await query.edit_message_text("❌ Invalid payment method."); return
        msg=(f"💳 ***ငွေဖြည့်ရန် ({pinfo['name']})***\n💰 Amount: `{amount:,} MMK`\n\n📱 {pinfo['name']}\n📞 Number: `{pinfo['num']}`\n👤 Name: {pinfo['acc']}\n\n"
             f"⚠️ ***Important:*** ငွေလွှဲ Note/Remark တွင် သင်၏ {pinfo['name']} အကောင့်အမည်ကို ရေးပါ။\n\n💡 ***ငွေလွှဲပြီးလျှင် screenshot ကို ဤ chat တွင် တင်ပေးပါ။***\n⏰ Admin စစ်ဆေးမည်။\n\nℹ️ /cancel");
        try: await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed edit topup msg: {e}")
        if pinfo.get('qr'): try: await query.message.reply_photo(pinfo['qr'], caption=f"👆 {pinfo['name']} QR\n`{pinfo['num']}`\n{pinfo['acc']}", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"Failed send QR {pinfo['qr']}: {e}")
        return

    # --- Registration request button ---
    elif data == "request_register":
        if is_user_authorized(user_id): await context.bot.send_message(user_id, "✅ သင် အသုံးပြုခွင့် ရပြီးသားပါ။ /start နှိပ်ပါ။"); return
        uname = user.username or "-"; name = f"{user.first_name} {user.last_name or ''}".strip()
        kbd=[[InlineKeyboardButton("✅ Approve", callback_data=f"register_approve_{user_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"register_reject_{user_id}")]]; markup=InlineKeyboardMarkup(kbd)
        omsg=(f"📝 ***Reg Request***\n👤 Name: {user.mention_markdown()}\n🆔 ID: `{user_id}`\n📱 User: @{uname}\n⏰ {datetime.now():%H:%M:%S}\n\n Approve?")
        try: await context.bot.send_message(ADMIN_ID, omsg, parse_mode=ParseMode.MARKDOWN, reply_markup=markup) # Simpler: send text only to owner
             await query.edit_message_text(f"✅ ***Register တောင်းဆိုမှု ပို့ပြီး!***\n🆔 Your ID: `{user_id}`\n⏳ Owner approve စောင့်ပါ။", parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.error(f"Failed send reg req owner {ADMIN_ID}: {e}"); await context.bot.send_message(user_id, "❌ Register error. Contact owner.")
        return

    # --- Admin action Callbacks ---
    if not is_admin(user_id): logger.warning(f"Non-admin {user_id} tried admin cb {data}. Ignoring."); return

    # --- Registration approve ---
    if data.startswith("register_approve_"):
        tid = data.split("_")[-1]
        if is_user_authorized(tid):
            logger.info(f"User {tid} already authorized.")
            try: await query.edit_message_reply_markup(reply_markup=None) # Still try remove buttons
            except Exception as e: logger.warning(f"Failed remove markup already auth {tid}: {e}")
            return
        try:
            auth_users_col.update_one({"_id": tid}, {"$set": {"authorized_at": datetime.now(), "approved_by": user_id}}, upsert=True)
            set_user_restriction_status(tid, RESTRICTION_NONE)
            try: await query.edit_message_text(query.message.text + f"\n\n✅ Approved by {admin_name}", parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed edit reg msg: {e}")
            try: await context.bot.send_message(int(tid), f"🎉 Reg Approved! /start")
            except Exception as e: logger.warning(f"Failed send reg approval {tid}: {e}")
            if ADMIN_GROUP_ID: #... send group notification ...
                 pass
        except PyMongoError as e: logger.error(f"DB Error approve reg {tid}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Registration reject ---
    elif data.startswith("register_reject_"):
        tid = data.split("_")[-1]
        try: await query.edit_message_text(query.message.text + f"\n\n❌ Rejected by {admin_name}", parse_mode=ParseMode.MARKDOWN, reply_markup=None)
        except Exception as e: logger.warning(f"Failed edit reject msg: {e}")
        try: await context.bot.send_message(int(tid), "❌ Reg Rejected. Admin ဆက်သွယ်ပါ။")
        except Exception as e: logger.warning(f"Failed send reg rejection {tid}: {e}")
        if ADMIN_GROUP_ID: #... send group notification ...
            pass
        return

    # --- Topup approve ---
    elif data.startswith("topup_approve_"):
        tid = data.split("_")[-1] # topup_id
        try:
            res = users_col.find_one({"topups.topup_id": tid, "topups.status": STATUS_PENDING}, {"_id":1, "balance":1, "topups.$":1})
            if not res or not res.get("topups"): await context.bot.send_message(user_id,"⚠️ Topup processed/not found."); try: await query.edit_message_reply_markup(reply_markup=None); except: pass; return
            tuid=res["_id"]; mt=res["topups"][0]; amt=mt["amount"]; obal=res.get("balance",0); nbal=obal+amt
            upd_res = users_col.update_one({"_id":tuid, "topups.topup_id":tid, "topups.status":STATUS_PENDING}, {"$set":{"topups.$.status":STATUS_APPROVED,"topups.$.approved_by":admin_name,"topups.$.approved_at":datetime.now().isoformat(),"restriction_status":RESTRICTION_NONE}, "$inc":{"balance":amt}})
            if upd_res.matched_count==0: await context.bot.send_message(user_id,"⚠️ Topup race condition?"); return
            try: cap=query.message.caption or ""; ucap=cap.replace(f"⏳ {STATUS_PENDING}",f"✅ {STATUS_APPROVED}")+f"\n\n✅ By: {admin_name}"; await query.edit_message_caption(caption=ucap, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed edit topup approve caption: {e}")
            try: kbd=[[InlineKeyboardButton("💎 Order",url=f"https://t.me/{context.bot.username}?start=order")]]; await context.bot.send_message(int(tuid),f"✅ ***Topup OK!*** 🎉\n💰 Amt: `{amt:,} MMK`\n💳 Bal: `{nbal:,} MMK`\n👤 By: {admin_name}\n\n🔓 Bot ပြန်သုံးနိုင်!", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kbd))
            except Exception as e: logger.warning(f"Failed send topup approval {tuid}: {e}")
            if ADMIN_GROUP_ID: #... send group notification ...
                pass
        except PyMongoError as e: logger.error(f"DB Error approve topup {tid}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Topup reject ---
    elif data.startswith("topup_reject_"):
        tid = data.split("_")[-1] # topup_id
        try:
            res = users_col.find_one_and_update({"topups.topup_id": tid, "topups.status": STATUS_PENDING}, {"$set": {"topups.$.status": STATUS_REJECTED,"topups.$.rejected_by": admin_name,"topups.$.rejected_at": datetime.now().isoformat(),"restriction_status": RESTRICTION_NONE}}, projection={"_id":1, "topups.$":1})
            if res is None: await context.bot.send_message(user_id,"⚠️ Topup processed/not found."); try: await query.edit_message_reply_markup(reply_markup=None); except: pass; return
            tuid=res["_id"]; amt=res["topups"][0].get("amount",0)
            try: cap=query.message.caption or ""; ucap=cap.replace(f"⏳ {STATUS_PENDING}",f"❌ {STATUS_REJECTED}")+f"\n\n❌ By: {admin_name}"; await query.edit_message_caption(caption=ucap, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed edit topup reject caption: {e}")
            try: await context.bot.send_message(int(tuid),f"❌ ***Topup Rejected!***\n💰 Amt: `{amt:,} MMK`\n👤 By: {admin_name}\n📞 Admin ဆက်သွယ်ပါ။\n\n🔓 Bot ပြန်သုံးနိုင်!", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed send topup rejection {tuid}: {e}")
            if ADMIN_GROUP_ID: #... send group notification ...
                pass
        except PyMongoError as e: logger.error(f"DB Error reject topup {tid}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Order confirm ---
    elif data.startswith("order_confirm_"):
        oid = data.split("_")[-1] # order_id
        try:
            res = users_col.find_one_and_update({"orders.order_id": oid, "orders.status": STATUS_PENDING}, {"$set": {"orders.$.status": STATUS_CONFIRMED,"orders.$.confirmed_by": admin_name,"orders.$.confirmed_at": datetime.now().isoformat()}}, projection={"_id":1, "orders.$":1})
            if res is None: await context.bot.send_message(user_id,"⚠️ Order processed/not found."); try: await query.edit_message_reply_markup(reply_markup=None); except: pass; return
            tuid=res["_id"]; order=res["orders"][0]; tname=order.get("user_name","User")
            try: utxt=query.message.text.replace(f"⏳ {STATUS_PENDING}",f"✅ {STATUS_CONFIRMED}")+f"\n\n✅ By: {admin_name}"; await query.edit_message_text(utxt, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed edit order confirm msg: {e}")
            try: cid_notify=order.get("chat_id",int(tuid)); umention=f"[{tname}](tg://user?id={tuid})"; await context.bot.send_message(cid_notify,f"✅ ***Order Confirmed!***\n📝 ID: `{oid}`\n👤 User: {umention}\n🎮 ID: `{order['game_id']}`\n💎 Amt: {order['amount']}\n📊 ✅ {STATUS_CONFIRMED}\n\n💎 DM ပို့ပြီး!", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed send order confirm {cid_notify}: {e}")
            if ADMIN_GROUP_ID: #... send group notification ...
                pass
        except PyMongoError as e: logger.error(f"DB Error confirm order {oid}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Order cancel ---
    elif data.startswith("order_cancel_"):
        oid = data.split("_")[-1] # order_id
        try:
            udoc = users_col.find_one({"orders.order_id": oid, "orders.status": STATUS_PENDING}, {"_id":1, "orders.$":1})
            if not udoc or not udoc.get("orders"): await context.bot.send_message(user_id,"⚠️ Order processed/not found."); try: await query.edit_message_reply_markup(reply_markup=None); except: pass; return
            tuid=udoc["_id"]; order=udoc["orders"][0]; refund=order.get("price",0); tname=order.get("user_name","User")
            if refund<=0: logger.error(f"Invalid refund order {oid}"); await context.bot.send_message(user_id, "❌ Order price error!"); return
            users_col.update_one({"_id":tuid, "orders.order_id":oid}, {"$set":{"orders.$.status":STATUS_CANCELLED,"orders.$.cancelled_by":admin_name,"orders.$.cancelled_at":datetime.now().isoformat()}, "$inc":{"balance":refund}})
            try: utxt=query.message.text.replace(f"⏳ {STATUS_PENDING}",f"❌ {STATUS_CANCELLED}")+f"\n\n❌ By: {admin_name} (Refunded)"; await query.edit_message_text(utxt, parse_mode=ParseMode.MARKDOWN, reply_markup=None)
            except Exception as e: logger.warning(f"Failed edit order cancel msg: {e}")
            try: cid_notify=order.get("chat_id",int(tuid)); umention=f"[{tname}](tg://user?id={tuid})"; await context.bot.send_message(cid_notify,f"❌ ***Order Cancelled!***\n📝 ID: `{oid}`\n👤 User: {umention}\n🎮 ID: `{order['game_id']}`\n📊 ❌ {STATUS_CANCELLED}\n💰 Refunded: {refund:,} MMK\n📞 Admin ဆက်သွယ်ပါ။", parse_mode=ParseMode.MARKDOWN)
            except Exception as e: logger.warning(f"Failed send order cancel {cid_notify}: {e}")
            if ADMIN_GROUP_ID: #... send group notification ...
                pass
        except PyMongoError as e: logger.error(f"DB Error cancel order {oid}: {e}"); await context.bot.send_message(user_id, "❌ DB Error.")
        return

    # --- Report filter callbacks ---
    elif data.startswith("report_day_"): await daily_report_command(update, context); return
    elif data.startswith("report_month_"): await monthly_report_command(update, context); return
    elif data.startswith("report_year_"): await yearly_report_command(update, context); return

    # --- Other user buttons ---
    elif data == "copy_kpay": info=get_payment_info(); await query.message.reply_text(f"📱 ***KBZ Pay***\n`{info['kpay_number']}`\n👤 {info['kpay_name']}", parse_mode=ParseMode.MARKDOWN); return
    elif data == "copy_wave": info=get_payment_info(); await query.message.reply_text(f"📱 ***Wave Money***\n`{info['wave_number']}`\n👤 {info['wave_name']}", parse_mode=ParseMode.MARKDOWN); return
    elif data == "topup_button":
        info=get_payment_info(); kbd=[[InlineKeyboardButton("📱 Copy KPay",callback_data="copy_kpay")],[InlineKeyboardButton("📱 Copy Wave",callback_data="copy_wave")]]; markup=InlineKeyboardMarkup(kbd)
        msg=("💳 ***ငွေဖြည့်ရန်***\n1️⃣ `/topup amount`\n2️⃣ ငွေလွှဲ:\n   📱 KBZ: `{0}` ({1})\n   📱 Wave: `{2}` ({3})\n3️⃣ Screenshot တင်ပါ\n⏰ Admin စစ်ဆေးမည်.".format(info['kpay_number'], info['kpay_name'], info['wave_number'], info['wave_name']))
        try: await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        except Exception: await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return

    else: logger.warning(f"Unhandled callback: {data}")


# --- Post Init Function ---
async def post_init(application: Application):
    logger.info("🚀 Main bot application initialized.")


# --- Main Function ---
def main():
    if not BOT_TOKEN: logger.critical("❌ BOT_TOKEN missing!"); return

    application = ( Application.builder().token(BOT_TOKEN).post_init(post_init).build() )

    # --- Register Handlers ---
    # Middleware (Group 0)
    application.add_handler(MessageHandler(filters.ALL, check_restriction), group=0)
    application.add_handler(CallbackQueryHandler(check_restriction), group=0)

    # User Commands (Group 1)
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
    admin_cmds = ["approve","deduct","done","reply","ban","unban","sendgroup","maintenance","testgroup",
                  "setprice","removeprice","setwavenum","setkpaynum","setwavename","setkpayname","adminhelp"]
    owner_cmds = ["addadm","unadm","setkpayqr","removekpayqr","setwaveqr","removewaveqr","broadcast","d","m","y"]

    for cmd in admin_cmds + owner_cmds: # Register all, check permission inside handler
        application.add_handler(CommandHandler(cmd, globals()[f"{cmd}_command"]))

    # Callback Query Handler (Group 1)
    application.add_handler(CallbackQueryHandler(button_callback))

    # Message Handlers (Group 1)
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.PHOTO, handle_other_messages))

    logger.info("🤖 Bot starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("⚫ Bot stopped.")

if __name__ == "__main__":
    main()
