import os
import time
import json
import hmac
import hashlib
import subprocess
from decimal import Decimal
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify
from flask_cors import CORS
from dotenv import load_dotenv


# ============================================================
# XAUTUSD DASHBOARD API
#
# IMPORTANT:
# This file ONLY READS the bot/account.
# It does NOT start, stop, modify, or restart bot.py.
# ============================================================

load_dotenv()


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

SYMBOL = os.getenv(
    "DELTA_SYMBOL",
    "XAUTUSD"
).strip()

API_KEY = os.getenv(
    "DELTA_API_KEY",
    ""
).strip()

API_SECRET = os.getenv(
    "DELTA_API_SECRET",
    ""
).strip()

BOT_DIR = "/home/opc/xautusd-bot"

STATE_FILE = os.getenv(
    "STATE_FILE",
    os.path.join(
        BOT_DIR,
        "xautusd_state.json"
    )
)

BOT_FILE = os.path.join(
    BOT_DIR,
    "bot.py"
)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": "*"
        }
    }
)


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

    if isinstance(value, Decimal):
        return float(value)

    return value


def now_ist():
    return datetime.now(IST)


def iso_ist(dt):
    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(IST).isoformat()


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
# DELTA REQUEST
# ============================================================

def delta_request(
    method,
    path,
    params=None,
    authenticated=False
):

    params = params or {}

    query_string = (
        "?"
        + urlencode(
            params,
            doseq=True
        )
        if params
        else ""
    )

    headers = {}

    if authenticated:

        if not API_KEY or not API_SECRET:
            raise RuntimeError(
                "Delta API credentials are missing."
            )

        headers = sign_request(
            method,
            path,
            query_string,
            ""
        )

    response = session.request(
        method.upper(),
        BASE_URL + path,
        params=params,
        headers=headers,
        timeout=15
    )

    if not response.ok:
        raise RuntimeError(
            f"Delta API HTTP "
            f"{response.status_code}: "
            f"{response.text[:500]}"
        )

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

    data = delta_request(
        "GET",
        f"/v2/products/{SYMBOL}"
    )

    return data.get(
        "result",
        {}
    )


# ============================================================
# LIVE TICKER
# ============================================================

def get_ticker():

    data = delta_request(
        "GET",
        f"/v2/tickers/{SYMBOL}"
    )

    return data.get(
        "result",
        {}
    )


def get_current_price():

    ticker = get_ticker()

    for key in (
        "close",
        "last_price",
        "mark_price",
        "spot_price"
    ):

        value = ticker.get(key)

        if value not in (
            None,
            ""
        ):

            return decimal_value(
                value
            )

    return None


# ============================================================
# BALANCE
# ============================================================

def get_balance_data():

    data = delta_request(
        "GET",
        "/v2/wallet/balances",
        authenticated=True
    )

    wallets = data.get(
        "result",
        []
    )

    if isinstance(wallets, dict):
        wallets = [wallets]

    # Prefer USD / USDT.
    for wallet in wallets:

        asset = str(
            wallet.get(
                "asset_symbol",
                ""
            )
        ).upper()

        if asset not in (
            "USD",
            "USDT"
        ):
            continue

        balance = (
            wallet.get("balance")
            if wallet.get("balance") is not None
            else wallet.get(
                "available_balance"
            )
        )

        if balance not in (
            None,
            ""
        ):

            return {
                "balance": decimal_value(
                    balance
                ),
                "available_balance": decimal_value(
                    wallet.get(
                        "available_balance"
                    )
                ),
                "asset": asset,
                "raw": wallet
            }

    # Fallback.
    meta = data.get(
        "meta",
        {}
    )

    net_equity = meta.get(
        "net_equity"
    )

    if net_equity not in (
        None,
        ""
    ):

        return {
            "balance": decimal_value(
                net_equity
            ),
            "available_balance": None,
            "asset": "EQUITY",
            "raw": meta
        }

    raise RuntimeError(
        "USD/USDT balance was not found."
    )


# ============================================================
# POSITION
# ============================================================

def get_position_data(
    product_id
):

    data = delta_request(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(
                product_id
            )
        },
        authenticated=True
    )

    result = data.get(
        "result"
    )

    if not result:
        return {
            "size": 0,
            "entry_price": None,
            "raw": {}
        }

    return {
        "size": int(
            result.get(
                "size",
                0
            )
        ),
        "entry_price": decimal_value(
            result.get(
                "entry_price"
            )
        ),
        "raw": result
    }


# ============================================================
# BOT STATE FILE
# ============================================================

def load_bot_state():

    candidates = [
        STATE_FILE,
        os.path.join(
            BOT_DIR,
            "xautusd_state.json"
        ),
        os.path.join(
            BOT_DIR,
            "dashboard",
            "xautusd_state.json"
        )
    ]

    for filename in candidates:

        try:

            if not os.path.exists(
                filename
            ):
                continue

            with open(
                filename,
                "r",
                encoding="utf-8"
            ) as file:

                state = json.load(
                    file
                )

            if isinstance(
                state,
                dict
            ):
                return state

        except Exception:
            continue

    return {}


# ============================================================
# BOT PROCESS CHECK
#
# READ ONLY.
# NEVER STARTS OR STOPS BOT.
# ============================================================

def is_bot_running():

    try:

        result = subprocess.run(
            [
                "pgrep",
                "-af",
                BOT_FILE
            ],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return False

        lines = []

        for line in result.stdout.splitlines():

            if (
                "pgrep" in line
                or "dashboard_api.py" in line
            ):
                continue

            lines.append(
                line
            )

        return len(lines) > 0

    except Exception:

        return False


# ============================================================
# STOP LOSS
# ============================================================

def get_stop_loss(
    state
):

    # Strategy state.
    for key in (
        "current_sl",
        "stop_loss",
        "active_stop_loss"
    ):

        value = state.get(
            key
        )

        if value not in (
            None,
            ""
        ):

            return decimal_value(
                value
            )

    # Look for an active stop order as a second source.
    return None


# ============================================================
# OPEN STOP ORDER
# ============================================================

def get_open_stop_order(
    product_id
):

    try:

        data = delta_request(
            "GET",
            "/v2/orders",
            params={
                "product_ids": str(
                    product_id
                ),
                "states": "open,pending",
                "order_types": "all_stop"
            },
            authenticated=True
        )

        result = data.get(
            "result",
            []
        )

        if isinstance(
            result,
            dict
        ):
            result = [result]

        if not result:
            return None

        # Prefer stop-loss orders.
        for order in result:

            stop_type = str(
                order.get(
                    "stop_order_type",
                    ""
                )
            ).lower()

            if "stop" in stop_type:
                return order

        return result[0]

    except Exception:

        return None


# ============================================================
# UNREALIZED P&L
# ============================================================

def calculate_unrealized_pnl(
    position,
    current_price,
    product
):

    size = int(
        position.get(
            "size",
            0
        )
    )

    entry = position.get(
        "entry_price"
    )

    if (
        size == 0
        or entry is None
        or current_price is None
    ):
        return Decimal("0")

    entry = decimal_value(
        entry
    )

    if entry is None:
        return Decimal("0")

    contract_value = None

    for key in (
        "contract_value",
        "contract_value_usd",
        "contract_unit_value"
    ):

        value = product.get(
            key
        )

        if value not in (
            None,
            ""
        ):

            contract_value = decimal_value(
                value
            )

            if (
                contract_value is not None
                and contract_value > 0
            ):
                break

    if (
        contract_value is None
        or contract_value <= 0
    ):
        contract_value = Decimal("1")

    # Long = positive size.
    # Short = negative size.
    return (
        Decimal(size)
        * (
            current_price
            - entry
        )
        * contract_value
    )


# ============================================================
# FILLS
# ============================================================

def get_fills():

    data = delta_request(
        "GET",
        "/v2/fills",
        params={
            "product_ids": None
        },
        authenticated=True
    )

    result = data.get(
        "result",
        []
    )

    if isinstance(
        result,
        dict
    ):
        result = [result]

    return result


# ============================================================
# SAFE FILL REQUEST
# ============================================================

def get_xautusd_fills(
    product_id
):

    try:

        data = delta_request(
            "GET",
            "/v2/fills",
            params={
                "product_ids": str(
                    product_id
                ),
                "page_size": 50
            },
            authenticated=True
        )

        result = data.get(
            "result",
            []
        )

        if isinstance(
            result,
            dict
        ):
            result = [result]

        return result

    except Exception:

        return []


# ============================================================
# REALIZED P&L FROM FILLS
#
# FIFO calculation.
# Used for dashboard history.
# ============================================================

def calculate_fill_statistics(
    fills,
    product
):

    if not fills:

        return {
            "realized_pnl
