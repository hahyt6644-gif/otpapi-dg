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
ADMIN_KEY = "ADMIN_AMIT" 
OTP_FEED_URL = "https://weak-deloris-nothing672434-fe85179d.koyeb.app/api/otps?limit=100"

# Smart Cache for high-traffic stability
cache = {"data": None, "time": 0}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TITAN_MULTI_LOADER")

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        # Numbers Table
        cursor.execute('''CREATE TABLE IF NOT EXISTS numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE, service TEXT, server TEXT DEFAULT '58', 
            status TEXT DEFAULT 'available')''')
        # Orders Table
        cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT, phone TEXT, service TEXT, otp TEXT DEFAULT NULL,
            status TEXT DEFAULT 'pending', created_at DATETIME DEFAULT (DATETIME('now')))''')
        # Users Table
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT UNIQUE, 
            username TEXT UNIQUE, 
            created_at DATETIME DEFAULT (DATETIME('now')))''')
        conn.commit()

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def auto_cleanup():
    """Releases numbers after 60 mins of inactivity."""
    try:
        conn = get_db()
        limit_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).strftime('%Y-%m-%d %H:%M:%S')
        expired = conn.execute("SELECT phone, id FROM orders WHERE status = 'pending' AND created_at < ?", (limit_time,)).fetchall()
        for order in expired:
            conn.execute("UPDATE numbers SET status = 'available' WHERE phone = ?", (order['phone'],))
            conn.execute("UPDATE orders SET status = 'canceled' WHERE id = ?", (order['id'],))
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
        except Exception as e:
            logger.error(f"External Feed Error: {e}")
            return []
    return cache["data"]

# --- USER API (handler_api.php) ---

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
        
        conn.execute("UPDATE numbers SET status='busy' WHERE id=?", (num['id'],))
        cursor = conn.execute("INSERT INTO orders (api_key, phone, service) VALUES (?, ?, ?)", (api_key, num['phone'], service))
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return f"ACCESS_NUMBER:{order_id}:{num['phone']}"

    elif action == "getStatus":
        order_id = request.args.get('id')
        order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        
        if not order or order['status'] == 'canceled':
            conn.close()
            return "NO_ACTIVATION"
        if order['status'] == 'successful':
            conn.close()
            return f"STATUS_OK:{order['otp']}"
        
        # High-Precision Time Matching
        order_time = datetime.strptime(order['created_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        live_data = get_live_otps()
        for entry in live_data:
            otp_time_str = entry['timestamp'].split('.')[0].replace('T', ' ')
            otp_time = datetime.strptime(otp_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

            if entry['number'].strip() == order['phone'].strip() and otp_time > order_time:
                conn.execute("UPDATE orders SET otp=?, status='successful' WHERE id=?", (entry['otp'], order_id))
                conn.execute("UPDATE numbers SET status='available' WHERE phone=?", (order['phone'],))
                conn.commit()
                conn.close()
                return f"STATUS_OK:{entry['otp']}"
        
        conn.close()
        return "STATUS_WAIT_CODE"

    elif action == "setStatus":
        order_id = request.args.get('id')
        status = request.args.get('status')
        if status == "8": 
            order = conn.execute("SELECT phone FROM orders WHERE id=?", (order_id,)).fetchone()
            if order:
                conn.execute("UPDATE numbers SET status='available' WHERE phone=?", (order['phone'],))
                conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (order_id,))
                conn.commit()
                conn.close()
                return "ACCESS_CANCEL"
        conn.close()
        return "BAD_ID"

    conn.close()
    return "ERROR_SQL"

# --- ADMIN ROUTES ---

@app.route('/admin/generate_key', methods=['GET'])
def gen_key():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    username = request.args.get('username', 'User').strip()
    conn = get_db()
    existing = conn.execute("SELECT api_key FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        api_key = existing['api_key']
        conn.close()
        return jsonify({"api_key": api_key, "status": "Already generated", "username": username})
    new_key = secrets.token_hex(16)
    try:
        conn.execute("INSERT INTO users (api_key, username) VALUES (?, ?)", (new_key, username))
        conn.commit()
        conn.close()
        return jsonify({"api_key": new_key, "status": "Created", "username": username})
    except:
        conn.close()
        return "DATABASE_ERROR", 500

@app.route('/admin/set_key', methods=['GET'])
def set_key():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    username = request.args.get('username')
    new_api_key = request.args.get('api_key')
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET api_key = ? WHERE username = ?", (new_api_key, username))
            status = "Updated"
        else:
            conn.execute("INSERT INTO users (api_key, username) VALUES (?, ?)", (new_api_key, username))
            status = "Created"
        conn.commit()
        conn.close()
        return jsonify({"username": username, "api_key": new_api_key, "status": status})
    except:
        conn.close()
        return "ALREADY_IN_USE", 400

@app.route('/admin/add_bulk', methods=['POST'])
def add_bulk():
    """Adds multiple numbers for the same service/server at once."""
    if request.headers.get("Authorization") != ADMIN_KEY: return "Unauthorized", 401
    data = request.json
    numbers = data.get('numbers', [])
    service = data.get('service')
    server = data.get('server', '58')
    
    conn = get_db()
    added, skipped = 0, 0
    for phone in numbers:
        try:
            conn.execute("INSERT INTO numbers (phone, service, server) VALUES (?, ?, ?)", (phone.strip(), service, server))
            added += 1
        except:
            skipped += 1
    conn.commit()
    conn.close()
    return jsonify({"status": "Bulk Upload Complete", "added": added, "skipped": skipped})

@app.route('/admin/master', methods=['GET'])
def master_dashboard():
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    conn = get_db()
    data = {
        "stats": {
            "total": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "success": conn.execute("SELECT COUNT(*) FROM orders WHERE status='successful'").fetchone()[0],
            "canceled": conn.execute("SELECT COUNT(*) FROM orders WHERE status='canceled'").fetchone()[0]
        },
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
