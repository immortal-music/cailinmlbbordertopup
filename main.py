# main.py (MongoDB Version)

import json, os, asyncio
from datetime import datetime, timedelta
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
import re # Calculator အတွက် import လုပ်ထားတာ
import traceback # Error traceback အတွက်

# env.py ကနေ လိုအပ်တာတွေ import လုပ်ပါ
from env import BOT_TOKEN, ADMIN_ID, ADMIN_GROUP_ID, MONGO_URI

# db.py ကနေ လိုအပ်တဲ့ database objects တွေနဲ့ functions တွေကို import လုပ်ပါ
from db import (
    users_col, settings_col, clone_bots_col, initialize_settings,
    load_authorized_users_db, save_authorized_users_db,
    load_settings_db, save_settings_field_db,
    load_prices_db, save_prices_db,
    load_admins_db, add_admin_db, remove_admin_db,
    load_clone_bots_db, save_clone_bot_db, remove_clone_bot_db,
    get_clone_bot_by_admin, update_clone_bot_balance
)

# --- Global Variables ---
AUTHORIZED_USERS = set()
user_states = {}
bot_maintenance = {"orders": True, "topups": True, "general": True}
payment_info = {
    "kpay_number": "Default", "kpay_name": "Default", "kpay_image": None,
    "wave_number": "Default", "wave_name": "Default", "wave_image": None
}
pending_topups = {}
clone_bot_apps = {}

# --- Database Helper Functions (main.py specific wrappers) ---

def load_settings():
    """ Bot settings တွေကို MongoDB ကနေ ဆွဲထုတ်ပြီး global payment_info ကို update လုပ်မယ် """
    global payment_info
    settings_data = load_settings_db()
    payment_db = settings_data.get("payment_info", {})
    for key in payment_info:
        if key in payment_db:
            payment_info[key] = payment_db[key]
    print("ℹ️ Settings နှင့် Payment Info ကို DB မှ ရယူပြီးပါပြီ။")
    return settings_data

def save_settings_field(field_name, value):
    """ Settings document ထဲက field တစ်ခုကို update လုပ်မယ် """
    if field_name == "payment_info":
        global payment_info
        payment_info = value
    elif field_name.startswith("payment_info."):
        try:
            key = field_name.split('.')[-1]
            if key in payment_info: payment_info[key] = value
        except IndexError: pass
    return save_settings_field_db(field_name, value)

async def update_payment_info(key, value):
    """ Helper to update global payment_info and save the whole dict to DB """
    global payment_info
    payment_info[key] = value
    return save_settings_field_db("payment_info", payment_info)

def load_authorized_users():
    global AUTHORIZED_USERS
    authorized_list = load_authorized_users_db()
    AUTHORIZED_USERS = set(map(str, authorized_list))
    print(f"ℹ️ Authorized users {len(AUTHORIZED_USERS)} Loaded.")

def save_authorized_users():
    if save_authorized_users_db(list(AUTHORIZED_USERS)): print(f"ℹ️ Authorized users {len(AUTHORIZED_USERS)} Saved.")
    else: print("❌ Authorized users Save Error.")

def load_prices(): 
    return load_prices_db()
    
def save_prices(prices): 
    return save_prices_db(prices)

def get_user_data(user_id):
    if users_col is None: return None
    try:
        user_data = users_col.find_one({"_id": str(user_id)})
        if user_data:
            user_data.setdefault("balance", 0); user_data.setdefault("orders", [])
            user_data.setdefault("topups", []); user_data.setdefault("name", "Unknown")
            user_data.setdefault("username", "-")
        return user_data
    except Exception as e: 
        print(f"❌ Get User {user_id} Error: {e}"); return None

def update_user_data(user_id, update_fields):
    if users_col is None: return False
    try: 
        users_col.update_one({"_id": str(user_id)}, {"$set": update_fields}, upsert=True); return True
    except Exception as e: 
        print(f"❌ Update User {user_id} Error: {e}"); return False

def increment_user_balance(user_id, amount):
    if users_col is None: return False
    try:
        result = users_col.update_one({"_id": str(user_id)}, {"$inc": {"balance": amount}}, upsert=True)
        if result.upserted_id: 
            users_col.update_one({"_id": str(user_id)}, {"$setOnInsert": {"name": "New User", "username": "-", "orders": [], "topups": []}}, upsert=True)
        return True
    except Exception as e: 
        print(f"❌ Inc Balance {user_id} Error: {e}"); return False

def add_to_user_list(user_id, list_field, item):
    if users_col is None: return False
    try:
        users_col.update_one({"_id": str(user_id)}, {"$setOnInsert": {list_field: []}}, upsert=True)
        users_col.update_one({"_id": str(user_id)}, {"$push": {list_field: item}})
        return True
    except Exception as e: 
        print(f"❌ Push List {user_id}.{list_field} Error: {e}"); return False

def find_and_update_order_mongo(order_id, update_fields_without_prefix):
    if users_col is None: return None, None
    try:
        user_doc = users_col.find_one({"orders.order_id": order_id})
        if not user_doc: return None, None
        target_user_id = user_doc["_id"]; original_order = None; order_index = -1
        for i, o in enumerate(user_doc.get("orders", [])):
             if o.get("order_id") == order_id:
                 if o.get("status") != "pending": return target_user_id, o # Already processed
                 original_order = o; order_index = i; break
        if not original_order or order_index == -1: return None, None
        update_query = { f"orders.{order_index}.{key}": value for key, value in update_fields_without_prefix.items() }
        result = users_col.update_one({"_id": target_user_id, f"orders.{order_index}.order_id": order_id}, {"$set": update_query})
        if result.modified_count > 0: updated_order_data = {**original_order, **update_fields_without_prefix}; return target_user_id, updated_order_data
        elif result.matched_count > 0: print(f"ℹ️ Order ({order_id}) matched but not modified."); return target_user_id, original_order
        else: print(f"❌ Order ({order_id}) update match error."); return None, None
    except Exception as e: print(f"❌ Order ({order_id}) update error: {e}"); return None, None

def find_and_update_topup_mongo(topup_id, update_fields_without_prefix):
    if users_col is None: return None, None, None
    try:
        user_doc = users_col.find_one({"topups.topup_id": topup_id})
        if not user_doc: return None, None, None
        target_user_id = user_doc["_id"]; original_topup = None; topup_index = -1
        for i, t in enumerate(user_doc.get("topups", [])):
             if t.get("topup_id") == topup_id: original_topup = t; topup_index = i; break
        if not original_topup or topup_index == -1: return None, None, None
        status_before = original_topup.get("status"); topup_amount = original_topup.get("amount", 0)
        if status_before != "pending": return target_user_id, topup_amount, status_before # Already processed
        update_query = { f"topups.{topup_index}.{key}": value for key, value in update_fields_without_prefix.items() }
        result = users_col.update_one({"_id": target_user_id, f"topups.{topup_index}.topup_id": topup_id}, {"$set": update_query})
        if result.modified_count > 0: return target_user_id, topup_amount, status_before
        elif result.matched_count > 0: print(f"ℹ️ Topup ({topup_id}) matched but not modified."); return target_user_id, topup_amount, status_before
        else: print(f"❌ Topup ({topup_id}) update match error."); return None, None, None
    except Exception as e: print(f"❌ Topup ({topup_id}) update error: {e}"); return None, None, None

def get_admins(): 
    return load_admins_db()

# --- Utility Functions (Multi-line for Syntax Safety) ---

def is_user_authorized(user_id):
    """ Check if user is authorized (uses global AUTHORIZED_USERS set) """
    return str(user_id) in AUTHORIZED_USERS or is_admin(str(user_id))

async def is_bot_admin_in_group(bot, chat_id):
    """ Check if bot is admin in the group """
    if not chat_id or chat_id == 0: 
        return False
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
        is_admin_status = bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
        return is_admin_status
    except Exception as e: 
        print(f"Error check bot admin group {chat_id}: {e}")
        return False

def simple_reply(message_text):
    """ Simple auto-replies for common queries """
    message_lower = message_text.lower()
    if any(w in message_lower for w in ["hello","hi","မင်္ဂလာပါ","ဟယ်လို","ဟိုင်း","ကောင်းလား"]): 
        return "👋 မင်္ဂလာပါ! /start နှိပ်ပါ"
    elif any(w in message_lower for w in ["help","ကူညီ","အကူအညီ","မသိ","လမ်းညွှန်"]): 
        return "📱 Commands:\n• /start\n• /mmb\n• /balance\n• /topup\n• /price\n• /history"
    else: 
        return "📱 ***MLBB Bot***\n💎 /mmb သုံးပါ\n💰 /price နှိပ်ပါ\n🆘 /start နှိပ်ပါ"

def validate_game_id(g): 
    return g and g.isdigit() and 6<=len(g)<=10

def validate_server_id(s): 
    return s and s.isdigit() and 3<=len(s)<=5

def is_banned_account(g): 
    b=["123456789","000000000","111111111"]
    if g in b: return True
    if len(set(g))==1 and len(g)>5: return True
    if g.startswith("000") or g.endswith("000"): return True
    return False

def get_price(d):
    """ Get price for diamonds (uses load_prices which reads from DB) """
    cp=load_prices()
    if d in cp: return cp[d]
    if d.startswith("wp") and d[2:].isdigit(): 
        n=int(d[2:])
        return n*6000 if 1<=n<=10 else None # Example price
    t={"11":950,"22":1900,"33":2850,"56":4200,"112":8200,"86":5100,"172":10200,"257":15300,"343":20400,"429":25500,"514":30600,"600":35700,"706":40800,"878":51000,"963":56100,"1049":61200,"1135":66300,"1412":81600,"2195":122400,"3688":204000,"5532":306000,"9288":510000,"12976":714000,"55":3500,"165":10000,"275":16000,"565":33000}
    return t.get(d)

def is_payment_screenshot(up): 
    return up.message and up.message.photo

async def check_pending_topup(uid): 
    ud=get_user_data(uid)
    return any(t.get("status")=="pending" for t in ud.get("topups",[])) if ud else False

async def send_pending_topup_warning(up:Update): 
    await up.message.reply_text("⏳ ***Pending Topup ရှိ!***\n❌ Admin approve စောင့်ပါ။\n📞 Admin ကို ဆက်သွယ်ပါ။\n💡 /balance နဲ့ စစ်ပါ။",parse_mode="Markdown")

async def check_maintenance_mode(ct): 
    return bot_maintenance.get(ct,True)

async def send_maintenance_message(update: Update, command_type: str):
    """ Send maintenance mode message (Fixed Syntax) """
    user_name = update.effective_user.first_name or "User"
    msg = f"👋 {user_name}!\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
    if command_type == "orders":
        msg += "⏸️ ***Bot အော်ဒါတင်ခြင်း ခေတ္တပိတ်ထားပါသည်** ⏸️***"
    elif command_type == "topups":
        msg += "⏸️ ***Bot ငွေဖြည့်ခြင်း ခေတ္တပိတ်ထားပါသည်*** ⏸️"
    else: # general
        msg += "⏸️ ***Bot ခေတ္တပိတ်ထားပါသည်*** ⏸️"
    msg += "\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n***🔄 Admin ဖွင့်မှ သုံးနိုင်ပါမည်။***\n\n📞 Admin ကို ဆက်သွယ်ပါ။"
    try:
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        print(f"Error sending maintenance message (Markdown failed?): {e}")
        msg_plain = msg.replace("*", "").replace("`", "").replace("⏸️", "").replace("🔄", "").replace("📞", "")
        await update.message.reply_text(msg_plain)

def is_owner(uid):
    """Check if user is the owner (FIXED SYNTAX)"""
    try:
        return int(uid) == ADMIN_ID
    except (ValueError, TypeError):
        return False

def is_admin(uid):
    """Check if user is any admin (owner or appointed admin) (FIXED SYNTAX)"""
    try:
        user_id_int = int(uid)
        if user_id_int == ADMIN_ID:
            return True
        admin_list = get_admins() # Reads from DB
        return user_id_int in admin_list
    except (ValueError, TypeError):
        return False

# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; user_id = str(user.id)
    username = user.username or "-"; name = f"{user.first_name} {user.last_name or ''}".strip()
    if not is_user_authorized(user_id):
        kb = [[InlineKeyboardButton("📝 Register တောင်းဆို", callback_data="request_register")]]; markup=InlineKeyboardMarkup(kb)
        await update.message.reply_text(f"🚫 ***Bot သုံးခွင့်မရှိ!***\n\n👋 `{name}`!\n🆔 `{user_id}`\n\n❌ ***သုံးခွင့်တောင်းပါ***\n\n• Button နှိပ်\n• /register သုံး\n• Admin approve စောင့်", parse_mode="Markdown", reply_markup=markup); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    
    user_data = get_user_data(user_id)
    if not user_data:
        print(f"Creating user {user_id} in /start")
        initial_data = {"_id": user_id, "name": name, "username": username, "balance": 0, "orders": [], "topups": []}
        if users_col is not None: # <-- FIX: Check users_col
            try:
                users_col.insert_one(initial_data)
            except Exception as e:
                print(f"❌ User ({user_id}) insert error: {e}")
        else:
            print("❌ DB conn error (user creation failed in start)")
    elif user_data.get("name") != name or user_data.get("username") != username: 
        update_user_data(user_id, {"name": name, "username": username})

    if user_id in user_states: del user_states[user_id]
    clickable_name = f"[{name}](tg://user?id={user_id})"
    msg = (f"👋 ***မင်္ဂလာပါ*** {clickable_name}!\n🆔 `{user_id}`\n\n💎 ***𝙆𝙀𝘼 𝙈𝙇𝘽𝘽 𝘼𝙐𝙏𝙊 𝙏𝙊𝙋 𝙐𝙋 𝘽𝙊𝙏***\n\n***Commands***:\n➤ /mmb\n➤ /balance\n➤ /topup\n➤ /price\n➤ /history\n\n📌 ဥပမာ:\n`/mmb 123 12 wp1`\n`/mmb 456 45 86`\n\n📞 Owner ကို ဆက်သွယ်ပါ။")
    try:
        photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        if photos.total_count > 0: await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photos.photos[0][0].file_id, caption=msg, parse_mode="Markdown")
        else: await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e: print(f"Error photo /start: {e}"); await update.message.reply_text(msg, parse_mode="Markdown")

async def mmb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_user_authorized(user_id): kb=[[InlineKeyboardButton("👑 Owner",url=f"tg://user?id={ADMIN_ID}")]]; await update.message.reply_text("🚫 သုံးခွင့်မရှိပါ။",reply_markup=InlineKeyboardMarkup(kb)); return
    if not await check_maintenance_mode("orders"): await send_maintenance_message(update, "orders"); return
    if user_id in user_states and user_states[user_id]=="waiting_approval": await update.message.reply_text("⏳ ***Admin approve စောင့်ပါ။***",parse_mode="Markdown"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ***Topup အရင်ပြီးအောင်လုပ်ပါ!***",parse_mode="Markdown"); return

    args = context.args
    if len(args) != 3: await update.message.reply_text("❌ Format:\n/mmb gameid serverid amount\nဥပမာ:\n`/mmb 123 12 86`", parse_mode="Markdown"); return
    game_id, server_id, amount_str = args
    if not validate_game_id(game_id): return await update.message.reply_text("❌ ***Game ID မှား!*** (6-10 digits)", parse_mode="Markdown")
    if not validate_server_id(server_id): return await update.message.reply_text("❌ ***Server ID မှား!*** (3-5 digits)", parse_mode="Markdown")

    if is_banned_account(game_id):
        await update.message.reply_text(f"🚫 ***Account Ban!*** ID: `{game_id}`\n❌ Topup လုပ်မရပါ။", parse_mode="Markdown")
        admin_msg = (f"🚫 ***Banned Account***\n👤 User: [{update.effective_user.first_name}](tg://user?id={user_id}) (`{user_id}`)\n🎮 `{game_id}` (`{server_id}`) Amt: {amount_str}")
        admins=get_admins(); asyncio.gather(*[context.bot.send_message(chat_id=aid, text=admin_msg, parse_mode="Markdown") for aid in admins]); return

    price = get_price(amount_str)
    if not price: await update.message.reply_text("❌ Diamond amount မှား!\n/price ကို ကြည့်ပါ။", parse_mode="Markdown"); return

    user_data = get_user_data(user_id); user_balance = user_data.get("balance", 0) if user_data else 0
    if user_balance < price:
        kb=[[InlineKeyboardButton("💳 ငွေဖြည့်", callback_data="topup_button")]]; markup=InlineKeyboardMarkup(kb)
        await update.message.reply_text(f"❌ ***Balance မလုံလောက်!***\n💰 လိုအပ်: {price:,}\n💳 လက်ကျန်: {user_balance:,}\n❗ လိုသေး: {price - user_balance:,}\n`/topup amount` သုံးပါ။", parse_mode="Markdown", reply_markup=markup); return

    order_id = f"ORD{datetime.now().strftime('%y%m%d%H%M')}{user_id[-2:]}"
    order_data = {"order_id": order_id, "game_id": game_id, "server_id": server_id, "amount": amount_str, "price": price, "status": "pending", "timestamp": datetime.now().isoformat(), "chat_id": update.effective_chat.id}
    bal_ok = increment_user_balance(user_id, -price); ord_ok = add_to_user_list(user_id, "orders", order_data)

    if not (bal_ok and ord_ok):
        print(f"❌ Order fail {order_id} user {user_id}. BalOK:{bal_ok}, OrdOK:{ord_ok}")
        if bal_ok and not ord_ok: increment_user_balance(user_id, price); await update.message.reply_text("❌ Order DB Error! ငွေ ပြန်အမ်းပြီး။")
        else: await update.message.reply_text("❌ Order Error!")
        return

    ud = get_user_data(user_id); nb = ud.get("balance", 0) if ud else user_balance - price
    await update.message.reply_text(f"✅ ***Order OK!***\n📝 ID: `{order_id}`\n🎮 `{game_id}` (`{server_id}`) 💎 {amount_str}\n💰 ကုန်ကျ: {price:,}\n💳 လက်ကျန်: {nb:,}\n📊 Status: ⏳ ***Pending***\n⚠️ ***Admin confirm စောင့်ပါ။***", parse_mode="Markdown")

    kb=[[InlineKeyboardButton("✅ Confirm", callback_data=f"order_confirm_{order_id}"), InlineKeyboardButton("❌ Cancel", callback_data=f"order_cancel_{order_id}")]]
    markup=InlineKeyboardMarkup(kb); un = update.effective_user.first_name or user_id
    admin_msg = (f"🔔 ***Order!*** ID: `{order_id}`\n👤 [{un}](tg://user?id={user_id})\n🎮 `{game_id}` (`{server_id}`) 💎 {amount_str}\n💰 {price:,} MMK\n📊 ⏳ Pending")
    admins=get_admins();
    for aid in admins:
        try: await context.bot.send_message(chat_id=aid, text=admin_msg, parse_mode="Markdown", reply_markup=markup)
        except Exception as e: print(f"Fail send order admin {aid}: {e}")

    if ADMIN_GROUP_ID:
        try:
            if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                gmsg = (f"🛒 Order `{order_id}`\n👤 [{un}](tg://user?id={user_id})\n🎮 `{game_id}` (`{server_id}`) 💎 {amount_str} ({price:,} MMK)\n#NewOrder")
                await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=gmsg, parse_mode="Markdown")
        except Exception as e: print(f"Fail send order group {ADMIN_GROUP_ID}: {e}")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_user_authorized(user_id): kb=[[InlineKeyboardButton("👑 Owner",url=f"tg://user?id={ADMIN_ID}")]]; await update.message.reply_text("🚫 သုံးခွင့်မရှိပါ။",reply_markup=InlineKeyboardMarkup(kb)); return
    if user_id in user_states and user_states[user_id]=="waiting_approval": await update.message.reply_text("⏳ ***Admin approve စောင့်ပါ။***",parse_mode="Markdown"); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ***Topup ဆက်လုပ်ပါ။***",parse_mode="Markdown"); return

    user_data = get_user_data(user_id)
    if not user_data: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return

    bal = user_data.get("balance", 0); orders_n = len(user_data.get("orders", [])); topups_n = len(user_data.get("topups", []))
    name = user_data.get('name','?').replace('*','').replace('_','').replace('`',''); un = user_data.get('username','-').replace('*','').replace('_','').replace('`','')
    un_disp = f"@{un}" if un and un != "-" else "None"

    pend_n=0; pend_amt=0
    for t in user_data.get("topups", []):
        if t.get("status") == "pending": pend_n+=1; pend_amt+=t.get("amount",0)
    st_msg = f"\n⏳ Pending: {pend_n} ({pend_amt:,} MMK)\n❗ Order ထားမရပါ။" if pend_n>0 else ""

    kb=[[InlineKeyboardButton("💳 ငွေဖြည့်", callback_data="topup_button")]]; markup=InlineKeyboardMarkup(kb)
    bal_txt = (f"💳 ***Account Info***\n💰 Balance: `{bal:,} MMK`\n📦 Orders: {orders_n}\n💳 Topups: {topups_n}{st_msg}\n\n👤 Name: {name}\n🆔 Username: {un_disp}")

    try:
        photos = await context.bot.get_user_profile_photos(user_id=int(user_id), limit=1)
        if photos.total_count > 0: await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photos.photos[0][0].file_id, caption=bal_txt, parse_mode="Markdown", reply_markup=markup)
        else: await update.message.reply_text(bal_txt, parse_mode="Markdown", reply_markup=markup)
    except Exception as e: print(f"Err balance photo {user_id}: {e}"); await update.message.reply_text(bal_txt, parse_mode="Markdown", reply_markup=markup)

async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_user_authorized(user_id): kb=[[InlineKeyboardButton("👑 Owner",url=f"tg://user?id={ADMIN_ID}")]]; await update.message.reply_text("🚫 သုံးခွင့်မရှိပါ။",reply_markup=InlineKeyboardMarkup(kb)); return
    if not await check_maintenance_mode("topups"): await send_maintenance_message(update, "topups"); return
    if user_id in user_states and user_states[user_id]=="waiting_approval": await update.message.reply_text("⏳ ***Admin approve စောင့်ပါ။***",parse_mode="Markdown"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ***Topup ဆက်လုပ်ပါ!***",parse_mode="Markdown"); return

    args = context.args
    if len(args) != 1: await update.message.reply_text("❌ Format: `/topup <amount>`\nဥပမာ: `/topup 10000`\n💡 Min: 1,000",parse_mode="Markdown"); return
    try:
        amount = int(args[0])
        if amount < 1000: await update.message.reply_text("❌ ***Amount နည်းလွန်း!***\n💰 ***Min: 1,000 MMK***",parse_mode="Markdown"); return
    except ValueError: await update.message.reply_text("❌ ***Amount မှား!***\n💰 ***ဂဏန်းသာ ရေးပါ။***",parse_mode="Markdown"); return

    pending_topups[user_id] = {"amount": amount, "timestamp": datetime.now().isoformat()}
    kb = [[InlineKeyboardButton("📱 KBZ Pay", callback_data=f"topup_pay_kpay_{amount}")],[InlineKeyboardButton("📱 Wave Money", callback_data=f"topup_pay_wave_{amount}")],[InlineKeyboardButton("❌ Cancel", callback_data="topup_cancel")]]
    markup=InlineKeyboardMarkup(kb)
    await update.message.reply_text(f"💳 ***ငွေဖြည့်***\n✅ Amount: `{amount:,} MMK`\n***⬇️ Payment app ရွေးပါ***:\nℹ️ /cancel", parse_mode="Markdown", reply_markup=markup)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_user_authorized(user_id): return
    if not is_payment_screenshot(update): await update.message.reply_text("❌ ***Screenshot သာ လက်ခံသည်။***", parse_mode="Markdown"); return
    if user_id not in pending_topups: await update.message.reply_text("❌ ***Topup process မရှိ!***\n🔄 `/topup amount` သုံးပါ။", parse_mode="Markdown"); return

    pending = pending_topups[user_id]; amount = pending["amount"]
    payment_method = pending.get("payment_method", "Unknown")
    if payment_method == "Unknown": await update.message.reply_text("❌ ***Payment app အရင်ရွေးပါ!***", parse_mode="Markdown"); return

    user_states[user_id] = "waiting_approval"
    topup_id = f"TOP{datetime.now().strftime('%y%m%d%H%M')}{user_id[-3:]}"
    user_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    topup_data = {"topup_id": topup_id, "amount": amount, "payment_method": payment_method, "status": "pending", "timestamp": datetime.now().isoformat(), "screenshot_file_id": update.message.photo[-1].file_id, "chat_id": update.effective_chat.id }
    ud = get_user_data(user_id)
    if not ud: update_user_data(user_id, {"name": user_name, "username": update.effective_user.username or "-", "balance": 0, "orders": [], "topups": []}) # Ensure user exists

    if not add_to_user_list(user_id, "topups", topup_data):
        print(f"❌ DB save fail topup {topup_id} user {user_id}."); await update.message.reply_text("❌ DB Error! Admin ကို ဆက်သွယ်ပါ။")
        if user_id in user_states: del user_states[user_id]; return

    admin_msg = (f"💳 ***Topup Request***\n👤 [{user_name}](tg://user?id={user_id}) (`{user_id}`)\n💰 `{amount:,} MMK` ({payment_method.upper()})\n🔖 ID: `{topup_id}`\n📊 ⏳ Pending\n***Screenshot စစ်ပါ။***")
    kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"topup_approve_{topup_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"topup_reject_{topup_id}")]]
    markup=InlineKeyboardMarkup(kb); photo_id = update.message.photo[-1].file_id
    admins = get_admins();
    for aid in admins:
        try: await context.bot.send_photo(chat_id=aid, photo=photo_id, caption=admin_msg, parse_mode="Markdown", reply_markup=markup)
        except Exception as e: print(f"Fail send topup admin {aid}: {e}")

    if ADMIN_GROUP_ID:
        try:
            if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                gmsg = (f"💳 ***Topup!***\n👤 [{user_name}](tg://user?id={user_id})\n💰 `{amount:,} MMK` ({payment_method.upper()})\n🔖 `{topup_id}`\n📊 ⏳ Pending\n`/approve {user_id} {amount}`\n#TopupRequest")
                await context.bot.send_photo(chat_id=ADMIN_GROUP_ID, photo=photo_id, caption=gmsg, parse_mode="Markdown", reply_markup=markup)
        except Exception as e: print(f"Fail send topup group {ADMIN_GROUP_ID}: {e}")

    del pending_topups[user_id]
    await update.message.reply_text(f"✅ ***Screenshot OK!***\n💰 `{amount:,} MMK`\n\n🔒 ***ကန့်သတ်ပါ***\n❌ ***Admin approve မလုပ်မချင်း သုံးမရပါ။***\n⏰ ***Admin စစ်ဆေးပြီး approve လုပ်ပါမည်။***", parse_mode="Markdown")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_user_authorized(user_id): kb=[[InlineKeyboardButton("👑 Owner",url=f"tg://user?id={ADMIN_ID}")]]; await update.message.reply_text("🚫 သုံးခွင့်မရှိပါ။",reply_markup=InlineKeyboardMarkup(kb)); return
    if user_id in user_states and user_states[user_id]=="waiting_approval": await update.message.reply_text("⏳ ***Admin approve စောင့်ပါ။***",parse_mode="Markdown"); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ***Topup ဆက်လုပ်ပါ။***",parse_mode="Markdown"); return
    if await check_pending_topup(user_id): await send_pending_topup_warning(update); return

    user_data = get_user_data(user_id)
    if not user_data: await update.message.reply_text("❌ /start ကို အရင်နှိပ်ပါ။"); return
    orders = user_data.get("orders", []); topups = user_data.get("topups", [])
    if not orders and not topups: await update.message.reply_text("📋 မှတ်တမ်း မရှိသေးပါ။"); return

    msg = "📋 ***နောက်ဆုံး မှတ်တမ်းများ***\n\n"; limit = 5
    if orders:
        msg += f"🛒 ***Orders (Last {limit}):***\n"
        for o in orders[-limit:]:
            st=o.get("status","?"); em="✅" if st=="confirmed" else ("❌" if st=="cancelled" else "⏳")
            ts=o.get('timestamp',''); dt = '?'
            if ts: try: dt=datetime.fromisoformat(ts).strftime('%y-%m-%d') except: pass
            msg += f"{em} `{o.get('order_id','?')}` ({o.get('amount','?')} dia) {o.get('price',0):,} MMK [{dt}]\n"
        msg += "\n"
    if topups:
        msg += f"💳 ***Topups (Last {limit}):***\n"
        for t in topups[-limit:]:
            st=t.get("status","?"); em="✅" if st=="approved" else ("❌" if st=="rejected" else "⏳")
            ts=t.get('timestamp',''); dt = '?'
            if ts: try: dt=datetime.fromisoformat(ts).strftime('%y-%m-%d') except: pass
            msg += f"{em} {t.get('amount',0):,} MMK ({t.get('payment_method','?').upper()}) [{dt}]\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_user_authorized(user_id): kb=[[InlineKeyboardButton("👑 Owner",url=f"tg://user?id={ADMIN_ID}")]]; await update.message.reply_text("🚫 သုံးခွင့်မရှိပါ။",reply_markup=InlineKeyboardMarkup(kb)); return
    if user_id in user_states and user_states[user_id]=="waiting_approval": await update.message.reply_text("⏳ ***Admin approve စောင့်ပါ။***",parse_mode="Markdown"); return
    if user_id in pending_topups: await update.message.reply_text("⏳ ***Topup ဆက်လုပ်ပါ။***",parse_mode="Markdown"); return

    custom_prices = load_prices()
    default_prices = { "wp1": 6000, "wp2": 12000, "wp3": 18000, "wp4": 24000, "wp5": 30000, "wp6": 36000, "wp7": 42000, "wp8": 48000, "wp9": 54000, "wp10": 60000, "11": 950, "22": 1900, "33": 2850, "56": 4200, "86": 5100, "112": 8200, "172": 10200, "257": 15300, "343": 20400, "429": 25500, "514": 30600, "600": 35700, "706": 40800, "878": 51000, "963": 56100, "1049": 61200, "1135": 66300, "1412": 81600, "2195": 122400, "3688": 204000, "5532": 306000, "9288": 510000, "12976": 714000, "55": 3500, "165": 10000, "275": 16000, "565": 33000 }
    current_prices = {**default_prices, **custom_prices}

    price_msg = "💎 ***MLBB ဈေးနှုန်းများ***\n\n🎟️ ***Weekly Pass***:\n"
    for i in range(1, 11): wpk = f"wp{i}"; price_msg += f"• {wpk} = {current_prices.get(wpk, 'N/A'):,} MMK\n" if wpk in current_prices else ""
    price_msg += "\n💎 ***Regular Diamonds***:\n"
    reg_dia = ["11","22","33","56","86","112","172","257","343","429","514","600","706","878","963","1049","1135","1412","2195","3688","5532","9288","12976"]
    for d in reg_dia: price_msg += f"• {d} = {current_prices.get(d, 'N/A'):,} MMK\n" if d in current_prices else ""
    price_msg += "\n💎 ***2X Pass***:\n"
    dx_dia = ["55", "165", "275", "565"]
    for d in dx_dia: price_msg += f"• {d} = {current_prices.get(d, 'N/A'):,} MMK\n" if d in current_prices else ""
    other_customs = {k: v for k, v in custom_prices.items() if k not in default_prices}
    if other_customs: price_msg += "\n🔥 ***Special Items***:\n"; for item, price in other_customs.items(): price_msg += f"• {item} = {price:,} MMK\n"
    price_msg += "\n📝 `/mmb gameid serverid amount`\nဥပမာ:\n`/mmb 123 12 wp1`"
    await update.message.reply_text(price_msg, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_user_authorized(user_id): return
    if user_id in pending_topups: del pending_topups[user_id]; await update.message.reply_text("✅ ***ငွေဖြည့်ခြင်း ပယ်ဖျက်!***", parse_mode="Markdown")
    else: await update.message.reply_text("ℹ️ ***Pending ငွေဖြည့်မှု မရှိပါ။***", parse_mode="Markdown")

async def c_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id in user_states and user_states[user_id]=="waiting_approval": return await update.message.reply_text("❌ Admin approve စောင့်ပါ။",parse_mode="Markdown")
    args = context.args
    if not args: return await update.message.reply_text("🧮 `/c <expression>`",parse_mode="Markdown")
    expression = ''.join(args).replace(' ',''); pattern = r'^[0-9+\-*/().]+$'
    if not re.match(pattern, expression) or not any(op in expression for op in ['+','-','*','/']): return await update.message.reply_text("❌ Invalid!",parse_mode="Markdown")
    try: result = eval(expression); await update.message.reply_text(f"🧮 `{expression}` = ***{result:,}***",parse_mode="Markdown")
    except ZeroDivisionError: await update.message.reply_text("❌ Zero ဖြင့် စားမရပါ။")
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}")

async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; req_user_id = str(user.id)
    username = user.username or "-"; name = f"{user.first_name} {user.last_name or ''}".strip()
    load_authorized_users()
    if is_user_authorized(req_user_id): await update.message.reply_text("✅ သုံးခွင့်ရပြီးသား!", parse_mode="Markdown"); return
    kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"register_approve_{req_user_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"register_reject_{req_user_id}")]]
    markup = InlineKeyboardMarkup(kb); username_display = f"@{username}" if username != "-" else "None"
    owner_msg = (f"📝 ***Register Request***\n👤 [{name}](tg://user?id={req_user_id}) (`{req_user_id}`)\n📱 Username: {username_display}\n⏰ {datetime.now().strftime('%H:%M:%S')}\n***Approve?***")
    admins = get_admins(); sent_admins = 0; photo_id = None
    try: photos = await context.bot.get_user_profile_photos(user_id=int(req_user_id), limit=1); photo_id = photos.photos[0][0].file_id if photos.total_count > 0 else None
    except: pass
    
    tasks = []
    for admin_id in admins:
        if photo_id: tasks.append(context.bot.send_photo(chat_id=admin_id, photo=photo_id, caption=owner_msg, parse_mode="Markdown", reply_markup=markup))
        else: tasks.append(context.bot.send_message(chat_id=admin_id, text=owner_msg, parse_mode="Markdown", reply_markup=markup))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    sent_admins = sum(1 for r in results if not isinstance(r, Exception)); failed_admins = len(admins) - sent_admins
    if failed_admins > 0: print(f"⚠️ Failed send register req to {failed_admins} admins.")
    
    user_confirm = (f"✅ ***Request ပို့ပြီး!***\n👤 {name}\n🆔 `{req_user_id}`\n⏳ ***Admin approve စောင့်ပါ ({sent_admins} notified)***")
    try:
        if photo_id: await update.message.reply_photo(photo=photo_id, caption=user_confirm, parse_mode="Markdown")
        else: await update.message.reply_text(user_confirm, parse_mode="Markdown")
    except Exception as e: print(f"Err confirm reg user {req_user_id}: {e}"); await update.message.reply_text(user_confirm, parse_mode="Markdown")

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id); admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()
    if not is_admin(admin_user_id): await update.message.reply_text("❌ Admin မဟုတ်ပါ။"); return
    args = context.args
    if len(args) != 2: await update.message.reply_text("❌ Format: `/approve <user_id> <amount>`"); return
    target_user_id = args[0]
    try: amount = int(args[1]);
    except ValueError: return await update.message.reply_text("❌ Amount မှား!")
    if amount <= 0: return await update.message.reply_text("❌ Amount > 0 ဖြစ်ရမည်!")
    
    target_user_data = get_user_data(target_user_id)
    if not target_user_data: return await update.message.reply_text(f"❌ User ID `{target_user_id}` မတွေ့ပါ။")
    
    pending_topup_found = None; topup_id = None
    for topup in reversed(target_user_data.get("topups", [])):
         if topup.get("status") == "pending" and topup.get("amount") == amount:
             pending_topup_found = topup; topup_id = topup.get("topup_id"); break
    if not pending_topup_found or not topup_id: return await update.message.reply_text(f"❌ User `{target_user_id}` အတွက် `{amount:,}` MMK Pending topup မတွေ့ပါ။")

    topup_update_fields = {"status": "approved", "approved_by": admin_name, "approved_at": datetime.now().isoformat()}
    tid, tamt, tstat = find_and_update_topup_mongo(topup_id, topup_update_fields)
    
    if tid is None:
        if tstat == "approved": return await update.message.reply_text(f"ℹ️ Topup `{topup_id}` approved ပြီးသား။")
        else: return await update.message.reply_text(f"❌ Topup `{topup_id}` approve လုပ်မရပါ။ (DB Error?)")

    balance_added = increment_user_balance(target_user_id, amount)
    if not balance_added: print(f"⚠️ Topup {topup_id} approved, but balance fail {target_user_id}!"); await update.message.reply_text("❌ DB Balance Error!"); return

    if target_user_id in user_states: del user_states[target_user_id]
    
    updated_user_data = get_user_data(target_user_id); new_balance = updated_user_data.get("balance", "Error") if updated_user_data else "Error"
    try:
        bot_username = (await context.bot.get_me()).username
        kb=[[InlineKeyboardButton("💎 Order",url=f"https://t.me/{bot_username}?start=order")]];markup=InlineKeyboardMarkup(kb)
        await context.bot.send_message(chat_id=int(target_user_id), text=f"✅ ***Topup Approved!*** 🎉\n💰 Amount: `{amount:,}`\n💳 Balance: `{new_balance:,}`\n👤 By: {admin_name}\n⏰ {datetime.now().strftime('%H:%M:%S')}\n\n🎉 ***Diamonds ဝယ်နိုင်ပြီ!***\n🔓 ***Bot ပြန်သုံးနိုင်ပြီ!***", parse_mode="Markdown", reply_markup=markup)
    except Exception as e: print(f"Fail notify user {target_user_id} approve: {e}"); await update.message.reply_text(f"⚠️ User {target_user_id} ကို အကြောင်းမကြားနိုင်ပါ။")
    
    await update.message.reply_text(f"✅ ***Approve OK!***\n👤 User: `{target_user_id}`\n💰 Amount: `{amount:,}`\n💳 New Bal: `{new_balance:,}`\n🔓 Restrictions Cleared!", parse_mode="Markdown")

async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id)
    if not is_admin(admin_user_id): await update.message.reply_text("❌ Admin မဟုတ်ပါ။"); return
    args = context.args
    if len(args) != 2: await update.message.reply_text("❌ Format: `/deduct <user_id> <amount>`"); return
    target_user_id = args[0]
    try: amount = int(args[1]);
    except ValueError: return await update.message.reply_text("❌ Amount မှား!")
    if amount <= 0: return await update.message.reply_text("❌ Amount > 0 ဖြစ်ရမည်!")
    
    user_data = get_user_data(target_user_id)
    if not user_data: return await update.message.reply_text(f"❌ User ID `{target_user_id}` မတွေ့ပါ။")
    current_balance = user_data.get("balance", 0)
    if current_balance < amount: return await update.message.reply_text(f"❌ ***Balance မလုံလောက်!***\n👤 `{target_user_id}`\n💰 နှုတ်မည်: `{amount:,}`\n💳 လက်ကျန်: `{current_balance:,}`", parse_mode="Markdown")
    
    if not increment_user_balance(target_user_id, -amount): await update.message.reply_text("❌ DB Error! Balance မနှုတ်နိုင်ပါ။"); return
    
    ud=get_user_data(target_user_id); nb=ud.get("balance",0) if ud else current_balance-amount
    try: await context.bot.send_message(chat_id=int(target_user_id), text=f"⚠️ ***Balance နှုတ်ခံရ!***\n💰 Amount: `{amount:,}`\n💳 New Bal: `{nb:,}`\n⏰ {datetime.now().strftime('%H:%M:%S')}\n📞 Admin ကို ဆက်သွယ်ပါ။", parse_mode="Markdown")
    except Exception as e: print(f"Fail notify user {target_user_id} deduct: {e}"); await update.message.reply_text(f"⚠️ User {target_user_id} ကို အကြောင်းမကြားနိုင်ပါ။")
    
    await update.message.reply_text(f"✅ ***Deduct OK!***\n👤 User: `{target_user_id}`\n💰 နှုတ်: `{amount:,}`\n💳 New Bal: `{nb:,}`", parse_mode="Markdown")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id); admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()
    if not is_admin(admin_user_id): await update.message.reply_text("❌ Admin မဟုတ်ပါ။"); return
    args = context.args
    if len(args) != 1 or not args[0].isdigit(): await update.message.reply_text("❌ Format: /ban <user_id>"); return
    
    target_user_id = args[0]
    load_authorized_users()
    if is_owner(target_user_id): return await update.message.reply_text("❌ Owner ကို ban မရပါ။")
    if is_admin(target_user_id) and not is_owner(admin_user_id): return await update.message.reply_text("❌ Admin အချင်းချင်း ban မရပါ။")
    if target_user_id not in AUTHORIZED_USERS: return await update.message.reply_text(f"ℹ️ User `{target_user_id}` authorize မရှိပါ။")
    
    AUTHORIZED_USERS.remove(target_user_id); save_authorized_users()
    try: await context.bot.send_message(chat_id=int(target_user_id), text="🚫 ***Bot Ban!***\n❌ Admin က သင့်ကို ban လိုက်ပါပြီ။\n📞 Admin ကို ဆက်သွယ်ပါ။", parse_mode="Markdown")
    except Exception as e: print(f"Fail notify banned user {target_user_id}: {e}")
    
    ud=get_user_data(target_user_id); un=ud.get("name","?") if ud else "?"
    if ADMIN_ID != int(admin_user_id): # Notify owner if not owner
        try: await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚫 *User Ban Info*\n👤 Admin: [{admin_name}](tg://user?id={admin_user_id})\n🎯 Banned: [{un}](tg://user?id={target_user_id}) (`{target_user_id}`)", parse_mode="Markdown")
        except Exception as e: print(f"Fail notify owner ban: {e}")
    if ADMIN_GROUP_ID:
        try:
            if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                gmsg=(f"🚫 ***User Ban!***\n👤 User: [{un}](tg://user?id={target_user_id}) (`{target_user_id}`)\n👤 By: {admin_name}\n#UserBanned")
                await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=gmsg, parse_mode="Markdown")
        except Exception as e: print(f"Fail notify group ban: {e}")
        
    await update.message.reply_text(f"✅ User Ban OK!\n👤 `{target_user_id}`\n📝 Total auth: {len(AUTHORIZED_USERS)}", parse_mode="Markdown")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id); admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()
    if not is_admin(admin_user_id): await update.message.reply_text("❌ Admin မဟုတ်ပါ။"); return
    args = context.args
    if len(args) != 1 or not args[0].isdigit(): await update.message.reply_text("❌ Format: /unban <user_id>"); return
    
    target_user_id = args[0]
    load_authorized_users()
    if target_user_id in AUTHORIZED_USERS: return await update.message.reply_text(f"ℹ️ User `{target_user_id}` authorize ရှိပြီးသား။")
    
    AUTHORIZED_USERS.add(target_user_id); save_authorized_users()
    if target_user_id in user_states: del user_states[target_user_id]
    
    try: await context.bot.send_message(chat_id=int(target_user_id), text="🎉 *Bot Unban!*\n✅ Admin က ban ဖြုတ်ပေးပါပြီ။\n🚀 /start နှိပ်ပါ။", parse_mode="Markdown")
    except Exception as e: print(f"Fail notify unbanned user {target_user_id}: {e}")

    ud=get_user_data(target_user_id); un=ud.get("name","?") if ud else "?"
    if ADMIN_ID != int(admin_user_id): # Notify owner
        try: await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ *User Unban Info*\n👤 Admin: [{admin_name}](tg://user?id={admin_user_id})\n🎯 Unbanned: [{un}](tg://user?id={target_user_id}) (`{target_user_id}`)", parse_mode="Markdown")
        except Exception as e: print(f"Fail notify owner unban: {e}")
    if ADMIN_GROUP_ID:
        try:
            if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                gmsg=(f"✅ ***User Unban!***\n👤 User: [{un}](tg://user?id={target_user_id}) (`{target_user_id}`)\n👤 By: {admin_name}\n#UserUnbanned")
                await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=gmsg, parse_mode="Markdown")
        except Exception as e: print(f"Fail notify group unban: {e}")

    await update.message.reply_text(f"✅ User Unban OK!\n👤 `{target_user_id}`\n📝 Total auth: {len(AUTHORIZED_USERS)}", parse_mode="Markdown")

async def addadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id)
    if not is_owner(admin_user_id): await update.message.reply_text("❌ Owner Only!"); return
    args = context.args
    if len(args) != 1 or not args[0].isdigit(): await update.message.reply_text("❌ Format: /addadm <user_id>"); return
    new_admin_id = int(args[0])
    if new_admin_id in get_admins(): await update.message.reply_text("ℹ️ Admin ဖြစ်ပြီးသား။"); return
    if not add_admin_db(new_admin_id): await update.message.reply_text("❌ DB Error!"); return
    new_admin_list = get_admins()
    try: await context.bot.send_message(chat_id=new_admin_id, text="🎉 Admin ရာထူးရပြီ!\n✅ Owner က ခန့်အပ်။\n🔧 /adminhelp ကြည့်ပါ။\n⚠️ Owner command သုံးမရပါ။")
    except Exception as e: print(f"Fail notify new admin {new_admin_id}: {e}")
    await update.message.reply_text(f"✅ ***Admin Added!***\n👤 ID: `{new_admin_id}`\n📝 Total: {len(new_admin_list)}", parse_mode="Markdown")

async def unadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id)
    if not is_owner(admin_user_id): await update.message.reply_text("❌ Owner Only!"); return
    args = context.args
    if len(args) != 1 or not args[0].isdigit(): await update.message.reply_text("❌ Format: /unadm <user_id>"); return
    target_admin_id = int(args[0])
    if target_admin_id == ADMIN_ID: await update.message.reply_text("❌ Owner ကို ဖြုတ်မရ!"); return
    if target_admin_id not in get_admins(): await update.message.reply_text("ℹ️ Admin မဟုတ်ပါ။"); return
    if not remove_admin_db(target_admin_id): await update.message.reply_text("❌ DB Error!"); return
    new_admin_list = get_admins()
    try: await context.bot.send_message(chat_id=target_admin_id, text="⚠️ Admin ရာထူး ရုပ်သိမ်း!\n❌ Owner က ဖြုတ်လိုက်ပြီ။")
    except Exception as e: print(f"Fail notify removed admin {target_admin_id}: {e}")
    await update.message.reply_text(f"✅ ***Admin Removed!***\n👤 ID: `{target_admin_id}`\n📝 Total: {len(new_admin_list)}", parse_mode="Markdown")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id): await update.message.reply_text("❌ Owner Only!"); return
    args = context.args
    if not update.message.reply_to_message: await update.message.reply_text("❌ Message ကို reply လုပ်ပြီး သုံးပါ။\n`/broadcast user` or `/broadcast gp` or `/broadcast user gp`"); return
    if len(args) == 0: await update.message.reply_text("❌ Target ထည့်ပါ: `user`, `gp`, or `user gp`"); return
    send_to_users = "user" in args; send_to_groups = "gp" in args
    if not send_to_users and not send_to_groups: await update.message.reply_text("❌ Target မှား: `user`, `gp`, or `user gp`"); return

    replied_msg = update.message.reply_to_message
    user_success = 0; user_fail = 0; group_success = 0; group_fail = 0;
    user_ids = []; group_ids = set()

    if users_col is None: print("❌ DB Conn Error (Broadcast)"); return await update.message.reply_text("❌ DB Error!")

    try:
        if send_to_users: user_ids = [doc['_id'] for doc in users_col.find({}, {'_id': 1})]
        if send_to_groups:
            order_chats = users_col.distinct("orders.chat_id", {"orders.chat_id": {"$lt": 0}})
            topup_chats = users_col.distinct("topups.chat_id", {"topups.chat_id": {"$lt": 0}})
            group_ids.update(order_chats); group_ids.update(topup_chats)
    except Exception as e: print(f"❌ Broadcast ID fetch error: {e}"); await update.message.reply_text("❌ DB Error (Fetch IDs)!"); return

    await update.message.reply_text(f"Sending broadcast to {len(user_ids)} users and {len(group_ids)} groups... Please wait.")
    
    async def send_message_copy(chat_id, msg_to_copy):
        try:
            await msg_to_copy.copy(chat_id=chat_id)
            return True
        except Exception as e:
            print(f"Broadcast fail {chat_id}: {e}"); return False

    if send_to_users:
        for uid in user_ids:
            if await send_message_copy(int(uid), replied_msg): user_success += 1
            else: user_fail += 1
            await asyncio.sleep(0.05) # Rate limit
    if send_to_groups:
        for gid in group_ids:
            if await send_message_copy(gid, replied_msg): group_success += 1
            else: group_fail += 1
            await asyncio.sleep(0.05) # Rate limit

    targets_report = []
    if send_to_users: targets_report.append(f"Users: {user_success} OK, {user_fail} Fail")
    if send_to_groups: targets_report.append(f"Groups: {group_success} OK, {group_fail} Fail")
    await update.message.reply_text(f"✅ Broadcast Done!\n\n👥 {chr(10).join(targets_report)}\n📊 Total: {user_success + group_success} Sent", parse_mode="Markdown")

async def adminhelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id);
    if not is_admin(user_id): return await update.message.reply_text("❌ Admin မဟုတ်ပါ။")
    is_user_owner = is_owner(user_id)
    help_msg = "🔧 *Admin Commands* 🔧\n\n"
    if is_user_owner: help_msg += ("👑 *Owner Only:*\n• /addadm <id>\n• /unadm <id>\n• /ban <id>\n• /unban <id>\n• /broadcast ...\n• /addbot <token>\n• /removebot <id>\n• /addfund <admin_id> <amt>\n• /deductfund <admin_id> <amt>\n• /set*qr, /remove*qr\n\n")
    help_msg += ("💰 *Balance:*\n• /approve <id> <amt>\n• /deduct <id> <amt>\n💬 *Comm:*\n• /reply <id> <msg>\n• /done <id>\n• /sendgroup <msg>\n🔧 *Settings:*\n• /maintenance ...\n• /setprice <item> <price>\n• /removeprice <item>\n• /set*num <num>\n• /set*name <name>\n\n"
                 f"📊 *Status:*\n O:{'🟢' if bot_maintenance['orders'] else '🔴'} T:{'🟢' if bot_maintenance['topups'] else '🔴'} G:{'🟢' if bot_maintenance['general'] else '🔴'} | Auth:{len(AUTHORIZED_USERS)}\n\n"
                 f"💳 *Payment:*\n W: {payment_info['wave_number']} ({payment_info['wave_name']})\n K: {payment_info['kpay_number']} ({payment_info['kpay_name']})")
    await update.message.reply_text(help_msg, parse_mode="Markdown")

# --- Report Commands ---
def filter_by_date_str(items, date_field, start_str, end_str):
    filtered = []; len_compare = len(start_str)
    for item in items:
        ts_str = item.get(date_field)
        if ts_str and len(ts_str) >= len_compare:
            date_part = ts_str[:len_compare]
            if start_str <= date_part <= end_str: filtered.append(item)
    return filtered

async def report_base_command(update: Update, context: ContextTypes.DEFAULT_TYPE, period_type: str):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id): return await update.message.reply_text("❌ Owner Only!")
    args = context.args
    if len(args) == 0:
        today = datetime.now()
        if period_type=="day": y=today-timedelta(days=1); w=today-timedelta(days=7); kb=[[InlineKeyboardButton("📅 ဒီနေ့",callback_data=f"report_day_{today.strftime('%Y-%m-%d')}")],[InlineKeyboardButton("📅 မနေ့က",callback_data=f"report_day_{y.strftime('%Y-%m-%d')}")],[InlineKeyboardButton(f"📅 {w.strftime('%m/%d')}-{today.strftime('%m/%d')}",callback_data=f"report_day_range_{w.strftime('%Y-%m-%d')}_{today.strftime('%Y-%m-%d')}")]]; p="📊 ***ရက်စွဲ ရွေးပါ***\nFormat: `/d YYYY-MM-DD` or `/d START END`"
        elif period_type=="month": tm=today.strftime("%Y-%m");lm=(today.replace(day=1)-timedelta(days=1)).strftime("%Y-%m");m3=(today.replace(day=1)-timedelta(days=90)).strftime("%Y-%m");kb=[[InlineKeyboardButton("📅 ဒီလ",callback_data=f"report_month_{tm}")],[InlineKeyboardButton("📅 ပြီးခဲ့သောလ",callback_data=f"report_month_{lm}")],[InlineKeyboardButton(f"📅 {m3} မှ {tm}",callback_data=f"report_month_range_{m3}_{tm}")]]; p="📊 ***လ ရွေးပါ***\nFormat: `/m YYYY-MM` or `/m START END`"
        else: ty=today.strftime("%Y");ly=str(int(ty)-1);kb=[[InlineKeyboardButton("📅 ဒီနှစ်",callback_data=f"report_year_{ty}")],[InlineKeyboardButton("📅 ပြီးခဲ့သောနှစ်",callback_data=f"report_year_{ly}")],[InlineKeyboardButton(f"📅 {ly} & {ty}",callback_data=f"report_year_range_{ly}_{ty}")]]; p="📊 ***နှစ် ရွေးပါ***\nFormat: `/y YYYY` or `/y START END`"
        await update.message.reply_text(p, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)); return
    elif len(args)==1: start_str=end_str=args[0]
    elif len(args)==2: start_str, end_str = args[0], args[1]
    else: return await update.message.reply_text(f"❌ Format မှား! /{period_type[0]} YYYY...")
    len_exp=10 if period_type=="day" else (7 if period_type=="month" else 4)
    if len(start_str)!=len_exp or len(end_str)!=len_exp: return await update.message.reply_text(f"❌ Format မှား!")
    period_text = f"{period_type.capitalize()} ({start_str}{f' မှ {end_str}' if start_str!=end_str else ''})"
    ts=0; to=0; tt=0; tc=0
    if users_col is None: return await update.message.reply_text("❌ DB conn error.")
    try:
        all_users=users_col.find({}) # Note: This is inefficient for large user bases
        for ud in all_users:
            co=[o for o in ud.get("orders",[]) if o.get("status")=="confirmed"]; fo=filter_by_date_str(co,"confirmed_at",start_str,end_str)
            for o in fo: ts+=o.get("price",0); to+=1
            at=[t for t in ud.get("topups",[]) if t.get("status")=="approved"]; ft=filter_by_date_str(at,"approved_at",start_str,end_str)
            for t in ft: tt+=t.get("amount",0); tc+=1
    except Exception as e: print(f"❌ Report error: {e}"); return await update.message.reply_text("❌ Report error.")
    await update.message.reply_text(f"📊 ***Report***\n📅 {period_text}\n\n🛒 Orders:\n💰 `{ts:,} MMK` ({to} ခု)\n\n💳 Topups:\n💰 `{tt:,} MMK` ({tc} ခု)", parse_mode="Markdown")

async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await report_base_command(update, context, "day")
async def monthly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await report_base_command(update, context, "month")
async def yearly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await report_base_command(update, context, "year")

# --- Payment Settings Commands ---
async def setwavenum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin Only")
    args=context.args;
    if len(args)!=1: return await update.message.reply_text("❌ Format: /setwavenum <number>")
    if await update_payment_info("wave_number", args[0]): await update.message.reply_text(f"✅ Wave နံပါတ်: `{args[0]}`")
    else: await update.message.reply_text("❌ DB Error!")
async def setkpaynum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin Only")
    args=context.args;
    if len(args)!=1: return await update.message.reply_text("❌ Format: /setkpaynum <number>")
    if await update_payment_info("kpay_number", args[0]): await update.message.reply_text(f"✅ KPay နံပါတ်: `{args[0]}`")
    else: await update.message.reply_text("❌ DB Error!")
async def setwavename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin Only")
    args=context.args;
    if len(args)<1: return await update.message.reply_text("❌ Format: /setwavename <name>")
    name=" ".join(args)
    if await update_payment_info("wave_name", name): await update.message.reply_text(f"✅ Wave နာမည်: {name}")
    else: await update.message.reply_text("❌ DB Error!")
async def setkpayname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin Only")
    args=context.args;
    if len(args)<1: return await update.message.reply_text("❌ Format: /setkpayname <name>")
    name=" ".join(args)
    if await update_payment_info("kpay_name", name): await update.message.reply_text(f"✅ KPay နာမည်: {name}")
    else: await update.message.reply_text("❌ DB Error!")

async def setkpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(str(update.effective_user.id)): return await update.message.reply_text("❌ Owner Only!")
    if not update.message.reply_to_message or not update.message.reply_to_message.photo: return await update.message.reply_text("❌ ပုံကို reply လုပ်ပါ။")
    photo_id = update.message.reply_to_message.photo[-1].file_id
    if await update_payment_info("kpay_image", photo_id): await update.message.reply_text("✅ KPay QR ထည့်ပြီး!")
    else: await update.message.reply_text("❌ DB Error!")
async def removekpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(str(update.effective_user.id)): return await update.message.reply_text("❌ Owner Only!")
    if not payment_info.get("kpay_image"): return await update.message.reply_text("ℹ️ KPay QR မရှိပါ။")
    if await update_payment_info("kpay_image", None): await update.message.reply_text("✅ KPay QR ဖျက်ပြီး!")
    else: await update.message.reply_text("❌ DB Error!")
async def setwaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(str(update.effective_user.id)): return await update.message.reply_text("❌ Owner Only!")
    if not update.message.reply_to_message or not update.message.reply_to_message.photo: return await update.message.reply_text("❌ ပုံကို reply လုပ်ပါ။")
    photo_id = update.message.reply_to_message.photo[-1].file_id
    if await update_payment_info("wave_image", photo_id): await update.message.reply_text("✅ Wave QR ထည့်ပြီး!")
    else: await update.message.reply_text("❌ DB Error!")
async def removewaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(str(update.effective_user.id)): return await update.message.reply_text("❌ Owner Only!")
    if not payment_info.get("wave_image"): return await update.message.reply_text("ℹ️ Wave QR မရှိပါ။")
    if await update_payment_info("wave_image", None): await update.message.reply_text("✅ Wave QR ဖျက်ပြီး!")
    else: await update.message.reply_text("❌ DB Error!")

# --- Clone Bot Commands ---
async def addbot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=str(update.effective_user.id);
    if not is_admin(user_id): return await update.message.reply_text("❌ Admin Only!")
    args=context.args;
    if len(args)!=1: return await update.message.reply_text("❌ Format: /addbot <token>")
    bot_token=args[0]
    try:
        temp_bot=Bot(token=bot_token); bot_info=await temp_bot.get_me(); bot_un=bot_info.username; bot_id=str(bot_info.id)
        existing=load_clone_bots_db();
        if bot_id in existing: return await update.message.reply_text(f"ℹ️ Bot @{bot_un} ရှိပြီးသား!")
        bot_data={"token":bot_token,"username":bot_un,"owner_id":user_id,"balance":0,"status":"active","created_at":datetime.now().isoformat()}
        if save_clone_bot_db(bot_id, bot_data):
            asyncio.create_task(run_clone_bot(bot_token, bot_id, user_id))
            await update.message.reply_text(f"✅ Bot @{bot_un} Added & Running!\nID: `{bot_id}` Admin: `{user_id}`", parse_mode="Markdown")
        else: await update.message.reply_text("❌ DB Error!")
    except Exception as e: await update.message.reply_text(f"❌ Token မှား/Error: {e}")

async def listbots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=str(update.effective_user.id);
    if not is_admin(user_id): return await update.message.reply_text("❌ Admin Only!")
    bots=load_clone_bots_db();
    if not bots: return await update.message.reply_text("ℹ️ Clone bots မရှိပါ။")
    msg="🤖 ***Clone Bots List***\n\n";
    for bid, bdata in bots.items():
        st_ic="🟢" if bdata.get("status")=="active" else "🔴"; cr_str=bdata.get('created_at','?');
        try: cr_d=datetime.fromisoformat(cr_str).strftime('%Y-%m-%d')
        except: cr_d=cr_str
        msg+=(f"{st_ic} @{bdata.get('username','?')}\n├ ID: `{bid}`\n├ Admin: `{bdata.get('owner_id','?')}`\n├ Bal: {bdata.get('balance',0):,} MMK\n└ Created: {cr_d}\n\n")
    msg+=f"📊 Total: {len(bots)} bots"; await update.message.reply_text(msg, parse_mode="Markdown")

async def removebot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=str(update.effective_user.id);
    if not is_owner(user_id): return await update.message.reply_text("❌ Owner Only!")
    args=context.args;
    if len(args)!=1: return await update.message.reply_text("❌ Format: /removebot <bot_id>")
    bot_id=args[0]
    if remove_clone_bot_db(bot_id):
        if bot_id in clone_bot_apps:
            try: app=clone_bot_apps[bot_id]; await app.updater.stop(); await app.stop(); await app.shutdown(); del clone_bot_apps[bot_id]; print(f"✅ Clone bot {bot_id} stopped.")
            except Exception as e: print(f"⚠️ Error stopping clone {bot_id}: {e}"); if bot_id in clone_bot_apps: del clone_bot_apps[bot_id]
        await update.message.reply_text(f"✅ Bot (`{bot_id}`) ဖျက်ပြီး။")
    else: await update.message.reply_text(f"❌ Bot ID `{bot_id}` မတွေ့/မဖျက်နိုင်ပါ။")

async def addfund_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=str(update.effective_user.id);
    if not is_owner(user_id): return await update.message.reply_text("❌ Owner Only!")
    args=context.args;
    if len(args)!=2: return await update.message.reply_text("❌ Format: /addfund <admin_id> <amount>")
    admin_id_str=args[0];
    try: amount=int(args[1]);
    except ValueError: return await update.message.reply_text("❌ Amount မှား!")
    if amount<=0: return await update.message.reply_text("❌ Amount > 0 ဖြစ်ရမည်!")
    bot_id_found, bot_found = get_clone_bot_by_admin(admin_id_str)
    if not bot_found: return await update.message.reply_text(f"❌ Admin ID `{admin_id_str}` Bot မတွေ့ပါ။")
    if update_clone_bot_balance(bot_id_found, amount):
        nb = bot_found.get("balance",0)+amount
        try: await context.bot.send_message(chat_id=admin_id_str, text=(f"💰 Balance ဖြည့်ပြီး!\n✅ Owner က ဖြည့်ပေး!\n📥 Amount: `{amount:,}`\n💳 New Bal: `{nb:,}`\n🤖 Bot: @{bot_found.get('username','?')}"), parse_mode="Markdown")
        except Exception as e: print(f"Fail notify clone admin {admin_id_str} addfund: {e}")
        await update.message.reply_text(f"✅ Fund Added!\n👤 Admin: `{admin_id_str}`\n🤖 Bot: @{bot_found.get('username','?')}\n💰 Added: `{amount:,}`\n💳 New Bal: `{nb:,}`", parse_mode="Markdown")
    else: await update.message.reply_text("❌ DB Error!")

async def deductfund_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id=str(update.effective_user.id);
    if not is_owner(user_id): return await update.message.reply_text("❌ Owner Only!")
    args=context.args;
    if len(args)!=2: return await update.message.reply_text("❌ Format: /deductfund <admin_id> <amount>")
    admin_id_str=args[0];
    try: amount=int(args[1]);
    except ValueError: return await update.message.reply_text("❌ Amount မှား!")
    if amount<=0: return await update.message.reply_text("❌ Amount > 0 ဖြစ်ရမည်!")
    bot_id_found, bot_found = get_clone_bot_by_admin(admin_id_str)
    if not bot_found: return await update.message.reply_text(f"❌ Admin ID `{admin_id_str}` Bot မတွေ့ပါ။")
    cb=bot_found.get("balance",0);
    if cb<amount: return await update.message.reply_text(f"❌ Balance မလုံလောက်! လက်ကျန်: {cb:,}")
    if update_clone_bot_balance(bot_id_found, -amount):
        nb=cb-amount
        try: await context.bot.send_message(chat_id=admin_id_str, text=(f"💸 Balance နှုတ်ပြီး!\n⚠️ Owner က နှုတ်!\n📤 Amount: `{amount:,}`\n💳 New Bal: `{nb:,}`\n🤖 Bot: @{bot_found.get('username','?')}"), parse_mode="Markdown")
        except Exception as e: print(f"Fail notify clone admin {admin_id_str} deductfund: {e}")
        await update.message.reply_text(f"✅ Fund Deducted!\n👤 Admin: `{admin_id_str}`\n🤖 Bot: @{bot_found.get('username','?')}\n💸 Deducted: `{amount:,}`\n💳 New Bal: `{nb:,}`", parse_mode="Markdown")
    else: await update.message.reply_text("❌ DB Error!")


# --- Clone Bot Internal Functions ---
async def run_clone_bot(bot_token, bot_id, admin_id):
    try:
        app = Application.builder().token(bot_token).build()
        app.add_handler(CommandHandler("start", lambda u, c: clone_bot_start(u, c, admin_id)))
        app.add_handler(CommandHandler("mmb", lambda u, c: clone_bot_mmb(u, c, bot_id, admin_id)))
        app.add_handler(CallbackQueryHandler(lambda u, c: clone_bot_callback(u, c, bot_id, admin_id))) # Pass IDs
        clone_bot_apps[bot_id] = app
        await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
        print(f"✅ Clone bot {bot_id} started.")
    except Exception as e: print(f"❌ Clone bot {bot_id} start error: {e}"); save_clone_bot_db(bot_id, {"status": "error"})

async def clone_bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_id):
    user = update.effective_user
    await update.message.reply_text(f"👋 {user.first_name}!\n💎 /mmb gameid serverid amount\n📞 Admin ID: `{admin_id}`", parse_mode="Markdown")

async def clone_bot_mmb(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_id, admin_id):
    user = update.effective_user; user_id = str(user.id); args = context.args
    if len(args)!=3: await update.message.reply_text("❌ Format: /mmb gameid serverid amount"); return
    game_id, server_id, diamonds = args
    if not validate_game_id(game_id): await update.message.reply_text("❌ Game ID မှား!"); return
    if not validate_server_id(server_id): await update.message.reply_text("❌ Server ID မှား!"); return
    price = get_price(diamonds);
    if not price: await update.message.reply_text(f"❌ {diamonds} diamonds မရနိုင်ပါ!"); return

    ts = datetime.now().isoformat(); req_id = f"CLONE_{bot_id[:5]}_{user_id[-4:]}_{datetime.now().strftime('%H%M%S')}"
    kb = [[InlineKeyboardButton("✅ User OK", callback_data=f"clone_user_accept_{req_id}_{user_id}")],
          [InlineKeyboardButton("❌ User Reject", callback_data=f"clone_user_reject_{req_id}_{user_id}")],
          [InlineKeyboardButton("➡️ Owner ပို့", callback_data=f"clone_fwd_owner_{req_id}_{game_id}_{server_id}_{diamonds}_{price}_{user_id}")]]
    markup = InlineKeyboardMarkup(kb)
    try:
        await context.bot.send_message(chat_id=admin_id, text=(f"📦 Clone Order ({bot_id[:5]}..)\n👤 @{user.username or user.first_name} (`{user_id}`)\n🎮 `{game_id}` (`{server_id}`) 💎 {diamonds}\n💰 {price:,} MMK\n🔖 `{req_id}`"), parse_mode="Markdown", reply_markup=markup)
        await update.message.reply_text(f"✅ Order ပို့ပြီး!\n💎 {diamonds} ({price:,} MMK)\n⏰ Admin confirm စောင့်ပါ။")
    except Exception as e: print(f"Error send clone order to {admin_id}: {e}"); await update.message.reply_text(f"❌ Order ပို့မရပါ: {e}")

async def clone_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_id, admin_id):
    query = update.callback_query; await query.answer(); cbd = query.data; clone_bot = context.bot
    try:
        if cbd.startswith("clone_user_accept_"): parts=cbd.split("_"); req_id=parts[3]; euid=parts[4]; await clone_bot.send_message(chat_id=euid, text="✅ Order လက်ခံ! Diamonds စီစဥ်နေ..."); await query.edit_message_text(f"{query.message.text}\n\n✅ User OK ပြောပြီး", parse_mode="Markdown")
        elif cbd.startswith("clone_user_reject_"): parts=cbd.split("_"); req_id=parts[3]; euid=parts[4]; await clone_bot.send_message(chat_id=euid, text="❌ Order Reject! Admin ကို ဆက်သွယ်ပါ။"); await query.edit_message_text(f"{query.message.text}\n\n❌ User Reject ပြောပြီး", parse_mode="Markdown")
        elif cbd.startswith("clone_fwd_owner_"):
            parts = cbd.split("_"); req_id=parts[3]; gid=parts[4]; sid=parts[5]; dmd=parts[6]; prc=int(parts[7]); euid=parts[8]
            try:
                main_bot = Bot(token=BOT_TOKEN) # Temp instance
                owner_kb = [[InlineKeyboardButton(f"✅ Approve ({admin_id})", callback_data=f"main_approve_{admin_id}_{gid}_{sid}_{dmd}_{prc}_{euid}_{req_id}")],[InlineKeyboardButton(f"❌ Reject ({admin_id})", callback_data=f"main_reject_{admin_id}_{euid}_{req_id}")]]
                owner_markup = InlineKeyboardMarkup(owner_kb)
                owner_msg = (f"➡️ ***Clone Order Fwd***\n🤖 From: `{admin_id}` (BotID: {bot_id[:5]}..)\n👤 User: `{euid}`\n🎮 `{gid}` (`{sid}`) 💎 {dmd}\n💰 {prc:,} MMK\n🔖 `{req_id}`")
                await main_bot.send_message(chat_id=ADMIN_ID, text=owner_msg, parse_mode="Markdown", reply_markup=owner_markup)
                await query.edit_message_text(f"{query.message.text}\n\n➡️ ***Owner ဆီ ပို့ပြီး***", parse_mode="Markdown")
            except Exception as e_fwd: print(f"❌ Fail fwd clone {req_id}: {e_fwd}"); await query.message.reply_text(f"❌ Owner ဆီ ပို့မရပါ: {e_fwd}")
    except Exception as e_cb: print(f"Error clone CB ({bot_id}): {e_cb}"); await query.message.reply_text(f"Callback error: {e_cb}")

# --- General Handlers ---
async def handle_restricted_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if update.message and update.message.photo and user_id in user_states and user_states[user_id]=="waiting_approval": return # Let handle_photo manage
    if user_id in user_states and user_states[user_id]=="waiting_approval": await update.message.reply_text("❌ ***Admin approve စောင့်ပါ။***",parse_mode="Markdown"); return
    if not is_user_authorized(user_id):
        if update.message and update.message.text: await update.message.reply_text(simple_reply(update.message.text), parse_mode="Markdown")
        return
    if update.message and update.message.text: await update.message.reply_text(simple_reply(update.message.text), parse_mode="Markdown")
    else: await update.message.reply_text("📱 ***MLBB Bot***\n💎 /mmb\n💰 /price\n🆘 /start", parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user_id = str(query.from_user.id); admin_name = query.from_user.first_name or "Admin"; data = query.data

    try:
        # --- Payment Method Selection ---
        if data.startswith("topup_pay_"):
            parts = data.split("_"); payment_method = parts[2]; amount = int(parts[3])
            if user_id not in pending_topups: return await query.message.reply_text("❌ Process หมดอายุ။ /topup ပြန်စပါ။")
            pending_topups[user_id]["payment_method"] = payment_method
            p_name = "KBZ Pay" if payment_method == "kpay" else "Wave Money"; p_num = payment_info['kpay_number'] if payment_method == "kpay" else payment_info['wave_number']
            p_acc = payment_info['kpay_name'] if payment_method == "kpay" else payment_info['wave_name']; p_qr = payment_info.get('kpay_image') if payment_method == "kpay" else payment_info.get('wave_image')
            if p_qr: try: await query.message.reply_photo(photo=p_qr, caption=f"📱 **{p_name} QR**\n📞 `{p_num}`\n👤 {p_acc}", parse_mode="Markdown") except Exception as e: print(f"Err QR: {e}")
            await query.edit_message_text(f"💳 ***ငွေဖြည့်***\n✅ Amt: `{amount:,}`\n✅ Pmt: {p_name}\n\n***အဆင့် 2: Screenshot တင်ပါ။***\n📱 {p_name}\n📞 `{p_num}`\n👤 {p_acc}\n\n⚠️ ***Note မှာ သင့် {p_name} နာမည် ရေးပါ။***\n💡 ***Screenshot ဒီမှာ တင်ပါ။***\nℹ️ /cancel", parse_mode="Markdown"); return

        # --- Registration Request Button ---
        elif data == "request_register":
            user = query.from_user; req_user_id = str(user.id); username = user.username or "-"; name = f"{user.first_name} {user.last_name or ''}".strip()
            load_authorized_users()
            if is_user_authorized(req_user_id): return await query.answer("✅ သုံးခွင့်ရပြီး!", show_alert=True)
            kb=[[InlineKeyboardButton("✅ Approve",callback_data=f"register_approve_{req_user_id}"),InlineKeyboardButton("❌ Reject",callback_data=f"register_reject_{req_user_id}")]]
            markup=InlineKeyboardMarkup(kb); un_disp=f"@{username}" if username!="-" else "None"
            owner_msg=(f"📝 ***Register Request***\n👤 [{name}](tg://user?id={req_user_id}) (`{req_user_id}`)\n📱 {un_disp}\n⏰ {datetime.now().strftime('%H:%M:%S')}\n***Approve?***")
            admins=get_admins(); sent_admins=0; photo_id=None
            try: photos=await context.bot.get_user_profile_photos(user_id=int(req_user_id),limit=1); photo_id=photos.photos[0][0].file_id if photos.total_count>0 else None
            except: pass
            results=await asyncio.gather(*[context.bot.send_photo(chat_id=aid,photo=photo_id,caption=owner_msg,parse_mode="Markdown",reply_markup=markup) if photo_id else context.bot.send_message(chat_id=aid,text=owner_msg,parse_mode="Markdown",reply_markup=markup) for aid in admins], return_exceptions=True)
            sent_admins=sum(1 for r in results if not isinstance(r,Exception)); failed_admins=len(admins)-sent_admins
            if failed_admins>0: print(f"⚠️ Fail send reg req to {failed_admins} admins.")
            await query.answer("✅ Request ပို့ပြီး!", show_alert=True)
            user_confirm=(f"✅ ***Request ပို့ပြီး!***\n👤 {name}\n🆔 `{req_user_id}`\n⏳ ***Admin approve စောင့်ပါ ({sent_admins} notified)***")
            try: await query.edit_message_text(user_confirm, parse_mode="Markdown")
            except Exception as e: print(f"Err edit reg confirm {req_user_id}: {e}"); await query.message.reply_text(user_confirm, parse_mode="Markdown")
            return

        # --- Registration Approve/Reject ---
        elif data.startswith("register_approve_"):
            if not is_admin(user_id): return await query.answer("❌ Admin Only!", show_alert=True)
            target_user_id = str(data.replace("register_approve_", "")); load_authorized_users()
            if target_user_id in AUTHORIZED_USERS: await query.answer("ℹ️ Approve ပြီးသား!", show_alert=True); try: await query.edit_message_reply_markup(reply_markup=None) except: pass; return
            AUTHORIZED_USERS.add(target_user_id)
            if save_authorized_users():
                if target_user_id in user_states: del user_states[target_user_id]
                try: await query.edit_message_text(text=f"{query.message.text}\n\n✅ Approved by {admin_name}", parse_mode="Markdown", reply_markup=None)
                except: pass
                try: await context.bot.send_message(chat_id=int(target_user_id), text="🎉 Approved!\n✅ Admin လက်ခံ။\n🚀 /start နှိပ်သုံးပါ။")
                except Exception as e: print(f"Fail notify approved user {target_user_id}: {e}")
                if ADMIN_GROUP_ID: try: ud=get_user_data(target_user_id); un=ud.get("name",target_user_id) if ud else target_user_id; if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID): gmsg=(f"✅ Reg Approved!\n👤 [{un}](tg://user?id={target_user_id})\nBy: {admin_name}\n#RegApproved"); await context.bot.send_message(chat_id=ADMIN_GROUP_ID,text=gmsg,parse_mode="Markdown")
                except Exception as e: print(f"Fail notify group reg approve: {e}")
                await query.answer("✅ User approved!", show_alert=True)
            else: AUTHORIZED_USERS.discard(target_user_id); await query.answer("❌ DB Error!", show_alert=True)
            return

        elif data.startswith("register_reject_"):
            if not is_admin(user_id): return await query.answer("❌ Admin Only!", show_alert=True)
            target_user_id = str(data.replace("register_reject_", ""))
            try: await query.edit_message_text(text=f"{query.message.text}\n\n❌ Rejected by {admin_name}", parse_mode="Markdown", reply_markup=None)
            except: pass
            try: await context.bot.send_message(chat_id=int(target_user_id), text="❌ Registration Rejected\n📞 Admin ကို ဆက်သွယ်ပါ။")
            except Exception as e: print(f"Fail notify rejected user {target_user_id}: {e}")
            if ADMIN_GROUP_ID: try: ud=get_user_data(target_user_id); un=ud.get("name",target_user_id) if ud else target_user_id; if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID): gmsg=(f"❌ Reg Rejected!\n👤 [{un}](tg://user?id={target_user_id})\nBy: {admin_name}\n#RegRejected"); await context.bot.send_message(chat_id=ADMIN_GROUP_ID,text=gmsg,parse_mode="Markdown")
            except Exception as e: print(f"Fail notify group reg reject: {e}")
            await query.answer("❌ User rejected!", show_alert=True); return

        # --- Topup Cancel ---
        elif data == "topup_cancel":
            if user_id in pending_topups: del pending_topups[user_id]
            await query.edit_message_text("✅ ***ငွေဖြည့်ခြင်း ပယ်ဖျက်!***", parse_mode="Markdown"); return

        # --- Topup Approve/Reject Buttons ---
        elif data.startswith("topup_approve_") or data.startswith("topup_reject_"):
            if not is_admin(user_id): return await query.answer("❌ Admin မဟုတ်ပါ။")
            is_approve = data.startswith("topup_approve_"); topup_id = data.replace("topup_approve_", "").replace("topup_reject_", "")
            update_fields = {"status": "approved" if is_approve else "rejected", f"{'approved' if is_approve else 'rejected'}_by": admin_name, f"{'approved' if is_approve else 'rejected'}_at": datetime.now().isoformat()}
            target_user_id, topup_amount, status_before = find_and_update_topup_mongo(topup_id, update_fields)
            if target_user_id is None:
                if status_before is not None and status_before != "pending": await query.answer("⚠️ လုပ်ဆောင်ပြီးသား။"); try: await query.edit_message_reply_markup(reply_markup=None) except: pass
                else: await query.answer("❌ Topup မတွေ့/Update မရ။")
                return
            balance_updated = True
            if is_approve:
                balance_updated = increment_user_balance(target_user_id, topup_amount)
                if not balance_updated: print(f"⚠️ Topup {topup_id} approved, but balance fail {target_user_id}!"); await query.answer("❌ Balance update Error!", show_alert=True); revert_update={"status":"pending"}; find_and_update_topup_mongo(topup_id, revert_update); return
            if target_user_id in user_states: del user_states[target_user_id]
            try:
                cap=query.message.caption or ""; nst="✅ Approved" if is_approve else "❌ Rejected"; lines=cap.split('\n')
                for i,line in enumerate(lines):
                    if "Status:" in line: lines[i]=f"📊 Status: {nst} by {admin_name}"; break
                else: lines.append(f"📊 Status: {nst} by {admin_name}")
                new_cap="\n".join(lines); await query.edit_message_caption(caption=new_cap, parse_mode="Markdown", reply_markup=None)
            except Exception as e: print(f"Err edit topup caption {topup_id}: {e}"); try: await query.edit_message_reply_markup(reply_markup=None) except: pass
            ud=get_user_data(target_user_id); nb=ud.get("balance","Err") if ud else "Err"
            try:
                bot_username = (await context.bot.get_me()).username
                if is_approve:
                    kb=[[InlineKeyboardButton("💎 Order",url=f"https://t.me/{bot_username}?start=order")]];markup=InlineKeyboardMarkup(kb)
                    msg=(f"✅ ***Topup Approved!*** 🎉\n💰 Amount: `{topup_amount:,}`\n💳 Balance: `{nb:,}`\n👤 By: {admin_name}\n⏰ {datetime.now().strftime('%H:%M:%S')}\n\n🎉 ***Diamonds ဝယ်နိုင်ပြီ!***\n🔓 ***Bot ပြန်သုံးနိုင်ပြီ!***")
                    await context.bot.send_message(chat_id=int(target_user_id), text=msg, parse_mode="Markdown", reply_markup=markup)
                else: msg=(f"❌ ***Topup Rejected!***\n💰 Amount: `{topup_amount:,}`\n👤 By: {admin_name}\n⏰ {datetime.now().strftime('%H:%M:%S')}\n\n📞 ***Admin ကို ဆက်သွယ်ပါ။***\n🔓 ***Bot ပြန်သုံးနိုင်ပြီ!***"); await context.bot.send_message(chat_id=int(target_user_id), text=msg, parse_mode="Markdown")
            except Exception as e: print(f"Fail notify user topup {topup_id} status: {e}")
            await query.answer(f"✅ Topup {topup_id} { 'approved' if is_approve else 'rejected'}!", show_alert=is_approve); return

        # --- Order Confirm/Cancel Buttons ---
        elif data.startswith("order_confirm_") or data.startswith("order_cancel_"):
            if not is_admin(user_id): return await query.answer("❌ Admin မဟုတ်ပါ။")
            is_confirm = data.startswith("order_confirm_"); order_id = data.replace("order_confirm_", "").replace("order_cancel_", "")
            update_fields = {"status": "confirmed" if is_confirm else "cancelled", f"{'confirmed' if is_confirm else 'cancelled'}_by": admin_name, f"{'confirmed' if is_confirm else 'cancelled'}_at": datetime.now().isoformat()}
            target_user_id, updated_order = find_and_update_order_mongo(order_id, update_fields)
            if target_user_id is None:
                 if updated_order and updated_order.get("status") != "pending": await query.answer("⚠️ လုပ်ဆောင်ပြီးသား။"); try: await query.edit_message_reply_markup(reply_markup=None) except: pass
                 else: await query.answer("❌ Order မတွေ့/Update မရ။")
                 return
            refund_amount = updated_order.get("price", 0) if not is_confirm else 0
            bal_refunded = True
            if not is_confirm and refund_amount > 0:
                bal_refunded = increment_user_balance(target_user_id, refund_amount)
                if not bal_refunded: print(f"⚠️ Order {order_id} cancelled, but refund fail {target_user_id}!"); await query.answer("❌ Refund Error!", show_alert=True); revert_update={"status":"pending"}; find_and_update_order_mongo(order_id, revert_update); return
            try:
                ot=query.message.text or ""; nst="✅ Confirmed" if is_confirm else "❌ Cancelled"; lines=ot.split('\n')
                for i,line in enumerate(lines):
                    if "Status:" in line: lines[i]=f"📊 Status: {nst} by {admin_name}"; break
                else: lines.append(f"📊 Status: {nst} by {admin_name}")
                nt="\n".join(lines); await query.edit_message_text(text=nt, parse_mode="Markdown", reply_markup=None)
            except Exception as e: print(f"Err edit order msg {order_id}: {e}"); try: await query.edit_message_reply_markup(reply_markup=None) except: pass
            ud = get_user_data(target_user_id); un = ud.get("name", "?") if ud else "?"; nb = ud.get("balance", "Err") if ud else "Err"
            cid_notify = updated_order.get("chat_id", int(target_user_id))
            try:
                if is_confirm: msg=(f"✅ ***Order ({order_id}) Confirmed!***\n👤 {un}\n🎮 `{updated_order.get('game_id')}` (`{updated_order.get('server_id')}`)\n💎 {updated_order.get('amount')}\n📊 ✅ Confirmed\n\n💎 ***Diamonds ပို့ပြီး!***")
                else: msg=(f"❌ ***Order ({order_id}) Cancelled!***\n👤 {un}\n🎮 `{updated_order.get('game_id')}`\n💎 {updated_order.get('amount')}\n💰 Refund: {refund_amount:,}\n💳 Balance: `{nb:,}`\n📊 ❌ Cancelled\n\n📞 Admin ကို ဆက်သွယ်ပါ။")
                await context.bot.send_message(chat_id=cid_notify, text=msg, parse_mode="Markdown")
            except Exception as e: print(f"Fail notify user order {order_id} status: {e}")
            await query.answer(f"✅ Order {order_id} { 'confirmed' if is_confirm else 'cancelled'}!", show_alert=True); return

        # --- Report Filter Callbacks ---
        elif data.startswith("report_day_") or data.startswith("report_month_") or data.startswith("report_year_"):
            if not is_owner(user_id): return await query.answer("❌ Owner Only!", show_alert=True)
            pt="day" if "day" in data else ("month" if "month" in data else "year"); parts=data.replace(f"report_{pt}_","").split("_"); is_range="range" in parts
            if is_range: start_str=parts[1]; end_str=parts[2]
            else: start_str=end_str=parts[0]
            len_exp=10 if pt=="day" else (7 if pt=="month" else 4)
            if len(start_str)!=len_exp or len(end_str)!=len_exp: return await query.answer("❌ Date format မှား!", show_alert=True)
            p_text=f"{pt.capitalize()} ({start_str}{f' မှ {end_str}' if start_str!=end_str else ''})"; ts=0; to=0; tt=0; tc=0
            if users_col is None: return await query.edit_message_text("❌ DB conn error.")
            try:
                all_users=users_col.find({})
                for ud in all_users:
                    co=[o for o in ud.get("orders",[]) if o.get("status")=="confirmed"]; fo=filter_by_date_str(co,"confirmed_at",start_str,end_str)
                    for o in fo: ts+=o.get("price",0); to+=1
                    at=[t for t in ud.get("topups",[]) if t.get("status")=="approved"]; ft=filter_by_date_str(at,"approved_at",start_str,end_str)
                    for t in ft: tt+=t.get("amount",0); tc+=1
            except Exception as e: print(f"❌ Report error: {e}"); return await query.edit_message_text("❌ Report error.")
            await query.edit_message_text(f"📊 ***Report***\n📅 {p_text}\n\n🛒 Orders:\n💰 `{ts:,} MMK` ({to} ခု)\n\n💳 Topups:\n💰 `{tt:,} MMK` ({tc} ခု)", parse_mode="Markdown"); return

        # --- Copy Number Buttons ---
        elif data == "copy_kpay": await query.answer(f"Copied: {payment_info['kpay_number']}", show_alert=True); await query.message.reply_text(f"📱 KPay:\n`{payment_info['kpay_number']}`\n👤 {payment_info['kpay_name']}", parse_mode="Markdown"); return
        elif data == "copy_wave": await query.answer(f"Copied: {payment_info['wave_number']}", show_alert=True); await query.message.reply_text(f"📱 Wave:\n`{payment_info['wave_number']}`\n👤 {payment_info['wave_name']}", parse_mode="Markdown"); return

        # --- Topup Button from Balance ---
        elif data == "topup_button":
            kb = [[InlineKeyboardButton("📱 Copy KPay", callback_data="copy_kpay")], [InlineKeyboardButton("📱 Copy Wave", callback_data="copy_wave")]]; markup=InlineKeyboardMarkup(kb)
            txt = ("💳 ***ငွေဖြည့်***\n\n1️⃣ `/topup <amount>`\n2️⃣ ငွေလွှဲပါ:\n" f"📱 KPay: `{payment_info['kpay_number']}` ({payment_info['kpay_name']})\n" f"📱 Wave: `{payment_info['wave_number']}` ({payment_info['wave_name']})\n" "3️⃣ Screenshot ပို့ပါ။\n⏰ Admin စစ်မည်။")
            try: await query.edit_message_text(text=txt, parse_mode="Markdown", reply_markup=markup)
            except: await query.message.reply_text(text=txt, parse_mode="Markdown", reply_markup=markup); return

        # --- Clone Bot Related Callbacks (main owner actions) ---
        elif data.startswith("main_approve_") or data.startswith("main_reject_"):
            if not is_owner(user_id): return await query.answer("❌ Owner Only!", show_alert=True)
            is_appr = data.startswith("main_approve_"); parts = data.split("_")
            try: caid=parts[2]; euid=parts[-2] if is_appr else parts[3]; reqid=parts[-1]; gid=parts[3] if is_appr else None; sid=parts[4] if is_appr else None; dmd=parts[5] if is_appr else None; prc=int(parts[6]) if is_appr else 0
            except (IndexError, ValueError) as e: print(f"Err parse main CB: {data} -> {e}"); return await query.answer("❌ CB data error.")
            try: status_txt="✅ Approved by Owner" if is_appr else "❌ Rejected by Owner"; await query.edit_message_text(f"{query.message.text}\n\n***{status_txt}***",parse_mode="Markdown",reply_markup=None)
            except: pass
            try: # Notify clone admin
                if is_appr: ntf_msg=(f"✅ Order Approved (Owner)!\nReq: `{reqid}`\n🎮 `{gid}` (`{sid}`) 💎 {dmd}\n💰 {prc:,}\n💎 Diamonds ပို့ပေးပါ။")
                else: ntf_msg = f"❌ Order Rejected (Owner)!\nReq: `{reqid}`"
                await context.bot.send_message(chat_id=caid, text=ntf_msg, parse_mode="Markdown") # Use main bot context
            except Exception as e: print(f"Fail notify clone admin {caid}: {e}")
            await query.answer(f"✅ Order { 'approved' if is_appr else 'rejected'}!", show_alert=True); return

        # --- Fallback ---
        else: await query.answer("ℹ️ Unknown Button.")
    
    except Exception as e:
        print(f"❌ Error in button_callback: {e}")
        traceback.print_exc()
        try: await query.answer("Processing error!", show_alert=True)
        except: pass


# --- Bot Startup ---
async def post_init(application: Application):
    print("Bot starting... Loading initial data from MongoDB...")
    load_settings(); load_authorized_users()
    clone_bots = load_clone_bots_db(); print(f"Found {len(clone_bots)} clone bots.")
    for bot_id, bot_data in clone_bots.items():
        token = bot_data.get("token"); owner = bot_data.get("owner_id")
        if token and owner: print(f"🔄 Starting clone bot {bot_id}..."); asyncio.create_task(run_clone_bot(token, bot_id, owner))
        else: print(f"⚠️ Skip clone {bot_id} (no token/owner).")

def main():
    if not BOT_TOKEN: print("❌ BOT_TOKEN မရှိ!"); return
    if not MONGO_URI: print("❌ MONGO_URI မရှိ!"); return
    if settings_col is None or users_col is None or clone_bots_col is None: print("❌ DB collections မရပါ။"); return

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    handlers = [ CommandHandler(cmd, func) for cmd, func in [
        ("start", start), ("mmb", mmb_command), ("balance", balance_command), ("topup", topup_command),
        ("cancel", cancel_command), ("c", c_command), ("price", price_command), ("history", history_command),
        ("register", register_command), ("approve", approve_command), ("deduct", deduct_command),
        ("done", done_command), ("reply", reply_command), ("ban", ban_command), ("unban", unban_command),
        ("addadm", addadm_command), ("unadm", unadm_command), ("sendgroup", send_to_group_command),
        ("maintenance", maintenance_command), ("testgroup", testgroup_command), ("setprice", setprice_command),
        ("removeprice", removeprice_command), ("setwavenum", setwavenum_command), ("setkpaynum", setkpaynum_command),
        ("setwavename", setwavename_command), ("setkpayname", setkpayname_command), ("setkpayqr", setkpayqr_command),
        ("removekpayqr", removekpayqr_command), ("setwaveqr", setwaveqr_command), ("removewaveqr", removewaveqr_command),
        ("adminhelp", adminhelp_command), ("broadcast", broadcast_command), ("d", daily_report_command),
        ("m", monthly_report_command), ("y", yearly_report_command), ("addbot", addbot_command),
        ("listbots", listbots_command), ("removebot", removebot_command), ("addfund", addfund_command),
        ("deductfund", deductfund_command) ]]
    handlers.extend([ CallbackQueryHandler(button_callback), MessageHandler(filters.PHOTO & (~filters.COMMAND), handle_photo),
                     MessageHandler((filters.TEXT | filters.VOICE | filters.Sticker.ALL | filters.VIDEO | filters.ANIMATION |
                                     filters.AUDIO | filters.Document.ALL | filters.FORWARDED | filters.Entity("url") |
                                     filters.POLL) & (~filters.COMMAND), handle_restricted_content)])
    application.add_handlers(handlers)
    print("🤖 Bot စတင်နေပါပြီ (MongoDB Version)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": 
    main()
