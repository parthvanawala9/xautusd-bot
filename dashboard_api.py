import os
import time
import hmac
import hashlib
import json
import subprocess
from pathlib import Path

import requests
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv


# ============================================================
# XAUTUSD DASHBOARD API
# ============================================================
#
# This file is located in the ROOT of the repository:
#
# /home/opc/xautusd-bot/dashboard_api.py
#
# It works independently from bot.py.
#
# Dashboard:
#   http://127.0.0.1:8000
#
# API:
#   /api/health
#   /api/dashboard
#
# ============================================================


# ============================================================
# BASE DIRECTORY
# ============================================================

BASE_DIR = Path(__file__).resolve().parent


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv(BASE_DIR / ".env")


API_KEY = os.getenv(
    "DELTA_API_KEY",
    ""
).strip()

API_SECRET = os.getenv(
    "DELTA_API_SECRET",
    ""
).strip()

SYMBOL = os.getenv(
    "DELTA_SYMBOL",
    "XAUTUSD"
).strip()

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")


PORT = int(
    os.getenv(
        "DASHBOARD_PORT",
        "8000"
    )
)


# ============================================================
# FILES
# ============================================================

STATE_FILE = BASE_DIR / "xautusd_state.json"

TRADE_HISTORY_FILE = BASE_DIR / "trade_history.json"

DASHBOARD_JSON_FILE = BASE_DIR / "dashboard.json"

INDEX_FILE = BASE_DIR / "index.html"

STYLE_FILE = BASE_DIR / "style.css"


# ============================================================
# FLASK
# ============================================================

app = Flask(
    __name__,
    static_folder=None
)

CORS(app)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Dashboard"
})


# ============================================================
# DELTA SIGNATURE
# ============================================================

def generate_signature(
    method,
    endpoint,
    payload="",
    timestamp=""
):

    message = (
        method.upper()
        + timestamp
        + endpoint
        + payload
    )

    return hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


# ============================================================
# DELTA HEADERS
# ============================================================

def get_headers(
    method,
    endpoint,
    payload=""
):

    timestamp = str(
        int(time.time())
    )

    signature = generate_signature(
        method,
        endpoint,
        payload,
        timestamp
    )

    return {
        "api-key": API_KEY,
        "signature": signature,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }


# ============================================================
# SAFE JSON REQUEST
# ============================================================

def delta_get(
    endpoint,
    authenticated=False,
    timeout=10
):

    try:

        headers = {}

        if authenticated:

            headers = get_headers(
                "GET",
                endpoint
            )

        response = session.get(
            BASE_URL + endpoint,
            headers=headers,
            timeout=timeout
        )

        response.raise_for_status()

        data = response.json()

        if data.get("success") is False:

            return None

        return data

    except Exception:

        return None


# ============================================================
# LIVE BALANCE
# ============================================================

def fetch_account_balance():

    if not API_KEY or not API_SECRET:

        return None

    data = delta_get(
        "/v2/wallet/balances",
        authenticated=True
    )

    if not data:

        return None

    result = data.get(
        "result",
        []
    )

    if isinstance(result, dict):

        result = [result]

    if not isinstance(result, list):

        return None

    # --------------------------------------------------------
    # Prefer USD / USDT
    # --------------------------------------------------------

    for wallet in result:

        asset = str(
            wallet.get(
                "asset_symbol",
                ""
            )
        ).upper()

        if asset in (
            "USD",
            "USDT"
        ):

            value = (
                wallet.get(
                    "available_balance"
                )
                if wallet.get(
                    "available_balance"
                ) is not None
                else wallet.get(
                    "balance"
                )
            )

            try:

                return float(
                    value or 0
                )

            except Exception:

                return 0.0

    # --------------------------------------------------------
    # Fallback
    # --------------------------------------------------------

    if result:

        try:

            value = result[0].get(
                "balance",
                0
            )

            return float(
                value or 0
            )

        except Exception:

            return 0.0

    return None


# ============================================================
# LIVE PRICE
# ============================================================

def fetch_ticker_price():

    data = delta_get(
        f"/v2/tickers/{SYMBOL}",
        authenticated=False
    )

    if not data:

        return 0.0

    result = data.get(
        "result",
        {}
    )

    if not isinstance(
        result,
        dict
    ):

        return 0.0

    # Delta can expose different price fields.
    # Prefer close, then mark/last variants.

    for key in (
        "close",
        "mark_price",
        "last_price",
        "price"
    ):

        value = result.get(
            key
        )

        if value is not None:

            try:

                return float(
                    value
                )

            except Exception:

                pass

    return 0.0


# ============================================================
# LIVE POSITION
# ============================================================

def normalize_position(
    position
):

    if not isinstance(
        position,
        dict
    ):

        return None

    product_symbol = str(
        position.get(
            "product_symbol",
            ""
        )
    ).upper()

    size_raw = position.get(
        "size",
        0
    )

    try:

        size = float(
            size_raw or 0
        )

    except Exception:

        size = 0.0

    # --------------------------------------------------------
    # Only XAUTUSD
    # --------------------------------------------------------

    if (
        product_symbol
        and product_symbol != SYMBOL.upper()
        and "XAUT" not in product_symbol
    ):

        return None

    if size == 0:

        return {
            "direction": "FLAT",
            "size": 0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "unrealized_pnl": 0.0
        }

    direction = (
        "LONG"
        if size > 0
        else "SHORT"
    )

    def number(
        value,
        default=0.0
    ):

        try:

            return float(
                value or default
            )

        except Exception:

            return default

    return {
        "direction": direction,
        "size": abs(size),
        "entry_price": number(
            position.get(
                "entry_price"
            )
        ),
        "stop_loss": number(
            position.get(
                "stop_loss"
            )
        ),
        "unrealized_pnl": number(
            position.get(
                "unrealized_pnl"
            )
        )
    }


def fetch_live_position():

    if not API_KEY or not API_SECRET:

        return None

    # --------------------------------------------------------
    # First try the same endpoint used by bot.py
    # --------------------------------------------------------

    endpoints = [
        "/v2/positions"
    ]

    # Some Delta accounts/API versions expose this endpoint.
    endpoints.append(
        "/v2/positions/margined"
    )

    for endpoint in endpoints:

        data = delta_get(
            endpoint,
            authenticated=True
        )

        if not data:

            continue

        result = data.get(
            "result"
        )

        # ----------------------------------------------------
        # Result can be a dictionary
        # ----------------------------------------------------

        if isinstance(
            result,
            dict
        ):

            normalized = normalize_position(
                result
            )

            if normalized is not None:

                return normalized

            # Sometimes result contains nested positions.
            possible_positions = (
                result.get("positions")
                or result.get("data")
                or []
            )

            if isinstance(
                possible_positions,
                list
            ):

                for position in possible_positions:

                    normalized = normalize_position(
                        position
                    )

                    if (
                        normalized is not None
                        and normalized["direction"]
                        != "FLAT"
                    ):

                        return normalized

                return {
                    "direction": "FLAT",
                    "size": 0,
                    "entry_price": 0.0,
                    "stop_loss": 0.0,
                    "unrealized_pnl": 0.0
                }

        # ----------------------------------------------------
        # Result can be a list
        # ----------------------------------------------------

        if isinstance(
            result,
            list
        ):

            found_flat = False

            for position in result:

                normalized = normalize_position(
                    position
                )

                if normalized is None:

                    continue

                if normalized["direction"] != "FLAT":

                    return normalized

                found_flat = True

            if found_flat:

                return {
                    "direction": "FLAT",
                    "size": 0,
                    "entry_price": 0.0,
                    "stop_loss": 0.0,
                    "unrealized_pnl": 0.0
                }

    return None


# ============================================================
# READ JSON FILE
# ============================================================

def read_json_file(
    path
):

    try:

        if not path.exists():

            return {}

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(
                file
            )

    except Exception:

        return {}


# ============================================================
# LOCAL BOT STATE
# ============================================================

def fetch_local_state():

    state = read_json_file(
        STATE_FILE
    )

    if not isinstance(
        state,
        dict
    ):

        return {}

    return state


# ============================================================
# TRADE HISTORY
# ============================================================

def fetch_trade_history():

    data = read_json_file(
        TRADE_HISTORY_FILE
    )

    if isinstance(
        data,
        list
    ):

        return data

    if isinstance(
        data,
        dict
    ):

        trades = data.get(
            "trades"
        )

        if isinstance(
            trades,
            list
        ):

            return trades

    return []


# ============================================================
# BOT PROCESS STATUS
# ============================================================

def check_bot_process():

    try:

        result = subprocess.run(
            [
                "pgrep",
                "-af",
                "xautusd-bot/bot.py"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        output = (
            result.stdout
            or ""
        ).strip()

        if output:

            return {
                "running": True,
                "process": output
            }

    except Exception:

        pass

    # --------------------------------------------------------
    # Fallback: systemctl
    # --------------------------------------------------------

    try:

        result = subprocess.run(
            [
                "systemctl",
                "is-active",
                "xautusd-bot.service"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        status = (
            result.stdout
            or ""
        ).strip()

        if status == "active":

            return {
                "running": True,
                "process": "xautusd-bot.service active"
            }

    except Exception:

        pass

    return {
        "running": False,
        "process": ""
    }


# ============================================================
# BOT SERVICE STATUS
# ============================================================

def fetch_service_status():

    try:

        result = subprocess.run(
            [
                "systemctl",
                "is-active",
                "xautusd-bot.service"
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        status = (
            result.stdout
            or ""
        ).strip()

        return status

    except Exception:

        return "unknown"


# ============================================================
# DASHBOARD DATA
# ============================================================

def build_dashboard():

    local_state = fetch_local_state()

    trades = fetch_trade_history()

    if not isinstance(
        trades,
        list
    ):

        trades = []

    # --------------------------------------------------------
    # Live Delta data
    # --------------------------------------------------------

    current_price = (
        fetch_ticker_price()
    )

    live_balance = (
        fetch_account_balance()
    )

    live_position = (
        fetch_live_position()
    )

    # --------------------------------------------------------
    # Bot process
    # --------------------------------------------------------

    bot_status = (
        check_bot_process()
    )

    service_status = (
        fetch_service_status()
    )

    # --------------------------------------------------------
    # HIGH / LOW / SL
    # --------------------------------------------------------

    def state_number(
        key
    ):

        value = local_state.get(
            key
        )

        if value is None:

            return 0.0

        try:

            return float(
                value
            )

        except Exception:

            return 0.0

    running_high = state_number(
        "running_high"
    )

    running_low = state_number(
        "running_low"
    )

    current_sl = state_number(
        "current_sl"
    )

    trade_high = state_number(
        "trade_high"
    )

    trade_low = state_number(
        "trade_low"
    )

    # --------------------------------------------------------
    # Position fallback
    # --------------------------------------------------------

    if live_position is None:

        local_position = (
            local_state.get(
                "position"
            )
        )

        if isinstance(
            local_position,
            dict
        ):

            live_position = {
                "direction":
                    local_position.get(
                        "direction",
                        "FLAT"
                    ),

                "size":
                    float(
                        local_position.get(
                            "size",
                            0
                        )
                        or 0
                    ),

                "entry_price":
                    float(
                        local_position.get(
                            "entry_price",
                            0
                        )
                        or 0
                    ),

                "stop_loss":
                    float(
                        local_position.get(
                            "stop_loss",
                            0
                        )
                        or 0
                    ),

                "unrealized_pnl":
                    float(
                        local_position.get(
                            "unrealized_pnl",
                            0
                        )
                        or 0
                    )
            }

        else:

            live_position = {
                "direction": "FLAT",
                "size": 0,
                "entry_price": 0.0,
                "stop_loss": 0.0,
                "unrealized_pnl": 0.0
            }

    # --------------------------------------------------------
    # Balance fallback
    # --------------------------------------------------------

    if live_balance is None:

        try:

            live_balance = float(
                local_state.get(
                    "balance",
                    0
                )
                or 0
            )

        except Exception:

            live_balance = 0.0

    # --------------------------------------------------------
    # PNL
    # --------------------------------------------------------

    total_pnl = 0.0

    winning_trades = 0

    losing_trades = 0

    for trade in trades:

        if not isinstance(
            trade,
            dict
        ):

            continue

        try:

            pnl = float(
                trade.get(
                    "pnl",
                    0
                )
                or 0
            )

        except Exception:

            pnl = 0.0

        total_pnl += pnl

        if pnl > 0:

            winning_trades += 1

        elif pnl < 0:

            losing_trades += 1

    total_trades = len(
        trades
    )

    if total_trades > 0:

        win_rate = (
            winning_trades
            / total_trades
            * 100
        )

    else:

        win_rate = 0.0

    # --------------------------------------------------------
    # API response
    # --------------------------------------------------------

    return {
        "success": True,

        "timestamp": int(
            time.time()
        ),

        "bot": {
            "running":
                bool(
                    bot_status[
                        "running"
                    ]
                ),

            "service":
                service_status,

            "process":
                bot_status.get(
                    "process",
                    ""
                )
        },

        "symbol":
            SYMBOL,

        "current_price":
            current_price,

        "balance":
            live_balance,

        "total_pnl":
            round(
                total_pnl,
                4
            ),

        "today_pnl":
            round(
                total_pnl,
                4
            ),

        "position":
            live_position,

        "strategy": {
            "high":
                running_high,

            "low":
                running_low,

            "stop_loss":
                current_sl,

            "trade_high":
                trade_high,

            "trade_low":
                trade_low,

            "day_start":
                local_state.get(
                    "day_start"
                ),

            "session_ready":
                bool(
                    local_state.get(
                        "session_ready",
                        False
                    )
                )
        },

        "statistics": {
            "total_trades":
                total_trades,

            "winning_trades":
                winning_trades,

            "losing_trades":
                losing_trades,

            "win_rate":
                round(
                    win_rate,
                    1
                )
        },

        "trades":
            trades
    }


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/api/health",
    methods=["GET"]
)
def health():

    bot_status = (
        check_bot_process()
    )

    return jsonify({

        "success": True,

        "dashboard": "running",

        "bot_running":
            bot_status[
                "running"
            ],

        "bot_service":
            fetch_service_status(),

        "symbol":
            SYMBOL,

        "port":
            PORT,

        "state_file":
            STATE_FILE.exists(),

        "trade_history_file":
            TRADE_HISTORY_FILE.exists(),

        "index_file":
            INDEX_FILE.exists()
    })


# ============================================================
# DASHBOARD API
# ============================================================

@app.route(
    "/api/dashboard",
    methods=["GET"]
)
def dashboard():

    try:

        return jsonify(
            build_dashboard()
        )

    except Exception as exc:

        return jsonify({

            "success": False,

            "error":
                str(exc)

        }), 500


# ============================================================
# ROOT
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def index():

    if not INDEX_FILE.exists():

        return (
            "Dashboard index.html not found.",
            404
        )

    return send_from_directory(
        BASE_DIR,
        "index.html"
    )


# ============================================================
# STATIC CSS
# ============================================================

@app.route(
    "/style.css",
    methods=["GET"]
)
def style_css():

    if not STYLE_FILE.exists():

        return (
            "style.css not found.",
            404
        )

    return send_from_directory(
        BASE_DIR,
        "style.css"
    )


# ============================================================
# OPTIONAL DASHBOARD JSON
# ============================================================

@app.route(
    "/dashboard.json",
    methods=["GET"]
)
def dashboard_json():

    if not DASHBOARD_JSON_FILE.exists():

        return jsonify({
            "success": False,
            "error": "dashboard.json not found"
        }), 404

    data = read_json_file(
        DASHBOARD_JSON_FILE
    )

    return jsonify(
        data
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print(
        "========================================"
    )

    print(
        "XAUTUSD DASHBOARD API"
    )

    print(
        "========================================"
    )

    print(
        f"BASE DIR = {BASE_DIR}"
    )

    print(
        f"SYMBOL   = {SYMBOL}"
    )

    print(
        f"PORT     = {PORT}"
    )

    print(
        f"STATE    = {STATE_FILE}"
    )

    print(
        f"TRADES   = {TRADE_HISTORY_FILE}"
    )

    print(
        "========================================"
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        threaded=True
    )
