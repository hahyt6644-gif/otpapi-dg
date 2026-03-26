import sqlite3
import requests
import secrets
import logging
import time
import os
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# --- ENVIRONMENT CONFIG ---
DB_NAME = "sms_panel.db"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "ADMIN_AMIT")
OTP_FEED_URL = os.environ.get("OTP_FEED_URL", "https://wea79d.koyeb.app/api/otps?limit=100")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = os.environ.get("ADMIN_IDS", "").split(",")

# Smart Cache
cache = {"data": None, "time": 0}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TITAN_FINAL")

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
    """Blasts a success report to all Admin IDs on Telegram."""
    if not BOT_TOKEN or not ADMIN_IDS: return
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    message = (
        f"✅ *OTP SUCCESS - TITAN REPORT* ✅\n\n"
        f"👤 *User:* {order_data['username']}\n"
        f"🔑 *Key:* `{order_data['api_key']}`\n"
        f"📱 *Number:* `{order_data['phone']}`\n"
        f"🛠 *Service:* {order_data['service'].upper()}\n"
        f"📩 *OTP:* `{otp}`\n\n"
        f"⏰ *Time:* {now} UTC"
    )
    for admin_id in ADMIN_IDS:
        if not admin_id.strip(): continue
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": admin_id, "text": message, "parse_mode": "Markdown"}, timeout=5)
        except Exception as e: logger.error(f"Telegram Error: {e}")

def auto_cleanup():
    """Expires old pending orders (20 min window) without releasing numbers."""
    try:
        conn = get_db()
        limit = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute("UPDATE orders SET status = 'expired' WHERE status = 'pending' AND created_at < ?", (limit,))
        conn.commit()
        conn.close()
    except Exception as e: logger.error(f"Cleanup Error: {e}")

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

# --- API ENDPOINTS ---

@app.route('/stubs/handler_api.php', methods=['GET'])
def handler():
    auto_cleanup()
    api_key, action = request.args.get('api_key'), request.args.get('action')
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE api_key = ?", (api_key,)).fetchone()
    if not user:
        conn.close()
        return "BAD_KEY"

    if action == "getNumber":
        service, server = request.args.get('service'), request.args.get('server', '58')
        num = conn.execute("SELECT * FROM numbers WHERE service=? AND server=? AND status='available' LIMIT 1", (service, server)).fetchone()
        if not num:
            conn.close()
            return "NO_NUMBERS"
        
        # EXCLUSIVE BURN: Number marked 'used' immediately
        conn.execute("UPDATE numbers SET status='used' WHERE id=?", (num['id'],))
        cursor = conn.execute("INSERT INTO orders (api_key, username, phone, service, server) VALUES (?, ?, ?, ?, ?)", 
                             (api_key, user['username'], num['phone'], service, server))
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

            # Triple Match: Phone + Time + Sender Name
            if entry['number'].strip() == order['phone'].strip() and otp_time > order_time:
                if order['service'].lower() in entry['sender'].lower():
                    conn.execute("UPDATE orders SET otp=?, status='successful' WHERE id=?", (entry['otp'], order_id))
                    conn.commit()
                    send_admin_notification(order, entry['otp'])
                    conn.close()
                    return f"STATUS_OK:{entry['otp']}"
        conn.close()
        return "STATUS_WAIT_CODE"

    elif action == "setStatus":
        order_id, status_act = request.args.get('id'), request.args.get('status')
        if status_act == "8":
            order = conn.execute("SELECT created_at, status FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                conn.close()
                return "BAD_ID"
            
            # 1-MINUTE CANCEL LOCK
            created_at = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - created_at).total_seconds() < 60:
                conn.close()
                return "STATUS_WAIT_RETRY" # Tells bot to show "Wait 1 min"
            
            if order['status'] == 'pending':
                conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (order_id,))
                conn.commit()
                conn.close()
                return "ACCESS_CANCEL"
        conn.close()
        return "BAD_ID"
    conn.close()
    return "ERROR_SQL"

@app.route('/admin/set_key', methods=['GET'])
def set_key():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    u, k = request.args.get('username'), request.args.get('api_key')
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (api_key, username) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET api_key=excluded.api_key", (k, u))
        conn.commit()
        return jsonify({"status": "Success", "user": u, "key": k})
    except: return "DB_ERROR", 500
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
        "stock": [dict(s) for s in conn.execute("SELECT service, COUNT(*) as count FROM numbers WHERE status='available' GROUP BY service").fetchall()],
        "history": [dict(h) for h in conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 50").fetchall()],
        "keys": [dict(k) for k in conn.execute("SELECT * FROM users").fetchall()]
    }
    conn.close()
    return jsonify(data)

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5050)))
