from flask import Flask, jsonify
from flask_cors import CORS
import os
import json
import time
import hmac
import hashlib
import subprocess
from decimal import Decimal
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv


# ============================================================
# XAUTUSD DASHBOARD API
# ============================================================

app = Flask(__name__)
CORS(app)


# ============================================================
# PATHS
# ============================================================

BASE_DIR = "/home/opc/xautusd-bot"

TRADE_HISTORY = os.path.join(
    BASE_DIR,
    "trade_history.json"
)

STATE_FILE = os.path.join(
    BASE_DIR,
    "xautusd_state.json"
)

ENV_FILE = os.path.join(
    BASE_DIR,
    ".env"
)

load_dotenv(ENV_FILE)


# ============================================================
# DELTA CONFIG
# ============================================================

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

SYMBOL = os.getenv(
    "DELTA_SYMBOL",
    "XAUTUSD"
)

API_KEY = os.getenv(
    "DELTA_API_KEY",
    ""
).strip()

API_SECRET = os.getenv(
    "DELTA_API_SECRET",
    ""
).strip()


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Dashboard/1.0"
})


# ============================================================
# HELPERS
# ============================================================

def decimal_value(value, default=None):

    if value is None:
        return default

    try:
        return Decimal(str(value))
    except Exception:
        return default


def json_number(value):

    if value is None:
        return None

    try:
        number = Decimal(str(value))

        if number == number.to_integral_value():
            return int(number)

        return float(number)

    except Exception:
        return None


# ============================================================
# BOT STATUS
# ============================================================

def get_bot_running():

    try:

        result = subprocess.run(
            [
                "pgrep",
                "-af",
                "/home/opc/xautusd-bot/bot.py"
            ],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return False

        lines = result.stdout.strip().splitlines()

        for line in lines:

            if "pgrep" not in line:
                return True

        return False

    except Exception:
        return False


# ============================================================
# DELTA AUTHENTICATION
# ============================================================

def sign_request(
    method,
    path,
    query_string="",
    body=""
):

    timestamp = str(
        int(time.time())
    )

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


# ============================================================
# DELTA API CALL
# ============================================================

def api_call(
    method,
    path,
    params=None,
    body=None,
    auth=False
):

    params = params or {}

    body_text = ""

    if body is not None:

        body_text = json.dumps(
            body,
            separators=(",", ":"),
            ensure_ascii=False
        )

    query_string = (
        "?" + urlencode(
            params,
            doseq=True
        )
        if params
        else ""
    )

    headers = {}

    if auth:

        if not API_KEY or not API_SECRET:

            raise RuntimeError(
                "Delta API credentials are missing."
            )

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
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if data.get("success") is False:

        raise RuntimeError(
            f"Delta API error: {data}"
        )

    return data


# ============================================================
# PRODUCT
# ============================================================

def get_product():

    data = api_call(
        "GET",
        f"/v2/products/{SYMBOL}"
    )

    return data.get(
        "result",
        {}
    )


# ============================================================
# LIVE PRICE
# ============================================================

def get_price():

    data = api_call(
        "GET",
        f"/v2/tickers/{SYMBOL}"
    )

    ticker = data.get(
        "result",
        {}
    )

    value = (
        ticker.get("close")
        or ticker.get("last_price")
        or ticker.get("mark_price")
    )

    if value is None:

        raise RuntimeError(
            "Delta ticker returned no price."
        )

    return Decimal(
        str(value)
    )


# ============================================================
# LIVE POSITION
# ============================================================

def get_position(
    product_id
):

    data = api_call(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(product_id)
        },
        auth=True
    )

    result = data.get(
        "result"
    )

    if not result:

        return {
            "size": 0,
            "entry_price": None
        }

    if not isinstance(
        result,
        dict
    ):

        return {
            "size": 0,
            "entry_price": None
        }

    return {
        "size": int(
            result.get(
                "size",
                0
            )
        ),
        "entry_price":
            result.get(
                "entry_price"
            )
    }


# ============================================================
# LIVE BALANCE
# ============================================================

def get_balance():

    data = api_call(
        "GET",
        "/v2/wallet/balances",
        auth=True
    )

    for wallet in data.get(
        "result",
        []
    ):

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
                wallet.get("balance")
                or wallet.get(
                    "available_balance"
                )
            )

            if value is not None:

                return Decimal(
                    str(value)
                )

    meta = data.get(
        "meta",
        {}
    )

    net_equity = meta.get(
        "net_equity"
    )

    if net_equity is not None:

        return Decimal(
            str(net_equity)
        )

    return None


# ============================================================
# STATE FILE
# ============================================================

def load_state():

    try:

        if not os.path.exists(
            STATE_FILE
        ):

            return {}

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        if isinstance(
            data,
            dict
        ):

            return data

        return {}

    except Exception:

        return {}


# ============================================================
# TRADE HISTORY
# ============================================================

def load_trades():

    try:

        if not os.path.exists(
            TRADE_HISTORY
        ):

            return []

        with open(
            TRADE_HISTORY,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        if isinstance(
            data,
            dict
        ):

            trades = data.get(
                "trades",
                []
            )

            if isinstance(
                trades,
                list
            ):

                return trades

        if isinstance(
            data,
            list
        ):

            return data

        return []

    except Exception:

        return []


# ============================================================
# TRADE P&L HELPERS
# ============================================================

def trade_pnl(trade):

    possible_keys = [
        "pnl",
        "realized_pnl",
        "realised_pnl",
        "profit",
        "profit_loss"
    ]

    for key in possible_keys:

        value = trade.get(
            key
        )

        if value is not None:

            return decimal_value(
                value,
                Decimal("0")
            )

    return Decimal("0")


def calculate_trade_statistics(
    trades
):

    total_trades = len(
        trades
    )

    winning = 0
    losing = 0

    total_pnl = Decimal("0")

    for trade in trades:

        pnl = trade_pnl(
            trade
        )

        total_pnl += pnl

        if pnl > 0:

            winning += 1

        elif pnl < 0:

            losing += 1

    closed_trades = (
        winning + losing
    )

    if closed_trades > 0:

        win_rate = (
            Decimal(winning)
            / Decimal(closed_trades)
            * Decimal("100")
        )

    else:

        win_rate = Decimal("0")

    return {
        "total_trades":
            total_trades,

        "winning_trades":
            winning,

        "losing_trades":
            losing,

        "win_rate":
            json_number(
                win_rate
            ),

        "total_pnl":
            json_number(
                total_pnl
            )
    }


# ============================================================
# TODAY P&L
# ============================================================

def get_today_pnl(
    trades
):

    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).date()

    total = Decimal("0")

    for trade in trades:

        timestamp = (
            trade.get("timestamp")
            or trade.get("time")
            or trade.get("created_at")
            or trade.get("createdAt")
        )

        if not timestamp:

            continue

        try:

            if isinstance(
                timestamp,
                (int, float)
            ):

                trade_date = (
                    datetime.fromtimestamp(
                        timestamp,
                        tz=ZoneInfo(
                            "Asia/Kolkata"
                        )
                    ).date()
                )

            else:

                text = str(
                    timestamp
                )

                trade_date = (
                    datetime.fromisoformat(
                        text.replace(
                            "Z",
                            "+00:00"
                        )
                    )
                    .astimezone(
                        ZoneInfo(
                            "Asia/Kolkata"
                        )
                    )
                    .date()
                )

            if trade_date == today:

                total += trade_pnl(
                    trade
                )

        except Exception:

            continue

    return total


# ============================================================
# UNREALIZED P&L
# ============================================================

def calculate_unrealized_pnl(
    size,
    entry_price,
    current_price
):

    if not size:
        return Decimal("0")

    entry = decimal_value(
        entry_price
    )

    current = decimal_value(
        current_price
    )

    if entry is None or current is None:

        return Decimal("0")

    # XAUTUSD position P&L is represented
    # approximately as price difference × size.
    if size > 0:

        return (
            current - entry
        ) * Decimal(
            abs(size)
        )

    return (
        entry - current
    ) * Decimal(
        abs(size)
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/api/dashboard")
def dashboard():

    errors = []

    # --------------------------------------------------------
    # BOT
    # --------------------------------------------------------

    bot_running = get_bot_running()


    # --------------------------------------------------------
    # TRADES
    # --------------------------------------------------------

    trades = load_trades()

    statistics = (
        calculate_trade_statistics(
            trades
        )
    )

    today_pnl = get_today_pnl(
        trades
    )


    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    state = load_state()

    state_sl = decimal_value(
        state.get(
            "current_sl"
        )
    )


    # --------------------------------------------------------
    # PRODUCT
    # --------------------------------------------------------

    product = {}

    try:

        product = get_product()

    except Exception as exc:

        errors.append(
            f"Product: {exc}"
        )


    product_id = product.get(
        "id"
    )


    # --------------------------------------------------------
    # LIVE PRICE
    # --------------------------------------------------------

    current_price = None

    try:

        current_price = get_price()

    except Exception as exc:

        errors.append(
            f"Price: {exc}"
        )


    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    balance = None

    try:

        balance = get_balance()

    except Exception as exc:

        errors.append(
            f"Balance: {exc}"
        )


    # --------------------------------------------------------
    # POSITION
    # --------------------------------------------------------

    raw_position = {
        "size": 0,
        "entry_price": None
    }

    if product_id is not None:

        try:

            raw_position = get_position(
                product_id
            )

        except Exception as exc:

            errors.append(
                f"Position: {exc}"
            )


    size = int(
        raw_position.get(
            "size",
            0
        ) or 0
    )

    entry_price = raw_position.get(
        "entry_price"
    )


    # --------------------------------------------------------
    # DIRECTION
    # --------------------------------------------------------

    if size > 0:

        direction = "LONG"

    elif size < 0:

        direction = "SHORT"

    else:

        direction = "FLAT"


    # --------------------------------------------------------
    # STOP LOSS
    # --------------------------------------------------------

    stop_loss = state_sl


    # --------------------------------------------------------
    # UNREALIZED P&L
    # --------------------------------------------------------

    unrealized_pnl = (
        calculate_unrealized_pnl(
            size,
            entry_price,
            current_price
        )
    )


    # --------------------------------------------------------
    # TOTAL P&L
    # --------------------------------------------------------

    total_pnl = statistics[
        "total_pnl"
    ]

    if total_pnl is None:

        total_pnl = 0


    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    response = {

        "success": True,

        "bot_running":
            bot_running,

        "symbol":
            SYMBOL,

        "current_price":
            json_number(
                current_price
            ),

        "balance":
            json_number(
                balance
            ),

        "today_pnl":
            json_number(
                today_pnl
            ),

        "total_pnl":
            total_pnl,

        "position": {

            "direction":
                direction,

            "size":
                abs(size),

            "entry_price":
                json_number(
                    entry_price
                ),

            "stop_loss":
                json_number(
                    stop_loss
                ),

            "unrealized_pnl":
                json_number(
                    unrealized_pnl
                )
        },

        "statistics": {

            "total_trades":
                statistics[
                    "total_trades"
                ],

            "winning_trades":
                statistics[
                    "winning_trades"
                ],

            "losing_trades":
                statistics[
                    "losing_trades"
                ],

            "win_rate":
                statistics[
                    "win_rate"
                ]
        },

        "trades":
            trades,

        "errors":
            errors
    }


    return jsonify(
        response
    )


# ============================================================
# START BOT
# ============================================================

@app.post("/api/start")
def start_bot():

    return jsonify({

        "success": True,

        "message":
            "START command received."

    })


# ============================================================
# STOP BOT
# ============================================================

@app.post("/api/stop")
def stop_bot():

    return jsonify({

        "success": True,

        "message":
            "STOP command received."

    })


# ============================================================
# HEALTH
# ============================================================

@app.get("/api/health")
def health():

    return jsonify({

        "status":
            "ok",

        "bot_running":
            get_bot_running(),

        "symbol":
            SYMBOL

    })


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=8000,
        debug=False
            )
