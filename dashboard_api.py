from flask import Flask, jsonify, request
from flask_cors import CORS
import os
import json

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "trade_history.json")


def load_history():
    try:
        with open(HISTORY_FILE, "r") as f:
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
    trades = load_history()

    winning = [t for t in trades if float(t.get("pnl", 0) or 0) > 0]
    losing = [t for t in trades if float(t.get("pnl", 0) or 0) < 0]

    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in trades)

    return jsonify({
        "bot_running": True,
        "symbol": "XAUTUSD",
        "current_price": None,
        "balance": None,
        "today_pnl": total_pnl,
        "total_pnl": total_pnl,
        "position": {
            "direction": "FLAT",
            "entry_price": None,
            "stop_loss": None,
            "unrealized_pnl": 0
        },
        "statistics": {
            "total_trades": len(trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "win_rate": (
                round(len(winning) / len(trades) * 100, 2)
                if trades else 0
            )
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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8000,
        debug=False
    )
