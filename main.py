import sqlite3
import requests
import secrets
import logging
import time
import os
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# --- ENVIRONMENT CONFIGURATION ---
# Set these in Render "Environment Variables" or a .env file
DB_NAME = "sms_panel.db"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "ADMIN_AMIT")
OTP_FEED_URL = os.environ.get("OTP_FEED_URL", "https://otplala-d0")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# ADMIN_IDS should be comma-separated: "12345,67890"
ADMIN_IDS = os.environ.get("ADMIN_IDS", "").split(",")

# Smart Cache
cache = {"data": None, "time": 0}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TITAN_ETERNAL")

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE, service TEXT, server TEXT DEFAULT '58', 
            status TEXT DEFAULT 'available')''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT, username TEXT, phone TEXT, service TEXT, server TEXT,
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

def send_admin_notification(order_data, otp):
    """Sends a detailed success report to all Admin Telegram IDs."""
    if not BOT_TOKEN or not ADMIN_IDS: return
    
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    message = (
        f"🔥 *OTP RECEIVED - TITAN LOG* 🔥\n\n"
        f"👤 *User:* {order_data['username']}\n"
        f"🔑 *API Key:* `{order_data['api_key']}`\n"
        f"📱 *Number:* `{order_data['phone']}`\n"
        f"🛠 *Service:* {order_data['service'].upper()}\n"
        f"🌍 *Server:* {order_data['server']}\n"
        f"📩 *OTP:* `{otp}`\n\n"
        f"⏰ *Date/Time:* {now} UTC"
    )
    
    for admin_id in ADMIN_IDS:
        if not admin_id.strip(): continue
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": admin_id, "text": message, "parse_mode": "Markdown"}, timeout=5)
        except Exception as e:
            logger.error(f"Telegram Error for {admin_id}: {e}")

def auto_cleanup():
    """Sets pending orders to 'expired' - Numbers remain 'used'."""
    try:
        conn = get_db()
        limit_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute("UPDATE orders SET status = 'expired' WHERE status = 'pending' AND created_at < ?", (limit_time,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Cleanup Error: {e}")

def get_live_otps():
    global cache
    current_time = time.time()
    if cache["data"] is None or (current_time - cache["time"]) > 10:
        try:
            resp = requests.get(OTP_FEED_URL, timeout=5)
            cache["data"] = resp.json().get("otps", [])
            cache["time"] = current_time
        except: return []
    return cache["data"]

# --- USER API ---

@app.route('/stubs/handler_api.php', methods=['GET'])
def handler():
    auto_cleanup()
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
        
        # EXCLUSIVITY LOCK: Set status to 'used' immediately. NEVER available again.
        conn.execute("UPDATE numbers SET status='used' WHERE id=?", (num['id'],))
        cursor = conn.execute(
            "INSERT INTO orders (api_key, username, phone, service, server) VALUES (?, ?, ?, ?, ?)", 
            (api_key, user['username'], num['phone'], service, server)
        )
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return f"ACCESS_NUMBER:{order_id}:{num['phone']}"

    elif action == "getStatus":
        order_id = request.args.get('id')
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        
        if not order or order['status'] in ['canceled', 'expired']:
            conn.close()
            return "NO_ACTIVATION"
        if order['status'] == 'successful':
            conn.close()
            return f"STATUS_OK:{order['otp']}"
        
        order_time = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        live_data = get_live_otps()
        for entry in live_data:
            otp_time_str = entry['timestamp'].split('.')[0].replace('T', ' ')
            otp_time = datetime.strptime(otp_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

            if entry['number'].strip() == order['phone'].strip() and otp_time > order_time:
                otp_code = entry['otp']
                conn.execute("UPDATE orders SET otp=?, status='successful' WHERE id=?", (otp_code, order_id))
                conn.commit()
                # SEND BOT NOTIFICATION
                send_admin_notification(order, otp_code)
                conn.close()
                return f"STATUS_OK:{otp_code}"
        
        conn.close()
        return "STATUS_WAIT_CODE"

    elif action == "setStatus":
        order_id = request.args.get('id')
        if request.args.get('status') == "8": # Cancel
            conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (order_id,))
            conn.commit()
            conn.close()
            return "ACCESS_CANCEL"
        conn.close()
        return "BAD_ID"

    conn.close()
    return "ERROR_SQL"

# --- ADMIN ROUTES ---

@app.route('/admin/set_key', methods=['GET'])
def set_key():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    username, new_api_key = request.args.get('username'), request.args.get('api_key')
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET api_key = ? WHERE username = ?", (new_api_key, username))
        else:
            conn.execute("INSERT INTO users (api_key, username) VALUES (?, ?)", (new_api_key, username))
        conn.commit()
        return jsonify({"status": "Success", "username": username, "api_key": new_api_key})
    except: return "ERROR", 500
    finally: conn.close()

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
    return jsonify({"status": "Complete"})

@app.route('/admin/master', methods=['GET'])
def master():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    conn = get_db()
    data = {
        "keys": [dict(k) for k in conn.execute("SELECT * FROM users").fetchall()],
        "stock": [dict(s) for s in conn.execute("SELECT server, service, COUNT(*) as count FROM numbers WHERE status='available' GROUP BY server, service").fetchall()],
        "history": [dict(h) for h in conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 50").fetchall()]
    }
    conn.close()
    return jsonify(data)

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get("PORT", 5050))
    app.run(host='0.0.0.0', port=port)
