const $ = (id) => document.getElementById(id);
let dashboardLoading = false;

function number(value, decimals = 2) {
  if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) {
    return "--";
  }
  return Number(value).toFixed(decimals);
}

function integer(value) {
  if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) {
    return "--";
  }
  return Math.round(Number(value)).toString();
}

function money(value) {
  if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) {
    return "--";
  }

  const n = Number(value);

  return n >= 0
    ? "+$" + n.toFixed(2)
    : "-$" + Math.abs(n).toFixed(2);
}

function leverage(value) {
  if (value === null || value === undefined || value === "" || Number.isNaN(Number(value))) {
    return "--";
  }

  const n = Number(value);

  if (!Number.isFinite(n) || n <= 0) {
    return "--";
  }

  return Number.isInteger(n) ? `${n}x` : `${n.toFixed(2)}x`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/*
 * Different bot/backend versions may use slightly different field names.
 * These helpers allow the dashboard to work with all of them.
 */
function firstValue(obj, keys, fallback = null) {
  if (!obj || typeof obj !== "object") return fallback;

  for (const key of keys) {
    const value = obj[key];

    if (
      value !== undefined &&
      value !== null &&
      value !== ""
    ) {
      return value;
    }
  }

  return fallback;
}

function normalizePosition(account) {
  const raw = account?.position || {};
  const exchangePosition = account?.exchange_position || {};

  const merged = {
    ...exchangePosition,
    ...raw
  };

  const direction = firstValue(
    merged,
    ["direction", "side", "position_side"],
    "FLAT"
  );

  const size = firstValue(
    merged,
    ["size", "position_size", "quantity", "contracts"],
    0
  );

  const entryPrice = firstValue(
    merged,
    ["entry_price", "avg_entry_price", "average_entry_price", "entry"],
    null
  );

  const stopLoss = firstValue(
    merged,
    [
      "stop_loss",
      "strategy_stop_loss",
      "exchange_stop_loss",
      "sl",
      "stop"
    ],
    null
  );

  const liquidationPrice = firstValue(
    merged,
    [
      "liquidation_price",
      "liq_price",
      "liquidation",
      "liquidationPrice"
    ],
    firstValue(
      account,
      [
        "liquidation_price",
        "liq_price",
        "liquidationPrice"
      ],
      null
    )
  );

  const markPrice = firstValue(
    merged,
    [
      "mark_price",
      "current_price",
      "current",
      "price",
      "last_price"
    ],
    firstValue(
      account,
      [
        "mark_price",
        "current_price"
      ],
      null
    )
  );

  const leverageValue = firstValue(
    merged,
    [
      "leverage",
      "actual_leverage",
      "position_leverage"
    ],
    firstValue(
      account,
      [
        "leverage",
        "actual_leverage"
      ],
      null
    )
  );

  const unrealizedPnl = firstValue(
    merged,
    [
      "unrealized_pnl",
      "unrealizedPnL",
      "unrealized_profit",
      "pnl"
    ],
    0
  );

  const dayHigh = firstValue(
    merged,
    [
      "day_high",
      "today_high",
      "high",
      "session_high"
    ],
    firstValue(
      account,
      [
        "day_high",
        "today_high",
        "session_high"
      ],
      null
    )
  );

  const dayLow = firstValue(
    merged,
    [
      "day_low",
      "today_low",
      "low",
      "session_low"
    ],
    firstValue(
      account,
      [
        "day_low",
        "today_low",
        "session_low"
      ],
      null
    )
  );

  const margin = firstValue(
    merged,
    ["margin", "position_margin"],
    null
  );

  return {
    ...merged,
    direction,
    size,
    entry_price: entryPrice,
    stop_loss: stopLoss,
    liquidation_price: liquidationPrice,
    mark_price: markPrice,
    leverage: leverageValue,
    unrealized_pnl: unrealizedPnl,
    day_high: dayHigh,
    day_low: dayLow,
    margin
  };
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      ...(options.headers || {}),
      "Content-Type": "application/json",
      "Accept": "application/json"
    }
  });

  let data = null;

  try {
    data = await response.json();
  } catch (error) {
    throw new Error(`Invalid server response (${response.status}).`);
  }

  if (!response.ok || data?.success === false) {
    throw new Error(data?.message || `Request failed.`);
  }

  return data;
}

async function startBot(accountId) {
  try {
    await apiFetch("/api/bot/start", {
      method: "POST",
      body: JSON.stringify({
        account_id: accountId
      })
    });

    await loadDashboard(true);
  } catch (error) {
    alert(error.message);
  }
}

async function stopBot(accountId) {
  if (!window.confirm("STOP BOT will close open positions. Continue?")) {
    return;
  }

  try {
    await apiFetch("/api/bot/stop", {
      method: "POST",
      body: JSON.stringify({
        account_id: accountId
      })
    });

    await loadDashboard(true);
  } catch (error) {
    alert(error.message);
  }
}

function openClientForm() {
  const form = $("client-form");

  if (form) {
    form.style.display =
      form.style.display === "none"
        ? "block"
        : "none";
  }
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
        name,
        api_key: apiKey,
        api_secret: apiSecret,
        subscription_start: new Date(start).toISOString(),
        subscription_expiry: new Date(expiry).toISOString(),
        subscription_fee: Number(fee)
      })
    });

    if ($("client-form")) {
      $("client-form").style.display = "none";
    }

    await loadDashboard(true);

    alert("Client added successfully!");
  } catch (error) {
    alert(error.message);
  }
}

async function deleteClient(accountId) {
  if (!window.confirm("Remove this client account?")) {
    return;
  }

  try {
    await apiFetch("/api/client/delete", {
      method: "POST",
      body: JSON.stringify({
        account_id: accountId
      })
    });

    await loadDashboard(true);
  } catch (error) {
    alert(error.message);
  }
}

function copyClientLink(token) {
  const link =
    `${window.location.origin}/?token=${token}`;

  if (
    navigator.clipboard &&
    typeof navigator.clipboard.writeText === "function"
  ) {
    navigator.clipboard
      .writeText(link)
      .then(() => {
        alert(
          "Client private link copied to clipboard:\n\n" +
          link
        );
      })
      .catch(() => {
        window.prompt(
          "Copy this client private link:",
          link
        );
      });
  } else {
    window.prompt(
      "Copy this client private link:",
      link
    );
  }
}

function renderPerformance(account) {
  const stats = account.statistics || {};
  const today = stats.today || {};
  const allTime = stats.all_time || {};

  return `
    <div
      class="performance-section"
      style="
        margin-top:20px;
        padding-top:15px;
        border-top:1px solid #e2e8f0;
      "
    >
      <h3
        style="
          font-size:15px;
          font-weight:800;
          margin-bottom:10px;
        "
      >
        Trading Performance
      </h3>

      <div
        style="
          display:grid;
          grid-template-columns:1fr 1fr;
          gap:12px;
        "
      >
        <div
          style="
            background:#f8fafc;
            padding:12px;
            border-radius:10px;
            border:1px solid #e2e8f0;
          "
        >
          <h4
            style="
              font-size:11px;
              font-weight:800;
              color:#64748b;
              margin-bottom:8px;
            "
          >
            TODAY
          </h4>

          <div
            style="
              font-size:12px;
              display:flex;
              flex-direction:column;
              gap:4px;
            "
          >
            <div>
              Trades:
              <strong>${today.total_trades || 0}</strong>
            </div>

            <div>
              Wins / Losses:
              <strong style="color:#16a34a;">
                ${today.winning_trades || 0}
              </strong>
              /
              <strong style="color:#dc2626;">
                ${today.losing_trades || 0}
              </strong>
            </div>

            <div>
              Win Rate:
              <strong>
                ${number(today.win_rate, 1)}%
              </strong>
            </div>

            <div>
              P&L:
              <strong
                class="${(today.pnl || 0) >= 0
                  ? "trade-profit"
                  : "trade-loss"}"
              >
                ${money(today.pnl)}
              </strong>
            </div>
          </div>
        </div>

        <div
          style="
            background:#f8fafc;
            padding:12px;
            border-radius:10px;
            border:1px solid #e2e8f0;
          "
        >
          <h4
            style="
              font-size:11px;
              font-weight:800;
              color:#64748b;
              margin-bottom:8px;
            "
          >
            ALL TIME
          </h4>

          <div
            style="
              font-size:12px;
              display:flex;
              flex-direction:column;
              gap:4px;
            "
          >
            <div>
              Trades:
              <strong>${allTime.total_trades || 0}</strong>
            </div>

            <div>
              Wins / Losses:
              <strong style="color:#16a34a;">
                ${allTime.winning_trades || 0}
              </strong>
              /
              <strong style="color:#dc2626;">
                ${allTime.losing_trades || 0}
              </strong>
            </div>

            <div>
              Win Rate:
              <strong>
                ${number(allTime.win_rate, 1)}%
              </strong>
            </div>

            <div>
              P&L:
              <strong
                class="${(allTime.pnl || 0) >= 0
                  ? "trade-profit"
                  : "trade-loss"}"
              >
                ${money(allTime.pnl)}
              </strong>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderTradeHistory(account) {
  const history =
    Array.isArray(account.trade_history)
      ? account.trade_history
      : [];

  let rows = "";

  if (history.length > 0) {
    rows = history
      .slice(-20)
      .reverse()
      .map((trade) => {
        const pnl = Number(trade.pnl || 0);

        const pnlClass =
          pnl > 0
            ? "trade-profit"
            : pnl < 0
              ? "trade-loss"
              : "trade-flat";

        return `
          <div
            style="
              display:grid;
              grid-template-columns:
                1.2fr
                0.8fr
                1fr
                1fr
                0.7fr
                1fr;
              gap:6px;
              padding:10px;
              background:#f8fafc;
              border:1px solid #edf1f5;
              border-radius:8px;
              align-items:center;
              font-size:11px;
              margin-bottom:6px;
            "
          >
            <div>
              <span
                style="
                  font-size:9px;
                  color:#94a3b8;
                  display:block;
                "
              >
                DATE
              </span>

              <strong>
                ${escapeHtml(trade.date)}
              </strong>
            </div>

            <div>
              <span
                style="
                  font-size:9px;
                  color:#94a3b8;
                  display:block;
                "
              >
                SIDE
              </span>

              <strong
                style="
                  color:${trade.direction === "LONG"
                    ? "#16a34a"
                    : "#dc2626"};
                "
              >
                ${escapeHtml(trade.direction)}
              </strong>
            </div>

            <div>
              <span
                style="
                  font-size:9px;
                  color:#94a3b8;
                  display:block;
                "
              >
                ENTRY
              </span>

              <strong>
                ${number(trade.entry_price)}
              </strong>
            </div>

            <div>
              <span
                style="
                  font-size:9px;
                  color:#94a3b8;
                  display:block;
                "
              >
                EXIT
              </span>

              <strong>
                ${number(trade.exit_price)}
              </strong>
            </div>

            <div>
              <span
                style="
                  font-size:9px;
                  color:#94a3b8;
                  display:block;
                "
              >
                SIZE
              </span>

              <strong>
                ${escapeHtml(trade.size)}
              </strong>
            </div>

            <div>
              <span
                style="
                  font-size:9px;
                  color:#94a3b8;
                  display:block;
                "
              >
                P&L
              </span>

              <strong class="${pnlClass}">
                ${money(pnl)}
              </strong>
            </div>
          </div>
        `;
      })
      .join("");
  } else {
    rows = `
      <div
        style="
          padding:15px;
          text-align:center;
          color:#94a3b8;
          font-size:12px;
          background:#f8fafc;
          border-radius:8px;
        "
      >
        No closed trades recorded yet.
      </div>
    `;
  }

  return `
    <div
      style="
        margin-top:20px;
        padding-top:15px;
        border-top:1px solid #e2e8f0;
      "
    >
      <div
        style="
          display:flex;
          justify-content:space-between;
          align-items:center;
          margin-bottom:10px;
        "
      >
        <h3
          style="
            font-size:15px;
            font-weight:800;
            margin:0;
          "
        >
          Trade History
        </h3>

        <span
          style="
            font-size:11px;
            color:#64748b;
            font-weight:700;
          "
        >
          ${history.length} Total Closed Trades
        </span>
      </div>

      <div
        style="
          max-height:300px;
          overflow-y:auto;
          display:flex;
          flex-direction:column;
          gap:6px;
        "
      >
        ${rows}
      </div>
    </div>
  `;
}

function renderRunningPosition(position) {
  const direction =
    String(position.direction || "FLAT").toUpperCase();

  const size = Number(position.size || 0);

  const isRunning =
    direction === "LONG" ||
    direction === "SHORT" ||
    size !== 0;

  if (!isRunning) {
    return `
      <div
        style="
          margin-top:15px;
          padding:14px;
          border-radius:10px;
          background:#f8fafc;
          border:1px solid #e2e8f0;
        "
      >
        <div
          style="
            font-size:11px;
            font-weight:800;
            color:#64748b;
          "
        >
          RUNNING POSITION
        </div>

        <div
          style="
            margin-top:5px;
            font-size:13px;
            font-weight:800;
            color:#94a3b8;
          "
        >
          NO OPEN POSITION
        </div>
      </div>
    `;
  }

  const sideColor =
    direction === "LONG"
      ? "#16a34a"
      : "#dc2626";

  const pnl = Number(position.unrealized_pnl || 0);

  return `
    <div
      style="
        margin-top:15px;
        padding:15px;
        border-radius:12px;
        background:#ffffff;
        border:2px solid ${sideColor};
        box-shadow:0 2px 8px rgba(15,23,42,0.06);
      "
    >
      <div
        style="
          display:flex;
          justify-content:space-between;
          align-items:center;
          margin-bottom:12px;
          gap:10px;
        "
      >
        <div>
          <div
            style="
              font-size:10px;
              font-weight:900;
              color:#64748b;
              letter-spacing:.5px;
            "
          >
            RUNNING POSITION
          </div>

          <div
            style="
              margin-top:3px;
              font-size:20px;
              font-weight:900;
              color:${sideColor};
            "
          >
            ${escapeHtml(direction)}
          </div>
        </div>

        <div
          style="
            text-align:right;
          "
        >
          <div
            style="
              font-size:10px;
              font-weight:800;
              color:#64748b;
            "
          >
            LEVERAGE
          </div>

          <div
            style="
              margin-top:2px;
              font-size:20px;
              font-weight:900;
            "
          >
            ${leverage(position.leverage)}
          </div>
        </div>
      </div>

      <div
        style="
          display:grid;
          grid-template-columns:
            repeat(2, minmax(0, 1fr));
          gap:8px;
        "
      >
        <div
          style="
            padding:10px;
            background:#f8fafc;
            border-radius:8px;
          "
        >
          <span
            style="
              display:block;
              font-size:9px;
              color:#64748b;
              font-weight:800;
            "
          >
            SIZE
          </span>

          <strong>
            ${number(position.size, 0)}
          </strong>
        </div>

        <div
          style="
            padding:10px;
            background:#f8fafc;
            border-radius:8px;
          "
        >
          <span
            style="
              display:block;
              font-size:9px;
              color:#64748b;
              font-weight:800;
            "
          >
            ENTRY PRICE
          </span>

          <strong>
            ${number(position.entry_price)}
          </strong>
        </div>

        <div
          style="
            padding:10px;
            background:#f8fafc;
            border-radius:8px;
          "
        >
          <span
            style="
              display:block;
              font-size:9px;
              color:#64748b;
              font-weight:800;
            "
          >
            CURRENT / MARK PRICE
          </span>

          <strong>
            ${number(position.mark_price)}
          </strong>
        </div>

        <div
          style="
            padding:10px;
            background:#f8fafc;
            border-radius:8px;
          "
        >
          <span
            style="
              display:block;
              font-size:9px;
              color:#64748b;
              font-weight:800;
            "
          >
            STOP LOSS
          </span>

          <strong>
            ${number(position.stop_loss)}
          </strong>
        </div>

        <div
          style="
            padding:10px;
            background:#fff7ed;
            border-radius:8px;
            border:1px solid #fed7aa;
          "
        >
          <span
            style="
              display:block;
              font-size:9px;
              color:#c2410c;
              font-weight:800;
            "
          >
            DAY HIGH
          </span>

          <strong>
            ${number(position.day_high)}
          </strong>
        </div>

        <div
          style="
            padding:10px;
            background:#fff7ed;
            border-radius:8px;
            border:1px solid #fed7aa;
          "
        >
          <span
            style="
              display:block;
              font-size:9px;
              color:#c2410c;
              font-weight:800;
            "
          >
            DAY LOW
          </span>

          <strong>
            ${number(position.day_low)}
          </strong>
        </div>

        <div
          style="
            padding:10px;
            background:#fef2f2;
            border-radius:8px;
            border:1px solid #fecaca;
          "
        >
          <span
            style="
              display:block;
              font-size:9px;
              color:#b91c1c;
              font-weight:800;
            "
          >
            LIQUIDATION PRICE
          </span>

          <strong>
            ${number(position.liquidation_price)}
          </strong>
        </div>

        <div
          style="
            padding:10px;
            background:#f0fdf4;
            border-radius:8px;
            border:1px solid #bbf7d0;
          "
        >
          <span
            style="
              display:block;
              font-size:9px;
              color:#15803d;
              font-weight:800;
            "
          >
            UNREALIZED P&L
          </span>

          <strong
            class="${pnl >= 0
              ? "trade-profit"
              : "trade-loss"}"
          >
            ${money(pnl)}
          </strong>
        </div>
      </div>

      ${
        position.margin !== null &&
        position.margin !== undefined
          ? `
            <div
              style="
                margin-top:8px;
                padding:8px 10px;
                background:#f8fafc;
                border-radius:8px;
                font-size:10px;
                color:#64748b;
              "
            >
              Position Margin:
              <strong style="color:#0f172a;">
                ${number(position.margin)}
              </strong>
            </div>
          `
          : ""
      }
    </div>
  `;
}

function renderAccount(account) {
  const running =
    account.bot_enabled === true;

  const primary =
    account.account_type === "primary";

  const position =
    normalizePosition(account);

  const unrealizedPnl =
    Number(position.unrealized_pnl || 0);

  const accountLeverage =
    firstValue(
      account,
      ["leverage", "actual_leverage"],
      position.leverage
    );

  const accountLiquidation =
    firstValue(
      account,
      [
        "liquidation_price",
        "liq_price",
        "liquidationPrice"
      ],
      position.liquidation_price
    );

  const currentPrice =
    firstValue(
      account,
      [
        "current_price",
        "mark_price"
      ],
      position.mark_price
    );

  let clientLinkBox = "";

  if (!primary && account.token) {
    clientLinkBox = `
      <div
        style="
          margin:15px 0;
          padding:12px;
          background:#e0f2fe;
          border:1px solid #bae6fd;
          border-radius:8px;
        "
      >
        <span
          style="
            font-size:10px;
            font-weight:800;
            color:#0369a1;
          "
        >
          CLIENT PRIVATE PORTAL LINK:
        </span>

        <div
          style="
            display:flex;
            gap:8px;
            margin-top:5px;
          "
        >
          <input
            type="text"
            readonly
            value="${escapeHtml(
              window.location.origin +
              "/?token=" +
              account.token
            )}"
            style="
              width:100%;
              padding:6px;
              font-size:11px;
              border:1px solid #cbd5e1;
              border-radius:6px;
              background:#fff;
            "
          />

          <button
            class="secondary-button"
            onclick="copyClientLink('${escapeHtml(account.token)}')"
          >
            COPY
          </button>
        </div>
      </div>
    `;
  }

  return `
    <section
      class="card account-card"
      style="margin-bottom:25px;"
    >
      <div class="account-header">
        <div>
          <div class="account-type">
            ${
              primary
                ? "PRIMARY ADMIN ACCOUNT"
                : "CLIENT ACCOUNT"
            }
          </div>

          <h2>
            ${escapeHtml(account.account_name)}
          </h2>

          <p>
            ${escapeHtml(account.account_id)}
          </p>
        </div>

        <div
          class="${
            running
              ? "account-running"
              : "account-stopped"
          }"
        >
          ${
            running
              ? "BOT RUNNING"
              : "BOT STOPPED"
          }
        </div>
      </div>

      ${clientLinkBox}

      <div class="account-stats">
        <div>
          <span>Balance</span>
          <strong>
            $${number(account.balance)}
          </strong>
        </div>

        <div>
          <span>Current / Mark Price</span>
          <strong>
            ${number(currentPrice)}
          </strong>
        </div>

        <div>
          <span>Position</span>
          <strong
            style="
              color:${
                position.direction === "LONG"
                  ? "#16a34a"
                  : position.direction === "SHORT"
                    ? "#dc2626"
                    : "inherit"
              };
            "
          >
            ${escapeHtml(position.direction || "FLAT")}
          </strong>
        </div>

        <div>
          <span>Size</span>
          <strong>
            ${number(position.size, 0)}
          </strong>
        </div>

        <div>
          <span>Entry Price</span>
          <strong>
            ${number(position.entry_price)}
          </strong>
        </div>

        <div>
          <span>Stop Loss</span>
          <strong>
            ${number(position.stop_loss)}
          </strong>
        </div>

        <div>
          <span>Day High</span>
          <strong>
            ${number(position.day_high)}
          </strong>
        </div>

        <div>
          <span>Day Low</span>
          <strong>
            ${number(position.day_low)}
          </strong>
        </div>

        <div>
          <span>Actual Leverage</span>
          <strong>
            ${leverage(accountLeverage)}
          </strong>
        </div>

        <div>
          <span>Liquidation Price</span>
          <strong>
            ${number(accountLiquidation)}
          </strong>
        </div>

        <div>
          <span>Unrealized P&L</span>
          <strong
            class="${
              unrealizedPnl >= 0
                ? "trade-profit"
                : "trade-loss"
            }"
          >
            ${money(unrealizedPnl)}
          </strong>
        </div>

        <div>
          <span>All-Time P&L</span>
          <strong
            class="${
              (account.statistics?.all_time?.pnl || 0) >= 0
                ? "trade-profit"
                : "trade-loss"
            }"
          >
            ${money(
              account.statistics?.all_time?.pnl
            )}
          </strong>
        </div>
      </div>

      ${renderRunningPosition(position)}

      <div class="account-actions">
        ${
          running
            ? `
              <button
                class="danger-button"
                onclick="stopBot('${escapeHtml(account.account_id)}')"
              >
                ■ STOP BOT
              </button>
            `
            : `
              <button
                class="success-button"
                onclick="startBot('${escapeHtml(account.account_id)}')"
              >
                ▶ START BOT
              </button>
            `
        }

        ${
          !primary
            ? `
              <button
                class="delete-button"
                onclick="deleteClient('${escapeHtml(account.account_id)}')"
              >
                DELETE CLIENT
              </button>
            `
            : ""
        }
      </div>

      ${renderPerformance(account)}

      ${renderTradeHistory(account)}
    </section>
  `;
}

async function loadDashboard(force = false) {
  if (dashboardLoading && !force) {
    return;
  }

  dashboardLoading = true;

  try {
    const urlParams =
      new URLSearchParams(
        window.location.search
      );

    const token =
      urlParams.get("token");

    const fetchUrl =
      token
        ? `/api/dashboard?token=${encodeURIComponent(token)}`
        : "/api/dashboard";

    const data =
      await apiFetch(fetchUrl, {
        method: "GET",
        cache: "no-store"
      });

    const accounts =
      Array.isArray(data.accounts)
        ? data.accounts
        : [];

    if (token) {
      const adminSec =
        $("admin-management-section");

      if (adminSec) {
        adminSec.style.display = "none";
      }
    }

    if ($("server-ip-display")) {
      $("server-ip-display").textContent =
        data.server_ip || "Unknown";
    }

    const container =
      $("accounts-container");

    if (container) {
      container.innerHTML =
        accounts
          .map(renderAccount)
          .join("");
    }

    if ($("bot-status")) {
      $("bot-status").textContent =
        data.server_online === false
          ? "OFFLINE"
          : "SYSTEM ONLINE";
    }

    if ($("last-update")) {
      $("last-update").textContent =
        "Last update: " +
        new Date().toLocaleTimeString();
    }
  } catch (error) {
    console.error(
      "Dashboard error:",
      error
    );

    if ($("bot-status")) {
      $("bot-status").textContent =
        "ERROR";
    }
  } finally {
    dashboardLoading = false;
  }
}

loadDashboard();

setInterval(() => {
  loadDashboard();
}, 3000);
