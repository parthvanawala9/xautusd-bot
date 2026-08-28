// ============================================================
// XAUTUSD DASHBOARD FRONTEND
// Reads generated dashboard.json from GitHub Pages root
// ============================================================

let refreshTimer = null;

function money(value) {
    if (value === null || value === undefined || value === "") return "--";
    const num = Number(value);
    if (Number.isNaN(num)) return "--";
    return "$" + num.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
}

function number(value) {
    if (value === null || value === undefined || value === "") return "--";
    const num = Number(value);
    if (Number.isNaN(num)) return "--";
    return num.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

function setPnl(element, value) {
    if (!element) return;
    element.textContent = money(value);
    element.classList.remove("profit", "loss");
    if (Number(value) > 0) element.classList.add("profit");
    if (Number(value) < 0) element.classList.add("loss");
}

function updateBotStatus(running) {
    const status = document.getElementById("botStatus");
    const statusCard = document.getElementById("botStatusCard");
    if (!status) return;

    if (running) {
        status.className = "status running";
        status.innerHTML = '<span class="status-dot"></span> BOT RUNNING';
        if (statusCard) {
            statusCard.textContent = "RUNNING";
            statusCard.classList.remove("loss");
            statusCard.classList.add("profit");
        }
    } else {
        status.className = "status stopped";
        status.innerHTML = '<span class="status-dot"></span> BOT STOPPED';
        if (statusCard) {
            statusCard.textContent = "STOPPED";
            statusCard.classList.remove("profit");
            statusCard.classList.add("loss");
        }
    }
}

function updatePosition(position) {
    position = position || {};
    const direction = position.direction || "FLAT";

    const badge = document.getElementById("positionBadge");
    if (badge) {
        badge.textContent = direction;
        badge.className = direction === "LONG" ? "position-long" : direction === "SHORT" ? "position-short" : "position-flat";
    }

    const directionElement = document.getElementById("direction");
    if (directionElement) directionElement.textContent = direction;

    const sizeElement = document.getElementById("positionSize");
    if (sizeElement) sizeElement.textContent = number(position.size);

    const entryElement = document.getElementById("entryPrice");
    if (entryElement) entryElement.textContent = number(position.entry_price);

    const stopElement = document.getElementById("stopLoss");
    if (stopElement) stopElement.textContent = number(position.stop_loss);

    const unrealizedElement = document.getElementById("unrealizedPnl");
    if (unrealizedElement) setPnl(unrealizedElement, position.unrealized_pnl);
}

function renderTrades(trades) {
    const table = document.getElementById("tradeTable");
    if (!table) return;

    if (!trades || trades.length === 0) {
        table.innerHTML = `<tr><td colspan="6" class="empty">No trades logged yet</td></tr>`;
        return;
    }

    table.innerHTML = trades.map(trade => {
        const side = String(trade.side || "").toUpperCase();
        const sideClass = side === "BUY" ? "side-buy" : "side-sell";
        return `
            <tr>
                <td>${formatTime(trade.timestamp)}</td>
                <td class="${sideClass}">${side || "--"}</td>
                <td>${number(trade.price)}</td>
                <td>${number(trade.size)}</td>
                <td>${money(trade.commission)}</td>
                <td class="${Number(trade.pnl) > 0 ? "profit" : Number(trade.pnl) < 0 ? "loss" : ""}">${money(trade.pnl)}</td>
            </tr>
        `;
    }).join("");
}

function formatTime(timestamp) {
    if (!timestamp) return "--";
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;
    return date.toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

async function loadDashboard() {
    try {
        const response = await fetch("./dashboard.json?cache=" + Date.now());

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        const data = await response.json();

        updateBotStatus(data.bot_running ?? true);

        const currentPrice = document.getElementById("currentPrice");
        if (currentPrice) currentPrice.textContent = number(data.current_price);

        const balance = document.getElementById("balance");
        if (balance) balance.textContent = money(data.balance);

        const totalPnl = document.getElementById("totalPnl");
        if (totalPnl) setPnl(totalPnl, data.total_pnl);

        const todayPnl = document.getElementById("todayPnl");
        if (todayPnl) setPnl(todayPnl, data.today_pnl);

        const winRate = document.getElementById("winRate");
        if (winRate) {
            const val = Number(data.statistics?.win_rate);
            winRate.textContent = Number.isNaN(val) ? "0.0%" : val.toFixed(1) + "%";
        }

        const totalTrades = document.getElementById("totalTrades");
        if (totalTrades) totalTrades.textContent = data.statistics?.total_trades ?? "0";

        const winningTrades = document.getElementById("winningTrades");
        if (winningTrades) winningTrades.textContent = data.statistics?.winning_trades ?? "0";

        const losingTrades = document.getElementById("losingTrades");
        if (losingTrades) losingTrades.textContent = data.statistics?.losing_trades ?? "0";

        updatePosition(data.position);
        renderTrades(data.trades);

        const lastUpdated = document.getElementById("lastUpdated");
        if (lastUpdated) {
            lastUpdated.textContent = new Date().toLocaleTimeString("en-IN", {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit"
            });
        }

    } catch (error) {
        console.error("Dashboard Load Error:", error);
    }
}

loadDashboard();
refreshTimer = setInterval(loadDashboard, 5000);
