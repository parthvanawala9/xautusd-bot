async function loadDashboardData() {
  try {
    // Relative fetch with timestamp to prevent caching issues
    const response = await fetch(`./dashboard.json?t=${new Date().getTime()}`);
    
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    
    const data = await response.json();
    renderDashboard(data);
  } catch (error) {
    console.error("Error loading dashboard data:", error);
    const statusEl = document.getElementById("bot-status");
    if (statusEl) {
      statusEl.textContent = "Offline / Error Loading Data";
      statusEl.className = "status-offline";
    }
  }
}

function renderDashboard(data) {
  // Update Price & Balance
  if (data.current_price !== undefined) {
    document.getElementById("current-price").textContent = `$${parseFloat(data.current_price).toFixed(2)}`;
  }
  if (data.balance !== undefined) {
    document.getElementById("wallet-balance").textContent = `$${parseFloat(data.balance).toFixed(2)}`;
  }

  // Update Status Indicator
  const statusEl = document.getElementById("bot-status");
  if (statusEl) {
    if (data.bot_running) {
      statusEl.textContent = "Active & Running";
      statusEl.className = "status-online";
    } else {
      statusEl.textContent = "Bot Stopped";
      statusEl.className = "status-offline";
    }
  }

  // Update Position Data
  if (data.position) {
    document.getElementById("pos-direction").textContent = data.position.direction || "FLAT";
    document.getElementById("pos-size").textContent = data.position.size || "0";
    document.getElementById("pos-entry").textContent = `$${parseFloat(data.position.entry_price || 0).toFixed(2)}`;
    document.getElementById("pos-sl").textContent = `$${parseFloat(data.position.stop_loss || 0).toFixed(2)}`;
    
    const pnlEl = document.getElementById("pos-pnl");
    if (pnlEl) {
      const pnl = parseFloat(data.position.unrealized_pnl || 0);
      pnlEl.textContent = `$${pnl.toFixed(2)}`;
      pnlEl.className = pnl >= 0 ? "text-green" : "text-red";
    }
  }

  // Update Statistics
  if (data.statistics) {
    document.getElementById("total-trades").textContent = data.statistics.total_trades || 0;
    document.getElementById("win-rate").textContent = `${parseFloat(data.statistics.win_rate || 0).toFixed(1)}%`;
  }
}

// Load on page startup and auto-refresh every 30 seconds
document.addEventListener("DOMContentLoaded", () => {
  loadDashboardData();
  setInterval(loadDashboardData, 30000);
});
