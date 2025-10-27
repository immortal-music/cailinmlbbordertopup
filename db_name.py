from pymongo import MongoClient

connection_string = "mongodb+srv://wanglinmongodb:wanglin@cluster0.tny5vhz.mongodb.net/?retryWrites=true&w=majority"
client = MongoClient(connection_string)

# Database name ကို ဒီမှာ သတ်မှတ်ပါ
db_name = "mlbb_bot_db" # သင် အသုံးပြုလိုသော database အမည်
db = client[db_name]
# သို့မဟုတ်
# db = client.get_database(db_name)

print(f"Connecting to database: {db.name}") # Output: Connecting to database: mlbb_bot_db

# --- collections များကို ရယူပါ ---
users_col = db["users"]
# ... etc
