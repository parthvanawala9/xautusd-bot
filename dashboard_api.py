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
# READ-ONLY DASHBOARD
#
# IMPORTANT:
# This file NEVER starts, stops, restarts, modifies,
# or controls bot.py.
#
# It only reads:
#   - Delta account
#   - Delta market data
#   - Delta position
#   - Delta fills
#   - existing bot state file
# ============================================================


# ============================================================
# PATHS
# ============================================================

BOT_DIR = "/home/opc/xautusd-bot"

DASHBOARD_DIR = os.path.join(
    BOT_DIR,
    "dashboard"
)

BOT_FILE = os.path.join(
    BOT_DIR,
    "bot.py"
)

STATE_FILE = os.path.join(
    BOT_DIR,
    "xautusd_state.json"
)


# ============================================================
# LOAD ENVIRONMENT
#
# IMPORTANT:
# The dashboard process may not inherit the exact same
# environment as bot.py.
#
# Therefore explicitly try the bot .env and dashboard .env.
# ============================================================

ENV_FILES = [
    os.path.join(BOT_DIR, ".env"),
    os.path.join(DASHBOARD_DIR, ".env"),
]

for env_file in ENV_FILES:
    if os.path.exists(env_file):
        load_dotenv(
            env_file,
            override=False
        )


# Also load normal dotenv location.
load_dotenv(
    override=False
)


# ============================================================
# CONFIGURATION
# ============================================================

IST = ZoneInfo(
    "Asia/Kolkata"
)

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

STATE_FILE = os.getenv(
    "STATE_FILE",
    os.path.join(
        BOT_DIR,
        "xautusd_state.json"
    )
)


# ============================================================
# FLASK
# ============================================================

app = Flask(
    __name__
)

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
    "User-Agent": "XAUTUSD-Dashboard/2.0"
})


# ============================================================
# DECIMAL HELPERS
# ============================================================

def decimal_value(
    value,
    default=None
):
    if value is None:
        return default

    if isinstance(
        value,
        Decimal
    ):
        return value

    try:
        return Decimal(
            str(value)
        )
    except Exception:
        return default


def json_number(
    value
):
    if value is None:
        return None

    if isinstance(
        value,
        Decimal
    ):
        return float(
            value
        )

    return value


def safe_int(
    value,
    default=0
):
    try:
        return int(
            value
        )
    except Exception:
        return default


# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(
        IST
    )


def parse_delta_time(
    value
):
    """
    Delta timestamps are commonly returned as
    microseconds since epoch.
    """

    if value is None:
        return None

    try:
        number = int(
            str(value)
        )

        # Microseconds
        if number > 10**14:
            return datetime.fromtimestamp(
                number / 1_000_000,
                tz=timezone.utc
            )

        # Milliseconds
        if number > 10**11:
            return datetime.fromtimestamp(
                number / 1_000,
                tz=timezone.utc
            )

        # Seconds
        return datetime.fromtimestamp(
            number,
            tz=timezone.utc
        )

    except Exception:
        return None


def iso_ist(
    dt
):
    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=timezone.utc
        )

    return dt.astimezone(
        IST
    ).isoformat()


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
        int(
            time.time()
        )
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

    # Remove None values.
    clean_params = {}

    for key, value in params.items():

        if value is None:
            continue

        clean_params[
            key
        ] = value

    query_string = (
        "?"
        + urlencode(
            clean_params,
            doseq=True
        )
        if clean_params
        else ""
    )

    headers = {}

    if authenticated:

        if not API_KEY:
            raise RuntimeError(
                "DELTA_API_KEY is missing "
                "from dashboard environment."
            )

        if not API_SECRET:
            raise RuntimeError(
                "DELTA_API_SECRET is missing "
                "from dashboard environment."
            )

        headers = sign_request(
            method,
            path,
            query_string,
            ""
        )

    try:

        response = session.request(
            method.upper(),
            BASE_URL + path,
            params=clean_params,
            headers=headers,
            timeout=15
        )

    except Exception as exc:

        raise RuntimeError(
            f"Delta connection failed: {exc}"
        ) from exc

    if not response.ok:

        raise RuntimeError(
            f"Delta API HTTP "
            f"{response.status_code}: "
            f"{response.text[:500]}"
        )

    try:

        data = response.json()

    except Exception as exc:

        raise RuntimeError(
            "Delta returned invalid JSON: "
            + response.text[:500]
        ) from exc

    if data.get(
        "success"
    ) is False:

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

    result = data.get(
        "result",
        {}
    )

    if not isinstance(
        result,
        dict
    ):
        return {}

    return result


# ============================================================
# TICKER
# ============================================================

def get_ticker():

    return delta_request(
        "GET",
        f"/v2/tickers/{SYMBOL}"
    ).get(
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

        value = ticker.get(
            key
        )

        if value not in (
            None,
            ""
        ):

            price = decimal_value(
                value
            )

            if price is not None:
                return price

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

    if isinstance(
        wallets,
        dict
    ):
        wallets = [
            wallets
        ]

    # First prefer USD / USDT.
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

        balance = decimal_value(
            wallet.get(
                "balance"
            )
        )

        available = decimal_value(
            wallet.get(
                "available_balance"
            )
        )

        if balance is not None:

            return {
                "balance": balance,
                "available_balance": available,
                "asset": asset,
                "raw": wallet
            }

    # If USD/USDT wasn't found, use net equity.
    meta = data.get(
        "meta",
        {}
    )

    net_equity = decimal_value(
        meta.get(
            "net_equity"
        )
    )

    if net_equity is not None:

        return {
            "balance": net_equity,
            "available_balance": None,
            "asset": "EQUITY",
            "raw": meta
        }

    raise RuntimeError(
        "No USD/USDT wallet or net_equity "
        "was returned by Delta."
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
            "realized_pnl": Decimal("0"),
            "realized_funding": Decimal("0"),
            "raw": {}
        }

    return {
        "size": safe_int(
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
        "realized_pnl": decimal_value(
            result.get(
                "realized_pnl"
            ),
            Decimal("0")
        ),
        "realized_funding": decimal_value(
            result.get(
                "realized_funding"
            ),
            Decimal("0")
        ),
        "raw": result
    }


# ============================================================
# BOT STATE
# ============================================================

def load_bot_state():

    candidates = [
        STATE_FILE,

        os.path.join(
            BOT_DIR,
            "xautusd_state.json"
        ),

        os.path.join(
            DASHBOARD_DIR,
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

        for line in result.stdout.splitlines():

            if (
                "pgrep" in line
                or "dashboard_api.py" in line
            ):
                continue

            if BOT_FILE in line:

                return True

        return False

    except Exception:

        return False


# ============================================================
# STOP LOSS
# ============================================================

def get_stop_loss(
    state
):

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

            result = decimal_value(
                value
            )

            if result is not None:
                return result

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
                "states": "open,pending"
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
            result = [
                result
            ]

        if not result:
            return None

        # Prefer actual stop-loss orders.
        for order in result:

            stop_type = str(
                order.get(
                    "stop_order_type",
                    ""
                )
            ).lower()

            if "stop" in stop_type:

                return order

        return None

    except Exception:

        return None


# ============================================================
# CONTRACT VALUE
# ============================================================

def get_contract_value(
    product
):

    for key in (
        "contract_value",
        "contract_value_usd",
        "contract_unit_value"
    ):

        value = decimal_value(
            product.get(
                key
            )
        )

        if (
            value is not None
            and value > 0
        ):

            return value

    return Decimal("1")


# ============================================================
# UNREALIZED P&L
# ============================================================

def calculate_unrealized_pnl(
    position,
    current_price,
    product
):

    size = safe_int(
        position.get(
            "size",
            0
        )
    )

    entry = decimal_value(
        position.get(
            "entry_price"
        )
    )

    if (
        size == 0
        or entry is None
        or current_price is None
    ):
        return Decimal("0")

    contract_value = get_contract_value(
        product
    )

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

def get_xautusd_fills(
    product_id
):

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
        result = [
            result
        ]

    return result


# ============================================================
# FILL TIME
# ============================================================

def fill_datetime(
    fill
):

    for key in (
        "created_at",
        "timestamp",
        "time"
    ):

        value = fill.get(
            key
        )

        dt = parse_delta_time(
            value
        )

        if dt is not None:
            return dt

    return None


# ============================================================
# FILL STATISTICS
#
# FIFO position matching.
#
# This is dashboard-side calculation only.
# It does not modify anything on Delta.
# ============================================================

def calculate_fill_statistics(
    fills,
    product
):

    contract_value = get_contract_value(
        product
    )

    if not fills:

        return {
            "realized_pnl": Decimal("0"),
            "today_pnl": Decimal("0"),
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0,
            "trades": []
        }

    normalized = []

    for fill in fills:

        side = str(
            fill.get(
                "side",
                ""
            )
        ).lower()

        if side not in (
            "buy",
            "sell"
        ):
            continue

        size = safe_int(
            fill.get(
                "size",
                0
            )
        )

        price = decimal_value(
            fill.get(
                "price"
            )
        )

        if (
            size <= 0
            or price is None
        ):
            continue

        dt = fill_datetime(
            fill
        )

        commission = decimal_value(
            fill.get(
                "commission"
            ),
            Decimal("0")
        )

        normalized.append({
            "side": side,
            "size": size,
            "price": price,
            "dt": dt,
            "commission": commission,
            "id": fill.get("id"),
            "order_id": fill.get("order_id"),
            "fill_type": fill.get(
                "fill_type"
            )
        })

    # Oldest first.
    normalized.sort(
        key=lambda x: (
            x["dt"] or datetime.min.replace(
                tzinfo=timezone.utc
            ),
            str(x["id"] or "")
        )
    )

    # FIFO lots:
    #
    # positive quantity = long
    # negative quantity = short
    lots = []

    realized_gross = Decimal("0")
    realized_commission = Decimal("0")

    completed_trades = []

    for fill in normalized:

        qty = (
            fill["size"]
            if fill["side"] == "buy"
            else -fill["size"]
        )

        price = fill["price"]

        # Commission is always part of realized account P&L
        # when a fill occurs.
        realized_commission += (
            fill["commission"]
        )

        remaining = qty

        while (
            remaining != 0
            and lots
            and (
                (
                    remaining > 0
                    and lots[0]["qty"] < 0
                )
                or
                (
                    remaining < 0
                    and lots[0]["qty"] > 0
                )
            )
        ):

            lot = lots[0]

            match_qty = min(
                abs(remaining),
                abs(lot["qty"])
            )

            if lot["qty"] > 0:

                # Existing long closed by sell.
                gross = (
                    price
                    - lot["price"]
                ) * Decimal(
                    match_qty
                ) * contract_value

                trade_direction = "LONG"

            else:

                # Existing short closed by buy.
                gross = (
                    lot["price"]
                    - price
                ) * Decimal(
                    match_qty
                ) * contract_value

                trade_direction = "SHORT"

            realized_gross += gross

            close_dt = fill["dt"]

            completed_trades.append({
                "direction": trade_direction,
                "size": match_qty,
                "entry_price": json_number(
                    lot["price"]
                ),
                "exit_price": json_number(
                    price
                ),
                "pnl": json_number(
                    gross
                ),
                "entry_time": iso_ist(
                    lot["dt"]
                ),
                "exit_time": iso_ist(
                    close_dt
                )
            })

            if lot["qty"] > 0:
                lot["qty"] -= match_qty
            else:
                lot["qty"] += match_qty

            if remaining > 0:
                remaining -= match_qty
            else:
                remaining += match_qty

            if lot["qty"] == 0:
                lots.pop(0)

        if remaining != 0:

            lots.append({
                "qty": remaining,
                "price": price,
                "dt": fill["dt"]
            })

    # Total realized after commissions.
    realized_total = (
        realized_gross
        - realized_commission
    )

    # Today's completed trade P&L.
    today = now_ist().date()

    today_pnl = Decimal("0")

    for trade in completed_trades:

        exit_time = trade.get(
            "exit_time"
        )

        if not exit_time:
            continue

        try:

            dt = datetime.fromisoformat(
                exit_time
            )

            if dt.date() == today:

                today_pnl += decimal_value(
                    trade.get(
                        "pnl"
                    ),
                    Decimal("0")
                )

        except Exception:
            continue

    # Today's commission.
    today_commission = Decimal("0")

    for fill in normalized:

        dt = fill.get(
            "dt"
        )

        if dt is None:
            continue

        if dt.astimezone(
            IST
        ).date() == today:

            today_commission += (
                fill["commission"]
            )

    today_pnl -= today_commission

    winning_trades = 0
    losing_trades = 0

    for trade in completed_trades:

        pnl = decimal_value(
            trade.get(
                "pnl"
            ),
            Decimal("0")
        )

        if pnl > 0:
            winning_trades += 1

        elif pnl < 0:
            losing_trades += 1

    total_trades = (
        winning_trades
        + losing_trades
    )

    win_rate = (
        (
            winning_trades
            / total_trades
        ) * 100
        if total_trades > 0
        else 0
    )

    # Most recent trades first.
    completed_trades.reverse()

    # Keep dashboard response reasonably small.
    completed_trades = completed_trades[
        :50
    ]

    return {
        "realized_pnl": realized_total,
        "today_pnl": today_pnl,
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": round(
            win_rate,
            2
        ),
        "trades": completed_trades
    }


# ============================================================
# SERIALIZE TRADE
# ============================================================

def serialize_trade(
    trade
):

    result = {}

    for key, value in trade.items():

        result[key] = json_number(
            value
        )

    return result


# ============================================================
# DASHBOARD DATA
# ============================================================

def build_dashboard():

    errors = []

    # --------------------------------------------------------
    # PRODUCT
    # --------------------------------------------------------

    product = {}

    try:

        product = get_product()

    except Exception as exc:

        errors.append(
            "Product: " + str(exc)
        )

    product_id = product.get(
        "id"
    )

    # --------------------------------------------------------
    # MARKET PRICE
    # --------------------------------------------------------

    current_price = None

    try:

        current_price = get_current_price()

    except Exception as exc:

        errors.append(
            "Price: " + str(exc)
        )

    # --------------------------------------------------------
    # BOT STATE
    # --------------------------------------------------------

    state = load_bot_state()

    bot_running = is_bot_running()

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    balance_data = {
        "balance": None,
        "available_balance": None,
        "asset": None
    }

    try:

        balance_data = get_balance_data()

    except Exception as exc:

        errors.append(
            "Balance: " + str(exc)
        )

    # --------------------------------------------------------
    # POSITION
    # --------------------------------------------------------

    position = {
        "size": 0,
        "entry_price": None,
        "realized_pnl": Decimal("0"),
        "realized_funding": Decimal("0"),
        "raw": {}
    }

    if product_id is not None:

        try:

            position = get_position_data(
                product_id
            )

        except Exception as exc:

            errors.append(
                "Position: " + str(exc)
            )

    # --------------------------------------------------------
    # STOP LOSS
    # --------------------------------------------------------

    stop_loss = get_stop_loss(
        state
    )

    # If state does not have SL, try active order.
    if (
        stop_loss is None
        and product_id is not None
    ):

        stop_order = get_open_stop_order(
            product_id
        )

        if stop_order:

            stop_loss = decimal_value(
                stop_order.get(
                    "stop_price"
                )
            )

    # --------------------------------------------------------
    # UNREALIZED PNL
    # --------------------------------------------------------

    unrealized_pnl = calculate_unrealized_pnl(
        position,
        current_price,
        product
    )

    # --------------------------------------------------------
    # FILLS / STATISTICS
    # --------------------------------------------------------

    fill_statistics = {
        "realized_pnl": Decimal("0"),
        "today_pnl": Decimal("0"),
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate": 0,
        "trades": []
    }

    fills = []

    if product_id is not None:

        try:

            fills = get_xautusd_fills(
                product_id
            )

            fill_statistics = calculate_fill_statistics(
                fills,
                product
            )

        except Exception as exc:

            errors.append(
                "Fills: " + str(exc)
            )

    # --------------------------------------------------------
    # P&L
    # --------------------------------------------------------

    # If fills produced a realized P&L, use it.
    total_realized_pnl = fill_statistics[
        "realized_pnl"
    ]

    today_pnl = fill_statistics[
        "today_pnl"
    ]

    # If there are no fills available, use Delta's
    # current position realized_pnl as a fallback.
    if not fills:

        total_realized_pnl = position.get(
            "realized_pnl",
            Decimal("0")
        )

    total_pnl = (
        total_realized_pnl
        + unrealized_pnl
    )

    # --------------------------------------------------------
    # POSITION DIRECTION
    # --------------------------------------------------------

    size = safe_int(
        position.get(
            "size",
            0
        )
    )

    if size > 0:

        direction = "LONG"

    elif size < 0:

        direction = "SHORT"

    else:

        direction = "FLAT"

    # --------------------------------------------------------
    # RESPONSE
    # --------------------------------------------------------

    return {

        "status": "ok",

        "symbol": SYMBOL,

        "bot_running": bot_running,

        "current_price": json_number(
            current_price
        ),

        "balance": json_number(
            balance_data.get(
                "balance"
            )
        ),

        "available_balance": json_number(
            balance_data.get(
                "available_balance"
            )
        ),

        "balance_asset": balance_data.get(
            "asset"
        ),

        "position": {
            "direction": direction,
            "size": size,
            "entry_price": json_number(
                position.get(
                    "entry_price"
                )
            ),
            "stop_loss": json_number(
                stop_loss
            ),
            "unrealized_pnl": json_number(
                unrealized_pnl
            )
        },

        # Compatibility fields for existing frontend.
        "entry_price": json_number(
            position.get(
                "entry_price"
            )
        ),

        "stop_loss": json_number(
            stop_loss
        ),

        "unrealized_pnl": json_number(
            unrealized_pnl
        ),

        "today_pnl": json_number(
            today_pnl
        ),

        "total_pnl": json_number(
            total_pnl
        ),

        "statistics": {

            "total_trades": fill_statistics[
                "total_trades"
            ],

            "winning_trades": fill_statistics[
                "winning_trades"
            ],

            "losing_trades": fill_statistics[
                "losing_trades"
            ],

            "win_rate": fill_statistics[
                "win_rate"
            ],

            "today_pnl": json_number(
                today_pnl
            ),

            "total_pnl": json_number(
                total_pnl
            )
        },

        "trades": [
            serialize_trade(
                trade
            )
            for trade
            in fill_statistics[
                "trades"
            ]
        ],

        # Useful diagnostics.
        "diagnostics": {

            "api_credentials_loaded": bool(
                API_KEY
                and API_SECRET
            ),

            "base_url": BASE_URL,

            "product_id": product_id,

            "state_file": STATE_FILE,

            "state_file_exists": os.path.exists(
                STATE_FILE
            ),

            "bot_file_exists": os.path.exists(
                BOT_FILE
            ),

            "fill_count": len(
                fills
            ),

            "errors": errors
        }
    }


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/api/health",
    methods=["GET"]
)
def health():

    return jsonify({
        "status": "ok"
    })


# ============================================================
# DASHBOARD
# ============================================================

@app.route(
    "/api/dashboard",
    methods=["GET"]
)
def dashboard():

    try:

        data = build_dashboard()

        return jsonify(
            data
        )

    except Exception as exc:

        return jsonify({

            "status": "error",

            "bot_running": is_bot_running(),

            "error": str(
                exc
            )

        }), 500


# ============================================================
# DEBUG ENDPOINT
#
# READ ONLY.
#
# This helps us see exactly which part fails if the dashboard
# ever shows null again.
# ============================================================

@app.route(
    "/api/debug",
    methods=["GET"]
)
def debug():

    result = {

        "status": "ok",

        "symbol": SYMBOL,

        "base_url": BASE_URL,

        "api_credentials_loaded": bool(
            API_KEY
            and API_SECRET
        ),

        "api_key_length": len(
            API_KEY
        ),

        "api_secret_loaded": bool(
            API_SECRET
        ),

        "bot_running": is_bot_running(),

        "bot_file_exists": os.path.exists(
            BOT_FILE
        ),

        "state_file": STATE_FILE,

        "state_file_exists": os.path.exists(
            STATE_FILE
        ),

        "tests": {}
    }

    # NEVER return the actual API key or secret.

    try:

        product = get_product()

        result["tests"]["product"] = {
            "ok": True,
            "id": product.get(
                "id"
            ),
            "symbol": product.get(
                "symbol"
            )
        }

    except Exception as exc:

        result["tests"]["product"] = {
            "ok": False,
            "error": str(
                exc
            )
        }

    try:

        price = get_current_price()

        result["tests"]["ticker"] = {
            "ok": True,
            "price": json_number(
                price
            )
        }

    except Exception as exc:

        result["tests"]["ticker"] = {
            "ok": False,
            "error": str(
                exc
            )
        }

    try:

        balance = get_balance_data()

        result["tests"]["balance"] = {
            "ok": True,
            "balance": json_number(
                balance.get(
                    "balance"
                )
            ),
            "available_balance": json_number(
                balance.get(
                    "available_balance"
                )
            ),
            "asset": balance.get(
                "asset"
            )
        }

    except Exception as exc:

        result["tests"]["balance"] = {
            "ok": False,
            "error": str(
                exc
            )
        }

    try:

        product = get_product()

        product_id = product.get(
            "id"
        )

        if product_id is None:

            raise RuntimeError(
                "Product ID not found."
            )

        position = get_position_data(
            product_id
        )

        result["tests"]["position"] = {
            "ok": True,
            "size": position.get(
                "size"
            ),
            "entry_price": json_number(
                position.get(
                    "entry_price"
                )
            )
        }

    except Exception as exc:

        result["tests"]["position"] = {
            "ok": False,
            "error": str(
                exc
            )
        }

    return jsonify(
        result
    )


# ============================================================
# ROOT
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def root():

    return jsonify({
        "service": "XAUTUSD Dashboard API",
        "status": "ok",
        "dashboard_endpoint": "/api/dashboard",
        "health_endpoint": "/api/health",
        "debug_endpoint": "/api/debug"
    })


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    print(
        "=========================================="
    )

    print(
        "XAUTUSD DASHBOARD API"
    )

    print(
        "READ-ONLY MODE"
    )

    print(
        "BOT WILL NOT BE STARTED OR STOPPED"
    )

    print(
        f"BOT DIR: {BOT_DIR}"
    )

    print(
        f"SYMBOL: {SYMBOL}"
    )

    print(
        f"BASE URL: {BASE_URL}"
    )

    print(
        f"API CREDENTIALS LOADED: "
        f"{bool(API_KEY and API_SECRET)}"
    )

    print(
        "=========================================="
    )

    app.run(
        host="0.0.0.0",
        port=8000,
        debug=False
            )
