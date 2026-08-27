from flask import Flask, jsonify
from flask_cors import CORS
import os
import json
import subprocess

app = Flask(__name__)
CORS(app)

BASE_DIR = "/home/opc/xautusd-bot"
TRADE_HISTORY = os.path.join(BASE_DIR, "trade_history.json")


def get_bot_running():
    try:
        result = subprocess.run(
            ["pgrep", "-f", "/home/opc/xautusd-bot/bot.py"],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except Exception:
        return False


def load_trades():
    try:
        if not os.path.exists(TRADE_HISTORY):
            return []

        with open(TRADE_HISTORY, "r") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data.get("trades", [])

        if isinstance(data, list):
            return data

        return []

    except Exception:
        return []


@app.get("/api/dashboard")
def dashboard():

    trades = load_trades()

    return jsonify({
        "bot_running": get_bot_running(),
        "symbol": "XAUTUSD",
        "current_price": None,
        "balance": None,
        "today_pnl": 0,
        "total_pnl": 0,
        "position": {
            "direction": "FLAT",
            "entry_price": None,
            "stop_loss": None,
            "unrealized_pnl": 0
        },
        "statistics": {
            "total_trades": len(trades),
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0
        },
        "trades": trades
    })


@app.post("/api/start")
def start_bot():
    return jsonify({
        "success": True,
        "message": "START command received"
    })


@app.post("/api/stop")
def stop_bot():
    return jsonify({
        "success": True,
        "message": "STOP command received"
    })


@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok"
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8000,
        debug=False
    )
