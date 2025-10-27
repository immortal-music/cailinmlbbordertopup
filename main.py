import json, os, asyncio
from datetime import datetime, timedelta
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
import re # Calculator အတွက် import လုပ်ထားတာ

# env.py ကနေ လိုအပ်တာတွေ import လုပ်ပါ
from env import BOT_TOKEN, ADMIN_ID, ADMIN_GROUP_ID, MONGO_URI

# db.py ကနေ လိုအပ်တဲ့ database objects တွေနဲ့ functions တွေကို import လုပ်ပါ
from db import users_col, settings_col, clone_bots_col, initialize_settings, load_authorized_users_db, save_authorized_users_db

# --- Global Variables ---
# Authorized users - ဒီ set ကို global အဖြစ် ဆက်သုံးပြီး startup မှာ db ကနေ load လုပ်မယ်
AUTHORIZED_USERS = set()

# User states for restricting actions after screenshot
user_states = {}

# Bot maintenance mode (ဒီ setting ကို db ထဲ ထည့်သိမ်းလို့လည်း ရနိုင်ပါတယ်၊ လောလောဆယ် ဒီအတိုင်းထားပါမယ်)
bot_maintenance = {
    "orders": True,
    "topups": True,
    "general": True
}

# Payment information (ဒီ setting ကို db ထဲ ထည့်သိမ်းလို့လည်း ရနိုင်ပါတယ်၊ လောလောဆယ် ဒီအတိုင်းထားပါမယ်)
payment_info = {
    "kpay_number": "09678786528",
    "kpay_name": "Ma May Phoo Wai",
    "kpay_image": None,
    "wave_number": "09673585480",
    "wave_name": "Nine Nine",
    "wave_image": None
}

# Pending topup process အတွက် dictionary
pending_topups = {}

# Clone bot application instances တွေကို မှတ်ထားဖို့ dictionary
clone_bot_apps = {}

# --- Database Helper Functions (main.py specific) ---

def load_settings():
    """ Bot settings တွေကို MongoDB ကနေ ဆွဲထုတ်မယ် """
    if settings_col is None:
        print("❌ Database connection မရှိပါ။ Default settings ကို သုံးပါမည်။")
        return {"prices": {}, "authorized_users": [], "admin_ids": [ADMIN_ID]}
    try:
        settings_data = settings_col.find_one({"_id": "bot_config"})
        if settings_data:
            # Default value တွေပါ သေချာအောင်လုပ်ပါ
            settings_data.setdefault("prices", {})
            settings_data.setdefault("authorized_users", [])
            settings_data.setdefault("admin_ids", [ADMIN_ID])
            # Load payment info from DB if it exists
            payment_db = settings_data.get("payment_info", {})
            payment_info.update(payment_db) # Update global dict
            return settings_data
        else:
            print("⚠️ Settings document မတွေ့ပါ။ Default settings အသစ် ထည့်သွင်းပါမည်။")
            initialize_settings() # db.py ထဲက function
            return {"prices": {}, "authorized_users": [], "admin_ids": [ADMIN_ID]}
    except Exception as e:
        print(f"❌ Settings များ ရယူရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return {"prices": {}, "authorized_users": [], "admin_ids": [ADMIN_ID]}

def save_settings_field(field_name, value):
    """ Settings document ထဲက field တစ်ခုကို update လုပ်မယ် """
    if settings_col is None:
        print("❌ Database connection မရှိပါ။ Settings မသိမ်းနိုင်ပါ။")
        return False
    try:
        settings_col.update_one(
            {"_id": "bot_config"},
            {"$set": {field_name: value}},
            upsert=True
        )
        # Update payment_info in DB if applicable
        if field_name.startswith("payment_info."):
             settings_col.update_one({"_id": "bot_config"}, {"$set": {field_name: value}}, upsert=True)
        elif field_name == "payment_info": # Save the whole dict
             settings_col.update_one({"_id": "bot_config"}, {"$set": {"payment_info": value}}, upsert=True)
        return True
    except Exception as e:
        print(f"❌ Settings ({field_name}) သိမ်းရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

def load_authorized_users():
    """ Authorized users တွေကို MongoDB ကနေ ဆွဲထုတ်ပြီး global set ကို update လုပ်မယ် """
    global AUTHORIZED_USERS
    authorized_list = load_authorized_users_db() # db.py ထဲက function
    AUTHORIZED_USERS = set(authorized_list)
    print(f"ℹ️ Authorized users {len(AUTHORIZED_USERS)} ယောက်ကို MongoDB မှ ရယူပြီးပါပြီ။")

def save_authorized_users():
    """ လက်ရှိ AUTHORIZED_USERS set ကို MongoDB ထဲကို ပြန်သိမ်းမယ် """
    if save_authorized_users_db(list(AUTHORIZED_USERS)): # db.py ထဲက function
        print(f"ℹ️ Authorized users {len(AUTHORIZED_USERS)} ယောက်ကို MongoDB သို့ သိမ်းဆည်းပြီးပါပြီ။")
    else:
        print("❌ Authorized users များကို MongoDB သို့ သိမ်းဆည်းရာတွင် အမှားဖြစ်ပွားခဲ့သည်။")

def load_prices():
    """ Custom prices တွေကို MongoDB ကနေ ဆွဲထုတ်မယ် """
    settings = load_settings()
    return settings.get("prices", {})

def save_prices(prices):
    """ Prices တွေကို MongoDB ထဲကို သိမ်းမယ် """
    return save_settings_field("prices", prices)

def get_user_data(user_id):
    """ User တစ်ယောက်ရဲ့ data ကို MongoDB ကနေ ဆွဲထုတ်မယ် """
    if users_col is None: return None
    try:
        user_data = users_col.find_one({"_id": str(user_id)})
        # Default fields တွေ ရှိအောင် လုပ်ပေးမယ် (find_one က None ပြန်လာနိုင်သည်)
        if user_data:
             user_data.setdefault("balance", 0)
             user_data.setdefault("orders", [])
             user_data.setdefault("topups", [])
             user_data.setdefault("name", "Unknown")
             user_data.setdefault("username", "-")
        return user_data
    except Exception as e:
        print(f"❌ User data ({user_id}) ရယူရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return None

def update_user_data(user_id, update_fields):
    """ User data ကို MongoDB ထဲမှာ update လုပ်မယ် ( $set ကိုသုံး) """
    if users_col is None: return False
    try:
        users_col.update_one(
            {"_id": str(user_id)},
            {"$set": update_fields},
            upsert=True # User မရှိရင် အသစ်ဆောက်မယ်
        )
        return True
    except Exception as e:
        print(f"❌ User data ({user_id}) update လုပ်ရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

def increment_user_balance(user_id, amount):
    """ User balance ကို တိုး/လျော့ လုပ်မယ် ($inc ကိုသုံး) """
    if users_col is None: return False
    try:
        result = users_col.update_one(
            {"_id": str(user_id)},
            {"$inc": {"balance": amount}},
            upsert=True # User မရှိရင် balance field နဲ့ အသစ်ဆောက်မယ်
        )
        # Check if user was created and initialize other fields if necessary
        if result.upserted_id:
             # If a new user was created by upsert, add default fields
             users_col.update_one(
                 {"_id": str(user_id)},
                 {"$setOnInsert": {"name": "New User", "username": "-", "orders": [], "topups": []}},
                 upsert=True
             )
        return True
    except Exception as e:
        print(f"❌ User balance ({user_id}) update လုပ်ရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

def add_to_user_list(user_id, list_field, item):
     """ User document ထဲက list field တစ်ခုထဲကို item အသစ်ထည့်မယ် ($push ကိုသုံး) """
     if users_col is None: return False
     try:
         # Ensure the user document exists and has the list field
         users_col.update_one(
             {"_id": str(user_id)},
             {"$setOnInsert": {list_field: []}}, # Only creates the list if the user is inserted
             upsert=True
         )
         # Now push the item
         users_col.update_one(
             {"_id": str(user_id)},
             {"$push": {list_field: item}}
         )
         return True
     except Exception as e:
         print(f"❌ User list ({user_id}, {list_field}) ထဲသို့ ထည့်ရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
         return False

def find_and_update_order(order_id, updates):
     """ Order ID နဲ့ ရှာပြီး order status/details ကို update လုပ်မယ် """
     if users_col is None: return None
     try:
         # orders array ထဲက element ကို update လုပ်ဖို့ arrayFilters သုံးရမယ်
         result = users_col.update_one(
             {"orders.order_id": order_id},
             {"$set": updates},
             array_filters=[{"elem.order_id": order_id}]
         )
         if result.modified_count > 0:
             # Update အောင်မြင်ရင် user data ကို ပြန်ရှာပြီး return လုပ်မယ်
             user_data = users_col.find_one({"orders.order_id": order_id})
             if user_data:
                  for order in user_data.get("orders", []):
                       if order.get("order_id") == order_id:
                            return user_data.get("_id"), order # Return user_id and the updated order
             return None, None # Should not happen if modified_count > 0
         else:
              # Order မတွေ့ရင် (သို့) update မဖြစ်ရင် None ပြန်မယ်
              return None, None
     except Exception as e:
         print(f"❌ Order ({order_id}) update လုပ်ရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
         return None, None

def find_and_update_topup(topup_id, updates):
     """ Topup ID နဲ့ ရှာပြီး topup status/details ကို update လုပ်မယ် """
     if users_col is None: return None, None, None
     try:
         target_user_id = None
         topup_amount = 0
         topup_status_before = None

         # Find the user first
         user_doc = users_col.find_one({"topups.topup_id": topup_id})
         if not user_doc:
             return None, None, None # Topup not found

         target_user_id = user_doc["_id"]
         original_topup = None
         for t in user_doc.get("topups", []):
             if t.get("topup_id") == topup_id:
                 original_topup = t
                 break

         if not original_topup:
             return None, None, None # Should not happen if user_doc found

         topup_status_before = original_topup.get("status")
         topup_amount = original_topup.get("amount", 0)

         # Update the specific topup entry
         result = users_col.update_one(
             {"_id": target_user_id, "topups.topup_id": topup_id},
             {"$set": updates}
             # array_filters are generally needed for updating specific elements,
             # but $set with field paths like "topups.$.status" is more common.
             # Let's use positional operator '$'
             # We need to construct the update fields dynamically for the positional operator
             # Example update: {"topups.$.status": "approved", "topups.$.approved_by": admin_name, ...}
         )

         if result.modified_count > 0:
             return target_user_id, topup_amount, topup_status_before
         else:
             # Check if status was already the target status
             if topup_status_before == updates.get("topups.$.status"):
                  print(f"ℹ️ Topup ({topup_id}) status is already '{topup_status_before}'. No update needed.")
                  return target_user_id, topup_amount, topup_status_before # Indicate it was found but not modified
             else:
                  print(f"❌ Topup ({topup_id}) ကို update မလုပ်နိုင်ပါ (อาจไม่พบ หรือไม่มีการเปลี่ยนแปลง).")
                  return None, None, None
     except Exception as e:
         print(f"❌ Topup ({topup_id}) update လုပ်ရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
         return None, None, None

def get_admins():
    """ Admin ID list ကို MongoDB ကနေ ဆွဲထုတ်မယ် """
    settings = load_settings()
    # Ensure ADMIN_ID is always included, even if DB fetch fails or is empty initially
    admin_ids = settings.get("admin_ids", [])
    if ADMIN_ID not in admin_ids:
        admin_ids.append(ADMIN_ID)
    return admin_ids

def add_admin_db(admin_id_to_add):
    """ Admin ID အသစ်ကို MongoDB ထဲကို ထည့်မယ် ($addToSet ကိုသုံး عشان မထပ်အောင်) """
    if settings_col is None: return False
    try:
        settings_col.update_one(
            {"_id": "bot_config"},
            {"$addToSet": {"admin_ids": admin_id_to_add}},
            upsert=True # Settings doc မရှိရင် ဆောက်မယ်
        )
        return True
    except Exception as e:
        print(f"❌ Admin ({admin_id_to_add}) ထည့်ရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

def remove_admin_db(admin_id_to_remove):
    """ Admin ID ကို MongoDB ထဲကနေ ဖယ်ထုတ်မယ် ($pull ကိုသုံး) """
    # Owner ကို ဖယ်လို့မရအောင် စစ်ဆေးပါ
    if admin_id_to_remove == ADMIN_ID:
         print("❌ Owner ကို Admin list မှ ဖယ်ရှား၍ မရပါ။")
         return False
    if settings_col is None: return False
    try:
        settings_col.update_one(
            {"_id": "bot_config"},
            {"$pull": {"admin_ids": admin_id_to_remove}}
        )
        return True
    except Exception as e:
        print(f"❌ Admin ({admin_id_to_remove}) ဖယ်ရှားရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

# --- Clone Bot Functions (MongoDB versions) ---
def load_clone_bots_db():
    """ Clone bots တွေကို MongoDB ကနေ ဆွဲထုတ်မယ် """
    if clone_bots_col is None: return {}
    try:
        bots_cursor = clone_bots_col.find({})
        # Cursor ကနေ dictionary ပြောင်းမယ် (_id ကို key အဖြစ်သုံး)
        return {bot["_id"]: bot for bot in bots_cursor}
    except Exception as e:
        print(f"❌ Clone bots များ ရယူရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return {}

def save_clone_bot_db(bot_id, bot_data):
    """ Clone bot data ကို MongoDB ထဲကို သိမ်းမယ်/update လုပ်မယ် """
    if clone_bots_col is None: return False
    try:
        # bot_id ကို string ပြောင်းပြီး _id အဖြစ် သုံးမယ်
        clone_bots_col.update_one(
            {"_id": str(bot_id)},
            {"$set": bot_data},
            upsert=True # Bot မရှိရင် အသစ်ထည့်မယ်
        )
        return True
    except Exception as e:
        print(f"❌ Clone bot ({bot_id}) သိမ်းဆည်းရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

def remove_clone_bot_db(bot_id):
    """ Clone bot ကို MongoDB ထဲကနေ ဖျက်မယ် """
    if clone_bots_col is None: return False
    try:
        result = clone_bots_col.delete_one({"_id": str(bot_id)})
        return result.deleted_count > 0 # ဖျက်สำเร็จရင် True ပြန်မယ်
    except Exception as e:
        print(f"❌ Clone bot ({bot_id}) ဖျက်ရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

def get_clone_bot_by_admin(admin_id):
     """ Clone bot admin ID နဲ့ သက်ဆိုင်ရာ bot data ကို ရှာမယ် """
     if clone_bots_col is None: return None, None
     try:
          bot_data = clone_bots_col.find_one({"owner_id": str(admin_id)})
          if bot_data:
               return bot_data.get("_id"), bot_data # bot_id နဲ့ data ပြန်မယ်
          else:
               return None, None
     except Exception as e:
          print(f"❌ Admin ID ({admin_id}) ဖြင့် Clone bot ရှာရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
          return None, None

def update_clone_bot_balance(bot_id, amount_change):
     """ Clone bot ရဲ့ balance ကို တိုး/လျော့ လုပ်မယ် ($inc ကိုသုံး) """
     if clone_bots_col is None: return False
     try:
          result = clone_bots_col.update_one(
               {"_id": str(bot_id)},
               {"$inc": {"balance": amount_change}}
          )
          return result.modified_count > 0
     except Exception as e:
          print(f"❌ Clone bot balance ({bot_id}) update လုပ်ရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
          return False


# --- Utility Functions (Mostly unchanged) ---

def is_user_authorized(user_id):
    """ Check if user is authorized (uses global AUTHORIZED_USERS set) """
    # load_authorized_users() # Call this at startup and after modifications
    return str(user_id) in AUTHORIZED_USERS or is_admin(str(user_id)) # Admins are always authorized

async def is_bot_admin_in_group(bot, chat_id):
    """ Check if bot is admin in the group """
    if not chat_id: return False # chat_id မရှိရင် false ပြန်ပါ
    try:
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
        is_admin_status = bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
        print(f"Bot admin check for group {chat_id}: {is_admin_status}, status: {bot_member.status}")
        return is_admin_status
    except Exception as e:
        print(f"Error checking bot admin status in group {chat_id}: {e}")
        return False

def simple_reply(message_text):
    """ Simple auto-replies for common queries (unchanged) """
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
    """ Validate MLBB Game ID (6-10 digits) (unchanged) """
    if not game_id: return False
    if not game_id.isdigit(): return False
    if len(game_id) < 6 or len(game_id) > 10: return False
    return True

def validate_server_id(server_id):
    """ Validate MLBB Server ID (3-5 digits) (unchanged) """
    if not server_id: return False
    if not server_id.isdigit(): return False
    if len(server_id) < 3 or len(server_id) > 5: return False
    return True

def is_banned_account(game_id):
    """ Check if MLBB account is banned (unchanged - simple version) """
    banned_ids = ["123456789", "000000000", "111111111"]
    if game_id in banned_ids: return True
    if len(set(game_id)) == 1 and len(game_id) > 5: return True # Check longer repetitive IDs
    if game_id.startswith("000") or game_id.endswith("000"): return True
    return False

def get_price(diamonds):
    """ Get price for diamonds (uses load_prices) """
    custom_prices = load_prices()
    if diamonds in custom_prices:
        return custom_prices[diamonds]

    # Default prices (unchanged)
    if diamonds.startswith("wp") and diamonds[2:].isdigit():
        n = int(diamonds[2:])
        if 1 <= n <= 10:
            return n * 6000 # Example price
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

def is_payment_screenshot(update):
    """ Check if the image is likely a payment screenshot (unchanged - simple version) """
    if update.message and update.message.photo:
        return True
    return False

async def send_pending_topup_warning(update: Update):
    """ Send pending topup warning message (unchanged) """
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
    """ Check if specific command type is in maintenance mode (unchanged) """
    return bot_maintenance.get(command_type, True)

async def send_maintenance_message(update: Update, command_type):
    """ Send maintenance mode message (unchanged) """
    user_name = update.effective_user.first_name or "User"
    # ... (message definitions remain the same) ...
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
    else: # general
         msg = (
             f"***မင်္ဂလာပါ*** {user_name}! 👋\n\n"
             "━━━━━━━━━━━━━━━━━━━━━━━━\n"
             "⏸️ ***Bot အား ခေတ္တ ယာယီပိတ်ထားပါသည်*** ⏸️\n"
             "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
             "***🔄 Admin မှ ပြန်လည်ဖွင့်ပေးမှ အသုံးပြုနိုင်ပါမည်။***\n\n"
             "📞 ***အရေးပေါ်ဆိုရင် Admin ကို ဆက်သွယ်ပါ။***"
         )
    await update.message.reply_text(msg, parse_mode="Markdown")

def is_owner(user_id):
    """ Check if user is the owner (unchanged) """
    return int(user_id) == ADMIN_ID

def is_admin(user_id):
    """ Check if user is any admin (uses get_admins) """
    # Owner is always admin
    if is_owner(user_id):
        return True
    # Check other admins from DB
    admin_list = get_admins()
    return int(user_id) in admin_list

# --- Command Handlers (Modified for MongoDB) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or "-"
    name = f"{user.first_name} {user.last_name or ''}".strip()

    # Load authorized users from DB into global set at startup,
    # Here we just check the global set which should be up-to-date
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

    # Check for pending topups from DB
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    # Check if user exists in DB, if not, create basic entry
    user_data = get_user_data(user_id)
    if not user_data:
        print(f"Creating new user entry for {user_id}")
        initial_user_data = {
            "_id": user_id,
            "name": name,
            "username": username,
            "balance": 0,
            "orders": [],
            "topups": []
        }
        if users_col is not None:
             try:
                 users_col.insert_one(initial_user_data)
             except Exception as e:
                  print(f"❌ User ({user_id}) အသစ် ထည့်သွင်းရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        else:
             print("❌ Database connection မရှိပါ။ User အသစ် မထည့်နိုင်ပါ။")


    # Clear any restricted state when starting
    if user_id in user_states:
        del user_states[user_id]

    clickable_name = f"[{name}](tg://user?id={user_id})"
    msg = (
        f"👋 ***မင်္ဂလာပါ*** {clickable_name}!\n"
        f"🆔 ***Telegram User ID:*** `{user_id}`\n\n"
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
        print(f"Error getting/sending profile photo for {user_id}: {e}")
        await update.message.reply_text(msg, parse_mode="Markdown")

async def mmb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_user_authorized(user_id):
        keyboard = [[InlineKeyboardButton("👑 Contact Owner", url=f"tg://user?id={ADMIN_ID}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🚫 အသုံးပြုခွင့် မရှိပါ!\n\nOwner ထံ bot အသုံးပြုခွင့် တောင်းဆိုပါ။",
            reply_markup=reply_markup
        )
        return

    if not await check_maintenance_mode("orders"):
        await send_maintenance_message(update, "orders")
        return

    if user_id in user_states and user_states[user_id] == "waiting_approval":
        await update.message.reply_text(
            "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
            "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
            "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n"
            "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        return

    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

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
            "***မှန်ကန်တဲ့ format***:\n/mmb gameid serverid amount\n\n"
            "***ဥပမာ***:\n`/mmb 123456789 12345 wp1`\n`/mmb 123456789 12345 86`",
            parse_mode="Markdown"
        )
        return

    game_id, server_id, amount_str = args # Use amount_str temporarily

    if not validate_game_id(game_id):
        await update.message.reply_text("❌ ***Game ID မှားနေပါတယ်!*** (6-10 digits)", parse_mode="Markdown")
        return
    if not validate_server_id(server_id):
        await update.message.reply_text("❌ ***Server ID မှားနေပါတယ်!*** (3-5 digits)", parse_mode="Markdown")
        return

    if is_banned_account(game_id):
        await update.message.reply_text(
            "🚫 ***Account Ban ဖြစ်နေပါတယ်!***\n\n"
            f"🎮 Game ID: `{game_id}`\n🌐 Server ID: `{server_id}`\n\n"
            "❌ ဒီ account မှာ diamond topup လုပ်လို့ မရပါ။\n\n"
            "🔄 ***အခြား account သုံးပြီး ထပ်ကြိုးစားကြည့်ပါ။***\n"
            "📞 ***ပြဿနာရှိရင် admin ကို ဆက်သွယ်ပါ။***",
            parse_mode="Markdown"
        )
        # Notify admin (unchanged logic)
        admin_msg = (
             f"🚫 ***Banned Account Topup ကြိုးစားမှု***\n\n"
             f"👤 User: [{update.effective_user.first_name}](tg://user?id={user_id}) (`{user_id}`)\n"
             f"🎮 Game ID: `{game_id}`\n🌐 Server ID: `{server_id}`\n💎 Amount: {amount_str}\n"
             f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
             "***⚠️ ဒီ account မှာ topup လုပ်လို့ မရပါ။***"
         )
        admins_list = get_admins()
        for admin_id_notify in admins_list:
             try:
                 await context.bot.send_message(chat_id=admin_id_notify, text=admin_msg, parse_mode="Markdown")
             except Exception as e:
                 print(f"Failed to send banned account notification to admin {admin_id_notify}: {e}")
        return

    price = get_price(amount_str)
    if not price:
        await update.message.reply_text(
            "❌ Diamond amount မှားနေပါတယ်!\n\n"
            "***ရရှိနိုင်တဲ့ amounts များအတွက် /price ကို ကြည့်ပါ။***",
            parse_mode="Markdown"
        )
        return

    user_data = get_user_data(user_id)
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

    # --- Process Order with MongoDB ---
    order_id = f"ORD{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id[-3:]}" # Make ID slightly more unique
    order_data = {
        "order_id": order_id,
        "game_id": game_id,
        "server_id": server_id,
        "amount": amount_str, # Store the string amount (like 'wp1' or '86')
        "price": price,
        "status": "pending",
        "timestamp": datetime.now().isoformat(),
        # "user_id": user_id, # User ID is the document _id, no need to store again
        "chat_id": update.effective_chat.id
    }

    # Deduct balance and Add order using MongoDB operations
    balance_deducted = increment_user_balance(user_id, -price) # Use negative amount to deduct
    order_added = add_to_user_list(user_id, "orders", order_data)

    if not (balance_deducted and order_added):
        # Handle potential DB error - maybe try to refund if one succeeded and the other failed?
        print(f"❌ Order ({order_id}) processing failed for user {user_id}. Balance Deducted: {balance_deducted}, Order Added: {order_added}")
        # Attempt to refund if balance was deducted but order failed
        if balance_deducted and not order_added:
             increment_user_balance(user_id, price) # Add back the price
             await update.message.reply_text("❌ Order တင်ရာတွင် အမှားဖြစ်ပွားပါသည်။ ငွေ ပြန်အမ်းပြီးပါပြီ။ Admin ကို ဆက်သွယ်ပါ။")
        else:
             await update.message.reply_text("❌ Order တင်ရာတွင် အမှားဖြစ်ပွားပါသည်။ Admin ကို ဆက်သွယ်ပါ။")
        return

    # Get updated balance after deduction
    updated_user_data = get_user_data(user_id)
    new_balance = updated_user_data.get("balance", 0) if updated_user_data else user_balance - price # Estimate if fetch fails

    # Notify user
    await update.message.reply_text(
        f"✅ ***အော်ဒါ အောင်မြင်ပါပြီ!***\n\n"
        f"📝 ***Order ID:*** `{order_id}`\n"
        f"🎮 ***Game ID:*** `{game_id}`\n"
        f"🌐 ***Server ID:*** `{server_id}`\n"
        f"💎 ***Diamond:*** {amount_str}\n"
        f"💰 ***ကုန်ကျစရိတ်:*** {price:,} MMK\n"
        f"💳 ***လက်ကျန်ငွေ:*** {new_balance:,} MMK\n"
        f"📊 Status: ⏳ ***စောင့်ဆိုင်းနေသည်***\n\n"
        "⚠️ ***Admin က confirm လုပ်ပြီးမှ diamonds များ ရရှိပါမယ်။***\n"
        "📞 ***ပြဿနာရှိရင် admin ကို ဆက်သွယ်ပါ။***",
        parse_mode="Markdown"
    )

    # --- Notify Admins ---
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"order_confirm_{order_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"order_cancel_{order_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    user_name = update.effective_user.first_name or user_id

    admin_msg = (
        f"🔔 ***အော်ဒါအသစ်ရောက်ပါပြီ!***\n\n"
        f"📝 Order ID: `{order_id}`\n"
        f"👤 User: [{user_name}](tg://user?id={user_id}) (`{user_id}`)\n"
        f"🎮 Game ID: `{game_id}`\n"
        f"🌐 Server ID: `{server_id}`\n"
        f"💎 Amount: {amount_str}\n"
        f"💰 Price: {price:,} MMK\n"
        f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 Status: ⏳ ***စောင့်ဆိုင်းနေသည်***"
    )

    admin_list = get_admins()
    for admin_id in admin_list:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_msg,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        except Exception as e:
            print(f"Failed to send order notification to admin {admin_id}: {e}")

    # Notify admin group (unchanged logic, check if bot is admin)
    if ADMIN_GROUP_ID:
        try:
            if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                group_msg = (
                     f"🛒 ***အော်ဒါအသစ် ရောက်ပါပြီ!***\n\n"
                     f"📝 Order ID: `{order_id}`\n"
                     f"👤 User: [{user_name}](tg://user?id={user_id})\n"
                     f"🎮 Game ID: `{game_id}`\n"
                     f"🌐 Server ID: `{server_id}`\n"
                     f"💎 Amount: {amount_str}\n"
                     f"💰 Price: {price:,} MMK\n"
                     f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                     f"📊 Status: ⏳ စောင့်ဆိုင်းနေသည်\n\n"
                     f"#NewOrder #MLBB"
                 )
                await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=group_msg, parse_mode="Markdown")
        except Exception as e:
            print(f"Failed to send order notification to group {ADMIN_GROUP_ID}: {e}")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_user_authorized(user_id):
        # ... (authorization error message - unchanged) ...
        keyboard = [[InlineKeyboardButton("👑 Contact Owner", url=f"tg://user?id={ADMIN_ID}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
             "🚫 အသုံးပြုခွင့် မရှိပါ!\n\nOwner ထံ bot အသုံးပြုခွင့် တောင်းဆိုပါ။",
             reply_markup=reply_markup
        )
        return

    if user_id in user_states and user_states[user_id] == "waiting_approval":
        # ... (restricted message - unchanged) ...
         await update.message.reply_text(
             "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
             "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
             "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n\n"
             "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
             parse_mode="Markdown"
         )
         return

    if user_id in pending_topups:
         # ... (pending topup process message - unchanged) ...
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

    # --- Get User Data from MongoDB ---
    user_data = get_user_data(user_id)

    if not user_data:
        # If user data doesn't exist even after /start, try creating again or show error
        await update.message.reply_text("❌ User data မတွေ့ပါ။ /start ကို အရင်နှိပ်ပါ။")
        # Optionally try to create user here as well
        # update_user_data(user_id, {"name": update.effective_user.first_name or "User", "username": update.effective_user.username or "-"})
        return

    balance = user_data.get("balance", 0)
    total_orders = len(user_data.get("orders", []))
    total_topups = len(user_data.get("topups", []))
    name = user_data.get('name', 'Unknown')
    username = user_data.get('username', 'None')

    # Check for pending topups from DB data
    pending_topups_count = 0
    pending_amount = 0
    for topup in user_data.get("topups", []):
        if topup.get("status") == "pending":
            pending_topups_count += 1
            pending_amount += topup.get("amount", 0)

    # Sanitize name/username for Markdown (unchanged)
    name = name.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
    username = username.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
    username_display = f"@{username}" if username and username != "-" else "None"


    status_msg = ""
    if pending_topups_count > 0:
        status_msg = f"\n⏳ ***Pending Topups***: {pending_topups_count} ခု ({pending_amount:,} MMK)\n❗ ***Diamond order ထားလို့မရပါ။ Admin approve စောင့်ပါ။***"

    keyboard = [[InlineKeyboardButton("💳 ငွေဖြည့်မယ်", callback_data="topup_button")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    balance_text = (
        f"💳 ***သင့်ရဲ့ Account အချက်အလက်များ***\n\n"
        f"💰 ***လက်ကျန်ငွေ***: `{balance:,} MMK`\n"
        f"📦 ***စုစုပေါင်း အော်ဒါများ***: {total_orders}\n"
        f"💳 ***စုစုပေါင်း ငွေဖြည့်မှုများ***: {total_topups}{status_msg}\n\n"
        f"***👤 နာမည်***: {name}\n"
        f"***🆔 Username***: {username_display}"
    )

    # Send balance info (with profile photo if possible - unchanged logic)
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
            await update.message.reply_text(balance_text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        print(f"Error getting/sending profile photo for balance {user_id}: {e}")
        await update.message.reply_text(balance_text, parse_mode="Markdown", reply_markup=reply_markup)


async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_user_authorized(user_id):
        # ... (authorization error) ...
         keyboard = [[InlineKeyboardButton("👑 Contact Owner", url=f"tg://user?id={ADMIN_ID}")]]
         reply_markup = InlineKeyboardMarkup(keyboard)
         await update.message.reply_text(
             "🚫 အသုံးပြုခွင့် မရှိပါ!\n\nOwner ထံ bot အသုံးပြုခွင့် တောင်းဆိုပါ။",
             reply_markup=reply_markup
         )
         return

    if not await check_maintenance_mode("topups"):
        await send_maintenance_message(update, "topups")
        return

    if user_id in user_states and user_states[user_id] == "waiting_approval":
        # ... (restricted message) ...
         await update.message.reply_text(
             "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
             "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
             "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n\n"
             "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
             parse_mode="Markdown"
         )
         return

    # Use check_pending_topup which checks DB
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    if user_id in pending_topups:
        # ... (pending process message - unchanged) ...
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
            "**ဥပမာ**: `/topup 50000`\n"
            "💡 ***အနည်းဆုံး 1,000 MMK ဖြည့်ရပါမည်။***",
            parse_mode="Markdown"
        )
        return

    try:
        amount = int(args[0])
        if amount < 1000:
            await update.message.reply_text(
                "❌ ***ငွေပမာဏ နည်းလွန်းပါတယ်!***\n\n💰 ***အနည်းဆုံး 1,000 MMK ဖြည့်ရပါမည်။***",
                parse_mode="Markdown"
            )
            return
    except ValueError:
        await update.message.reply_text(
            "❌ ***ငွေပမာဏ မှားနေပါတယ်!***\n\n💰 ***ကိန်းဂဏန်းများသာ ရေးပါ။***\n"
            "***ဥပမာ***: `/topup 5000`",
            parse_mode="Markdown"
        )
        return

    # Store in global pending_topups dictionary (unchanged, used for multi-step process)
    pending_topups[user_id] = {
        "amount": amount,
        "timestamp": datetime.now().isoformat()
    }

    # Show payment method selection (unchanged)
    keyboard = [
        [InlineKeyboardButton("📱 KBZ Pay", callback_data=f"topup_pay_kpay_{amount}")],
        [InlineKeyboardButton("📱 Wave Money", callback_data=f"topup_pay_wave_{amount}")],
        [InlineKeyboardButton("❌ ငြင်းပယ်မယ်", callback_data="topup_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"💳 ***ငွေဖြည့်လုပ်ငန်းစဉ်***\n\n✅ ***ပမာဏ***: `{amount:,} MMK`\n\n"
        f"***အဆင့် 1***: Payment method ရွေးချယ်ပါ\n\n"
        f"***⬇️ ငွေလွှဲမည့် app ရွေးချယ်ပါ***:\n\n"
        f"***ℹ️ ပယ်ဖျက်ရန်*** /cancel ***နှိပ်ပါ***",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

# --- handle_photo (Modified for MongoDB) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_user_authorized(user_id): return

    if not is_payment_screenshot(update):
         await update.message.reply_text(
             "❌ ***သင့်ပုံ လက်မခံပါ!***\n\n"
             "🔍 ***Payment screenshot သာ လက်ခံပါတယ်။***\n"
             "💳 ***KPay, Wave လွှဲမှု screenshot များသာ တင်ပေးပါ။***",
             parse_mode="Markdown"
         )
         return

    if user_id not in pending_topups:
        await update.message.reply_text(
            "❌ ***Topup process မရှိပါ!***\n\n🔄 ***အရင်ဆုံး `/topup amount` command ကို သုံးပါ။***",
            parse_mode="Markdown"
        )
        return

    pending = pending_topups[user_id]
    amount = pending["amount"]
    payment_method = pending.get("payment_method", "Unknown")

    if payment_method == "Unknown":
        await update.message.reply_text(
            "❌ ***Payment app ကို အရင်ရွေးပါ!***\n\n"
            "📱 ***KPay သို့မဟုတ် Wave ကို ရွေးချယ်ပြီးမှ screenshot တင်ပါ။***",
            parse_mode="Markdown"
        )
        return

    # Set user state to restricted
    user_states[user_id] = "waiting_approval"

    topup_id = f"TOP{datetime.now().strftime('%Y%m%d%H%M%S')}{user_id[-4:]}"
    user_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    # --- Save Topup Request to MongoDB ---
    topup_request_data = {
        "topup_id": topup_id,
        "amount": amount,
        "payment_method": payment_method,
        "status": "pending",
        "timestamp": datetime.now().isoformat(),
        "screenshot_file_id": update.message.photo[-1].file_id if update.message.photo else None, # Save file_id
        "chat_id": update.effective_chat.id # Store chat ID where topup was initiated
    }

    # Ensure user exists before adding topup
    user_exists = get_user_data(user_id)
    if not user_exists:
         # Create basic user doc if doesn't exist (should normally exist after /start)
         update_user_data(user_id, {"name": user_name, "username": update.effective_user.username or "-", "balance": 0, "orders": [], "topups": []})

    topup_added = add_to_user_list(user_id, "topups", topup_request_data)

    if not topup_added:
         print(f"❌ Failed to save topup request ({topup_id}) for user {user_id} to DB.")
         await update.message.reply_text("❌ Database အမှားကြောင့် ငွေဖြည့် တောင်းဆိုမှု မသိမ်းဆည်းနိုင်ပါ။ Admin ကို ဆက်သွယ်ပါ။")
         # Remove restriction if DB save failed
         if user_id in user_states: del user_states[user_id]
         return

    # --- Notify Admins (Send photo with caption) ---
    admin_msg = (
        f"💳 ***ငွေဖြည့်တောင်းဆိုမှု***\n\n"
        f"👤 User: [{user_name}](tg://user?id={user_id}) (`{user_id}`)\n"
        f"💰 Amount: `{amount:,} MMK`\n"
        f"📱 Payment: {payment_method.upper()}\n"
        f"🔖 Topup ID: `{topup_id}`\n"
        f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 Status: ⏳ ***စောင့်ဆိုင်းနေသည်***\n\n"
        f"***Screenshot စစ်ဆေးပြီး လုပ်ဆောင်ပါ။***"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"topup_approve_{topup_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"topup_reject_{topup_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    admin_list = get_admins()
    photo_file_id = update.message.photo[-1].file_id

    for admin_id in admin_list:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=admin_msg,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        except Exception as e:
            print(f"Failed to send topup photo to admin {admin_id}: {e}")

    # Notify admin group (unchanged logic)
    if ADMIN_GROUP_ID:
        try:
            if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                group_msg = (
                     f"💳 ***ငွေဖြည့်တောင်းဆိုမှု***\n\n"
                     f"👤 User: [{user_name}](tg://user?id={user_id})\n"
                     f"🆔 User ID: `{user_id}`\n"
                     f"💰 Amount: `{amount:,} MMK`\n"
                     f"📱 Payment: {payment_method.upper()}\n"
                     f"🔖 Topup ID: `{topup_id}`\n"
                     f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                     f"📊 Status: ⏳ စောင့်ဆိုင်းနေသည်\n\n"
                     # Add Approve command example for group
                     f"***Approve လုပ်ရန်:*** `/approve {user_id} {amount}`\n\n"
                     f"#TopupRequest #Payment"
                 )
                # Send photo to group as well
                await context.bot.send_photo(
                     chat_id=ADMIN_GROUP_ID,
                     photo=photo_file_id,
                     caption=group_msg,
                     parse_mode="Markdown",
                     reply_markup=reply_markup # Also add buttons to group message
                 )
        except Exception as e:
            print(f"Failed to send topup photo to group {ADMIN_GROUP_ID}: {e}")

    # Clear from pending_topups dictionary now that it's in DB
    del pending_topups[user_id]

    await update.message.reply_text(
        f"✅ ***Screenshot လက်ခံပါပြီ!***\n\n💰 ***ပမာဏ:*** `{amount:,} MMK`\n"
        f"⏰ ***အချိန်:*** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "🔒 ***အသုံးပြုမှု ယာယီ ကန့်သတ်ပါ***\n"
        "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ Commands/Messages အသုံးပြုလို့ မရပါ။***\n\n"
        "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n"
        "📞 ***ပြဿနာရှိရင် admin ကို ဆက်သွယ်ပါ။***",
        parse_mode="Markdown"
    )

# --- Admin Commands (Modified for MongoDB) ---

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id)
    admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text("❌ Format: `/approve <user_id> <amount>`")
        return

    target_user_id = args[0]
    try:
        amount = int(args[1])
        if amount <= 0:
             await update.message.reply_text("❌ Amount သည် 0 ထက်ကြီးရမည်!")
             return
    except ValueError:
        await update.message.reply_text("❌ Amount သည် ကိန်းဂဏန်းဖြစ်ရမည်!")
        return

    # --- Find the latest pending topup with matching amount ---
    target_user_data = get_user_data(target_user_id)
    if not target_user_data:
        await update.message.reply_text(f"❌ User ID `{target_user_id}` မတွေ့ပါ။")
        return

    pending_topup_found = None
    topup_index = -1 # To update the correct element
    for i, topup in enumerate(reversed(target_user_data.get("topups", []))): # Check recent ones first
         if topup.get("status") == "pending" and topup.get("amount") == amount:
             pending_topup_found = topup
             # Calculate the original index because we reversed the list for search
             topup_index = len(target_user_data.get("topups", [])) - 1 - i
             break

    if not pending_topup_found:
        await update.message.reply_text(f"❌ User `{target_user_id}` အတွက် `{amount:,}` MMK ပမာဏဖြင့် Pending topup မတွေ့ပါ။")
        return

    topup_id = pending_topup_found.get("topup_id", f"UNKNOWN_{datetime.now().timestamp()}") # Get ID if available

    # --- Update Topup Status and User Balance in DB ---
    topup_update_fields = {
        f"topups.{topup_index}.status": "approved",
        f"topups.{topup_index}.approved_by": admin_name,
        f"topups.{topup_index}.approved_at": datetime.now().isoformat()
    }

    topup_updated = update_user_data(target_user_id, topup_update_fields)
    balance_added = increment_user_balance(target_user_id, amount)

    if not (topup_updated and balance_added):
         print(f"❌ Topup approve ({topup_id}) processing failed for user {target_user_id}.")
         # Attempt to revert status if balance failed? More complex logic needed.
         await update.message.reply_text("❌ Database အမှားကြောင့် Approve မလုပ်နိုင်ပါ။")
         return

    # Clear user restriction state
    if target_user_id in user_states:
        del user_states[target_user_id]

    # Get new balance
    updated_user_data = get_user_data(target_user_id)
    new_balance = updated_user_data.get("balance", 0) if updated_user_data else "Error fetching"

    # Notify user
    try:
        keyboard = [[InlineKeyboardButton("💎 Order တင်မယ်", url=f"https://t.me/{context.bot.username}?start=order")]] # Use bot username from context
        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(
            chat_id=int(target_user_id),
            text=f"✅ ***ငွေဖြည့်မှု အတည်ပြုပါပြီ!*** 🎉\n\n"
                 f"💰 ***ပမာဏ:*** `{amount:,} MMK`\n"
                 f"💳 ***လက်ကျန်ငွေ:*** `{new_balance:,} MMK`\n"
                 f"👤 ***Approved by:*** [{admin_name}](tg://user?id={admin_user_id})\n"
                 f"⏰ ***အချိန်:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                 f"🎉 ***ယခုအခါ diamonds များ ဝယ်ယူနိုင်ပါပြီ!***\n"
                 f"🔓 ***Bot လုပ်ဆောင်ချက်များ ပြန်လည် အသုံးပြုနိုင်ပါပြီ!***\n\n"
                 f"💎 ***Order တင်ရန်:*** `/mmb gameid serverid amount`",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    except Exception as e:
        print(f"Failed to notify user {target_user_id} about approval: {e}")
        await update.message.reply_text(f"⚠️ User {target_user_id} ကို အကြောင်းမကြားနိုင်ပါ။ Approve တော့ အောင်မြင်ပါသည်။")


    # Confirm to admin who issued the command
    await update.message.reply_text(
        f"✅ ***Approve အောင်မြင်ပါပြီ!***\n\n"
        f"👤 User ID: `{target_user_id}`\n💰 Amount: `{amount:,} MMK`\n"
        f"💳 User's new balance: `{new_balance:,} MMK`\n"
        f"🔓 User restrictions cleared!",
        parse_mode="Markdown"
    )

    # Notify other admins & group (similar to button_callback logic)
    # ... (Add notification logic here if needed for /approve command too) ...


async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id)

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text("❌ Format: `/deduct <user_id> <amount>`")
        return

    target_user_id = args[0]
    try:
        amount = int(args[1])
        if amount <= 0:
            await update.message.reply_text("❌ Amount သည် 0 ထက်ကြီးရမည်!")
            return
    except ValueError:
        await update.message.reply_text("❌ Amount သည် ကိန်းဂဏန်းဖြစ်ရမည်!")
        return

    user_data = get_user_data(target_user_id)
    if not user_data:
        await update.message.reply_text(f"❌ User ID `{target_user_id}` မတွေ့ပါ။")
        return

    current_balance = user_data.get("balance", 0)

    if current_balance < amount:
        await update.message.reply_text(
            f"❌ ***နှုတ်လို့မရပါ!***\n\n👤 User ID: `{target_user_id}`\n"
            f"💰 ***နှုတ်ချင်တဲ့ပမာဏ***: `{amount:,} MMK`\n"
            f"💳 ***User လက်ကျန်ငွေ***: `{current_balance:,} MMK`",
            parse_mode="Markdown"
        )
        return

    # Deduct balance using $inc
    deducted = increment_user_balance(target_user_id, -amount)

    if not deducted:
         await update.message.reply_text("❌ Database အမှားကြောင့် Balance မနှုတ်နိုင်ပါ။")
         return

    # Get new balance
    updated_user_data = get_user_data(target_user_id)
    new_balance = updated_user_data.get("balance", 0) if updated_user_data else current_balance - amount

    # Notify user
    try:
        user_msg = (
            f"⚠️ ***လက်ကျန်ငွေ နှုတ်ခံရမှု***\n\n"
            f"💰 ***နှုတ်ခံရတဲ့ပမာဏ***: `{amount:,} MMK`\n"
            f"💳 ***လက်ကျန်ငွေ***: `{new_balance:,} MMK`\n"
            f"⏰ ***အချိန်***: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "📞 မေးခွန်းရှိရင် admin ကို ဆက်သွယ်ပါ။"
        )
        await context.bot.send_message(chat_id=int(target_user_id), text=user_msg, parse_mode="Markdown")
    except Exception as e:
        print(f"Failed to notify user {target_user_id} about deduction: {e}")
        await update.message.reply_text(f"⚠️ User {target_user_id} ကို အကြောင်းမကြားနိုင်ပါ။ Balance နှုတ်ခြင်း အောင်မြင်ပါသည်။")


    # Confirm to admin
    await update.message.reply_text(
        f"✅ ***Balance နှုတ်ခြင်း အောင်မြင်ပါပြီ!***\n\n"
        f"👤 User ID: `{target_user_id}`\n"
        f"💰 ***နှုတ်ခဲ့တဲ့ပမာဏ***: `{amount:,} MMK`\n"
        f"💳 ***User လက်ကျန်ငွေ***: `{new_balance:,} MMK`",
        parse_mode="Markdown"
    )

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id)
    admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    # Allow any admin to ban
    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("❌ Format: /ban <user_id>")
        return

    target_user_id = args[0]
    # Load current list from DB before modifying
    load_authorized_users() # Ensure global set is fresh

    if target_user_id not in AUTHORIZED_USERS:
        # Also check if the target is an admin (cannot ban admins except maybe owner?)
        if is_admin(target_user_id) and not is_owner(admin_user_id):
            await update.message.reply_text("❌ Admin အချင်းချင်း ban လုပ်၍ မရပါ။ Owner ကို ဆက်သွယ်ပါ။")
            return
        if is_owner(target_user_id):
             await update.message.reply_text("❌ Owner ကို ban လုပ်၍ မရပါ။")
             return

        await update.message.reply_text(f"ℹ️ User `{target_user_id}` သည် authorize မလုပ်ထားပါ (သို့) admin ဖြစ်နေပါသည်။")
        return

    # Remove from global set and save to DB
    AUTHORIZED_USERS.remove(target_user_id)
    save_authorized_users() # Saves the updated set to MongoDB

    # Notify user (unchanged)
    try:
        await context.bot.send_message(
            chat_id=int(target_user_id),
            text="🚫 Bot အသုံးပြုခွင့် ပိတ်ပင်ခံရမှု\n\n"
                 "❌ Admin က သင့်ကို ban လုပ်လိုက်ပါပြီ။\n\n"
                 "📞 အကြောင်းရင်း သိရှိရန် Admin ကို ဆက်သွယ်ပါ။",
            parse_mode="Markdown"
        )
    except Exception as e: print(f"Failed to notify banned user {target_user_id}: {e}")

    # Notify owner (unchanged, but get user name from DB if possible)
    user_data = get_user_data(target_user_id)
    user_name = user_data.get("name", "Unknown") if user_data else "Unknown"
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🚫 *User Ban Notification*\n\n"
                 f"👤 Admin: [{admin_name}](tg://user?id={admin_user_id})\n"
                 f"🎯 Banned User: [{user_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\n"
                 f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode="Markdown"
        )
    except Exception as e: print(f"Failed to notify owner about ban: {e}")

    # Notify admin group (unchanged logic)
    if ADMIN_GROUP_ID:
         try:
             if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                 group_msg = (
                     f"🚫 ***User Ban ဖြစ်ပါပြီ!***\n\n"
                     f"👤 User: [{user_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\n"
                     f"👤 Ban လုပ်သူ: {admin_name}\n"
                     f"📊 Status: 🚫 Ban ဖြစ်ပြီး\n\n"
                     f"#UserBanned"
                 )
                 await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=group_msg, parse_mode="Markdown")
         except Exception as e: print(f"Failed to notify group about ban: {e}")


    await update.message.reply_text(
        f"✅ User Ban အောင်မြင်ပါပြီ!\n\n"
        f"👤 User ID: `{target_user_id}`\n🎯 Status: Banned\n"
        f"📝 Total authorized users: {len(AUTHORIZED_USERS)}",
        parse_mode="Markdown"
    )

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id)
    admin_name = f"{update.effective_user.first_name} {update.effective_user.last_name or ''}".strip()

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ သင်သည် admin မဟုတ်ပါ!")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("❌ Format: /unban <user_id>")
        return

    target_user_id = args[0]
    load_authorized_users() # Ensure global set is fresh

    if target_user_id in AUTHORIZED_USERS:
        await update.message.reply_text(f"ℹ️ User `{target_user_id}` သည် authorize ပြုလုပ်ထားပြီးသား ဖြစ်ပါသည်။")
        return

    # Add to global set and save to DB
    AUTHORIZED_USERS.add(target_user_id)
    save_authorized_users()

    # Clear restrictions
    if target_user_id in user_states:
        del user_states[target_user_id]

    # Notify user (unchanged)
    try:
        await context.bot.send_message(
            chat_id=int(target_user_id),
            text="🎉 *Bot အသုံးပြုခွင့် ပြန်လည်ရရှိပါပြီ!*\n\n"
                 "✅ Admin က သင့် ban ကို ဖြုတ်ပေးလိုက်ပါပြီ။\n\n"
                 "🚀 ယခုအခါ /start နှိပ်ပြီး bot ကို အသုံးပြုနိုင်ပါပြီ!",
            parse_mode="Markdown"
        )
    except Exception as e: print(f"Failed to notify unbanned user {target_user_id}: {e}")

    # Notify owner (unchanged, get name from DB)
    user_data = get_user_data(target_user_id)
    user_name = user_data.get("name", "Unknown") if user_data else "Unknown"
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"✅ *User Unban Notification*\n\n"
                 f"👤 Admin: [{admin_name}](tg://user?id={admin_user_id})\n"
                 f"🎯 Unbanned User: [{user_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\n"
                 f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode="Markdown"
        )
    except Exception as e: print(f"Failed to notify owner about unban: {e}")

    # Notify admin group (unchanged logic)
    if ADMIN_GROUP_ID:
        try:
             if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                 group_msg = (
                     f"✅ ***User Unban ဖြစ်ပါပြီ!***\n\n"
                     f"👤 User: [{user_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\n"
                     f"👤 Unban လုပ်သူ: {admin_name}\n"
                     f"📊 Status: ✅ Unban ဖြစ်ပြီး\n\n"
                     f"#UserUnbanned"
                 )
                 await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=group_msg, parse_mode="Markdown")
        except Exception as e: print(f"Failed to notify group about unban: {e}")


    await update.message.reply_text(
        f"✅ User Unban အောင်မြင်ပါပြီ!\n\n"
        f"👤 User ID: `{target_user_id}`\n🎯 Status: Unbanned\n"
        f"📝 Total authorized users: {len(AUTHORIZED_USERS)}",
        parse_mode="Markdown"
    )

async def addadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id)

    if not is_owner(admin_user_id):
        await update.message.reply_text("❌ Owner သာ admin ခန့်အပ်နိုင်ပါတယ်!")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("❌ Format: /addadm <user_id>")
        return

    new_admin_id = int(args[0])
    current_admins = get_admins()

    if new_admin_id in current_admins:
        await update.message.reply_text("ℹ️ User သည် admin ဖြစ်ပြီးသားပါ။")
        return

    # Add admin to DB
    added = add_admin_db(new_admin_id)

    if not added:
        await update.message.reply_text("❌ Database အမှားကြောင့် Admin မထည့်နိုင်ပါ။")
        return

    new_admin_list = get_admins() # Get updated list

    # Notify new admin (unchanged)
    try:
        await context.bot.send_message(
            chat_id=new_admin_id,
            text="🎉 Admin ရာထူးရရှိမှု\n\n✅ Owner က သင့်ကို Admin အဖြစ် ခန့်အပ်ပါပြီ။\n\n"
                 "🔧 Admin commands များကို /adminhelp နှိပ်၍ ကြည့်နိုင်ပါတယ်။"
                 # Add limitations reminder
                 "\n\n⚠️ သတိပြုရန်:\n• Owner ကလွဲ၍ Admin အသစ်/ဖြုတ်ခြင်း မလုပ်နိုင်ပါ။"
        )
    except Exception as e: print(f"Failed to notify new admin {new_admin_id}: {e}")

    await update.message.reply_text(
        f"✅ ***Admin ထပ်မံထည့်သွင်းပါပြီ!***\n\n👤 User ID: `{new_admin_id}`\n"
        f"🎯 Status: Admin\n📝 Total admins: {len(new_admin_list)}",
        parse_mode="Markdown"
    )

async def unadm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_user_id = str(update.effective_user.id)

    if not is_owner(admin_user_id):
        await update.message.reply_text("❌ Owner သာ admin ဖြုတ်နိုင်ပါတယ်!")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("❌ Format: /unadm <user_id>")
        return

    target_admin_id = int(args[0])

    if target_admin_id == ADMIN_ID:
        await update.message.reply_text("❌ Owner ကို ဖြုတ်လို့ မရပါ!")
        return

    current_admins = get_admins()
    if target_admin_id not in current_admins:
        await update.message.reply_text("ℹ️ User သည် admin မဟုတ်ပါ။")
        return

    # Remove admin from DB
    removed = remove_admin_db(target_admin_id)

    if not removed:
        await update.message.reply_text("❌ Database အမှားကြောင့် Admin မဖြုတ်နိုင်ပါ။")
        return

    new_admin_list = get_admins() # Get updated list

    # Notify removed admin (unchanged)
    try:
        await context.bot.send_message(
            chat_id=target_admin_id,
            text="⚠️ Admin ရာထူး ရုပ်သိမ်းခံရမှု\n\n"
                 "❌ Owner က သင့်ရဲ့ admin ရာထူးကို ရုပ်သိမ်းလိုက်ပါပြီ。\n\n"
                 "📞 အကြောင်းရင်း သိရှိရန် Owner ကို ဆက်သွယ်ပါ။"
        )
    except Exception as e: print(f"Failed to notify removed admin {target_admin_id}: {e}")

    await update.message.reply_text(
        f"✅ ***Admin ဖြုတ်ခြင်း အောင်မြင်ပါပြီ!***\n\n👤 User ID: `{target_admin_id}`\n"
        f"🎯 Status: Removed from Admin\n📝 Total admins: {len(new_admin_list)}",
        parse_mode="Markdown"
    )

# ... (broadcast_command needs changes to get user IDs and group chat IDs from DB) ...
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     user_id = str(update.effective_user.id)
     if not is_owner(user_id):
          await update.message.reply_text("❌ Owner သာ broadcast လုပ်နိုင်ပါတယ်!")
          return

     args = context.args
     if not update.message.reply_to_message:
         # ... (no reply message error - unchanged) ...
         await update.message.reply_text("❌ စာ သို့မဟုတ် ပုံကို reply လုပ်ပြီး broadcast command သုံးပါ။\n...")
         return
     if len(args) == 0:
         # ... (no target error - unchanged) ...
         await update.message.reply_text("❌ Target (user/gp) ထည့်ပါ။\n...")
         return

     send_to_users = "user" in args
     send_to_groups = "gp" in args
     if not send_to_users and not send_to_groups:
         # ... (invalid target error - unchanged) ...
         await update.message.reply_text("❌ Target မှားနေပါသည် (user/gp/user gp)။\n...")
         return

     replied_msg = update.message.reply_to_message
     user_success = 0
     user_fail = 0
     group_success = 0
     group_fail = 0

     # --- Get User IDs from DB ---
     user_ids_to_send = []
     if send_to_users:
          if users_col:
              try:
                   # Find all user IDs (projection={'_id': 1})
                   all_users = users_col.find({}, {'_id': 1})
                   user_ids_to_send = [user['_id'] for user in all_users]
              except Exception as e:
                   print(f"❌ Broadcast အတွက် User ID များ ရယူရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
          else:
               print("❌ Database connection မရှိပါ။ User များထံ မပို့နိုင်ပါ။")

     # --- Get Group Chat IDs from DB (distinct chat_ids from orders/topups) ---
     group_ids_to_send = set()
     if send_to_groups:
          if users_col:
              try:
                   # Get distinct chat_ids < 0 from orders array
                   order_chats = users_col.distinct("orders.chat_id", {"orders.chat_id": {"$lt": 0}})
                   # Get distinct chat_ids < 0 from topups array
                   topup_chats = users_col.distinct("topups.chat_id", {"topups.chat_id": {"$lt": 0}})
                   group_ids_to_send.update(order_chats)
                   group_ids_to_send.update(topup_chats)
              except Exception as e:
                   print(f"❌ Broadcast အတွက် Group ID များ ရယူရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
          else:
               print("❌ Database connection မရှိပါ။ Group များထံ မပို့နိုင်ပါ။")


     # --- Sending Logic ---
     if replied_msg.photo:
         photo_file_id = replied_msg.photo[-1].file_id
         caption = replied_msg.caption or ""
         caption_entities = replied_msg.caption_entities or None

         # Send to Users
         for uid in user_ids_to_send:
             try:
                 await context.bot.send_photo(chat_id=int(uid), photo=photo_file_id, caption=caption, caption_entities=caption_entities)
                 user_success += 1
                 await asyncio.sleep(0.05) # Rate limit
             except Exception as e:
                 print(f"Broadcast photo failed for user {uid}: {e}")
                 user_fail += 1

         # Send to Groups
         for gid in group_ids_to_send:
             try:
                 await context.bot.send_photo(chat_id=gid, photo=photo_file_id, caption=caption, caption_entities=caption_entities)
                 group_success += 1
                 await asyncio.sleep(0.05) # Rate limit
             except Exception as e:
                 print(f"Broadcast photo failed for group {gid}: {e}")
                 group_fail += 1

     elif replied_msg.text:
         message_text = replied_msg.text
         entities = replied_msg.entities or None

         # Send to Users
         for uid in user_ids_to_send:
             try:
                 await context.bot.send_message(chat_id=int(uid), text=message_text, entities=entities)
                 user_success += 1
                 await asyncio.sleep(0.05)
             except Exception as e:
                 print(f"Broadcast text failed for user {uid}: {e}")
                 user_fail += 1

         # Send to Groups
         for gid in group_ids_to_send:
             try:
                 await context.bot.send_message(chat_id=gid, text=message_text, entities=entities)
                 group_success += 1
                 await asyncio.sleep(0.05)
             except Exception as e:
                 print(f"Broadcast text failed for group {gid}: {e}")
                 group_fail += 1
     else:
         await update.message.reply_text("❌ Text သို့မဟုတ် Photo သာ broadcast လုပ်နိုင်ပါတယ်!")
         return

     # Report results (unchanged)
     targets_report = []
     if send_to_users:
         targets_report.append(f"Users: {user_success} အောင်မြင်, {user_fail} မအောင်မြင်")
     if send_to_groups:
         targets_report.append(f"Groups: {group_success} အောင်မြင်, {group_fail} မအောင်မြင်")
     await update.message.reply_text(
         f"✅ Broadcast ပြီးပါပြီ!\n\n👥 {chr(10).join(targets_report)}\n\n"
         f"📊 စုစုပေါင်း: {user_success + group_success} ပို့ပြီး",
         parse_mode="Markdown"
     )


# --- History Command (Modified) ---
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_user_authorized(user_id):
        # ... auth error ...
         keyboard = [[InlineKeyboardButton("👑 Contact Owner", url=f"tg://user?id={ADMIN_ID}")]]
         reply_markup = InlineKeyboardMarkup(keyboard)
         await update.message.reply_text(
             "🚫 အသုံးပြုခွင့် မရှိပါ!\n\nOwner ထံ bot အသုံးပြုခွင့် တောင်းဆိုပါ။",
             reply_markup=reply_markup
         )
         return

    if user_id in user_states and user_states[user_id] == "waiting_approval":
        # ... restricted error ...
         await update.message.reply_text(
             "⏳ ***Screenshot ပို့ပြီးပါပြီ!***\n\n"
             "❌ ***Admin က လက်ခံပြီးကြောင်း အတည်ပြုတဲ့အထိ commands တွေ အသုံးပြုလို့ မရပါ။***\n\n"
             "⏰ ***Admin က approve လုပ်ပြီးမှ ပြန်လည် အသုံးပြုနိုင်ပါမယ်။***\n\n"
             "📞 ***အရေးပေါ်ဆိုရင် admin ကို ဆက်သွယ်ပါ။***",
             parse_mode="Markdown"
         )
         return
    if user_id in pending_topups:
        # ... pending process error ...
         await update.message.reply_text(
             "⏳ ***Topup လုပ်ငန်းစဉ် ဆက်လက်လုပ်ဆောင်ပါ!***\n\n"
             # ... rest of message ...
             "💡 ***ပယ်ဖျက်ပြီးမှ အခြား commands များ အသုံးပြုနိုင်ပါမယ်။***",
             parse_mode="Markdown"
         )
         return
    if await check_pending_topup(user_id):
        await send_pending_topup_warning(update)
        return

    user_data = get_user_data(user_id)
    if not user_data:
        await update.message.reply_text("❌ User data မတွေ့ပါ။ /start ကို အရင်နှိပ်ပါ။")
        return

    orders = user_data.get("orders", [])
    topups = user_data.get("topups", [])

    if not orders and not topups:
        await update.message.reply_text("📋 သင့်မှာ မည်သည့် မှတ်တမ်းမှ မရှိသေးပါ။")
        return

    msg = "📋 ***သင့်ရဲ့ နောက်ဆုံး မှတ်တမ်းများ***\n\n"
    limit = 5 # Show last 5

    if orders:
        msg += f"🛒 ***အော်ဒါများ (နောက်ဆုံး {limit} ခု):***\n"
        # Sort orders by timestamp descending if needed, then take last 5
        # sorted_orders = sorted(orders, key=lambda x: x.get('timestamp', ''), reverse=True)
        for order in orders[-limit:]: # Get last 5 directly from stored list
            status = order.get("status", "pending")
            status_emoji = "✅" if status == "confirmed" else ("❌" if status == "cancelled" else "⏳")
            ts = order.get('timestamp', '')
            date_str = datetime.fromisoformat(ts).strftime('%Y-%m-%d') if ts else 'N/A'
            msg += f"{status_emoji} `{order.get('order_id', 'N/A')}` ({order.get('amount', '?')} dia) - {order.get('price', 0):,} MMK [{date_str}]\n"
        msg += "\n"

    if topups:
        msg += f"💳 ***ငွေဖြည့်များ (နောက်ဆုံး {limit} ခု):***\n"
        # sorted_topups = sorted(topups, key=lambda x: x.get('timestamp', ''), reverse=True)
        for topup in topups[-limit:]:
            status = topup.get("status", "pending")
            status_emoji = "✅" if status == "approved" else ("❌" if status == "rejected" else "⏳")
            ts = topup.get('timestamp', '')
            date_str = datetime.fromisoformat(ts).strftime('%Y-%m-%d') if ts else 'N/A'
            msg += f"{status_emoji} {topup.get('amount', 0):,} MMK ({topup.get('payment_method', '?').upper()}) [{date_str}]\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


# --- Report Commands (Modified - Need date string parsing carefully) ---
# Helper for report date filtering
def filter_by_date(items, date_field, start_date_str, end_date_str):
    filtered_items = []
    try:
        start_dt = datetime.fromisoformat(start_date_str + "T00:00:00")
        end_dt = datetime.fromisoformat(end_date_str + "T23:59:59")
    except ValueError:
        print(f"⚠️ Invalid date format for filtering: {start_date_str}, {end_date_str}")
        return []

    for item in items:
        timestamp_str = item.get(date_field)
        if timestamp_str:
            try:
                item_dt = datetime.fromisoformat(timestamp_str)
                if start_dt <= item_dt <= end_dt:
                    filtered_items.append(item)
            except ValueError:
                 # Try parsing just the date part if ISO format fails
                 try:
                      item_dt = datetime.fromisoformat(timestamp_str[:10] + "T00:00:00")
                      if start_dt.date() <= item_dt.date() <= end_dt.date():
                           filtered_items.append(item)
                 except ValueError:
                      print(f"⚠️ Could not parse date {timestamp_str} in item {item.get('order_id') or item.get('topup_id')}")
                      continue
    return filtered_items

async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_owner(user_id):
        await update.message.reply_text("❌ Owner သာ ကြည့်နိုင်ပါတယ်!")
        return

    args = context.args
    # ... (Argument parsing and date selection logic remains the same) ...
    if len(args) == 0:
         # Show date filter buttons (unchanged)
         today = datetime.now()
         yesterday = today - timedelta(days=1)
         week_ago = today - timedelta(days=7)
         keyboard = [
             [InlineKeyboardButton("📅 ဒီနေ့", callback_data=f"report_day_{today.strftime('%Y-%m-%d')}")],
             [InlineKeyboardButton("📅 မနေ့က", callback_data=f"report_day_{yesterday.strftime('%Y-%m-%d')}")],
             [InlineKeyboardButton(f"📅 {week_ago.strftime('%m/%d')} မှ {today.strftime('%m/%d')}", callback_data=f"report_day_range_{week_ago.strftime('%Y-%m-%d')}_{today.strftime('%Y-%m-%d')}")],
         ]
         reply_markup = InlineKeyboardMarkup(keyboard)
         await update.message.reply_text("📊 ***ရက်စွဲ ရွေးချယ်ပါ***\n...", parse_mode="Markdown", reply_markup=reply_markup)
         return
    elif len(args) == 1:
         start_date_str = end_date_str = args[0]
         period_text = f"ရက် ({start_date_str})"
    elif len(args) == 2:
         start_date_str = args[0]
         end_date_str = args[1]
         period_text = f"ရက် ({start_date_str} မှ {end_date_str})"
    else:
        # ... (Invalid format message - unchanged) ...
        await update.message.reply_text("❌ Format မှားနေပါတယ်!\n...")
        return

    # --- Fetch Data from MongoDB ---
    total_sales = 0
    total_orders = 0
    total_topups = 0
    topup_count = 0

    if users_col is None:
        await update.message.reply_text("❌ Database connection မရှိပါ။ Report မထုတ်နိုင်ပါ။")
        return

    try:
         # Aggregate directly in MongoDB if possible (more efficient for large data)
         # Example: Get all users and process in Python (simpler for now)
         all_users = users_col.find({})
         for user_data in all_users:
             # Filter confirmed orders
             confirmed_orders = [o for o in user_data.get("orders", []) if o.get("status") == "confirmed"]
             filtered_orders = filter_by_date(confirmed_orders, "confirmed_at", start_date_str, end_date_str)
             for order in filtered_orders:
                 total_sales += order.get("price", 0)
                 total_orders += 1

             # Filter approved topups
             approved_topups = [t for t in user_data.get("topups", []) if t.get("status") == "approved"]
             filtered_topups = filter_by_date(approved_topups, "approved_at", start_date_str, end_date_str)
             for topup in filtered_topups:
                 total_topups += topup.get("amount", 0)
                 topup_count += 1
    except Exception as e:
         print(f"❌ Report data ရယူရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
         await update.message.reply_text("❌ Report data ရယူရာတွင် အမှားဖြစ်ပွားနေပါသည်။")
         return


    await update.message.reply_text(
        f"📊 ***ရောင်းရငွေ & ငွေဖြည့် မှတ်တမ်း***\n\n📅 ကာလ: {period_text}\n\n"
        f"🛒 ***Order Confirmed စုစုပေါင်း***:\n💰 ***ငွေ***: `{total_sales:,} MMK`\n📦 ***အရေအတွက်***: {total_orders}\n\n"
        f"💳 ***Topup Approved စုစုပေါင်း***:\n💰 ***ငွေ***: `{total_topups:,} MMK`\n📦 ***အရေအတွက်***: {topup_count}",
        parse_mode="Markdown"
    )

# --- Monthly and Yearly Reports ---
# monthly_report_command and yearly_report_command will need similar modifications
# to fetch data from MongoDB and filter by month/year string derived from timestamps.
# This requires careful string slicing and comparison or using MongoDB aggregation pipeline.
# For simplicity, you can adapt the daily_report logic with appropriate date string checks.

# Placeholder for Monthly Report - Adapt Daily Report Logic
async def monthly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     await update.message.reply_text("⏳ Monthly report function (MongoDB) is under construction.")

# Placeholder for Yearly Report - Adapt Daily Report Logic
async def yearly_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     await update.message.reply_text("⏳ Yearly report function (MongoDB) is under construction.")


# --- Other Admin/Payment Commands (Modified where necessary) ---

async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin မဟုတ်ပါ။")
    args = context.args
    if len(args) != 2: return await update.message.reply_text("❌ Format: /setprice <item> <price>")
    item = args[0]
    try: price = int(args[1])
    except ValueError: return await update.message.reply_text("❌ Price သည် ဂဏန်းဖြစ်ရမည်!")
    if price < 0: return await update.message.reply_text("❌ Price သည် 0 ထက် မငယ်ရ!")

    current_prices = load_prices()
    current_prices[item] = price
    if save_prices(current_prices):
        await update.message.reply_text(f"✅ ***ဈေးနှုန်း ပြောင်းလဲပါပြီ!***\n💎 Item: `{item}`\n💰 New Price: `{price:,} MMK`", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Database အမှားကြောင့် ဈေးနှုန်း မပြောင်းနိုင်ပါ။")

async def removeprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin မဟုတ်ပါ။")
    args = context.args
    if len(args) != 1: return await update.message.reply_text("❌ Format: /removeprice <item>")
    item = args[0]
    current_prices = load_prices()
    if item not in current_prices: return await update.message.reply_text(f"❌ `{item}` အတွက် custom price မရှိပါ။")

    del current_prices[item]
    if save_prices(current_prices):
        await update.message.reply_text(f"✅ ***Custom Price ဖျက်ပါပြီ!***\n💎 Item: `{item}`\n🔄 Default price ကို ပြန်သုံးပါမယ်။", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Database အမှားကြောင့် ဈေးနှုန်း မဖျက်နိုင်ပါ။")

# Payment info update commands (Save to global dict AND DB settings)
async def update_payment_info(key, value):
     """ Helper to update global payment_info and save to DB """
     payment_info[key] = value
     return save_settings_field("payment_info", payment_info) # Save the whole dict

async def setwavenum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin မဟုတ်ပါ။")
    args = context.args
    if len(args) != 1: return await update.message.reply_text("❌ Format: /setwavenum <number>")
    new_number = args[0]
    if await update_payment_info("wave_number", new_number):
         await update.message.reply_text(f"✅ Wave နံပါတ် ပြောင်းပြီးပါပြီ: `{new_number}`")
    else:
         await update.message.reply_text("❌ Database အမှားကြောင့် မပြောင်းနိုင်ပါ။")

async def setkpaynum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin မဟုတ်ပါ။")
    args = context.args
    if len(args) != 1: return await update.message.reply_text("❌ Format: /setkpaynum <number>")
    new_number = args[0]
    if await update_payment_info("kpay_number", new_number):
         await update.message.reply_text(f"✅ KPay နံပါတ် ပြောင်းပြီးပါပြီ: `{new_number}`")
    else:
         await update.message.reply_text("❌ Database အမှားကြောင့် မပြောင်းနိုင်ပါ။")

async def setwavename_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin မဟုတ်ပါ။")
    args = context.args
    if len(args) < 1: return await update.message.reply_text("❌ Format: /setwavename <name>")
    new_name = " ".join(args)
    if await update_payment_info("wave_name", new_name):
         await update.message.reply_text(f"✅ Wave နာမည် ပြောင်းပြီးပါပြီ: {new_name}")
    else:
         await update.message.reply_text("❌ Database အမှားကြောင့် မပြောင်းနိုင်ပါ။")

async def setkpayname_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)): return await update.message.reply_text("❌ Admin မဟုတ်ပါ။")
    args = context.args
    if len(args) < 1: return await update.message.reply_text("❌ Format: /setkpayname <name>")
    new_name = " ".join(args)
    if await update_payment_info("kpay_name", new_name):
         await update.message.reply_text(f"✅ KPay နာမည် ပြောင်းပြီးပါပြီ: {new_name}")
    else:
         await update.message.reply_text("❌ Database အမှားကြောင့် မပြောင်းနိုင်ပါ။")

async def setkpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(str(update.effective_user.id)): return await update.message.reply_text("❌ Owner သာ QR ထည့်နိုင်ပါသည်။")
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
         return await update.message.reply_text("❌ ပုံကို reply လုပ်ပြီး command သုံးပါ။")
    photo_id = update.message.reply_to_message.photo[-1].file_id
    if await update_payment_info("kpay_image", photo_id):
         await update.message.reply_text("✅ KPay QR Code ထည့်သွင်းပြီးပါပြီ!")
    else:
         await update.message.reply_text("❌ Database အမှားကြောင့် မသိမ်းနိုင်ပါ။")

async def removekpayqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(str(update.effective_user.id)): return await update.message.reply_text("❌ Owner သာ QR ဖျက်နိုင်ပါသည်။")
    if not payment_info.get("kpay_image"): return await update.message.reply_text("ℹ️ KPay QR code မရှိပါ။")
    if await update_payment_info("kpay_image", None):
         await update.message.reply_text("✅ KPay QR Code ဖျက်ပြီးပါပြီ!")
    else:
         await update.message.reply_text("❌ Database အမှားကြောင့် မဖျက်နိုင်ပါ။")

async def setwaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(str(update.effective_user.id)): return await update.message.reply_text("❌ Owner သာ QR ထည့်နိုင်ပါသည်။")
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
         return await update.message.reply_text("❌ ပုံကို reply လုပ်ပြီး command သုံးပါ။")
    photo_id = update.message.reply_to_message.photo[-1].file_id
    if await update_payment_info("wave_image", photo_id):
         await update.message.reply_text("✅ Wave QR Code ထည့်သွင်းပြီးပါပြီ!")
    else:
         await update.message.reply_text("❌ Database အမှားကြောင့် မသိမ်းနိုင်ပါ။")

async def removewaveqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(str(update.effective_user.id)): return await update.message.reply_text("❌ Owner သာ QR ဖျက်နိုင်ပါသည်။")
    if not payment_info.get("wave_image"): return await update.message.reply_text("ℹ️ Wave QR code မရှိပါ။")
    if await update_payment_info("wave_image", None):
         await update.message.reply_text("✅ Wave QR Code ဖျက်ပြီးပါပြီ!")
    else:
         await update.message.reply_text("❌ Database အမှားကြောင့် မဖျက်နိုင်ပါ။")


# --- Clone Bot Commands (Modified for DB) ---
async def addbot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     user_id = str(update.effective_user.id)
     if not is_admin(user_id):
          return await update.message.reply_text("❌ Admin များသာ bot ထည့်နိုင်ပါသည်။")

     args = context.args
     if len(args) != 1:
          # ... (invalid format message - unchanged) ...
          return await update.message.reply_text("❌ Format: /addbot <bot_token>\n...")

     bot_token = args[0]
     try:
          temp_bot = Bot(token=bot_token)
          bot_info = await temp_bot.get_me()
          bot_username = bot_info.username
          bot_id = str(bot_info.id)

          # Check DB if bot already exists
          existing_bots = load_clone_bots_db()
          if bot_id in existing_bots:
               return await update.message.reply_text(f"ℹ️ Bot (@{bot_username}) ထည့်ပြီးသားပါ။")

          bot_data = {
              # "_id": bot_id, # save_clone_bot_db will add this
              "token": bot_token,
              "username": bot_username,
              "owner_id": user_id,
              "balance": 0,
              "status": "active",
              "created_at": datetime.now().isoformat() # Use ISO format
          }
          if save_clone_bot_db(bot_id, bot_data):
               # Start clone bot instance (unchanged logic)
               asyncio.create_task(run_clone_bot(bot_token, bot_id, user_id))
               await update.message.reply_text(
                   f"✅ Bot (@{bot_username}) ထည့်သွင်းပြီး စတင် run နေပါပြီ!\n"
                   f"🆔 Bot ID: `{bot_id}`\n👤 Admin: `{user_id}`",
                   parse_mode="Markdown"
               )
          else:
               await update.message.reply_text("❌ Database အမှားကြောင့် Bot မသိမ်းနိုင်ပါ။")

     except Exception as e:
          await update.message.reply_text(f"❌ Bot token မှားနေပါသည် သို့မဟုတ် ချိတ်ဆက်မရပါ။\nError: {e}")

async def listbots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     user_id = str(update.effective_user.id)
     if not is_admin(user_id):
          return await update.message.reply_text("❌ Admin များသာ ကြည့်နိုင်ပါသည်။")

     clone_bots = load_clone_bots_db()
     if not clone_bots:
          return await update.message.reply_text("ℹ️ Clone bot များ မရှိသေးပါ။")

     msg = "🤖 ***Clone Bots List***\n\n"
     for bot_id, bot_data in clone_bots.items():
         status_icon = "🟢" if bot_data.get("status") == "active" else "🔴"
         created_str = bot_data.get('created_at', 'Unknown')
         # Try parsing ISO format for display
         try:
             created_dt = datetime.fromisoformat(created_str)
             created_display = created_dt.strftime('%Y-%m-%d')
         except:
             created_display = created_str # Show raw string if parsing fails
         msg += (
             f"{status_icon} @{bot_data.get('username', 'Unknown')}\n"
             f"├ ID: `{bot_id}`\n"
             f"├ Admin: `{bot_data.get('owner_id', 'Unknown')}`\n"
             f"├ Balance: {bot_data.get('balance', 0):,} MMK\n"
             f"└ Created: {created_display}\n\n"
         )
     msg += f"📊 စုစုပေါင်း: {len(clone_bots)} bots"
     await update.message.reply_text(msg, parse_mode="Markdown")

async def removebot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     user_id = str(update.effective_user.id)
     if not is_owner(user_id):
          return await update.message.reply_text("❌ Owner သာ bot ဖျက်နိုင်ပါသည်။")

     args = context.args
     if len(args) != 1:
          return await update.message.reply_text("❌ Format: /removebot <bot_id>")

     bot_id = args[0]
     if remove_clone_bot_db(bot_id):
          # Stop bot if running (unchanged logic)
          if bot_id in clone_bot_apps:
              try:
                  # Need to properly shutdown the application instance
                  app_instance = clone_bot_apps[bot_id]
                  if app_instance.updater and app_instance.updater.is_running:
                     await app_instance.updater.stop()
                  await app_instance.stop()
                  await app_instance.shutdown()
                  del clone_bot_apps[bot_id]
                  print(f"✅ Clone bot {bot_id} stopped.")
              except Exception as e:
                  print(f"⚠️ Error stopping clone bot {bot_id}: {e}")
                  # Remove from dict anyway if shutdown fails
                  if bot_id in clone_bot_apps: del clone_bot_apps[bot_id]

          await update.message.reply_text(f"✅ Bot (`{bot_id}`) ဖျက်ပြီးပါပြီ။")
     else:
          await update.message.reply_text(f"❌ Bot ID `{bot_id}` မတွေ့ပါ သို့မဟုတ် ဖျက်မရပါ။")

async def addfund_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     user_id = str(update.effective_user.id)
     if not is_owner(user_id):
          return await update.message.reply_text("❌ Owner သာ balance ဖြည့်နိုင်ပါသည်။")

     args = context.args
     if len(args) != 2: return await update.message.reply_text("❌ Format: /addfund <admin_id> <amount>")
     admin_id_str = args[0]
     try: amount = int(args[1])
     except ValueError: return await update.message.reply_text("❌ Amount သည် ဂဏန်းဖြစ်ရမည်!")
     if amount <= 0: return await update.message.reply_text("❌ Amount သည် 0 ထက်ကြီးရမည်!")

     # Find bot by admin ID
     bot_id_found, bot_found = get_clone_bot_by_admin(admin_id_str)
     if not bot_found:
          return await update.message.reply_text(f"❌ Admin ID `{admin_id_str}` နှင့် သက်ဆိုင်သော Bot မတွေ့ပါ။")

     # Update balance
     if update_clone_bot_balance(bot_id_found, amount):
          new_balance = bot_found.get("balance", 0) + amount # Calculate new balance
          # Notify admin (unchanged logic)
          try:
               await context.bot.send_message(
                   chat_id=admin_id_str,
                   text=(f"💰 Balance ဖြည့်သွင်းခြင်း\n\n✅ Main owner က သင့် bot ထံ balance ဖြည့်ပေးပါပြီ!\n\n"
                         f"📥 ဖြည့်သွင်းငွေ: `{amount:,} MMK`\n💳 လက်ကျန်ငွေ: `{new_balance:,} MMK`\n"
                         f"🤖 Bot: @{bot_found.get('username', '?')}"),
                   parse_mode="Markdown"
               )
          except Exception as e: print(f"Failed to notify clone admin {admin_id_str} about fund add: {e}")

          await update.message.reply_text(
              f"✅ Balance ဖြည့်ပြီးပါပြီ!\n\n👤 Admin: `{admin_id_str}`\n"
              f"🤖 Bot: @{bot_found.get('username', '?')}\n"
              f"💰 ဖြည့်သွင်းငွေ: `{amount:,} MMK`\n💳 လက်ကျန်ငွေ: `{new_balance:,} MMK`",
              parse_mode="Markdown"
          )
     else:
          await update.message.reply_text("❌ Database အမှားကြောင့် Balance မဖြည့်နိုင်ပါ။")

async def deductfund_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     user_id = str(update.effective_user.id)
     if not is_owner(user_id):
          return await update.message.reply_text("❌ Owner သာ balance နှုတ်နိုင်ပါသည်။")

     args = context.args
     if len(args) != 2: return await update.message.reply_text("❌ Format: /deductfund <admin_id> <amount>")
     admin_id_str = args[0]
     try: amount = int(args[1])
     except ValueError: return await update.message.reply_text("❌ Amount သည် ဂဏန်းဖြစ်ရမည်!")
     if amount <= 0: return await update.message.reply_text("❌ Amount သည် 0 ထက်ကြီးရမည်!")

     # Find bot by admin ID
     bot_id_found, bot_found = get_clone_bot_by_admin(admin_id_str)
     if not bot_found:
          return await update.message.reply_text(f"❌ Admin ID `{admin_id_str}` နှင့် သက်ဆိုင်သော Bot မတွေ့ပါ။")

     current_balance = bot_found.get("balance", 0)
     if current_balance < amount:
          return await update.message.reply_text(f"❌ Balance မလုံလောက်ပါ။ လက်ကျန်: {current_balance:,} MMK")

     # Update balance
     if update_clone_bot_balance(bot_id_found, -amount): # Use negative amount
          new_balance = current_balance - amount
          # Notify admin (unchanged logic)
          try:
               await context.bot.send_message(
                   chat_id=admin_id_str,
                   text=(f"💸 Balance နှုတ်ခြင်း\n\n⚠️ Main owner က သင့် bot ထံမှ balance နှုတ်လိုက်ပါပြီ!\n\n"
                         f"📤 နှုတ်သွားသော ငွေ: `{amount:,} MMK`\n💳 လက်ကျန်ငွေ: `{new_balance:,} MMK`\n"
                         f"🤖 Bot: @{bot_found.get('username', '?')}"),
                   parse_mode="Markdown"
               )
          except Exception as e: print(f"Failed to notify clone admin {admin_id_str} about fund deduct: {e}")

          await update.message.reply_text(
              f"✅ Balance နှုတ်ပြီးပါပြီ!\n\n👤 Admin: `{admin_id_str}`\n"
              f"🤖 Bot: @{bot_found.get('username', '?')}\n"
              f"💸 နှုတ်သွားသော ငွေ: `{amount:,} MMK`\n💳 လက်ကျန်ငွေ: `{new_balance:,} MMK`",
              parse_mode="Markdown"
          )
     else:
          await update.message.reply_text("❌ Database အမှားကြောင့် Balance မနှုတ်နိုင်ပါ။")


# --- Callback Query Handler (Modified for MongoDB) ---
# This needs significant changes to find and update orders/topups in DB

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Acknowledge callback quickly

    user_id = str(query.from_user.id)
    admin_name = query.from_user.first_name or "Admin"
    data = query.data

    # --- Payment Method Selection (unchanged logic, uses global pending_topups) ---
    if data.startswith("topup_pay_"):
        # ... (This part remains the same as it uses the temporary pending_topups dict) ...
        parts = data.split("_")
        payment_method = parts[2]
        amount = int(parts[3])
        if user_id in pending_topups:
             pending_topups[user_id]["payment_method"] = payment_method
        else: # Handle case where user clicks old button after restart
             return await query.message.reply_text("❌ လုပ်ငန်းစဉ် หมดอายุ ဖြစ်သွားပါပြီ။ /topup ကို ပြန်စပါ။")

        payment_name = "KBZ Pay" if payment_method == "kpay" else "Wave Money"
        payment_num = payment_info['kpay_number'] if payment_method == "kpay" else payment_info['wave_number']
        payment_acc_name = payment_info['kpay_name'] if payment_method == "kpay" else payment_info['wave_name']
        payment_qr = payment_info.get('kpay_image') if payment_method == "kpay" else payment_info.get('wave_image')

        # Send QR if available (unchanged)
        if payment_qr:
             try: await query.message.reply_photo(photo=payment_qr, caption=f"📱 **{payment_name} QR Code**\n\n📞 နံပါတ်: `{payment_num}`\n👤 နာမည်: {payment_acc_name}", parse_mode="Markdown")
             except Exception as e: print(f"Error sending QR photo: {e}")

        # Edit message to show details and ask for screenshot (unchanged)
        await query.edit_message_text(
             f"💳 ***ငွေဖြည့်လုပ်ငန်းစဉ်***\n\n✅ ***ပမာဏ:*** `{amount:,} MMK`\n✅ ***Payment:*** {payment_name}\n\n"
             f"***အဆင့် 2: ငွေလွှဲပြီး Screenshot တင်ပါ။***\n\n📱 {payment_name}\n📞 ***နံပါတ်:*** `{payment_num}`\n👤 ***အမည်:*** {payment_acc_name}\n\n"
             f"⚠️ ***အရေးကြီး: Note မှာ သင့် {payment_name} နာမည် ရေးပေးပါ။***\n\n💡 ***Screenshot ကို ဒီမှာ တင်ပေးပါ။***\nℹ️ ***ပယ်ဖျက်ရန် /cancel နှိပ်ပါ။***",
             parse_mode="Markdown"
        )
        return

    # --- Registration Request Button (unchanged logic) ---
    elif data == "request_register":
        # ... (unchanged - sends request to admin) ...
         user = query.from_user
         req_user_id = str(user.id)
         username = user.username or "-"
         name = f"{user.first_name} {user.last_name or ''}".strip()
         load_authorized_users()
         if is_user_authorized(req_user_id):
              return await query.answer("✅ သင်သည် အသုံးပြုခွင့် ရပြီးသား ဖြစ်ပါတယ်!", show_alert=True)

         keyboard = [[
             InlineKeyboardButton("✅ Approve", callback_data=f"register_approve_{req_user_id}"),
             InlineKeyboardButton("❌ Reject", callback_data=f"register_reject_{req_user_id}")
         ]]
         reply_markup = InlineKeyboardMarkup(keyboard)
         owner_msg = (f"📝 ***Registration Request***\n\n👤 User: [{name}](tg://user?id={req_user_id}) (`{req_user_id}`)\n"
                      f"📱 Username: @{username}\n⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n***အသုံးပြုခွင့် ပေးမလား?***")
         # Send to all admins
         admin_list = get_admins()
         for admin_id in admin_list:
              try:
                  # Send with profile photo if possible
                  user_photos = await context.bot.get_user_profile_photos(user_id=int(req_user_id), limit=1)
                  if user_photos.total_count > 0:
                       await context.bot.send_photo(chat_id=admin_id, photo=user_photos.photos[0][0].file_id, caption=owner_msg, parse_mode="Markdown", reply_markup=reply_markup)
                  else:
                       await context.bot.send_message(chat_id=admin_id, text=owner_msg, parse_mode="Markdown", reply_markup=reply_markup)
              except Exception as e:
                  print(f"Error sending registration request to admin {admin_id}: {e}")

         await query.answer("✅ Registration တောင်းဆိုမှု ပို့ပြီးပါပြီ!", show_alert=True)
         try: # Edit original message
              await query.edit_message_text("✅ ***Registration တောင်းဆိုမှု ပို့ပြီးပါပြီ!***\n\n⏳ ***Owner/Admin က approve လုပ်တဲ့အထိ စောင့်ပါ။***\n" f"🆔 ***သင့် User ID:*** `{req_user_id}`", parse_mode="Markdown")
         except: pass # Ignore if editing fails
         return

    # --- Registration Approve/Reject (Modified) ---
    elif data.startswith("register_approve_"):
        if not is_admin(user_id): # Check if the clicker is admin
            return await query.answer("❌ Admin များသာ approve လုပ်နိုင်ပါတယ်!", show_alert=True)

        target_user_id = data.replace("register_approve_", "")
        load_authorized_users() # Load fresh list

        if target_user_id in AUTHORIZED_USERS:
            await query.answer("ℹ️ User ကို approve လုပ်ပြီးသားပါ။", show_alert=True)
            # Still remove buttons from this message instance
            try: await query.edit_message_reply_markup(reply_markup=None)
            except: pass
            return

        # Add to set and save to DB
        AUTHORIZED_USERS.add(target_user_id)
        if save_authorized_users():
            # Clear restrictions
            if target_user_id in user_states: del user_states[target_user_id]

            # Update message, notify user, notify group (unchanged logic)
            try: await query.edit_message_text(text=query.message.text + f"\n\n✅ Approved by {admin_name}", parse_mode="Markdown", reply_markup=None)
            except: pass
            try: await context.bot.send_message(chat_id=int(target_user_id), text="🎉 Registration Approved!\n\n✅ Admin က သင့် registration ကို လက်ခံပါပြီ။\n\n🚀 /start နှိပ်ပြီး bot ကို အသုံးပြုနိုင်ပါပြီ!")
            except Exception as e: print(f"Failed to notify approved user {target_user_id}: {e}")
            # Notify group (unchanged logic)
            if ADMIN_GROUP_ID:
                 try:
                     # Get user name if possible
                     user_data = get_user_data(target_user_id)
                     user_name = user_data.get("name", target_user_id) if user_data else target_user_id
                     if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                          group_msg = (f"✅ ***Registration လက်ခံပြီး!***\n\n👤 User: [{user_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\n"
                                       f"👤 လက်ခံသူ: {admin_name}\n📊 Status: ✅ လက်ခံပြီး\n\n#RegistrationApproved")
                          await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=group_msg, parse_mode="Markdown")
                 except Exception as e: print(f"Failed to notify group about registration approval: {e}")

            await query.answer("✅ User approved!", show_alert=True)
        else:
            # Revert if save failed
            AUTHORIZED_USERS.discard(target_user_id)
            await query.answer("❌ Database အမှားကြောင့် Approve မလုပ်နိုင်ပါ။", show_alert=True)
        return

    elif data.startswith("register_reject_"):
        if not is_admin(user_id): # Check if the clicker is admin
            return await query.answer("❌ Admin များသာ reject လုပ်နိုင်ပါတယ်!", show_alert=True)

        target_user_id = data.replace("register_reject_", "")
        # No DB change needed, just update message and notify user/group

        # Update message, notify user, notify group (unchanged logic)
        try: await query.edit_message_text(text=query.message.text + f"\n\n❌ Rejected by {admin_name}", parse_mode="Markdown", reply_markup=None)
        except: pass
        try: await context.bot.send_message(chat_id=int(target_user_id), text="❌ Registration Rejected\n\nAdmin က သင့် registration ကို ငြင်းပယ်လိုက်ပါပြီ。\n\n📞 Admin ကို ဆက်သွယ်ပါ။")
        except Exception as e: print(f"Failed to notify rejected user {target_user_id}: {e}")
        # Notify group (unchanged logic)
        if ADMIN_GROUP_ID:
             try:
                 user_data = get_user_data(target_user_id)
                 user_name = user_data.get("name", target_user_id) if user_data else target_user_id
                 if await is_bot_admin_in_group(context.bot, ADMIN_GROUP_ID):
                      group_msg = (f"❌ ***Registration ငြင်းပယ်ပြီး!***\n\n👤 User: [{user_name}](tg://user?id={target_user_id}) (`{target_user_id}`)\n"
                                   f"👤 ငြင်းပယ်သူ: {admin_name}\n📊 Status: ❌ ငြင်းပယ်ပြီး\n\n#RegistrationRejected")
                      await context.bot.send_message(chat_id=ADMIN_GROUP_ID, text=group_msg, parse_mode="Markdown")
             except Exception as e: print(f"Failed to notify group about registration rejection: {e}")


        await query.answer("❌ User rejected!", show_alert=True)
        return

    # --- Topup Cancel (unchanged, uses global pending_topups) ---
    elif data == "topup_cancel":
        if user_id in pending_topups: del pending_topups[user_id]
        await query.edit_message_text("✅ ***ငွေဖြည့်ခြင်း ပယ်ဖျက်ပါပြီ!***\n\n💡 ***ပြန်ဖြည့်ချင်ရင်*** /topup ***နှိပ်ပါ။***", parse_mode="Markdown")
        return

    # --- Topup Approve/Reject Buttons (Modified) ---
    elif data.startswith("topup_approve_") or data.startswith("topup_reject_"):
        if not is_admin(user_id):
            return await query.answer("❌ Admin မဟုတ်ပါ။")

        is_approve = data.startswith("topup_approve_")
        topup_id = data.replace("topup_approve_", "").replace("topup_reject_", "")

        # --- Find and Update Topup in DB ---
        update_doc = {
            "topups.$.status": "approved" if is_approve else "rejected",
            f"topups.$.{'approved_by' if is_approve else 'rejected_by'}": admin_name,
            f"topups.$.{'approved_at' if is_approve else 'rejected_at'}": datetime.now().isoformat()
        }

        # Use find_one_and_update to get the document *before* update if needed,
        # or use update_one and then find the user. Let's use update_one first.

        # Construct the query to find the specific topup within the user's array
        target_user_id, topup_amount, status_before = find_and_update_topup_mongo(topup_id, update_doc)

        if target_user_id is None:
             # Check if status_before is available and matches target status (already processed)
             if status_before is not None and status_before == ("approved" if is_approve else "rejected"):
                  await query.answer("⚠️ Topup ကို လုပ်ဆောင်ပြီးသား ဖြစ်ပါသည်။")
                  try: await query.edit_message_reply_markup(reply_markup=None) # Remove buttons anyway
                  except: pass
             else:
                  await query.answer("❌ Topup မတွေ့ပါ သို့မဟုတ် Update မလုပ်နိုင်ပါ။")
             return

        # --- Actions After Successful DB Update ---

        # 1. Update Balance if Approving
        balance_updated = True
        if is_approve:
            balance_updated = increment_user_balance(target_user_id, topup_amount)
            if not balance_updated:
                print(f"⚠️ Topup {topup_id} status updated to approved, but failed to add balance for user {target_user_id}!")
                # CRITICAL: Need manual intervention or revert logic here!
                await query.answer("❌ Balance update မအောင်မြင်ပါ။ Admin ကို ဆက်သွယ်ပါ။", show_alert=True)
                # Maybe try reverting status?
                revert_update = {"topups.$.status": "pending"} # Simplified revert
                find_and_update_topup_mongo(topup_id, revert_update)
                return

        # 2. Clear User Restriction
        if target_user_id in user_states:
            del user_states[target_user_id]

        # 3. Update Admin's Message (Remove buttons, change status text)
        try:
            original_caption = query.message.caption or ""
            new_status_text = "✅ Approved" if is_approve else "❌ Rejected"
            # Replace the status line carefully
            lines = original_caption.split('\n')
            for i, line in enumerate(lines):
                 if "Status:" in line:
                      lines[i] = f"📊 Status: {new_status_text} by {admin_name}"
                      break
            else: # If status line not found, append it
                 lines.append(f"📊 Status: {new_status_text} by {admin_name}")
            new_caption = "\n".join(lines)

            await query.edit_message_caption(caption=new_caption, parse_mode="Markdown", reply_markup=None)
        except Exception as e:
            print(f"Error editing admin message caption for topup {topup_id}: {e}")
            try: # Fallback: just remove buttons
                 await query.edit_message_reply_markup(reply_markup=None)
            except: pass


        # 4. Notify User
        target_user_data = get_user_data(target_user_id) # Fetch fresh data for balance
        new_balance = target_user_data.get("balance", "Error") if target_user_data else "Error"
        try:
            if is_approve:
                keyboard = [[InlineKeyboardButton("💎 Order တင်မယ်", url=f"https://t.me/{context.bot.username}?start=order")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                user_msg = (f"✅ ***ငွေဖြည့်မှု အတည်ပြုပါပြီ!*** 🎉\n\n💰 ***ပမာဏ:*** `{topup_amount:,} MMK`\n"
                            f"💳 ***လက်ကျန်ငွေ:*** `{new_balance:,} MMK`\n👤 ***Approved by:*** {admin_name}\n"
                            f"⏰ ***အချိန်:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            f"🎉 ***ယခုအခါ diamonds များ ဝယ်ယူနိုင်ပါပြီ!***\n🔓 ***Bot ပြန်သုံးနိုင်ပါပြီ!***\n\n"
                            f"💎 ***Order တင်ရန်:*** `/mmb gameid serverid amount`")
                await context.bot.send_message(chat_id=int(target_user_id), text=user_msg, parse_mode="Markdown", reply_markup=reply_markup)
            else: # Rejected
                user_msg = (f"❌ ***ငွေဖြည့်မှု ငြင်းပယ်ခံရပါပြီ!***\n\n💰 ***ပမာဏ:*** `{topup_amount:,} MMK`\n"
                            f"👤 ***Rejected by:*** {admin_name}\n⏰ ***အချိန်:*** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            f"📞 ***Admin ကို ဆက်သွယ်ပါ။***\n🔓 ***Bot ပြန်သုံးနိုင်ပါပြီ!***")
                await context.bot.send_message(chat_id=int(target_user_id), text=user_msg, parse_mode="Markdown")
        except Exception as e:
            print(f"Failed to notify user {target_user_id} about topup {topup_id} status: {e}")

        # 5. Notify Other Admins & Group (unchanged concept, reuse logic from /approve or button callback)
        # ... Add notification logic ...

        await query.answer(f"✅ Topup {topup_id} { 'approved' if is_approve else 'rejected'}!", show_alert=is_approve) # Show alert only on approve?
        return

    # --- Order Confirm/Cancel Buttons (Modified) ---
    elif data.startswith("order_confirm_") or data.startswith("order_cancel_"):
         # Check if clicker is admin
         if not is_admin(user_id):
              return await query.answer("❌ Admin မဟုတ်ပါ။")

         is_confirm = data.startswith("order_confirm_")
         order_id = data.replace("order_confirm_", "").replace("order_cancel_", "")

         # --- Find and Update Order in DB ---
         update_fields = {
             "orders.$.status": "confirmed" if is_confirm else "cancelled",
             f"orders.$.{'confirmed_by' if is_confirm else 'cancelled_by'}": admin_name,
             f"orders.$.{'confirmed_at' if is_confirm else 'cancelled_at'}": datetime.now().isoformat()
         }

         # Find the user and order, then update
         target_user_id, updated_order = find_and_update_order_mongo(order_id, update_fields)

         if target_user_id is None:
              # Check if it was already processed by querying status
              existing_user = users_col.find_one({"orders.order_id": order_id})
              if existing_user:
                   for o in existing_user.get("orders", []):
                        if o.get("order_id") == order_id and o.get("status") != "pending":
                             await query.answer("⚠️ Order ကို လုပ်ဆောင်ပြီးသား ဖြစ်ပါသည်။")
                             try: await query.edit_message_reply_markup(reply_markup=None) # Remove buttons
                             except: pass
                             return
              await query.answer("❌ Order မတွေ့ပါ သို့မဟုတ် Update မလုပ်နိုင်ပါ။")
              return

         refund_amount = updated_order.get("price", 0) if not is_confirm else 0

         # --- Actions After Successful DB Update ---

         # 1. Refund Balance if Cancelling
         balance_refunded = True
         if not is_confirm and refund_amount > 0:
              balance_refunded = increment_user_balance(target_user_id, refund_amount)
              if not balance_refunded:
                   print(f"⚠️ Order {order_id} cancelled, but failed to refund balance for user {target_user_id}!")
                   # CRITICAL! Manual intervention needed.
                   await query.answer("❌ Refund update မအောင်မြင်ပါ။ Admin ကို ဆက်သွယ်ပါ။", show_alert=True)
                   # Maybe revert status?
                   revert_update = {"orders.$.status": "pending"}
                   find_and_update_order_mongo(order_id, revert_update)
                   return

         # 2. Update Admin's Message
         try:
              original_text = query.message.text or ""
              new_status_text = "✅ လက်ခံပြီး" if is_confirm else "❌ ငြင်းပယ်ပြီး"
              # Replace status line
              lines = original_text.split('\n')
              for i, line in enumerate(lines):
                  if "Status:" in line:
                      lines[i] = f"📊 Status: {new_status_text} by {admin_name}"
                      break
              else: lines.append(f"📊 Status: {new_status_text} by {admin_name}")
              new_text = "\n".join(lines)
              await query.edit_message_text(text=new_text, parse_mode="Markdown", reply_markup=None)
         except Exception as e:
              print(f"Error editing admin message text for order {order_id}: {e}")
              try: await query.edit_message_reply_markup(reply_markup=None) # Fallback
              except: pass

         # 3. Notify User & Other Admins/Group
         target_user_data = get_user_data(target_user_id)
         user_name = target_user_data.get("name", "User") if target_user_data else "User"
         new_balance = target_user_data.get("balance", "Error") if target_user_data else "Error"
         chat_id_to_notify = updated_order.get("chat_id", int(target_user_id)) # Notify in original chat

         try:
              if is_confirm:
                   user_msg = (f"✅ ***Order ({order_id}) လက်ခံပြီးပါပြီ!***\n\n"
                               f"👤 ***User:*** {user_name}\n"
                               f"🎮 ***Game ID:*** `{updated_order.get('game_id')}`\n"
                               f"🌐 ***Server ID:*** `{updated_order.get('server_id')}`\n"
                               f"💎 ***Amount:*** {updated_order.get('amount')}\n"
                               f"📊 Status: ✅ ***လက်ခံပြီး***\n\n"
                               "💎 ***Diamonds များ ပို့ဆောင်ပြီးပါပြီ။***")
              else: # Cancelled
                   user_msg = (f"❌ ***Order ({order_id}) ငြင်းပယ်ခံရပါပြီ!***\n\n"
                               f"👤 ***User:*** {user_name}\n"
                               f"🎮 ***Game ID:*** `{updated_order.get('game_id')}`\n"
                               f"💎 ***Amount:*** {updated_order.get('amount')}\n"
                               f"💰 ***ငွေပြန်အမ်း:*** {refund_amount:,} MMK\n"
                               f"💳 ***လက်ကျန်ငွေ:*** `{new_balance:,} MMK`\n"
                               f"📊 Status: ❌ ***ငြင်းပယ်ပြီး***\n\n"
                               "📞 ***Admin ကို ဆက်သွယ်ပါ။***")
              await context.bot.send_message(chat_id=chat_id_to_notify, text=user_msg, parse_mode="Markdown")
         except Exception as e:
              print(f"Failed to notify user/chat {chat_id_to_notify} about order {order_id} status: {e}")

         # Notify other admins & group (similar logic as before)
         # ... Add notification logic ...

         await query.answer(f"✅ Order {order_id} { 'confirmed' if is_confirm else 'cancelled'}!", show_alert=True)
         return

    # --- Report Filter Callbacks (Modified) ---
    elif data.startswith("report_day_") or data.startswith("report_month_") or data.startswith("report_year_"):
         if not is_owner(user_id):
              return await query.answer("❌ Owner သာ ကြည့်နိုင်ပါသည်။", show_alert=True)

         # Extract dates/period based on callback data (logic unchanged)
         period_type = "day" if "day" in data else ("month" if "month" in data else "year")
         parts = data.replace(f"report_{period_type}_", "").split("_")
         is_range = "range" in parts
         if is_range:
              start_str = parts[1]
              end_str = parts[2]
         else:
              start_str = end_str = parts[0]

         if period_type == "day": period_text = f"ရက် ({start_str}{f' မှ {end_str}' if is_range else ''})"
         elif period_type == "month": period_text = f"လ ({start_str}{f' မှ {end_str}' if is_range else ''})"
         else: period_text = f"နှစ် ({start_str}{f' မှ {end_str}' if is_range else ''})"

         # --- Fetch and Aggregate Data from DB ---
         total_sales = 0
         total_orders = 0
         total_topups = 0
         topup_count = 0

         if users_col is None:
              return await query.edit_message_text("❌ Database connection မရှိပါ။")

         try:
              all_users = users_col.find({})
              for user_data in all_users:
                   # Filter confirmed orders by date/month/year string
                   confirmed_orders = [o for o in user_data.get("orders", []) if o.get("status") == "confirmed"]
                   for order in confirmed_orders:
                        ts_str = order.get("confirmed_at", order.get("timestamp"))
                        if ts_str:
                             date_part = ts_str[:len(start_str)] # Extract YYYY or YYYY-MM or YYYY-MM-DD
                             if start_str <= date_part <= end_str:
                                  total_sales += order.get("price", 0)
                                  total_orders += 1

                   # Filter approved topups by date/month/year string
                   approved_topups = [t for t in user_data.get("topups", []) if t.get("status") == "approved"]
                   for topup in approved_topups:
                        ts_str = topup.get("approved_at", topup.get("timestamp"))
                        if ts_str:
                             date_part = ts_str[:len(start_str)]
                             if start_str <= date_part <= end_str:
                                  total_topups += topup.get("amount", 0)
                                  topup_count += 1
         except Exception as e:
              print(f"❌ Report data ({period_text}) ရယူရာတွင် အမှား: {e}")
              return await query.edit_message_text("❌ Report data ရယူရာတွင် အမှားဖြစ်ပွားနေပါသည်။")

         # Edit message with results (unchanged format)
         await query.edit_message_text(
             f"📊 ***ရောင်းရငွေ & ငွေဖြည့် မှတ်တမ်း***\n\n📅 ကာလ: {period_text}\n\n"
             f"🛒 ***Order Confirmed စုစုပေါင်း***:\n💰 ***ငွေ***: `{total_sales:,} MMK`\n📦 ***အရေအတွက်***: {total_orders}\n\n"
             f"💳 ***Topup Approved စုစုပေါင်း***:\n💰 ***ငွေ***: `{total_topups:,} MMK`\n📦 ***အရေအတွက်***: {topup_count}",
             parse_mode="Markdown"
         )
         return

    # --- Copy Number Buttons (unchanged) ---
    elif data == "copy_kpay":
        await query.answer(f"📱 KPay Number copied! {payment_info['kpay_number']}", show_alert=True)
        await query.message.reply_text(f"📱 ***KBZ Pay Number***\n\n`{payment_info['kpay_number']}`\n\n👤 Name: ***{payment_info['kpay_name']}***", parse_mode="Markdown")
        return
    elif data == "copy_wave":
        await query.answer(f"📱 Wave Number copied! {payment_info['wave_number']}", show_alert=True)
        await query.message.reply_text(f"📱 ***Wave Money Number***\n\n`{payment_info['wave_number']}`\n\n👤 Name: ***{payment_info['wave_name']}***", parse_mode="Markdown")
        return

    # --- Topup Button from Balance (unchanged logic) ---
    elif data == "topup_button":
         # ... (unchanged - shows payment numbers and asks for /topup command) ...
         keyboard = [
             [InlineKeyboardButton("📱 Copy KPay Number", callback_data="copy_kpay")],
             [InlineKeyboardButton("📱 Copy Wave Number", callback_data="copy_wave")]
         ]
         reply_markup = InlineKeyboardMarkup(keyboard)
         try:
              await query.edit_message_text(
                  text="💳 ***ငွေဖြည့်လုပ်ငန်းစဉ်***\n\n"
                       "***အဆင့် 1:*** `/topup <amount>` (ဥပမာ: `/topup 50000`)\n"
                       "***အဆင့် 2:*** ငွေလွှဲပါ:\n"
                       f"📱 KBZ Pay: `{payment_info['kpay_number']}` ({payment_info['kpay_name']})\n"
                       f"📱 Wave Money: `{payment_info['wave_number']}` ({payment_info['wave_name']})\n"
                       "***အဆင့် 3:*** Screenshot ပို့ပါ။\n\n"
                       "⏰ ***Admin မှ စစ်ဆေးအတည်ပြုပေးပါမည်။***",
                  parse_mode="Markdown",
                  reply_markup=reply_markup
              )
         except: # Handle message not modified error or other edit errors
             await query.message.reply_text( # Send as new message if edit fails
                 text="💳 ***ငွေဖြည့်လုပ်ငန်းစဉ်***\n\n"
                      "***အဆင့် 1:*** `/topup <amount>` (ဥပမာ: `/topup 50000`)\n"
                      "***အဆင့် 2:*** ငွေလွှဲပါ:\n"
                      f"📱 KBZ Pay: `{payment_info['kpay_number']}` ({payment_info['kpay_name']})\n"
                      f"📱 Wave Money: `{payment_info['wave_number']}` ({payment_info['wave_name']})\n"
                      "***အဆင့် 3:*** Screenshot ပို့ပါ။\n\n"
                      "⏰ ***Admin မှ စစ်ဆေးအတည်ပြုပေးပါမည်။***",
                 parse_mode="Markdown",
                 reply_markup=reply_markup
             )
         return

    # --- Clone Bot Related Callbacks (main bot owner actions) ---
    # These remain conceptually similar but need checking if clone bot info is needed from DB
    elif data.startswith("main_approve_") or data.startswith("main_reject_"):
         if not is_owner(user_id): return await query.answer("❌ Owner သာ လုပ်ဆောင်နိုင်ပါသည်။", show_alert=True)

         is_approve = data.startswith("main_approve_")
         parts = data.split("_")
         try:
              # Data format: main_approve_<clone_admin_id>_<game_id>_<server_id>_<diamonds>
              # Data format: main_reject_<clone_admin_id>
              clone_admin_id = parts[2]
              game_id = parts[3] if is_approve else None
              server_id = parts[4] if is_approve else None
              diamonds = parts[5] if is_approve else None
              price = get_price(diamonds) if is_approve else 0
         except IndexError:
              print(f"Error parsing main approve/reject callback data: {data}")
              return await query.answer("❌ Callback data အမှား။")

         # Update message (unchanged)
         try:
              status_text = "✅ Approved by Main Owner" if is_approve else "❌ Rejected by Main Owner"
              await query.edit_message_text(f"{query.message.text}\n\n***{status_text}***", parse_mode="Markdown", reply_markup=None)
         except: pass

         # Notify clone bot admin (unchanged)
         try:
              if is_approve:
                   notify_msg = (f"✅ Order Approved by Main Owner!\n\n🎮 Game ID: `{game_id}`\n🌐 Server ID: `{server_id}`\n"
                                 f"💎 Diamonds: {diamonds}\n💰 Price: {price:,} MMK\n\n💎 Diamonds ပို့ပေးပါ။")
              else:
                   notify_msg = "❌ Order Rejected by Main Owner!"
              await context.bot.send_message(chat_id=clone_admin_id, text=notify_msg, parse_mode="Markdown")
         except Exception as e: print(f"Failed to notify clone admin {clone_admin_id}: {e}")

         await query.answer(f"✅ Order { 'approved' if is_approve else 'rejected'}!", show_alert=True)
         return

    # Default fallback for unhandled callbacks
    else:
        await query.answer("ℹ️ မသိသော Button သို့မဟုတ် လုပ်ဆောင်ချက် ပြီးဆုံးသွားပါပြီ။")


# --- Helper to find and update topup using positional operator ---
def find_and_update_topup_mongo(topup_id, update_fields):
    if users_col is None: return None, None, None
    try:
        # Find the document containing the topup first to get details
        user_doc = users_col.find_one({"topups.topup_id": topup_id})
        if not user_doc:
            return None, None, None # Topup not found

        target_user_id = user_doc["_id"]
        original_topup = None
        topup_index = -1
        for i, t in enumerate(user_doc.get("topups", [])):
            if t.get("topup_id") == topup_id:
                original_topup = t
                topup_index = i
                break

        if not original_topup or topup_index == -1:
            return None, None, None # Should not happen

        status_before = original_topup.get("status")
        topup_amount = original_topup.get("amount", 0)

        # Construct update document using index
        update_query = {}
        for key, value in update_fields.items():
             # Replace the '$' with the actual index found
             update_query[key.replace("$.", f".{topup_index}.")] = value

        # Perform the update using the specific index
        result = users_col.update_one(
            {"_id": target_user_id, "topups.topup_id": topup_id}, # Ensure we target the correct user and topup still exists
            {"$set": update_query}
        )

        if result.modified_count > 0:
            return target_user_id, topup_amount, status_before
        elif result.matched_count > 0:
             # Matched but not modified, likely status was already set
             print(f"ℹ️ Topup ({topup_id}) matched but not modified. Current status might be the target status.")
             return target_user_id, topup_amount, status_before # Return info, but indicate no change needed
        else:
            print(f"❌ Topup ({topup_id}) ကို update မလုပ်နိုင်ပါ (match မတွေ့).")
            return None, None, None
    except Exception as e:
        print(f"❌ Topup ({topup_id}) update (mongo) လုပ်ရာတွင် အမှား: {e}")
        return None, None, None

# --- Helper to find and update order using positional operator ---
def find_and_update_order_mongo(order_id, update_fields):
    if users_col is None: return None, None
    try:
        # Find the document containing the order first
        user_doc = users_col.find_one({"orders.order_id": order_id})
        if not user_doc:
            return None, None # Order not found

        target_user_id = user_doc["_id"]
        original_order = None
        order_index = -1
        for i, o in enumerate(user_doc.get("orders", [])):
            if o.get("order_id") == order_id:
                original_order = o
                order_index = i
                break

        if not original_order or order_index == -1:
            return None, None # Should not happen

        # Construct update document using index
        update_query = {}
        for key, value in update_fields.items():
            update_query[key.replace("$.", f".{order_index}.")] = value

        # Perform the update
        result = users_col.update_one(
            {"_id": target_user_id, "orders.order_id": order_id},
            {"$set": update_query}
        )

        if result.modified_count > 0:
            # Re-fetch the updated order data to return (or construct from original + updates)
            updated_order_data = {**original_order, **{k.split('.')[-1]: v for k, v in update_query.items()}}
            return target_user_id, updated_order_data
        elif result.matched_count > 0:
             print(f"ℹ️ Order ({order_id}) matched but not modified.")
             return target_user_id, original_order # Return original if matched but not modified
        else:
            print(f"❌ Order ({order_id}) ကို update မလုပ်နိုင်ပါ (match မတွေ့).")
            return None, None
    except Exception as e:
        print(f"❌ Order ({order_id}) update (mongo) လုပ်ရာတွင် အမှား: {e}")
        return None, None


# --- Other Handlers (Mostly Unchanged) ---
# handle_restricted_content, cancel_command, c_command, etc.
# These generally don't interact directly with persistent data saving/loading
# so they should work as before, provided they check authorization status correctly.

async def handle_restricted_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ Handle non-command messages, applying restrictions if needed """
    user_id = str(update.effective_user.id)

    # Allow processing photo for restricted users (handled by handle_photo)
    if update.message and update.message.photo and user_id in user_states and user_states[user_id] == "waiting_approval":
         # Let handle_photo manage this
         return

    # Apply restrictions
    if user_id in user_states and user_states[user_id] == "waiting_approval":
        await update.message.reply_text(
            "❌ ***အသုံးပြုမှု ကန့်သတ်ထားပါ!***\n\n"
            "🔒 ***Admin approve လုပ်သည်အထိ စာပို့ခြင်း၊ command သုံးခြင်း မပြုနိုင်ပါ။***\n\n"
            "⏰ ***ခဏစောင့်ဆိုင်းပေးပါ။***",
            parse_mode="Markdown"
        )
        return # Block message

    # Handle unauthorized users
    if not is_user_authorized(user_id):
        if update.message and update.message.text:
            reply = simple_reply(update.message.text)
            await update.message.reply_text(reply, parse_mode="Markdown")
        # Ignore other content from unauthorized users silently or with a generic message
        return

    # Handle authorized, non-restricted users' non-command, non-photo messages
    if update.message and update.message.text:
         reply = simple_reply(update.message.text)
         await update.message.reply_text(reply, parse_mode="Markdown")
    else:
         # Generic reply for stickers, voice, etc. from authorized users
         await update.message.reply_text(
             "📱 ***MLBB Diamond Top-up Bot***\n\n"
             "💎 /mmb <gameid> <serverid> <amount>\n"
             "💰 /price\n🆘 /start",
             parse_mode="Markdown"
         )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     user_id = str(update.effective_user.id)
     if not is_user_authorized(user_id): return

     if user_id in pending_topups:
          del pending_topups[user_id]
          await update.message.reply_text(
              "✅ ***ငွေဖြည့်ခြင်း ပယ်ဖျက်ပါပြီ!***\n\n💡 ***ပြန်ဖြည့်ချင်ရင်*** /topup ***နှိပ်ပါ။***",
              parse_mode="Markdown"
          )
     else:
          await update.message.reply_text(
              "***ℹ️ လက်ရှိ ငွေဖြည့်မှု လုပ်ငန်းစဉ် မရှိပါ။***\n\n💡 ***ငွေဖြည့်ရန် /topup ***နှိပ်ပါ။***",
              parse_mode="Markdown"
          )

async def c_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
     # ... (calculator logic remains the same) ...
     # Check restriction state
     user_id = str(update.effective_user.id)
     if user_id in user_states and user_states[user_id] == "waiting_approval":
          return await update.message.reply_text("❌ ***Admin approve စောင့်နေစဉ် Calculator သုံးမရပါ။***", parse_mode="Markdown")

     args = context.args
     if not args:
          # ... (show calculator help) ...
          return await update.message.reply_text("🧮 /c <expression> (e.g. /c 2*5+1)", parse_mode="Markdown")

     expression = ''.join(args).replace(' ', '')
     pattern = r'^[0-9+\-*/().]+$'
     if not re.match(pattern, expression) or not any(op in expression for op in ['+', '-', '*', '/']):
          return await update.message.reply_text("❌ Invalid expression!", parse_mode="Markdown")

     try:
          result = eval(expression) # Be careful with eval
          # ... (display result - unchanged) ...
          await update.message.reply_text(f"🧮 `{expression}` = ***{result:,}***", parse_mode="Markdown")
     except ZeroDivisionError:
          await update.message.reply_text("❌ Zero ဖြင့် စား၍မရပါ။")
     except Exception as e:
          await update.message.reply_text(f"❌ Error: {e}")

# --- Bot Startup ---

async def post_init(application: Application):
    """ Load initial data after bot starts """
    print("Bot started. Loading initial data from MongoDB...")
    load_settings() # Load settings including payment info
    load_authorized_users() # Load authorized users into global set
    # Start clone bots (uses load_clone_bots_db)
    clone_bots = load_clone_bots_db()
    print(f"Found {len(clone_bots)} clone bots in DB.")
    for bot_id, bot_data in clone_bots.items():
         bot_token = bot_data.get("token")
         admin_id = bot_data.get("owner_id")
         if bot_token and admin_id:
             print(f"🔄 Starting clone bot {bot_id} (@{bot_data.get('username')})...")
             # Use create_task for concurrency
             asyncio.create_task(run_clone_bot(bot_token, bot_id, admin_id))
         else:
             print(f"⚠️ Skipping clone bot {bot_id} due to missing token or owner_id.")

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN environment variable မရှိပါ!")
        return
    if not MONGO_URI:
         print("❌ MONGO_URI environment variable မရှိပါ။ Database ကို ချိတ်ဆက်၍မရပါ။")
         # Decide if you want the bot to run without DB or exit
         return # Exit if DB is essential

    # Check DB connection early
    if settings_col is None or users_col is None or clone_bots_col is None:
         print("❌ Database collections များ သတ်မှတ်မရပါ။ Bot ကို ရပ်တန့်ပါမည်။")
         return


    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # --- Register Handlers ---
    # User commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mmb", mmb_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("topup", topup_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("c", c_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("register", register_command)) # User command to request registration

    # Admin commands
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("deduct", deduct_command))
    application.add_handler(CommandHandler("done", done_command)) # Simple notification
    application.add_handler(CommandHandler("reply", reply_command)) # Simple notification
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("addadm", addadm_command)) # Owner only
    application.add_handler(CommandHandler("unadm", unadm_command)) # Owner only
    application.add_handler(CommandHandler("sendgroup", send_to_group_command))
    application.add_handler(CommandHandler("maintenance", maintenance_command)) # Uses global dict
    application.add_handler(CommandHandler("testgroup", testgroup_command))
    application.add_handler(CommandHandler("setprice", setprice_command))
    application.add_handler(CommandHandler("removeprice", removeprice_command))
    application.add_handler(CommandHandler("setwavenum", setwavenum_command))
    application.add_handler(CommandHandler("setkpaynum", setkpaynum_command))
    application.add_handler(CommandHandler("setwavename", setwavename_command))
    application.add_handler(CommandHandler("setkpayname", setkpayname_command))
    application.add_handler(CommandHandler("setkpayqr", setkpayqr_command)) # Owner only
    application.add_handler(CommandHandler("removekpayqr", removekpayqr_command)) # Owner only
    application.add_handler(CommandHandler("setwaveqr", setwaveqr_command)) # Owner only
    application.add_handler(CommandHandler("removewaveqr", removewaveqr_command)) # Owner only
    application.add_handler(CommandHandler("adminhelp", adminhelp_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command)) # Owner only

    # Report commands (Owner only)
    application.add_handler(CommandHandler("d", daily_report_command))
    application.add_handler(CommandHandler("m", monthly_report_command))
    application.add_handler(CommandHandler("y", yearly_report_command))

    # Clone Bot Management commands
    application.add_handler(CommandHandler("addbot", addbot_command)) # Admin
    application.add_handler(CommandHandler("listbots", listbots_command)) # Admin
    application.add_handler(CommandHandler("removebot", removebot_command)) # Owner only
    application.add_handler(CommandHandler("addfund", addfund_command)) # Owner only
    application.add_handler(CommandHandler("deductfund", deductfund_command)) # Owner only

    # Callback query handler (Handles ALL inline button presses)
    application.add_handler(CallbackQueryHandler(button_callback))

    # Photo handler (for payment screenshots) - MUST be before the general message handler
    application.add_handler(MessageHandler(filters.PHOTO & (~filters.COMMAND), handle_photo))

    # General message handler (Handles text, stickers, etc. NOT commands or photos)
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.VOICE | filters.Sticker.ALL | filters.VIDEO |
         filters.ANIMATION | filters.AUDIO | filters.Document.ALL |
         filters.FORWARDED | filters.Entity("url") | filters.POLL) & (~filters.COMMAND),
        handle_restricted_content
    ))

    print("🤖 Bot စတင်နေပါသည် (MongoDB Version) - 24/7 Running Mode")
    print("✅ Database ချိတ်ဆက်မှု အောင်မြင်ပါသည်။")
    print("🔧 Admin commands များ အသုံးပြုနိုင်ပါပြီ။")

    # Run the bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
