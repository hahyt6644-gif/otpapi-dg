import sqlite3
import requests
import logging
import time
import os
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# --- ENVIRONMENT CONFIG ---
DB_NAME = "sms_panel.db"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "ADMIN_AMIT")
OTP_FEED_URL = os.environ.get("OTP_FEED_URL", "https://weak-deloris-nothinit=100")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = os.environ.get("ADMIN_IDS", "").split(",")

cache = {"data": None, "time": 0}
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE, service TEXT, server TEXT, 
            status TEXT DEFAULT 'available')''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT, username TEXT, phone TEXT, service TEXT, server TEXT,
            otp TEXT DEFAULT NULL, status TEXT DEFAULT 'pending', 
            created_at DATETIME DEFAULT (DATETIME('now')))''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT UNIQUE, username TEXT UNIQUE)''')
        conn.commit()

def send_admin_notification(order_data, otp):
    if not BOT_TOKEN or not ADMIN_IDS: return
    now = datetime.now(timezone.utc).strftime('%H:%M:%S')
    message = (f"✅ *OTP SUCCESS* ✅\n👤 *User:* {order_data['username']}\n"
               f"📱 *Num:* `{order_data['phone']}`\n📩 *OTP:* `{otp}`\n⏰ *Time:* {now} UTC")
    for admin_id in ADMIN_IDS:
        if admin_id.strip():
            try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": admin_id.strip(), "text": message, "parse_mode": "Markdown"}, timeout=5)
            except: pass

def get_live_otps():
    global cache
    current_time = time.time()
    if cache["data"] is None or (current_time - cache["time"]) > 5:
        try:
            cache["data"] = requests.get(OTP_FEED_URL, timeout=5).json().get("otps", [])
            cache["time"] = current_time
        except: return []
    return cache["data"]

# --- STRICT API HANDLER ---

@app.route('/stubs/handler_api.php', methods=['GET'])
def handler():
    try:
        api_key = request.args.get('api_key')
        action = request.args.get('action')
        conn = get_db()
        
        # 1. API KEY CHECK
        if not api_key:
            conn.close()
            return "BAD_KEY"
            
        user = conn.execute("SELECT * FROM users WHERE api_key = ?", (api_key,)).fetchone()
        if not user:
            conn.close()
            return "BAD_KEY"

        # ==========================================
        # ACTION: getNumber
        # ==========================================
        if action == "getNumber":
            service = request.args.get('service')
            server = request.args.get('server')
            
            if not service:
                conn.close()
                return "BAD_SERVICE"
            if not server:
                conn.close()
                return "BAD_SERVER"
                
            num = conn.execute("SELECT * FROM numbers WHERE service=? AND server=? AND status='available' LIMIT 1", (service, server)).fetchone()
            
            if not num:
                conn.close()
                return "NO_NUMBERS"
            
            conn.execute("UPDATE numbers SET status='used' WHERE id=?", (num['id'],))
            cursor = conn.execute("INSERT INTO orders (api_key, username, phone, service, server) VALUES (?, ?, ?, ?, ?)", 
                                 (api_key, user['username'], num['phone'], service, server))
            order_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return f"ACCESS_NUMBER:{order_id}:{num['phone']}"

        # ==========================================
        # ACTION: getStatus
        # ==========================================
        elif action == "getStatus":
            order_id = request.args.get('id')
            if not order_id:
                conn.close()
                return "BAD_ID"
                
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            
            if not order:
                conn.close()
                return "BAD_ID"
                
            if order['status'] == 'canceled':
                conn.close()
                return "NO_ACTIVATION"
                
            if order['status'] == 'successful':
                conn.close()
                return f"STATUS_OK:{order['otp']}"
            
            # OTP Matching Logic
            order_time = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            live_data = get_live_otps()
            
            for entry in live_data:
                otp_time_str = entry['timestamp'].split('.')[0].replace('T', ' ')
                otp_time = datetime.strptime(otp_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

                # Match: Number + Time (3 sec buffer) + Service Name matches Sender
                if entry['number'].strip() == order['phone'].strip() and otp_time > (order_time + timedelta(seconds=3)):
                    if order['service'].lower() in entry['sender'].lower():
                        conn.execute("UPDATE orders SET otp=?, status='successful' WHERE id=?", (entry['otp'], order_id))
                        conn.commit()
                        send_admin_notification(order, entry['otp'])
                        conn.close()
                        return f"STATUS_OK:{entry['otp']}"
            
            conn.close()
            return "STATUS_WAIT_CODE"

        # ==========================================
        # ACTION: setStatus
        # ==========================================
        elif action == "setStatus":
            order_id = request.args.get('id')
            status_act = request.args.get('status')
            
            if not order_id:
                conn.close()
                return "BAD_ID"
            if status_act not in ["8", "3"]:
                conn.close()
                return "BAD_STATUS"
                
            order = conn.execute("SELECT created_at, status FROM orders WHERE id=?", (order_id,)).fetchone()
            
            if not order:
                conn.close()
                return "BAD_ID"
                
            if status_act == "3": # Requesting another SMS
                conn.close()
                return "STATUS_WAIT_CODE"
                
            if status_act == "8": # Canceling Order
                if order['status'] == 'canceled':
                    conn.close()
                    return "STATUS_CANCEL"
                if order['status'] == 'successful':
                    conn.close()
                    return "NO_ACTIVATION"
                    
                # 🛑 1-MINUTE HARD LOCK
                created_at = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                seconds_passed = (datetime.now(timezone.utc) - created_at).total_seconds()
                
                if seconds_passed < 60:
                    conn.close()
                    # By returning STATUS_WAIT_CODE, the bot perfectly ignores the rejection
                    # and stays on the waiting screen without crashing or throwing weird errors.
                    return "STATUS_WAIT_CODE" 
                
                conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (order_id,))
                conn.commit()
                conn.close()
                return "ACCESS_CANCEL"
                
        # Fallback for invalid actions
        conn.close()
        return "ERROR_SQL"
        
    except Exception as e:
        logging.error(f"Server Error: {e}")
        return "ERROR_SQL"

# --- ADMIN ROUTES ---
@app.route('/admin/set_key', methods=['GET'])
def set_key():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    u, k = request.args.get('username'), request.args.get('api_key')
    conn = get_db()
    conn.execute("INSERT INTO users (api_key, username) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET api_key=excluded.api_key", (k, u))
    conn.commit()
    conn.close()
    return jsonify({"status": "Success", "user": u, "key": k})

@app.route('/admin/add_bulk', methods=['POST'])
def add_bulk():
    if request.headers.get("Authorization") != ADMIN_KEY: return "Unauthorized", 401
    data = request.json
    conn = get_db()
    for phone in data.get('numbers', []):
        try: conn.execute("INSERT INTO numbers (phone, service, server) VALUES (?, ?, ?)", (phone.strip(), data['service'], str(data.get('server', '58'))))
        except: continue
    conn.commit()
    conn.close()
    return "SUCCESS"

@app.route('/admin/master', methods=['GET'])
def master():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    conn = get_db()
    data = {
        "stock": [dict(s) for s in conn.execute("SELECT service, server, COUNT(*) as count FROM numbers WHERE status='available' GROUP BY service, server").fetchall()],
        "keys": [dict(k) for k in conn.execute("SELECT id, username, api_key FROM users").fetchall()],
        "history": [dict(h) for h in conn.execute("SELECT id, username, phone, service, otp, status, created_at FROM orders ORDER BY created_at DESC LIMIT 30").fetchall()]
    }
    conn.close()
    return jsonify(data)

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5050)))
