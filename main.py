import sqlite3
import requests
import secrets
import logging
import time
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# --- CONFIGURATION ---
DB_NAME = "sms_panel.db"
ADMIN_KEY = "ADMIN_SECRET_123"
OTP_FEED_URL = "https://weak-deloris-nothing672434-fe85179d.koyeb.app/api/otps?limit=100"

# Smart Cache variables
cache = {"data": None, "time": 0}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TITAN_SERVER")

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
        # Users Table (No Balance)
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT UNIQUE, username TEXT, created_at DATETIME DEFAULT (DATETIME('now')))''')
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
    """Smart Cache: Only fetches from Koyeb once every 10 seconds."""
    global cache
    current_time = time.time()
    if cache["data"] is None or (current_time - cache["time"]) > 10:
        try:
            resp = requests.get(OTP_FEED_URL, timeout=5)
            cache["data"] = resp.json().get("otps", [])
            cache["time"] = current_time
            logger.info("External OTP Feed Refreshed.")
        except Exception as e:
            logger.error(f"Feed Error: {e}")
            return []
    return cache["data"]

# --- THE USER API ---

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

    # ACTION: getNumber
    if action == "getNumber":
        service = request.args.get('service')
        server = request.args.get('server', '58')
        
        if not service: return "BAD_SERVICE"
            
        num = conn.execute(
            "SELECT * FROM numbers WHERE service=? AND server=? AND status='available' LIMIT 1", 
            (service, server)
        ).fetchone()
        
        if not num:
            conn.close()
            return "NO_NUMBERS"
        
        conn.execute("UPDATE numbers SET status='busy' WHERE id=?", (num['id'],))
        cursor = conn.execute("INSERT INTO orders (api_key, phone, service) VALUES (?, ?, ?)", (api_key, num['phone'], service))
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return f"ACCESS_NUMBER:{order_id}:{num['phone']}"

    # ACTION: getStatus
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

        live_data = get_live_otps()
        for entry in live_data:
            if entry['number'].strip() == order['phone'].strip():
                conn.execute("UPDATE orders SET otp=?, status='successful' WHERE id=?", (entry['otp'], order_id))
                conn.execute("UPDATE numbers SET status='available' WHERE phone=?", (order['phone'],))
                conn.commit()
                conn.close()
                return f"STATUS_OK:{entry['otp']}"
        
        conn.close()
        return "STATUS_WAIT_CODE"

    # ACTION: setStatus
    elif action == "setStatus":
        order_id = request.args.get('id')
        status = request.args.get('status')
        order = conn.execute("SELECT phone, status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            conn.close()
            return "BAD_ID"
            
        if status == "8": # Cancel
            conn.execute("UPDATE numbers SET status='available' WHERE phone=?", (order['phone'],))
            conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (order_id,))
            conn.commit()
            conn.close()
            return "ACCESS_CANCEL"
            
        conn.close()
        return "BAD_STATUS"

    conn.close()
    return "ERROR_SQL"

# --- THE MASTER ADMIN DASHBOARD ---

@app.route('/admin/master', methods=['GET'])
def master_dashboard():
    """Provides a full intelligence report of the server."""
    if request.args.get('admin_key') != ADMIN_KEY: return "Unauthorized", 401
    
    conn = get_db()
    
    # 1. API Keys List
    keys = conn.execute("SELECT username, api_key, created_at FROM users").fetchall()
    
    # 2. Stats
    total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    success_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE status='successful'").fetchone()[0]
    cancel_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE status='canceled'").fetchone()[0]
    
    # 3. Stock List
    stock = conn.execute("SELECT server, service, COUNT(*) as count FROM numbers WHERE status='available' GROUP BY server, service").fetchall()
    
    # 4. Recent History
    history = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 20").fetchall()
    
    conn.close()
    
    return jsonify({
        "server_status": "Active",
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

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5050)
