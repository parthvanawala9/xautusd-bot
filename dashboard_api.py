import os
import time
import hmac
import hashlib
import requests
import json
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
SYMBOL = os.getenv("DELTA_SYMBOL", "XAUTUSD").strip()
BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip('/')


def generate_signature(method, endpoint, payload="", timestamp=""):
    message = method + timestamp + endpoint + payload
    return hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def get_headers(method, endpoint, payload=""):
    timestamp = str(int(time.time()))
    signature = generate_signature(method, endpoint, payload, timestamp)
    return {
        "api-key": API_KEY,
        "signature": signature,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }


def fetch_account_balance():
    try:
        endpoint = "/v2/wallet/balances"
        url = BASE_URL + endpoint
        headers = get_headers("GET", endpoint)
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json().get("result", [])
            for asset in data:
                if asset.get("asset_symbol") in ["USDT", "DETO", "USD"]:
                    return float(asset.get("balance", 0.0))
            if data:
                return float(data[0].get("balance", 0.0))
        else:
            print(f"[Balance API Error] Status: {res.status_code}, Body: {res.text}")
    except Exception as e:
        print("[Balance Exception]:", e)
    return 0.0


def fetch_ticker_price():
    try:
        endpoint = f"/v2/tickers/{SYMBOL}"
        url = BASE_URL + endpoint
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return float(res.json().get("result", {}).get("close", 0.0))
        else:
            print(f"[Ticker API Error] Status: {res.status_code}, Body: {res.text}")
    except Exception as e:
        print("[Ticker Exception]:", e)
    return 0.0


def fetch_live_position():
    if not API_KEY or not API_SECRET:
        print("[Position] API key or secret missing from environment.")
        return {
            "direction": "FLAT",
            "size": 0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "unrealized_pnl": 0.0
        }

    endpoints = ["/v2/positions/margined", "/v2/positions"]

    for ep in endpoints:
        try:
            headers = get_headers("GET", ep)
            res = requests.get(BASE_URL + ep, headers=headers, timeout=10)
            
            print(f"[Position Check {ep}] Status: {res.status_code}")
            
            if res.status_code == 200:
                positions = res.json().get("result", [])
                for pos in positions:
                    prod_symbol = str(pos.get("product_symbol", "")).upper()
                    size = float(pos.get("size", 0))

                    if size != 0 and ("XAUT" in prod_symbol or prod_symbol == SYMBOL):
                        direction = "LONG" if size > 0 else "SHORT"
                        entry_price = float(pos.get("entry_price", 0.0))
                        stop_loss = float(pos.get("stop_loss", 0.0))
                        unrealized = float(pos.get("unrealized_pnl", 0.0))
                        realized = float(pos.get("realized_pnl", 0.0))

                        return {
                            "direction": direction,
                            "size": abs(size),
                            "entry_price": entry_price,
                            "stop_loss": stop_loss,
                            "unrealized_pnl": unrealized + realized
                        }
            else:
                print(f"[Position API Error] {ep} -> {res.text}")
        except Exception as e:
            print(f"[Position Exception on {ep}]:", e)

    return {
        "direction": "FLAT",
        "size": 0,
        "entry_price": 0.0,
        "stop_loss": 0.0,
        "unrealized_pnl": 0.0
    }


def fetch_trade_history():
    try:
        endpoint = f"/v2/fills?product_symbol={SYMBOL}&limit=20"
        url = BASE_URL + endpoint
        headers = get_headers("GET", endpoint)
        res = requests.get(url, headers=headers, timeout=10)
        
        if res.status_code == 200:
            fills = res.json().get("result", [])
            formatted_trades = []
            for fill in fills:
                formatted_trades.append({
                    "timestamp": fill.get("created_at"),
                    "side": fill.get("side"),
                    "price": float(fill.get("price", 0.0)),
                    "size": float(fill.get("size", 0.0)),
                    "commission": float(fill.get("fee", 0.0)),
                    "pnl": float(fill.get("pnl", 0.0))
                })
            return formatted_trades
        else:
            print(f"[Fills API Error] Status: {res.status_code}, Body: {res.text}")
    except Exception as e:
        print("[Fills Exception]:", e)
    return []


def build_dashboard():
    balance = fetch_account_balance()
    current_price = fetch_ticker_price()
    position = fetch_live_position()
    trades = fetch_trade_history()

    total_pnl = sum(t["pnl"] for t in trades)
    winning_trades = sum(1 for t in trades if t["pnl"] > 0)
    losing_trades = sum(1 for t in trades if t["pnl"] < 0)
    total_trades = len(trades)
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

    return {
        "success": True,
        "bot_running": True,
        "current_price": current_price,
        "balance": balance,
        "total_pnl": total_pnl,
        "today_pnl": total_pnl,
        "position": position,
        "statistics": {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": round(win_rate, 1)
        },
        "trades": trades
    }


if __name__ == "__main__":
    print(json.dumps(build_dashboard(), indent=2))
