import pymongo
from env import MONGO_URI, ADMIN_ID # MONGO_URI ကို env.py ကနေ ယူသုံးမယ်

# --- Database Connection ---
client = None
db = None
users_col = None
settings_col = None
clone_bots_col = None
DATABASE_NAME = "mlbb_bot_db_v1" # Database နာမည်ကို ဒီမှာ သတ်မှတ်ပါ

try:
    # MongoDB Atlas ကို ချိတ်ဆက်ခြင်း
    print(f"ℹ️ MongoDB Atlas သို့ ချိတ်ဆက်နေပါသည် ({MONGO_URI[:30]}...).")
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000) # Timeout 5 စက္ကန့်ထားကြည့်ပါ
    # Server information ကို ရယူပြီး connection ကို စမ်းစစ်ပါ
    client.server_info()
    # Database ကို သတ်မှတ်ခြင်း
    db = client[DATABASE_NAME] # db = client.get_database(DATABASE_NAME) လို့ရေးလဲရပါတယ်

    print(f"✅ MongoDB ({DATABASE_NAME}) ကို အောင်မြင်စွာ ချိတ်ဆက်ပြီးပါပြီ။")

    # --- Collections ---
    # User data (balance, orders, topups) သိမ်းမယ့် Collection
    users_col = db["users"]

    # Bot settings (prices, authorized_users, admins) သိမ်းမယ့် Collection
    # ဒီ setting တွေကို document တစ်ခုတည်းမှာပဲ စုသိမ်းပါမယ်
    settings_col = db["settings"]

    # Clone bots data သိမ်းမယ့် Collection
    clone_bots_col = db["clone_bots"]

    # Collection တွေ တကယ်ရှိရဲ့လား စစ်ဆေးကြည့်ပါ (optional)
    print(f"ℹ️ Collections: {db.list_collection_names()}")


except pymongo.errors.ServerSelectionTimeoutError as e:
    print(f"❌ MongoDB သို့ ချိတ်ဆက်ရာတွင် အချိန်ကုန်သွားပါသည် (Timeout Error): {e}")
    print("⚠️ Network connection, Firewall settings, သို့မဟုတ် MongoDB IP Whitelist ကို စစ်ဆေးပါ။")
except pymongo.errors.ConnectionFailure as e:
    print(f"❌ MongoDB ကို ချိတ်ဆက်ရာတွင် အမှားဖြစ်ပွားနေသည် (Connection Failure): {e}")
except pymongo.errors.ConfigurationError as e:
    print(f"❌ MongoDB URI ('{MONGO_URI}') ပုံစံ မှားယွင်းနေသည် (Configuration Error): {e}")
except Exception as e:
    print(f"❌ MongoDB ချိတ်ဆက်ရာတွင် မမျှော်လင့်သော အမှားဖြစ်ပွားနေသည်: {e}")
    # ချိတ်ဆက်မှု မအောင်မြင်ရင် variables တွေကို None ပြန်ထားပါ
    client = None
    db = None
    users_col = None
    settings_col = None
    clone_bots_col = None

# --- Settings Document ကို စတင် ပြင်ဆင်သတ်မှတ်ပေးသော Function ---
def initialize_settings():
    """ Bot စစဖွင့်ချိန်တွင် default settings document ရှိမရှိ စစ်ဆေးပြီး မရှိပါက အသစ်ထည့်သွင်းပေးသည်။ """
    if settings_col is not None:
        try:
            # "bot_config" ဆိုတဲ့ _id နဲ့ document ရှိပြီးသားလား စစ်ဆေး
            if settings_col.count_documents({"_id": "bot_config"}) == 0:
                print("ℹ️ MongoDB ထဲတွင် default bot settings များ ထည့်သွင်းနေပါသည်...")
                default_settings = {
                    "_id": "bot_config", # ဒီ document ကို အမြဲတမ်း ပြန်ရှာလို့လွယ်အောင် fixed ID သုံးထားသည်
                    "prices": {}, # Default prices တွေကို ဒီမှာ ထည့်ချင်ထည့်နိုင်ပါတယ်
                    "authorized_users": [], # စစချင်းမှာ authorized user မရှိသေးပါ
                    "admin_ids": [ADMIN_ID] # ပင်မ ADMIN_ID ကို အလိုအလျောက် ထည့်ပေးထားသည်
                    # အခြား settings တွေ လိုအပ်ရင် ဒီမှာ ထပ်ဖြည့်နိုင်ပါတယ်
                }
                settings_col.insert_one(default_settings)
                print("✅ Default settings များ ထည့်သွင်းပြီးပါပြီ။")
            else:
                print("ℹ️ Default settings document ရှိပြီးသားဖြစ်ပါသည်။")
                # ရှိပြီးသား setting document မှာ လိုအပ်တဲ့ field တွေ မပါရင် update လုပ်ပေးနိုင်ပါတယ် (ဥပမာ)
                settings_col.update_one(
                    {"_id": "bot_config"},
                    {"$setOnInsert": {"prices": {}, "authorized_users": [], "admin_ids": [ADMIN_ID]}}
                )
        except Exception as e:
            print(f"❌ Default settings များ စစ်ဆေး/ထည့်သွင်းရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
    else:
        print("❌ Settings collection မရှိသောကြောင့် settings များ initialize မလုပ်နိုင်ပါ။")


# --- Database Function များ ---

# Settings အားလုံးကို ရယူရန်
def load_settings_db():
    if settings_col is None:
        print("❌ Settings collection မရှိပါ။")
        return {"prices": {}, "authorized_users": [], "admin_ids": [ADMIN_ID]} # Default ပြန်ပေးမယ်
    try:
        settings_data = settings_col.find_one({"_id": "bot_config"})
        if settings_data:
            # Default value တွေပါ သေချာအောင်လုပ်ပါ
            settings_data.setdefault("prices", {})
            settings_data.setdefault("authorized_users", [])
            settings_data.setdefault("admin_ids", [ADMIN_ID])
            return settings_data
        else:
            # Setting မရှိသေးရင် (initialize လုပ်တာ အဆင်မပြေခဲ့ရင်) default ပြန်ပေးမယ်
            print("⚠️ Settings document မတွေ့ပါ။ Default settings ကို ပြန်ပေးပါမည်။")
            return {"prices": {}, "authorized_users": [], "admin_ids": [ADMIN_ID]}
    except Exception as e:
        print(f"❌ Settings များ ရယူရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return {"prices": {}, "authorized_users": [], "admin_ids": [ADMIN_ID]} # အမှားရှိရင် default ပေးမယ်

# Setting field တစ်ခုကို Update လုပ်ရန်
def save_settings_field_db(field_name, value):
    if settings_col is None:
        print("❌ Settings collection မရှိပါ။ Settings မသိမ်းနိုင်ပါ။")
        return False
    try:
        result = settings_col.update_one(
            {"_id": "bot_config"},
            {"$set": {field_name: value}},
            upsert=True # Document မရှိရင် အသစ်ဆောက်မယ်
        )
        print(f"ℹ️ Settings field '{field_name}' update result: {result.modified_count} modified, {result.upserted_id} upserted.")
        return True
    except Exception as e:
        print(f"❌ Settings ({field_name}) သိမ်းရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

# Authorized Users များကို Database မှ ရယူရန်
def load_authorized_users_db():
    settings = load_settings_db()
    return settings.get("authorized_users", [])

# Authorized Users များကို Database ထဲသို့ သိမ်းဆည်းရန်
def save_authorized_users_db(authorized_list):
    # Data type ကို သေချာအောင် list အဖြစ် ပြောင်းသိမ်းပါ
    return save_settings_field_db("authorized_users", list(authorized_list))

# Prices များကို Database မှ ရယူရန်
def load_prices_db():
    settings = load_settings_db()
    return settings.get("prices", {})

# Prices များကို Database ထဲသို့ သိမ်းဆည်းရန်
def save_prices_db(prices_dict):
    return save_settings_field_db("prices", prices_dict)

# Admin ID list ကို Database မှ ရယူရန်
def load_admins_db():
    settings = load_settings_db()
    # Owner ID က အမြဲ admin ဖြစ်ကြောင်း သေချာအောင်လုပ်ပါ
    admin_ids = settings.get("admin_ids", [])
    if ADMIN_ID not in admin_ids:
        admin_ids.append(ADMIN_ID)
    return admin_ids

# Admin ID အသစ်ထည့်ရန်
def add_admin_db(admin_id_to_add):
    if settings_col is None: return False
    try:
        # Number အဖြစ် သေချာအောင်ပြောင်းပါ
        admin_id_int = int(admin_id_to_add)
        result = settings_col.update_one(
            {"_id": "bot_config"},
            {"$addToSet": {"admin_ids": admin_id_int}} # addToSet က ရှိပြီးသားဆို ထပ်မထည့်ဘူး
        )
        print(f"ℹ️ Add admin result for {admin_id_int}: {result.modified_count} modified.")
        return True
    except ValueError:
        print(f"❌ Admin ID ({admin_id_to_add}) သည် number မဟုတ်ပါ။")
        return False
    except Exception as e:
        print(f"❌ Admin ({admin_id_to_add}) ထည့်ရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

# Admin ID ဖယ်ရှားရန်
def remove_admin_db(admin_id_to_remove):
    if settings_col is None: return False
    try:
        # Number အဖြစ် သေချာအောင်ပြောင်းပါ
        admin_id_int = int(admin_id_to_remove)
        # Owner ကို ဖျက်လို့မရအောင် စစ်ပါ
        if admin_id_int == ADMIN_ID:
            print(f"⚠️ Owner ID ({ADMIN_ID}) ကို ဖယ်ရှားလို့မရပါ။")
            return False
        result = settings_col.update_one(
            {"_id": "bot_config"},
            {"$pull": {"admin_ids": admin_id_int}} # pull က list ထဲက value ကို ဖယ်ထုတ်တယ်
        )
        print(f"ℹ️ Remove admin result for {admin_id_int}: {result.modified_count} modified.")
        return result.modified_count > 0 # ဖယ်လိုက်နိုင်ရင် True
    except ValueError:
        print(f"❌ Admin ID ({admin_id_to_remove}) သည် number မဟုတ်ပါ။")
        return False
    except Exception as e:
        print(f"❌ Admin ({admin_id_to_remove}) ဖယ်ရှားရာတွင် အမှားဖြစ်ပွားနေသည်: {e}")
        return False

# --- Initialization ---
# db.py file ကို import လုပ်ချိန်တွင် initialize_settings ကို အလိုအလျောက် run စေရန်
if db is not None:
    initialize_settings()
else:
    # အပေါ်မှာ error message ပြပြီးသားဖြစ်လို့ ဒီမှာ ထပ်မပြတော့ပါ
    pass
