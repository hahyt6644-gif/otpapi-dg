import sqlite3
import requests
import secrets
import logging
import time
import os
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# --- CONFIGURATION ---
DB_NAME = "sms_panel.db"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "ADMIN_AMIT")
OTP_FEED_URL = "https://weak-deloris-nothing672434-fe85179d.koyeb.app/api/otps?limit=100"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7538833010:AAHTTyzd6nnWQjy_emmYf3yp4eNckAos1a8")
ADMIN_IDS = os.environ.get("ADMIN_IDS", "6931296977,5425526761").split(",")

cache = {"data": None, "time": 0}
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        # Fixed: Added 'server' to numbers and 'username' to users/orders
        cursor.execute('''CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE, service TEXT, server TEXT DEFAULT '58', 
            status TEXT DEFAULT 'available')''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT, username TEXT, phone TEXT, service TEXT, 
            otp TEXT DEFAULT NULL, status TEXT DEFAULT 'pending', 
            created_at DATETIME DEFAULT (DATETIME('now')))''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT UNIQUE, username TEXT UNIQUE, 
            created_at DATETIME DEFAULT (DATETIME('now')))''')
        conn.commit()

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def send_admin_notification(order, otp):
    if not BOT_TOKEN: return
    now = datetime.now(timezone.utc).strftime('%H:%M:%S')
    message = (f"✅ *OTP SUCCESS* ✅\n👤 *User:* {order['username']}\n"
               f"📱 *Num:* `{order['phone']}`\n📩 *OTP:* `{otp}`\n⏰ *Time:* {now} UTC")
    for admin_id in ADMIN_IDS:
        try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", 
                           json={"chat_id": admin_id.strip(), "text": message, "parse_mode": "Markdown"}, timeout=5)
        except: pass

def get_live_otps():
    global cache
    current_time = time.time()
    if cache["data"] is None or (current_time - cache["time"]) > 5:
        try:
            resp = requests.get(OTP_FEED_URL, timeout=5)
            cache["data"] = resp.json().get("otps", [])
            cache["time"] = current_time
        except: return []
    return cache["data"]

# --- THE USER API ---

@app.route('/stubs/handler_api.php', methods=['GET'])
def handler():
    api_key = request.args.get('api_key')
    action = request.args.get('action')
    conn = get_db()
    
    user = conn.execute("SELECT * FROM users WHERE api_key = ?", (api_key,)).fetchone()
    if not user:
        conn.close()
        return "BAD_KEY"

    if action == "getNumber":
        service = request.args.get('service')
        server = request.args.get('server', '58')
        num = conn.execute("SELECT * FROM numbers WHERE service=? AND server=? AND status='available' LIMIT 1", (service, server)).fetchone()
        
        if not num:
            conn.close()
            return "NO_NUMBERS"
        
        conn.execute("UPDATE numbers SET status='busy' WHERE id=?", (num['id'],))
        # FIXED: Added username to the insert statement
        cursor = conn.execute("INSERT INTO orders (api_key, username, phone, service) VALUES (?, ?, ?, ?)", 
                              (api_key, user['username'], num['phone'], service))
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return f"ACCESS_NUMBER:{order_id}:{num['phone']}"

    elif action == "getStatus":
        order_id = request.args.get('id')
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            conn.close()
            return "BAD_ID"
        
        if order['status'] == 'successful':
            conn.close()
            return f"STATUS_OK:{order['otp']}"
        if order['status'] == 'canceled':
            conn.close()
            return "NO_ACTIVATION"

        order_time = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        live_data = get_live_otps()
        for entry in live_data:
            otp_time_str = entry['timestamp'].split('.')[0].replace('T', ' ')
            otp_time = datetime.strptime(otp_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            
            if entry['number'].strip() == order['phone'].strip() and otp_time > (order_time + timedelta(seconds=3)):
                if order['service'].lower() in entry['sender'].lower():
                    conn.execute("UPDATE orders SET otp=?, status='successful' WHERE id=?", (entry['otp'], order_id))
                    conn.commit()
                    send_admin_notification(order, entry['otp'])
                    conn.close()
                    return f"STATUS_OK:{entry['otp']}"
        
        conn.close()
        return "STATUS_WAIT_CODE"

    elif action == "setStatus":
        order_id, status = request.args.get('id'), request.args.get('status')
        order = conn.execute("SELECT created_at, phone, status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            conn.close()
            return "BAD_ID"
            
        if status == "8":
            created_at = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - created_at).total_seconds() < 60:
                conn.close()
                return "STATUS_WAIT_CODE" # Locks cancel for 1 min
            
            conn.execute("UPDATE numbers SET status='available' WHERE phone=?", (order['phone'],))
            conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (order_id,))
            conn.commit()
            conn.close()
            return "ACCESS_CANCEL"
            
        conn.close()
        return "BAD_STATUS"

    conn.close()
    return "ERROR_SQL"

# --- ADMIN ROUTES ---

@app.route('/admin/set_key', methods=['GET'])
def set_key():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    u, k = request.args.get('username'), request.args.get('api_key')
    conn = get_db()
    # FIXED: Added UNIQUE constraint handling
    conn.execute("INSERT INTO users (api_key, username) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET api_key=excluded.api_key", (k, u))
    conn.commit()
    conn.close()
    return jsonify({"status": "Success", "key": k})

@app.route('/admin/add_bulk', methods=['POST'])
def add_bulk():
    if request.headers.get("Authorization") != ADMIN_KEY: return "Unauthorized", 401
    data = request.json
    conn = get_db()
    for phone in data.get('numbers', []):
        try: conn.execute("INSERT INTO numbers (phone, service, server) VALUES (?, ?, ?)", (phone.strip(), data['service'], data.get('server', '58')))
        except: continue
    conn.commit()
    conn.close()
    return "SUCCESS"

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5050)))
