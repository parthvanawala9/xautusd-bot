const $ = (id) => document.getElementById(id);
let dashboardLoading = false;

function number(value, decimals = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(decimals);
}

function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  const n = Number(value);
  return n >= 0 ? "+$" + n.toFixed(2) : "-$" + Math.abs(n).toFixed(2);
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { ...(options.headers || {}), "Content-Type": "application/json", "Accept": "application/json" }
  });
  const data = await response.json();
  if (!response.ok || data?.success === false) {
    throw new Error(data?.message || `Request failed.`);
  }
  return data;
}

async function startBot(accountId) {
  try {
    await apiFetch("/api/bot/start", { method: "POST", body: JSON.stringify({ account_id: accountId }) });
    await loadDashboard(true);
  } catch (error) { alert(error.message); }
}

async function stopBot(accountId) {
  if (!window.confirm("STOP BOT will close open positions. Continue?")) return;
  try {
    await apiFetch("/api/bot/stop", { method: "POST", body: JSON.stringify({ account_id: accountId }) });
    await loadDashboard(true);
  } catch (error) { alert(error.message); }
}

function openClientForm() {
  const form = $("client-form");
  if (form) form.style.display = form.style.display === "none" ? "block" : "none";
}

async function addClient() {
  const name = $("client-name")?.value.trim();
  const apiKey = $("client-api-key")?.value.trim();
  const apiSecret = $("client-api-secret")?.value.trim();
  const start = $("client-start")?.value;
  const expiry = $("client-expiry")?.value;
  const fee = $("client-fee")?.value || 0;

  if (!name || !apiKey || !apiSecret || !start || !expiry) {
    alert("Please fill all required client fields.");
    return;
  }

  try {
    await apiFetch("/api/client/add", {
      method: "POST",
      body: JSON.stringify({
        name, api_key: apiKey, api_secret: apiSecret,
        subscription_start: new Date(start).toISOString(),
        subscription_expiry: new Date(expiry).toISOString(),
        subscription_fee: Number(fee)
      })
    });
    $("client-form").style.display = "none";
    await loadDashboard(true);
    alert("Client added successfully!");
  } catch (error) { alert(error.message); }
}

async function deleteClient(accountId) {
  if (!window.confirm("Remove this client account?")) return;
  try {
    await apiFetch("/api/client/delete", { method: "POST", body: JSON.stringify({ account_id: accountId }) });
    await loadDashboard(true);
  } catch (error) { alert(error.message); }
}

function copyClientLink(token) {
  const link = `${window.location.origin}/?token=${token}`;
  navigator.clipboard.writeText(link);
  alert("Client private link copied to clipboard:\n\n" + link);
}

function renderPerformance(account) {
  const stats = account.statistics || {};
  const today = stats.today || {};
  const allTime = stats.all_time || {};

  return `
    <div class="performance-section" style="margin-top:20px; padding-top:15px; border-top:1px solid #e2e8f0;">
      <h3 style="font-size:15px; font-weight:800; margin-bottom:10px;">Trading Performance</h3>
      <div style="display:grid; grid-template-columns: 1fr 1fr; gap:12px;">
        <div style="background:#f8fafc; padding:12px; border-radius:10px; border:1px solid #e2e8f0;">
          <h4 style="font-size:11px; font-weight:800; color:#64748b; margin-bottom:8px;">TODAY</h4>
          <div style="font-size:12px; display:flex; flex-direction:column; gap:4px;">
            <div>Trades: <strong>${today.total_trades || 0}</strong></div>
            <div>Wins / Losses: <strong style="color:#16a34a;">${today.winning_trades || 0}</strong> / <strong style="color:#dc2626;">${today.losing_trades || 0}</strong></div>
            <div>Win Rate: <strong>${number(today.win_rate, 1)}%</strong></div>
            <div>P&L: <strong class="${(today.pnl || 0) >= 0 ? 'trade-profit' : 'trade-loss'}">${money(today.pnl)}</strong></div>
          </div>
        </div>
        <div style="background:#f8fafc; padding:12px; border-radius:10px; border:1px solid #e2e8f0;">
          <h4 style="font-size:11px; font-weight:800; color:#64748b; margin-bottom:8px;">ALL TIME</h4>
          <div style="font-size:12px; display:flex; flex-direction:column; gap:4px;">
            <div>Trades: <strong>${allTime.total_trades || 0}</strong></div>
            <div>Wins / Losses: <strong style="color:#16a34a;">${allTime.winning_trades || 0}</strong> / <strong style="color:#dc2626;">${allTime.losing_trades || 0}</strong></div>
            <div>Win Rate: <strong>${number(allTime.win_rate, 1)}%</strong></div>
            <div>P&L: <strong class="${(allTime.pnl || 0) >= 0 ? 'trade-profit' : 'trade-loss'}">${money(allTime.pnl)}</strong></div>
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderTradeHistory(account) {
  const history = Array.isArray(account.trade_history) ? account.trade_history : [];

  let rows = "";
  if (history.length > 0) {
    rows = history.slice(-20).reverse().map(trade => {
      const pnl = Number(trade.pnl || 0);
      const pnlClass = pnl > 0 ? "trade-profit" : pnl < 0 ? "trade-loss" : "trade-flat";
      return `
        <div style="display:grid; grid-template-columns: 1.2fr 0.8fr 1fr 1fr 0.7fr 1fr; gap:6px; padding:10px; background:#f8fafc; border:1px solid #edf1f5; border-radius:8px; align-items:center; font-size:11px; margin-bottom:6px;">
          <div><span style="font-size:9px; color:#94a3b8; display:block;">DATE</span><strong>${escapeHtml(trade.date)}</strong></div>
          <div><span style="font-size:9px; color:#94a3b8; display:block;">SIDE</span><strong style="color:${trade.direction === 'LONG' ? '#16a34a':'#dc2626'}">${escapeHtml(trade.direction)}</strong></div>
          <div><span style="font-size:9px; color:#94a3b8; display:block;">ENTRY</span><strong>${number(trade.entry_price)}</strong></div>
          <div><span style="font-size:9px; color:#94a3b8; display:block;">EXIT</span><strong>${number(trade.exit_price)}</strong></div>
          <div><span style="font-size:9px; color:#94a3b8; display:block;">SIZE</span><strong>${trade.size}</strong></div>
          <div><span style="font-size:9px; color:#94a3b8; display:block;">P&L</span><strong class="${pnlClass}">${money(pnl)}</strong></div>
        </div>
      `;
    }).join("");
  } else {
    rows = `<div style="padding:15px; text-align:center; color:#94a3b8; font-size:12px; background:#f8fafc; border-radius:8px;">No closed trades recorded yet.</div>`;
  }

  return `
    <div style="margin-top:20px; padding-top:15px; border-top:1px solid #e2e8f0;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
        <h3 style="font-size:15px; font-weight:800; margin:0;">Trade History</h3>
        <span style="font-size:11px; color:#64748b; font-weight:700;">${history.length} Total Closed Trades</span>
      </div>
      <div style="max-height:300px; overflow-y:auto; display:flex; flex-direction:column; gap:6px;">
        ${rows}
      </div>
    </div>
  `;
}

function renderAccount(account) {
  const running = account.bot_enabled === true;
  const primary = account.account_type === "primary";
  const position = account.position || {};
  const unrealizedPnl = Number(position.unrealized_pnl || 0);

  let clientLinkBox = "";
  if (!primary && account.token) {
    clientLinkBox = `
      <div style="margin: 15px 0; padding: 12px; background: #e0f2fe; border: 1px solid #bae6fd; border-radius: 8px;">
        <span style="font-size:10px; font-weight:800; color:#0369a1;">CLIENT PRIVATE PORTAL LINK:</span>
        <div style="display:flex; gap:8px; margin-top:5px;">
          <input type="text" readonly value="${window.location.origin}/?token=${account.token}" style="width:100%; padding:6px; font-size:11px; border:1px solid #cbd5e1; border-radius:6px; background:#fff;" />
          <button class="secondary-button" onclick="copyClientLink('${account.token}')">COPY</button>
        </div>
      </div>
    `;
  }

  return `
    <section class="card account-card" style="margin-bottom:25px;">
      <div class="account-header">
        <div>
          <div class="account-type">${primary ? "PRIMARY ADMIN ACCOUNT" : "CLIENT ACCOUNT"}</div>
          <h2>${escapeHtml(account.account_name)}</h2>
          <p>${escapeHtml(account.account_id)}</p>
        </div>
        <div class="${running ? "account-running" : "account-stopped"}">
          ${running ? "BOT RUNNING" : "BOT STOPPED"}
        </div>
      </div>

      ${clientLinkBox}

      <div class="account-stats">
        <div><span>Balance</span><strong>$${number(account.balance)}</strong></div>
        <div><span>Price</span><strong>${number(account.current_price)}</strong></div>
        <div><span>Position</span><strong>${escapeHtml(position.direction || "FLAT")}</strong></div>
        <div><span>Size</span><strong>${position.size ?? 0}</strong></div>
        <div><span>Entry</span><strong>${number(position.entry_price)}</strong></div>
        <div><span>Stop Loss</span><strong>${number(position.stop_loss)}</strong></div>
        <div><span>Unrealized P&L</span><strong class="${unrealizedPnl >= 0 ? 'trade-profit' : 'trade-loss'}">${money(unrealizedPnl)}</strong></div>
        <div><span>All-Time P&L</span><strong class="${(account.statistics?.all_time?.pnl || 0) >= 0 ? 'trade-profit' : 'trade-loss'}">${money(account.statistics?.all_time?.pnl)}</strong></div>
      </div>

      <div class="account-actions">
        ${running ? `<button class="danger-button" onclick="stopBot('${escapeHtml(account.account_id)}')">■ STOP BOT</button>` : `<button class="success-button" onclick="startBot('${escapeHtml(account.account_id)}')">▶ START BOT</button>`}
        ${!primary ? `<button class="delete-button" onclick="deleteClient('${escapeHtml(account.account_id)}')">DELETE CLIENT</button>` : ""}
      </div>

      ${renderPerformance(account)}
      ${renderTradeHistory(account)}
    </section>
  `;
}

async function loadDashboard(force = false) {
  if (dashboardLoading && !force) return;
  dashboardLoading = true;

  try {
    const urlParams = new URLSearchParams(window.location.search);
    const token = urlParams.get("token");
    const fetchUrl = token ? `/api/dashboard?token=${token}` : "/api/dashboard";

    const data = await apiFetch(fetchUrl, { method: "GET", cache: "no-store" });
    const accounts = Array.isArray(data.accounts) ? data.accounts : [];

    if (token) {
      const adminSec = $("admin-management-section");
      if (adminSec) adminSec.style.display = "none";
    }

    if ($("server-ip-display")) {
      $("server-ip-display").textContent = data.server_ip || "Unknown";
    }

    const container = $("accounts-container");
    if (container) {
      container.innerHTML = accounts.map(renderAccount).join("");
    }

    if ($("bot-status")) {
      $("bot-status").textContent = data.server_online === false ? "OFFLINE" : "SYSTEM ONLINE";
    }
    if ($("last-update")) {
      $("last-update").textContent = "Last update: " + new Date().toLocaleTimeString();
    }
  } catch (error) {
    console.error(error);
    if ($("bot-status")) $("bot-status").textContent = "ERROR";
  } finally {
    dashboardLoading = false;
  }
}

loadDashboard();
setInterval(() => loadDashboard(), 3000);
