const API_URL = "";

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });

  if (!response.ok) {
    throw new Error(`API error ${response.status}`);
  }

  return response.json();
}

function setBotStatus(running) {
  $("botStatus").textContent = running ? "RUNNING" : "STOPPED";
  $("bigBotStatus").textContent = running ? "RUNNING" : "OFFLINE";

  $("botStatusDot").className =
    `status-dot ${running ? "online" : "offline"}`;

  $("bigStatusDot").className =
    `status-dot ${running ? "online" : "offline"}`;
}

function formatNumber(value, decimals = 2) {
  if (value === null || value === undefined || value === "") {
    return "--";
  }

  const number = Number(value);

  if (!Number.isFinite(number)) {
    return "--";
  }

  return number.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  });
}

function formatPnl(value) {
  if (value === null || value === undefined || value === "") {
    return "--";
  }

  const number = Number(value);

  if (!Number.isFinite(number)) {
    return "--";
  }

  const formatted =
    `${number >= 0 ? "+" : ""}$${formatNumber(number)}`;

  return formatted;
}

function applyPnlClass(element, value) {
  element.classList.remove("profit", "loss");

  if (Number(value) > 0) {
    element.classList.add("profit");
  } else if (Number(value) < 0) {
    element.classList.add("loss");
  }
}

async function loadDashboard() {
  try {
    const data = await api("/api/dashboard");

    $("connectionStatus").textContent = "Connected";

    setBotStatus(Boolean(data.bot_running));

    $("currentPrice").textContent =
      `$${formatNumber(data.current_price)}`;

    $("position").textContent =
      data.position?.direction || "FLAT";

    $("entryPrice").textContent =
      data.position?.entry_price
        ? `$${formatNumber(data.position.entry_price)}`
        : "--";

    $("stopLoss").textContent =
      data.position?.stop_loss
        ? `$${formatNumber(data.position.stop_loss)}`
        : "--";

    $("unrealizedPnl").textContent =
      formatPnl(data.position?.unrealized_pnl);

    applyPnlClass(
      $("unrealizedPnl"),
      data.position?.unrealized_pnl
    );

    $("todayPnl").textContent =
      formatPnl(data.today_pnl);

    applyPnlClass(
      $("todayPnl"),
      data.today_pnl
    );

    $("totalPnl").textContent =
      formatPnl(data.total_pnl);

    applyPnlClass(
      $("totalPnl"),
      data.total_pnl
    );

    $("totalTrades").textContent =
      data.statistics?.total_trades ?? "--";

    $("winningTrades").textContent =
      data.statistics?.winning_trades ?? "--";

    $("losingTrades").textContent =
      data.statistics?.losing_trades ?? "--";

    $("winRate").textContent =
      data.statistics?.win_rate !== undefined
        ? `${formatNumber(data.statistics.win_rate)}%`
        : "--";

    $("balance").textContent =
      data.balance !== undefined
        ? `$${formatNumber(data.balance)}`
        : "--";

    $("symbol").textContent =
      data.symbol || "XAUTUSD";

    $("lastUpdate").textContent =
      new Date().toLocaleTimeString();

    renderTrades(data.trades || []);

  } catch (error) {
    $("connectionStatus").textContent = "Disconnected";
    setBotStatus(false);
    console.error(error);
  }
}

function renderTrades(trades) {
  const table = $("tradeTable");

  $("tradeCount").textContent =
    `${trades.length} trade${trades.length === 1 ? "" : "s"}`;

  if (!trades.length) {
    table.innerHTML = `
      <tr>
        <td colspan="7" class="empty">
          No trades available
        </td>
      </tr>
    `;
    return;
  }

  table.innerHTML = trades.map((trade) => {
    const direction =
      String(trade.direction || "").toUpperCase();

    const pnl = Number(trade.pnl || 0);

    return `
      <tr>
        <td>${trade.time || "--"}</td>

        <td class="${direction === "LONG" ? "long" : "short"}">
          ${direction || "--"}
        </td>

        <td>${formatNumber(trade.entry_price)}</td>

        <td>${formatNumber(trade.exit_price)}</td>

        <td>${trade.size ?? "--"}</td>

        <td>${trade.reason || "--"}</td>

        <td class="${pnl >= 0 ? "profit" : "loss"}">
          ${formatPnl(pnl)}
        </td>
      </tr>
    `;
  }).join("");
}

async function startBot() {
  if (!confirm("Start the XAUTUSD bot?")) {
    return;
  }

  try {
    await api("/api/start", {
      method: "POST"
    });

    await loadDashboard();

  } catch (error) {
    alert("Could not start the bot.");
    console.error(error);
  }
}

async function stopBot() {
  if (!confirm(
    "STOP BOT?\n\nThe existing position will also be closed."
  )) {
    return;
  }

  try {
    await api("/api/stop", {
      method: "POST"
    });

    await loadDashboard();

  } catch (error) {
    alert("Could not stop the bot.");
    console.error(error);
  }
}

$("startBtn").addEventListener("click", startBot);
$("stopBtn").addEventListener("click", stopBot);

loadDashboard();

setInterval(loadDashboard, 3000);
