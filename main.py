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
OTP_FEED_URL = os.environ.get("OTP_FEED_URL", "https://weak-deloris-nothing672434-fe85179d.koyeb.app/api/otps?limit=100")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = os.environ.get("ADMIN_IDS", "").split(",")

# Smart Cache variables
cache = {"data": None, "time": 0}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TITAN_STRICT_SERVER")

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
            api_key TEXT UNIQUE, username TEXT, 
            created_at DATETIME DEFAULT (DATETIME('now')))''')
        conn.commit()

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def send_admin_notification(order_data, otp):
    """Sends TG notification to admins when OTP is received."""
    if not BOT_TOKEN or not ADMIN_IDS: return
    now = datetime.now(timezone.utc).strftime('%H:%M:%S')
    message = (f"✅ *OTP SUCCESS* ✅\n👤 *User:* {order_data['username']}\n"
               f"📱 *Num:* `{order_data['phone']}`\n📩 *OTP:* `{otp}`\n⏰ *Time:* {now} UTC")
    for admin_id in ADMIN_IDS:
        if admin_id.strip():
            try: requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": admin_id.strip(), "text": message, "parse_mode": "Markdown"}, timeout=5)
            except: pass

def get_live_otps():
    """Smart Cache: Only fetches from Koyeb once every 5 seconds."""
    global cache
    current_time = time.time()
    if cache["data"] is None or (current_time - cache["time"]) > 5:
        try:
            cache["data"] = requests.get(OTP_FEED_URL, timeout=5).json().get("otps", [])
            cache["time"] = current_time
        except Exception as e:
            logger.error(f"Feed Error: {e}")
            return []
    return cache["data"]

# --- THE USER API ---

@app.route('/stubs/handler_api.php', methods=['GET'])
def handler():
    # COMPLETELY REMOVED auto_cleanup() -> NO AUTO CANCEL
    api_key = request.args.get('api_key')
    action = request.args.get('action')
    
    conn = get_db()
    
    # 1. API Key Validation
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
        server = request.args.get('server', '58')
        
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
        
        # BURN POLICY: Set to 'used' permanently
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

        # High-Precision OTP Match
        order_time = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        live_data = get_live_otps()
        
        for entry in live_data:
            otp_time_str = entry['timestamp'].split('.')[0].replace('T', ' ')
            otp_time = datetime.strptime(otp_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

            # Match: Number + Timestamp (3s buffer) + Sender Name
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
        status = request.args.get('status')
        
        if not order_id:
            conn.close()
            return "BAD_ID"
        if status not in ["8", "3"]:
            conn.close()
            return "BAD_STATUS"
            
        order = conn.execute("SELECT created_at, status FROM orders WHERE id=?", (order_id,)).fetchone()
        
        if not order:
            conn.close()
            return "BAD_ID"
            
        if status == "3": # Bot requests next SMS
            conn.close()
            return "STATUS_WAIT_CODE"
            
        if status == "8": # Bot requests Cancel
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
                # Returns the exact standard response to make the bot stay silent and keep waiting
                return "STATUS_WAIT_CODE" 
            
            # Execute Cancel
            conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (order_id,))
            conn.commit()
            conn.close()
            return "ACCESS_CANCEL"

    conn.close()
    return "ERROR_SQL"

# --- THE MASTER ADMIN DASHBOARD ---

@app.route('/admin/master', methods=['GET'])
def master_dashboard():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    conn = get_db()
    
    # Advanced Stats Gather
    total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    success_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE status='successful'").fetchone()[0]
    cancel_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE status='canceled'").fetchone()[0]
    keys = conn.execute("SELECT username, api_key, created_at FROM users").fetchall()
    stock = conn.execute("SELECT server, service, COUNT(*) as count FROM numbers WHERE status='available' GROUP BY server, service").fetchall()
    history = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 20").fetchall()
    conn.close()
    
    return jsonify({
        "server_status": "Active (No Auto-Cancel)",
        "total_stats": {
            "total": total_orders,
            "success": success_orders,
            "canceled": cancel_orders
        },
        "api_keys": [dict(k) for k in keys],
        "available_stock": [dict(s) for s in stock],
        "recent_20_orders": [dict(h) for h in history]
    })

@app.route('/admin/generate_key', methods=['GET'])
def gen_key():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    new_key = secrets.token_hex(16)
    username = request.args.get('username', 'User')
    conn = get_db()
    conn.execute("INSERT INTO users (api_key, username) VALUES (?, ?)", (new_key, username))
    conn.commit()
    conn.close()
    return f"CREATED_KEY:{new_key} FOR {username}"

@app.route('/admin/set_key', methods=['GET'])
def set_key():
    """Allows Admin to manually define a custom key for a user."""
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    u, k = request.args.get('username'), request.args.get('api_key')
    conn = get_db()
    conn.execute("INSERT INTO users (api_key, username) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET api_key=excluded.api_key", (k, u))
    conn.commit()
    conn.close()
    return jsonify({"status": "Success", "user": u, "key": k})

@app.route('/admin/add', methods=['POST'])
def admin_add():
    if request.headers.get("Authorization") != ADMIN_KEY: return "Unauthorized", 401
    data = request.json
    conn = get_db()
    try:
        conn.execute("INSERT INTO numbers (phone, service, server) VALUES (?, ?, ?)", 
                     (data['phone'], data['service'], data.get('server', '58')))
        conn.commit()
        return "SUCCESS"
    except: return "EXISTS"
    finally: conn.close()

@app.route('/admin/add_bulk', methods=['POST'])
def add_bulk():
    if request.headers.get("Authorization") != ADMIN_KEY: return "Unauthorized", 401
    data = request.json
    conn = get_db()
    added, skipped = 0, 0
    for phone in data.get('numbers', []):
        try: 
            conn.execute("INSERT INTO numbers (phone, service, server) VALUES (?, ?, ?)", 
                         (phone.strip(), data['service'], str(data.get('server', '58'))))
            added += 1
        except: skipped += 1
    conn.commit()
    conn.close()
    return jsonify({"status": "Complete", "added": added, "skipped": skipped})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5050)))
