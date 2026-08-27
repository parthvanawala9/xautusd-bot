from flask import Flask, jsonify, request

app = Flask(__name__)


@app.get("/api/dashboard")
def dashboard():
    return jsonify({
        "bot_running": False,
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
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0
        },
        "trades": []
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
