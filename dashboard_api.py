from flask import Flask, jsonify
import os
import json
import time
import hmac
import hashlib
import subprocess
import signal
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

SYMBOL = os.getenv("DELTA_SYMBOL", "XAUTUSD")
API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

BOT_FILE = "bot.py"
PID_FILE = "bot.pid"

session = requests.Session()

session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Dashboard"
})


# ============================================================
# DELTA API
# ============================================================

def sign_request(method, path, query_string="", body=""):
    timestamp = str(int(time.time()))

    message = (
        method.upper()
        + timestamp
        + path
        + query_string
        + body
    )

    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    return {
        "api-key": API_KEY,
        "signature": signature,
        "timestamp": timestamp
    }


def api_call(method, path, params=None, body=None, auth=False):
    params = params or {}

    body_text = (
        json.dumps(
            body,
            separators=(",", ":")
        )
        if body is not None
        else ""
    )

    query_string = (
        "?" + urlencode(params, doseq=True)
        if params
        else ""
    )

    headers = {}

    if auth:
        headers = sign_request(
            method,
            path,
            query_string,
            body_text
        )

    response = session.request(
        method.upper(),
        BASE_URL + path,
        params=params,
        data=body_text if body is not None else None,
        headers=headers,
        timeout=10
    )

    response.raise_for_status()

    data = response.json()

    if data.get("success") is False:
        raise RuntimeError(str(data))

    return data


# ============================================================
# PRODUCT / PRICE / POSITION
# ============================================================

def get_product():
    return api_call(
        "GET",
        f"/v2/products/{SYMBOL}"
    )["result"]


def get_price():
    data = api_call(
        "GET",
        f"/v2/tickers/{SYMBOL}"
    )

    ticker = data["result"]

    value = (
        ticker.get("close")
        or ticker.get("last_price")
        or ticker.get("mark_price")
    )

    return float(value) if value is not None else 0


def get_position():
    product = get_product()

    product_id = product["id"]

    data = api_call(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(product_id)
        },
        auth=True
    )

    result = data.get("result", {})

    if not isinstance(result, dict):
        return {
            "size": 0,
            "entry_price": None
        }

    return {
        "size": int(result.get("size", 0)),
        "entry_price": result.get("entry_price")
    }


def get_balance():
    data = api_call(
        "GET",
        "/v2/wallet/balances",
        auth=True
    )

    for wallet in data.get("result", []):
        asset = str(
            wallet.get("asset_symbol", "")
        ).upper()

        if asset in ("USD", "USDT"):
            value = (
                wallet.get("balance")
                or wallet.get("available_balance")
            )

            if value is not None:
                return float(value)

    return 0


# ============================================================
# BOT PROCESS
# ============================================================

def get_pid():
    if not os.path.exists(PID_FILE):
        return None

    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())

        os.kill(pid, 0)

        return pid

    except Exception:
        return None


def bot_running():
    return get_pid() is not None


def start_bot():
    if bot_running():
        return False, "Bot is already running."

    process = subprocess.Popen(
        ["python3", BOT_FILE],
        stdout=open("bot.log", "a"),
        stderr=open("bot.log", "a"),
        start_new_session=True
    )

    with open(PID_FILE, "w") as f:
        f.write(str(process.pid))

    return True, "Bot started."


# ============================================================
# STOP BOT
# ============================================================

def stop_bot_process():
    pid = get_pid()

    if not pid:
        return True, "Bot is already stopped."

    try:
        os.killpg(
            os.getpgid(pid),
            signal.SIGTERM
        )

    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    time.sleep(1)

    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)

    return True, "Bot stopped."


# ============================================================
# EXIT EXISTING POSITION
# ============================================================

def exit_position():
    product = get_product()

    product_id = int(product["id"])

    position = get_position()

    size = int(position["size"])

    if size == 0:
        return True, "No open position."

    # Remove protective stop before manual exit
    try:
        api_call(
            "DELETE",
            "/v2/orders/all",
            body={
                "product_id": product_id,
                "cancel_limit_orders": False,
                "cancel_stop_orders": True,
                "cancel_reduce_only_orders": False
            },
            auth=True
        )
    except Exception:
        pass

    side = "sell" if size > 0 else "buy"

    body = {
        "product_id": product_id,
        "product_symbol": SYMBOL,
        "size": abs(size),
        "side": side,
        "order_type": "market_order",
        "reduce_only": True,
        "client_order_id": (
            f"dashboard_exit_{int(time.time() * 1000)}"
        )[:32]
    }

    api_call(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )

    return True, "Existing position exited."


# ============================================================
# TRADE HISTORY
# ============================================================

def get_trade_history():
    product = get_product()

    product_id = int(product["id"])

    try:
        data = api_call(
            "GET",
            "/v2/fills",
            params={
                "product_id": product_id,
                "page_size": 100
            },
            auth=True
        )

        result = data.get("result", [])

        if isinstance(result, dict):
            result = [result]

        trades = []

        for fill in result:

            trades.append({
                "id": fill.get("id"),
                "timestamp": fill.get("created_at"),
                "side": fill.get("side"),
                "price": fill.get("price"),
                "size": fill.get("size"),
                "commission": fill.get("commission"),
                "pnl": fill.get("realized_pnl")
            })

        return trades

    except Exception:
        return []


# ============================================================
# DASHBOARD DATA
# ============================================================

@app.get("/api/dashboard")
def dashboard():

    try:
        price = get_price()

        position = get_position()

        balance = get_balance()

        size = position["size"]

        entry = position["entry_price"]

        unrealized_pnl = 0

        if size != 0 and entry is not None:

            entry = float(entry)

            if size > 0:
                unrealized_pnl = (
                    price - entry
                ) * abs(size)

            else:
                unrealized_pnl = (
                    entry - price
                ) * abs(size)

        trades = get_trade_history()

        total_pnl = 0
        winning = 0
        losing = 0

        for trade in trades:

            pnl = trade.get("pnl")

            if pnl is None:
                continue

            try:
                pnl = float(pnl)
            except Exception:
                continue

            total_pnl += pnl

            if pnl > 0:
                winning += 1

            elif pnl < 0:
                losing += 1

        total_trades = winning + losing

        win_rate = (
            (winning / total_trades) * 100
            if total_trades
            else 0
        )

        return jsonify({

            "success": True,

            "bot_running": bot_running(),

            "symbol": SYMBOL,

            "current_price": price,

            "balance": balance,

            "today_pnl": 0,

            "total_pnl": total_pnl,

            "position": {

                "direction": (
                    "LONG"
                    if size > 0
                    else "SHORT"
                    if size < 0
                    else "FLAT"
                ),

                "size": abs(size),

                "entry_price": entry,

                "stop_loss": None,

                "unrealized_pnl": unrealized_pnl
            },

            "statistics": {

                "total_trades": total_trades,

                "winning_trades": winning,

                "losing_trades": losing,

                "win_rate": win_rate
            },

            "trades": trades
        })

    except Exception as exc:

        return jsonify({
            "success": False,
            "error": str(exc)
        }), 500


# ============================================================
# START
# ============================================================

@app.post("/api/start")
def start():

    try:

        success, message = start_bot()

        return jsonify({
            "success": success,
            "message": message
        })

    except Exception as exc:

        return jsonify({
            "success": False,
            "message": str(exc)
        }), 500


# ============================================================
# STOP
# ============================================================

@app.post("/api/stop")
def stop():

    try:

        success, message = stop_bot_process()

        return jsonify({
            "success": success,
            "message": message
        })

    except Exception as exc:

        return jsonify({
            "success": False,
            "message": str(exc)
        }), 500


# ============================================================
# STOP + EXIT
# ============================================================

@app.post("/api/stop-exit")
def stop_exit():

    try:

        stop_bot_process()

        time.sleep(1)

        success, message = exit_position()

        return jsonify({
            "success": success,
            "message": message
        })

    except Exception as exc:

        return jsonify({
            "success": False,
            "message": str(exc)
        }), 500


# ============================================================
# SERVER
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=8000,
        debug=False
    )
