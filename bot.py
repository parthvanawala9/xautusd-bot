import os
import time
import json
import hmac
import hashlib
import logging
import threading
import uuid
import atexit
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, urlparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests
import websocket
from dotenv import load_dotenv

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
WS_URL = os.getenv("DELTA_PUBLIC_WS_URL", "wss://public-socket.india.delta.exchange")
PORT = int(os.getenv("PORT") or os.getenv("DASHBOARD_PORT") or "8080")

SYMBOL = "XAUTUSD"
MARGIN_FRACTION = Decimal("0.10")
MAX_LEVERAGE = 100
MIN_LEVERAGE = 10
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = Decimal("3.0")

RECONNECT_SECONDS = 5
ENTRY_CONFIRM_TIMEOUT = 10
CLOSE_CONFIRM_TIMEOUT = 10
POLL_INTERVAL = 0.25

CLIENTS_FILE = os.path.join(DATA_DIR, "clients_config.json")
LOCK_FILE = os.path.join(DATA_DIR, "bot_multiclient.lock")
LOCK_HANDLE = None
PUBLIC_IP = "Loading..."

os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True
)

def now_ist():
    return datetime.now(IST)

def atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)

def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def get_public_ip():
    global PUBLIC_IP
    if PUBLIC_IP != "Loading...":
        return PUBLIC_IP
    try:
        response = requests.get("https://api.ipify.org?format=json", timeout=5)
        ip = response.json().get("ip")
        if ip:
            PUBLIC_IP = ip
            logging.info("RAILWAY OUTBOUND IP --> %s", ip)
    except Exception:
        PUBLIC_IP = "Unknown"
    return PUBLIC_IP

def acquire_single_process_lock():
    global LOCK_HANDLE
    try:
        import fcntl
    except ImportError:
        return True
    try:
        LOCK_HANDLE = open(LOCK_FILE, "w", encoding="utf-8")
        fcntl.flock(LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        return False
    return True

class DeltaClient:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "XAUTUSD-MultiClient-Bot/1.1"
        })

    def sign(self, method, path, query="", body=""):
        timestamp = str(int(time.time()))
        message = method.upper() + timestamp + path + query + body
        signature = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp
        }

    def api(self, method, path, params=None, body=None, auth=False):
        params = params or {}
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        query = "?" + urlencode(params, doseq=True) if params else ""
        headers = self.sign(method, path, query, body_text) if auth else {}

        response = self.session.request(
            method.upper(),
            BASE_URL + path,
            params=params,
            data=body_text if body is not None else None,
            headers=headers,
            timeout=(4, 12)
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            raise RuntimeError(f"Delta API error: {data}")
        return data

    def product(self):
        data = self.api("GET", f"/v2/products/{SYMBOL}")
        return data.get("result", {})

    def position(self, product_id):
        try:
            data = self.api("GET", "/v2/positions", params={"product_id": int(product_id)}, auth=True)
            result = data.get("result", {})
            position = result if isinstance(result, dict) else (result[0] if result else {})
            return {
                "size": int(position.get("size", 0)),
                "entry_price": float(position.get("entry_price") or position.get("avg_price") or 0.0),
                "stop_loss": float(position.get("stop_loss") or 0.0),
                "liquidation_price": float(position.get("liquidation_price") or 0.0),
                "margin": float(position.get("margin") or 0.0),
                "unrealized_pnl": float(position.get("unrealized_pnl") or 0.0),
                "realized_pnl": float(position.get("realized_pnl") or 0.0),
                "leverage": int(position.get("leverage") or position.get("user_leverage") or 10)
            }
        except Exception:
            return {"size": 0, "entry_price": 0.0, "stop_loss": 0.0, "unrealized_pnl": 0.0, "realized_pnl": 0.0, "leverage": 10, "margin": 0.0}

    def balance(self):
        try:
            data = self.api("GET", "/v2/wallet/balances", auth=True)
            result = data.get("result", [])
            if isinstance(result, dict):
                result = [result]
            for w in result:
                if str(w.get("asset_symbol", "")).upper() in ("USD", "USDT"):
                    val = w.get("available_balance") if w.get("available_balance") is not None else w.get("balance")
                    if val is not None:
                        return Decimal(str(val))
        except Exception:
            pass
        return Decimal("0")

    def set_leverage(self, product_id, leverage):
        return self.api("POST", f"/v2/products/{product_id}/orders/leverage", body={"leverage": str(int(leverage))}, auth=True)

    def calculate_order_size(self, product, price, leverage):
        balance = self.balance()
        if balance <= 0:
            return 1
        margin = balance * MARGIN_FRACTION
        notional = margin * Decimal(str(leverage))
        contract_value = Decimal(str(product.get("contract_value") or "0.001"))
        raw_size = notional / Decimal(str(price)) / contract_value
        increment = Decimal(str(product.get("lot_size") or "1"))
        minimum = Decimal(str(product.get("min_order_size") or increment))
        size_decimal = (raw_size / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        return int(max(size_decimal, minimum))

    def market_entry(self, product_id, direction, size):
        side = "buy" if direction == "LONG" else "sell"
        return self.api("POST", "/v2/orders", body={
            "product_id": int(product_id),
            "product_symbol": SYMBOL,
            "size": int(size),
            "side": side,
            "order_type": "market_order",
            "client_order_id": f"entry_{int(time.time()*1000)}"
        }, auth=True)

    def close_position(self, product_id, signed_size):
        if signed_size == 0:
            return
        side = "sell" if signed_size > 0 else "buy"
        try:
            self.api("DELETE", "/v2/orders/all", body={"product_id": int(product_id)}, auth=True)
            self.api("POST", "/v2/orders", body={
                "product_id": int(product_id),
                "product_symbol": SYMBOL,
                "size": abs(int(signed_size)),
                "side": side,
                "order_type": "market_order",
                "reduce_only": True,
                "client_order_id": f"close_{int(time.time()*1000)}"
            }, auth=True)
        except Exception:
            pass


class ClientBotInstance:
    def __init__(self, config):
        self.name = config["name"]
        self.api_key = config["api_key"]
        self.api_secret = config["api_secret"]
        self.starting_date = config["starting_date"]  # Format: "YYYY-MM-DD"
        self.expiry_date = config["expiry_date"]      # Format: "YYYY-MM-DD"
        self.bot_running = config.get("bot_running", False)
        
        self.client = DeltaClient(self.api_key, self.api_secret)
        self.product_id = 0
        self.product = None
        self.last_price = None
        self.stop_loss = 0.0
        self.lock = threading.RLock()
        self.prepare_product()

    def check_validity(self):
        try:
            today = datetime.now(IST).date()
            start_dt = datetime.strptime(self.starting_date, "%Y-%m-%d").date()
            expiry_dt = datetime.strptime(self.expiry_date, "%Y-%m-%d").date()

            if today < start_dt:
                return "NOT_STARTED"
            if today > expiry_dt:
                if self.bot_running:
                    logging.info("Client %s subscription expired on %s. Stopping bot.", self.name, self.expiry_date)
                    self.stop_bot()
                return "EXPIRED"
        except Exception:
            pass
        return "ACTIVE"

    def prepare_product(self):
        if self.product_id:
            return True
        try:
            self.product = self.client.product()
            self.product_id = int(self.product["id"])
            return True
        except Exception:
            return False

    def start_bot(self):
        status = self.check_validity()
        if status == "NOT_STARTED":
            return {"success": False, "message": f"Bot cannot start. Starting date is {self.starting_date}."}
        if status == "EXPIRED":
            return {"success": False, "message": "Subscription expired. Cannot start bot."}

        with self.lock:
            if not self.prepare_product():
                return {"success": False, "message": "Failed to connect to Delta Exchange with given API keys."}
            self.bot_running = True
            return {"success": True, "message": f"Bot started for {self.name}"}

    def stop_bot(self):
        with self.lock:
            self.bot_running = False
            try:
                if self.prepare_product():
                    pos = self.client.position(self.product_id)
                    size = pos.get("size", 0)
                    if size != 0:
                        self.client.close_position(self.product_id, size)
            except Exception:
                pass
            return {"success": True, "message": f"Bot stopped for {self.name}"}

    def evaluate(self, price, candles):
        status = self.check_validity()
        if not self.bot_running or status != "ACTIVE":
            if status == "EXPIRED" and self.bot_running:
                self.stop_bot()
            return

        with self.lock:
            self.last_price = float(price)
            if not candles or len(candles) < SUPERTREND_PERIOD:
                return

            highs = [float(c.get("high", 0)) for c in candles]
            lows = [float(c.get("low", 0)) for c in candles]
            closes = [float(c.get("close", 0)) for c in candles]

            atr = []
            for i in range(len(closes)):
                tr = highs[i] - lows[i] if i == 0 else max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
                atr.append(tr)

            period = SUPERTREND_PERIOD
            multiplier = float(SUPERTREND_MULTIPLIER)
            final_upper, final_lower = 0.0, 0.0
            st_dir = "BUY"

            for i in range(period, len(closes)):
                cur_atr = sum(atr[i - period + 1 : i + 1]) / period
                hl2 = (highs[i] + lows[i]) / 2.0
                ub = hl2 + (multiplier * cur_atr)
                lb = hl2 - (multiplier * cur_atr)
                final_upper = ub if (i == period or ub < final_upper or closes[i-1] > final_upper) else final_upper
                final_lower = lb if (i == period or lb > final_lower or closes[i-1] < final_lower) else final_lower

                st_dir = "BUY" if closes[i] >= final_lower else "SELL"

            st_level = final_lower if st_dir == "BUY" else final_upper
            self.stop_loss = float(st_level)

            pos = self.client.position(self.product_id)
            size = pos.get("size", 0)

            if st_dir == "BUY":
                if size < 0:
                    self.client.close_position(self.product_id, size)
                if size == 0:
                    lev = 10
                    self.client.set_leverage(self.product_id, lev)
                    qty = self.client.calculate_order_size(self.product, price, lev)
                    self.client.market_entry(self.product_id, "LONG", qty)
            elif st_dir == "SELL":
                if size > 0:
                    self.client.close_position(self.product_id, size)
                if size == 0:
                    lev = 10
                    self.client.set_leverage(self.product_id, lev)
                    qty = self.client.calculate_order_size(self.product, price, lev)
                    self.client.market_entry(self.product_id, "SHORT", qty)

    def get_dashboard_info(self):
        self.prepare_product()
        pos = self.client.position(self.product_id) if self.product_id else {}
        balance = float(self.client.balance()) if self.prepare_product() else 0.0
        status = self.check_validity()

        return {
            "name": self.name,
            "starting_date": self.starting_date,
            "expiry_date": self.expiry_date,
            "validity_status": status,
            "status": status if status != "ACTIVE" else ("ACTIVE" if self.bot_running else "STOPPED"),
            "bot_running": self.bot_running and status == "ACTIVE",
            "balance": balance,
            "last_price": self.last_price or 0.0,
            "position": "LONG" if pos.get("size", 0) > 0 else ("SHORT" if pos.get("size", 0) < 0 else "FLAT"),
            "size": abs(pos.get("size", 0)),
            "entry_price": pos.get("entry_price", 0.0),
            "stop_loss": self.stop_loss or pos.get("stop_loss", 0.0),
            "unrealized_pnl": pos.get("unrealized_pnl", 0.0),
            "leverage": pos.get("leverage", 10)
        }


class MultiClientManager:
    def __init__(self):
        self.clients = {}
        self.load_clients()

    def load_clients(self):
        data = load_json(CLIENTS_FILE, [])
        for c in data:
            name = c.get("name")
            if name:
                self.clients[name] = ClientBotInstance(c)

    def save_clients(self):
        data = []
        for name, inst in self.clients.items():
            data.append({
                "name": inst.name,
                "api_key": inst.api_key,
                "api_secret": inst.api_secret,
                "starting_date": inst.starting_date,
                "expiry_date": inst.expiry_date,
                "bot_running": inst.bot_running
            })
        atomic_write(CLIENTS_FILE, data)

    def add_client(self, name, api_key, api_secret, starting_date, expiry_date):
        config = {
            "name": name,
            "api_key": api_key,
            "api_secret": api_secret,
            "starting_date": starting_date,
            "expiry_date": expiry_date,
            "bot_running": False
        }
        self.clients[name] = ClientBotInstance(config)
        self.save_clients()

    def get_all_summaries(self):
        return [inst.get_dashboard_info() for inst in self.clients.values()]

MANAGER = MultiClientManager()


class MultiClientHandler(SimpleHTTPRequestHandler):
    def log_message(self, format_string, *args):
        pass

    def send_json(self, payload, status=200):
        try:
            raw = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(raw)
        except Exception:
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/clients":
            self.send_json({"success": True, "clients": MANAGER.get_all_summaries()})
            return

        if path.startswith("/api/client/"):
            name = path.split("/")[-1]
            if name in MANAGER.clients:
                self.send_json({"success": True, "client": MANAGER.clients[name].get_dashboard_info()})
                return
            self.send_json({"success": False, "message": "Client not found"}, 404)
            return

        # Master Dashboard
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Master Admin Dashboard - Multi-Client Bot</title>
    <style>
        body { background: #0b0f19; color: #e2e8f0; font-family: Arial, sans-serif; padding: 20px; }
        .card { background: #111827; padding: 20px; border-radius: 8px; border: 1px solid #1f2937; margin-bottom: 20px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #1f2937; font-size: 14px; }
        th { color: #9ca3af; }
        input, button { padding: 10px; margin: 5px 0; border-radius: 4px; border: 1px solid #374151; background: #1f2937; color: white; }
        button { background: #10b981; cursor: pointer; font-weight: bold; }
        .btn-stop { background: #ef4444; }
        .badge { padding: 4px 8px; border-radius: 4px; font-size: 12px; }
        .active { background: #065f46; color: #34d399; }
        .stopped { background: #7f1d1d; color: #f87171; }
        .warning { background: #b45309; color: #fde68a; }
    </style>
</head>
<body>
    <h1>Master Admin Dashboard (All Clients)</h1>
    <div class="card">
        <h3>Add New Client</h3>
        <form onsubmit="addClient(event)">
            <input type="text" id="cname" placeholder="Client Name" required />
            <input type="text" id="ckey" placeholder="Delta API Key" required />
            <input type="password" id="csec" placeholder="Delta API Secret" required />
            <br>
            <label>Starting Date:</label>
            <input type="date" id="cstart" required />
            <label>Expiry Date:</label>
            <input type="date" id="cexpiry" required />
            <br>
            <button type="submit">Add Client</button>
        </form>
    </div>

    <div class="card">
        <h3>Active Clients List</h3>
        <table>
            <thead>
                <tr>
                    <th>Client Name</th>
                    <th>Start & Expiry Date</th>
                    <th>Status</th>
                    <th>Balance</th>
                    <th>Position</th>
                    <th>PnL</th>
                    <th>Dedicated URL</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody id="client-table">
                <tr><td colspan="8" style="text-align: center;">Loading...</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        async function fetchClients() {
            let res = await fetch('/api/clients');
            let data = await res.json();
            if (data.success) {
                let html = '';
                data.clients.forEach(c => {
                    let clientUrl = window.location.origin + '/client/' + encodeURIComponent(c.name);
                    let badgeClass = c.bot_running ? 'active' : (c.validity_status === 'EXPIRED' ? 'stopped' : 'warning');
                    html += `<tr>
                        <td><b>${c.name}</b></td>
                        <td>${c.starting_date} to ${c.expiry_date}</td>
                        <td><span class="badge ${badgeClass}">${c.status}</span></td>
                        <td>$${c.balance.toFixed(2)}</td>
                        <td>${c.position} (${c.size})</td>
                        <td class="${c.unrealized_pnl >= 0 ? 'active' : 'stopped'}">$${c.unrealized_pnl.toFixed(2)}</td>
                        <td><a href="${clientUrl}" target="_blank" style="color: #38bdf8;">Open Dashboard</a></td>
                        <td>
                            <button onclick="toggleBot('${c.name}', ${!c.bot_running})" class="${c.bot_running ? 'btn-stop' : ''}">${c.bot_running ? 'Stop' : 'Start'}</button>
                        </td>
                    </tr>`;
                });
                document.getElementById('client-table').innerHTML = html || '<tr><td colspan="8" style="text-align: center;">No clients added yet.</td></tr>';
            }
        }

        async function addClient(e) {
            e.preventDefault();
            let payload = {
                name: document.getElementById('cname').value,
                api_key: document.getElementById('ckey').value,
                api_secret: document.getElementById('csec').value,
                starting_date: document.getElementById('cstart').value,
                expiry_date: document.getElementById('cexpiry').value
            };
            let res = await fetch('/api/client/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            let data = await res.json();
            alert(data.message);
            fetchClients();
            e.target.reset();
        }

        async function toggleBot(name, start) {
            let endpoint = start ? '/api/client/start' : '/api/client/stop';
            await fetch(endpoint, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name})
            });
            fetchClients();
        }

        setInterval(fetchClients, 3000);
        fetchClients();
    </script>
</body>
</html>
"""
            self.wfile.write(html.encode("utf-8"))
            return

        # Dedicated Client URL
        if path.startswith("/client/"):
            name = path.split("/")[-1]
            if name in MANAGER.clients:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                client_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Client Dashboard - {name}</title>
    <style>
        body {{ background: #0b0f19; color: #e2e8f0; font-family: Arial, sans-serif; padding: 20px; }}
        .card {{ background: #111827; padding: 20px; border-radius: 8px; border: 1px solid #1f2937; max-width: 500px; margin: auto; }}
        .row {{ display: flex; justify-content: space-between; margin: 12px 0; font-size: 16px; }}
        button {{ padding: 12px; width: 100%; border-radius: 6px; border: none; font-weight: bold; cursor: pointer; color: white; margin-top: 15px; }}
        .btn-start {{ background: #10b981; }}
        .btn-stop {{ background: #ef4444; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>Client: {name}</h2>
        <div class="row"><span>Status:</span> <span id="status">-</span></div>
        <div class="row"><span>Validity:</span> <span id="dates"></span></div>
        <div class="row"><span>Balance:</span> <span id="balance">$0.00</span></div>
        <div class="row"><span>LTP:</span> <span id="ltp">$0.00</span></div>
        <div class="row"><span>Position:</span> <span id="pos">-</span></div>
        <div class="row"><span>Unrealized PnL:</span> <span id="pnl">$0.00</span></div>
        <button id="toggle-btn" onclick="toggleBot()"></button>
    </div>
    <script>
        const clientName = "{name}";
        let isRunning = false;

        async function fetchInfo() {{
            let res = await fetch('/api/client/' + clientName);
            let data = await res.json();
            if(data.success) {{
                let c = data.client;
                isRunning = c.bot_running;
                document.getElementById('status').innerText = c.status;
                document.getElementById('dates').innerText = c.starting_date + ' to ' + c.expiry_date;
                document.getElementById('balance').innerText = '$' + c.balance.toFixed(2);
                document.getElementById('ltp').innerText = '$' + c.last_price.toFixed(2);
                document.getElementById('pos').innerText = c.position + ' (' + c.size + ')';
                document.getElementById('pnl').innerText = '$' + c.unrealized_pnl.toFixed(2);

                let btn = document.getElementById('toggle-btn');
                if(c.validity_status === 'EXPIRED') {{
                    btn.innerText = "SUBSCRIPTION EXPIRED";
                    btn.className = "btn-stop";
                    btn.disabled = true;
                }} else if(c.validity_status === 'NOT_STARTED') {{
                    btn.innerText = "NOT STARTED YET";
                    btn.className = "btn-stop";
                    btn.disabled = true;
                }} else if(isRunning) {{
                    btn.innerText = "STOP BOT";
                    btn.className = "btn-stop";
                    btn.disabled = false;
                }} else {{
                    btn.innerText = "START BOT";
                    btn.className = "btn-start";
                    btn.disabled = false;
                }}
            }}
        }}

        async function toggleBot() {{
            let endpoint = isRunning ? '/api/client/stop' : '/api/client/start';
            await fetch(endpoint, {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{name: clientName}})
            }});
            fetchInfo();
        }}

        setInterval(fetchInfo, 2000);
        fetchInfo();
    </script>
</body>
</html>
"""
                self.wfile.write(client_html.encode("utf-8"))
                return
            self.send_response(404)
            self.end_headers()
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get('content-length', 0))
        body = json.loads(self.rfile.read(length).decode('utf-8')) if length > 0 else {}

        if path == "/api/client/add":
            name = body.get("name")
            key = body.get("api_key")
            sec = body.get("api_secret")
            start = body.get("starting_date")
            expiry = body.get("expiry_date")
            if not name or not key or not sec or not start or not expiry:
                self.send_json({"success": False, "message": "All fields including dates are required."})
                return
            MANAGER.add_client(name, key, sec, start, expiry)
            self.send_json({"success": True, "message": f"Client {name} added successfully."})
            return

        if path == "/api/client/start":
            name = body.get("name")
            if name in MANAGER.clients:
                res = MANAGER.clients[name].start_bot()
                MANAGER.save_clients()
                self.send_json(res)
                return
            self.send_json({"success": False, "message": "Client not found."})
            return

        if path == "/api/client/stop":
            name = body.get("name")
            if name in MANAGER.clients:
                res = MANAGER.clients[name].stop_bot()
                MANAGER.save_clients()
                self.send_json(res)
                return
            self.send_json({"success": False, "message": "Client not found."})
            return

        self.send_json({"success": False, "message": "Unknown endpoint."}, 404)


def websocket_loop():
    def on_message(ws, message):
        try:
            data = json.loads(message)
            item = data.get("payload") or data.get("data") or data
            symbol = item.get("symbol") or item.get("product_symbol")
            price = item.get("price") or item.get("last_price") or item.get("close")
            if symbol == SYMBOL and price is not None:
                p = float(price)
                try:
                    res = requests.get(f"{BASE_URL}/v2/history/candles", params={
                        "resolution": "1m", "symbol": SYMBOL,
                        "start": int(time.time()) - 6000, "end": int(time.time())
                    }, timeout=5).json()
                    candles = res.get("result", [])
                except Exception:
                    candles = []

                for client in MANAGER.clients.values():
                    try:
                        client.evaluate(p, candles)
                    except Exception as e:
                        logging.error("Error evaluating client %s: %s", client.name, e)
        except Exception:
            pass

    while True:
        try:
            ws = websocket.WebSocketApp(WS_URL, on_open=lambda w: w.send(json.dumps({
                "type": "subscribe", "payload": {"channels": [{"name": "trades", "symbols": [SYMBOL]}]}
            })), on_message=on_message)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            pass
        time.sleep(RECONNECT_SECONDS)


def main():
    if not acquire_single_process_lock():
        logging.error("Another instance is already running.")
        return

    get_public_ip()

    ws_thread = threading.Thread(target=websocket_loop, daemon=True)
    ws_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), MultiClientHandler)
    logging.info("Multi-Client Dashboard running on port %s", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()

if __name__ == "__main__":
    main()
