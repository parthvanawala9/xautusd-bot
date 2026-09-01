const $ = (id) =>
  document.getElementById(id);


let historyView = "all";


// ============================================================
// BASIC FORMATTERS
// ============================================================

function number(
  value,
  decimals = 2
) {

  if (
    value === null ||
    value === undefined ||
    Number.isNaN(Number(value))
  ) {

    return "--";
  }

  return Number(value).toFixed(
    decimals
  );
}


function money(value) {

  if (
    value === null ||
    value === undefined ||
    Number.isNaN(Number(value))
  ) {

    return "--";
  }

  const n = Number(value);

  if (n >= 0) {

    return "+$" + n.toFixed(2);
  }

  return "-$" + Math.abs(n).toFixed(2);
}


// ============================================================
// BOT START / STOP CONTROL
// ============================================================

function ensureBotControls() {

  if (
    document.getElementById(
      "bot-control-section"
    )
  ) {

    return;
  }

  const container =
    document.querySelector(
      ".container"
    );

  if (!container) {

    return;
  }

  const section =
    document.createElement(
      "div"
    );

  section.id =
    "bot-control-section";

  section.className =
    "card bot-control-card";

  section.innerHTML = `

    <div class="bot-control-header">

      <div>

        <h2>
          Bot Control
        </h2>

        <p id="bot-control-message">
          Bot is stopped. Press START BOT to allow new trades.
        </p>

      </div>

      <div
        id="bot-control-status"
        class="bot-control-status stopped"
      >
        BOT STOPPED
      </div>

    </div>


    <div class="bot-control-buttons">

      <button
        id="start-bot-btn"
        class="bot-control-btn start-bot-btn"
        type="button"
        onclick="startBot()"
      >
        ▶ START BOT
      </button>


      <button
        id="stop-bot-btn"
        class="bot-control-btn stop-bot-btn"
        type="button"
        onclick="stopBot()"
      >
        ■ STOP BOT
      </button>

    </div>


    <div
      id="bot-control-warning"
      class="bot-control-warning"
    >
      START BOT is required before the strategy can take
      any new position.
    </div>

  `;

  /*
   * Put Bot Control at the very top of the dashboard.
   */
  container.insertBefore(
    section,
    container.firstChild
  );

  addBotControlStyles();
}


// ============================================================
// BOT CONTROL STYLES
// ============================================================

function addBotControlStyles() {

  if (
    document.getElementById(
      "bot-control-styles"
    )
  ) {

    return;
  }

  const style =
    document.createElement(
      "style"
    );

  style.id =
    "bot-control-styles";

  style.textContent = `

    .bot-control-card {
      margin-bottom: 20px;
      overflow: hidden;
    }


    .bot-control-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 15px;
      margin-bottom: 16px;
    }


    .bot-control-header h2 {
      margin: 0 0 5px 0;
    }


    .bot-control-header p {
      margin: 0;
      opacity: 0.65;
      font-size: 13px;
      line-height: 1.4;
    }


    .bot-control-status {
      padding: 9px 13px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 800;
      white-space: nowrap;
    }


    .bot-control-status.running {
      background: rgba(34, 197, 94, 0.14);
      color: #16a34a;
    }


    .bot-control-status.stopped {
      background: rgba(239, 68, 68, 0.14);
      color: #dc2626;
    }


    .bot-control-buttons {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }


    .bot-control-btn {
      border: 0;
      border-radius: 10px;
      padding: 13px 16px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 800;
      transition:
        opacity 0.15s ease,
        transform 0.15s ease;
    }


    .bot-control-btn:hover {
      opacity: 0.9;
    }


    .bot-control-btn:active {
      transform: scale(0.98);
    }


    .bot-control-btn:disabled {
      opacity: 0.45;
      cursor: not-allowed;
      transform: none;
    }


    .start-bot-btn {
      background: #16a34a;
      color: white;
    }


    .stop-bot-btn {
      background: #dc2626;
      color: white;
    }


    .bot-control-warning {
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 8px;
      background: rgba(128,128,128,0.08);
      font-size: 12px;
      line-height: 1.45;
      opacity: 0.75;
    }


    @media (max-width: 600px) {

      .bot-control-header {
        align-items: flex-start;
        flex-direction: column;
      }


      .bot-control-buttons {
        grid-template-columns: 1fr;
      }

    }

  `;

  document.head.appendChild(
    style
  );
}


// ============================================================
// BOT CONTROL UI
// ============================================================

function updateBotControls(
  data
) {

  ensureBotControls();

  const enabled =
    data.bot_enabled === true;


  const status =
    $("bot-control-status");


  const message =
    $("bot-control-message");


  const warning =
    $("bot-control-warning");


  const startButton =
    $("start-bot-btn");


  const stopButton =
    $("stop-bot-btn");


  if (status) {

    status.textContent =
      enabled
        ? "BOT RUNNING"
        : "BOT STOPPED";


    status.className =
      enabled
        ? "bot-control-status running"
        : "bot-control-status stopped";
  }


  if (message) {

    if (enabled) {

      message.textContent =
        "Bot is running. New positions can be taken according to the strategy.";

    } else {

      message.textContent =
        "Bot is stopped. It cannot take any new position.";
    }
  }


  if (warning) {

    warning.textContent =
      enabled
        ? "Bot is active. STOP BOT will close the current position, if any, and prevent all new trades."
        : "START BOT is required before the strategy can take any new position.";
  }


  if (startButton) {

    startButton.disabled =
      enabled;
  }


  if (stopButton) {

    stopButton.disabled =
      !enabled;
  }
}


// ============================================================
// START BOT
// ============================================================

async function startBot() {

  const button =
    $("start-bot-btn");


  if (button) {

    button.disabled = true;
    button.textContent =
      "STARTING...";
  }


  try {

    const response =
      await fetch(
        "/api/bot/start",
        {
          method: "POST",
          cache: "no-store"
        }
      );


    const data =
      await response.json();


    if (!response.ok || !data.success) {

      throw new Error(
        data.message
        || "Could not start bot."
      );
    }


    window.__lastDashboardData =
      null;


    await loadDashboard();


  } catch (error) {

    console.error(
      "Start bot error:",
      error
    );


    alert(
      error.message
      || "Could not start bot."
    );


    await loadDashboard();

  } finally {

    const current =
      $("start-bot-btn");

    if (current) {

      current.textContent =
        "▶ START BOT";
    }
  }
}


// ============================================================
// STOP BOT
// ============================================================

async function stopBot() {

  const confirmed =
    window.confirm(
      "STOP BOT will close the current open position, if any, and prevent all new trades. Continue?"
    );


  if (!confirmed) {

    return;
  }


  const button =
    $("stop-bot-btn");


  if (button) {

    button.disabled = true;
    button.textContent =
      "STOPPING...";
  }


  try {

    const response =
      await fetch(
        "/api/bot/stop",
        {
          method: "POST",
          cache: "no-store"
        }
      );


    const data =
      await response.json();


    if (!response.ok || !data.success) {

      throw new Error(
        data.message
        || "Could not stop bot."
      );
    }


    await loadDashboard();


    alert(
      data.message
      || "Bot stopped successfully."
    );


  } catch (error) {

    console.error(
      "Stop bot error:",
      error
    );


    alert(
      error.message
      || "Could not stop bot."
    );


    await loadDashboard();

  } finally {

    const current =
      $("stop-bot-btn");

    if (current) {

      current.textContent =
        "■ STOP BOT";
    }
  }
}


// ============================================================
// TOP STATUS
// ============================================================

function setStatus(
  running
) {

  const status =
    $("bot-status");

  if (!status) {

    return;
  }

  if (running) {

    status.textContent =
      "BOT RUNNING";

    status.className =
      "status online";

  } else {

    status.textContent =
      "OFFLINE";

    status.className =
      "status offline";
  }
}


// ============================================================
// HISTORY SECTION
// ============================================================

function ensureHistorySection() {

  if (
    document.getElementById(
      "trade-history-section"
    )
  ) {

    return;
  }

  const container =
    document.querySelector(
      ".container"
    );

  if (!container) {

    return;
  }

  const section =
    document.createElement(
      "div"
    );

  section.id =
    "trade-history-section";

  section.className =
    "card trade-history-card";

  section.innerHTML = `

    <div class="trade-history-header">

      <div>

        <h2>
          Trade History
        </h2>

        <p id="history-date">
          All completed trades
        </p>

      </div>

      <div class="history-count-box">

        <span>
          Total Saved
        </span>

        <strong id="history-saved-count">
          0
        </strong>

      </div>

    </div>


    <div class="history-view-buttons">

      <button
        id="history-all-btn"
        class="history-view-btn active"
        type="button"
        onclick="setHistoryView('all')"
      >
        ALL TIME
      </button>


      <button
        id="history-today-btn"
        class="history-view-btn"
        type="button"
        onclick="setHistoryView('today')"
      >
        TODAY
      </button>

    </div>


    <div
      id="all-time-stat-title"
      class="history-stat-title"
    >
      ALL-TIME PERFORMANCE
    </div>


    <div
      id="all-time-summary"
      class="history-summary"
    >

      <div class="history-stat">

        <span>
          Total Trades
        </span>

        <strong id="all-history-total">
          0
        </strong>

      </div>


      <div class="history-stat">

        <span>
          Winning
        </span>

        <strong id="all-history-winning">
          0
        </strong>

      </div>


      <div class="history-stat">

        <span>
          Losing
        </span>

        <strong id="all-history-losing">
          0
        </strong>

      </div>


      <div class="history-stat">

        <span>
          Win Rate
        </span>

        <strong id="all-history-winrate">
          0.0%
        </strong>

      </div>


      <div class="history-stat">

        <span>
          Total P&L
        </span>

        <strong id="all-history-pnl">
          $0.00
        </strong>

      </div>

    </div>


    <div
      id="today-stat-title"
      class="history-stat-title"
      style="display:none;"
    >
      TODAY'S PERFORMANCE
    </div>


    <div
      id="today-summary"
      class="history-summary"
      style="display:none;"
    >

      <div class="history-stat">

        <span>
          Today's Trades
        </span>

        <strong id="today-history-total">
          0
        </strong>

      </div>


      <div class="history-stat">

        <span>
          Winning
        </span>

        <strong id="today-history-winning">
          0
        </strong>

      </div>


      <div class="history-stat">

        <span>
          Losing
        </span>

        <strong id="today-history-losing">
          0
        </strong>

      </div>


      <div class="history-stat">

        <span>
          Win Rate
        </span>

        <strong id="today-history-winrate">
          0.0%
        </strong>

      </div>


      <div class="history-stat">

        <span>
          Today P&L
        </span>

        <strong id="today-history-pnl">
          $0.00
        </strong>

      </div>

    </div>


    <div class="history-table-wrapper">

      <table class="trade-history-table">

        <thead>

          <tr>

            <th>
              Date
            </th>

            <th>
              Entry Time
            </th>

            <th>
              Exit Time
            </th>

            <th>
              Trade
            </th>

            <th>
              Entry
            </th>

            <th>
              Exit
            </th>

            <th>
              Size
            </th>

            <th>
              Reason
            </th>

            <th>
              P&L
            </th>

          </tr>

        </thead>


        <tbody id="trade-history-body">

          <tr>

            <td colspan="9">
              No completed trades yet.
            </td>

          </tr>

        </tbody>

      </table>

    </div>

  `;

  container.appendChild(
    section
  );

  addHistoryStyles();
}


// ============================================================
// HISTORY STYLES
// ============================================================

function addHistoryStyles() {

  if (
    document.getElementById(
      "trade-history-styles"
    )
  ) {

    return;
  }

  const style =
    document.createElement(
      "style"
    );

  style.id =
    "trade-history-styles";

  style.textContent = `

    .trade-history-card {
      margin-top: 20px;
      overflow: hidden;
    }


    .trade-history-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
    }


    .trade-history-header h2 {
      margin: 0 0 4px 0;
    }


    .trade-history-header p {
      margin: 0;
      opacity: 0.65;
      font-size: 13px;
    }


    .history-count-box {
      padding: 10px 14px;
      border-radius: 10px;
      background: rgba(128,128,128,0.08);
      text-align: center;
      min-width: 80px;
    }


    .history-count-box span {
      display: block;
      font-size: 11px;
      opacity: 0.65;
      margin-bottom: 3px;
    }


    .history-count-box strong {
      font-size: 18px;
    }


    .history-view-buttons {
      display: flex;
      gap: 8px;
      margin-bottom: 18px;
    }


    .history-view-btn {
      border: 1px solid rgba(128,128,128,0.25);
      background: transparent;
      padding: 8px 14px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 12px;
      font-weight: 700;
    }


    .history-view-btn.active {
      background: rgba(128,128,128,0.16);
    }


    .history-stat-title {
      font-size: 12px;
      font-weight: 700;
      opacity: 0.65;
      margin-bottom: 8px;
      letter-spacing: 0.4px;
    }


    .history-summary {
      display: grid;
      grid-template-columns:
        repeat(5, minmax(100px, 1fr));
      gap: 10px;
      margin-bottom: 20px;
    }


    .history-stat {
      padding: 12px;
      border-radius: 10px;
      background: rgba(128,128,128,0.08);
    }


    .history-stat span {
      display: block;
      font-size: 12px;
      opacity: 0.65;
      margin-bottom: 5px;
    }


    .history-stat strong {
      font-size: 18px;
    }


    .history-table-wrapper {
      width: 100%;
      overflow-x: auto;
    }


    .trade-history-table {
      width: 100%;
      border-collapse: collapse;
      min-width: 980px;
    }


    .trade-history-table th,
    .trade-history-table td {
      padding: 10px;
      text-align: left;
      border-bottom: 1px solid
        rgba(128,128,128,0.15);
      white-space: nowrap;
    }


    .trade-history-table th {
      font-size: 12px;
      opacity: 0.65;
      font-weight: 600;
    }


    .trade-history-table td {
      font-size: 13px;
    }


    .history-long {
      font-weight: 700;
    }


    .history-short {
      font-weight: 700;
    }


    .history-profit {
      font-weight: 700;
    }


    .history-loss {
      font-weight: 700;
    }


    .history-empty {
      text-align: center !important;
      padding: 24px !important;
      opacity: 0.6;
    }


    @media (max-width: 800px) {

      .history-summary {
        grid-template-columns:
          repeat(2, minmax(120px, 1fr));
      }


      .trade-history-card {
        padding: 14px;
      }


      .trade-history-header {
        align-items: flex-start;
      }

    }

  `;

  document.head.appendChild(
    style
  );
}


// ============================================================
// HISTORY VIEW
// ============================================================

function setHistoryView(
  view
) {

  historyView = view;

  const allButton =
    $("history-all-btn");

  const todayButton =
    $("history-today-btn");


  if (allButton) {

    allButton.classList.toggle(
      "active",
      view === "all"
    );
  }


  if (todayButton) {

    todayButton.classList.toggle(
      "active",
      view === "today"
    );
  }


  const allTitle =
    $("all-time-stat-title");

  const allSummary =
    $("all-time-summary");

  const todayTitle =
    $("today-stat-title");

  const todaySummary =
    $("today-summary");


  if (view === "all") {

    if (allTitle) {

      allTitle.style.display =
        "block";
    }


    if (allSummary) {

      allSummary.style.display =
        "grid";
    }


    if (todayTitle) {

      todayTitle.style.display =
        "none";
    }


    if (todaySummary) {

      todaySummary.style.display =
        "none";
    }


    const subtitle =
      $("history-date");


    if (subtitle) {

      subtitle.textContent =
        "All completed trades — newest first";
    }

  } else {

    if (allTitle) {

      allTitle.style.display =
        "none";
    }


    if (allSummary) {

      allSummary.style.display =
        "none";
    }


    if (todayTitle) {

      todayTitle.style.display =
        "block";
    }


    if (todaySummary) {

      todaySummary.style.display =
        "grid";
    }


    const subtitle =
      $("history-date");


    if (subtitle) {

      subtitle.textContent =
        "Today's completed trades";
    }
  }


  if (
    window.__lastDashboardData
  ) {

    renderTradeHistory(
      window.__lastDashboardData
    );
  }
}


// ============================================================
// TIME FORMAT
// ============================================================

function formatTradeDate(
  value
) {

  if (!value) {

    return "--";
  }


  try {

    const date =
      new Date(value);


    if (
      Number.isNaN(
        date.getTime()
      )
    ) {

      return value;
    }


    return date.toLocaleDateString(
      [],
      {
        day: "2-digit",
        month: "2-digit",
        year: "numeric"
      }
    );

  } catch {

    return value;
  }
}


function formatTradeTime(
  value
) {

  if (!value) {

    return "--";
  }


  try {

    const date =
      new Date(value);


    if (
      Number.isNaN(
        date.getTime()
      )
    ) {

      return value;
    }


    return date.toLocaleTimeString(
      [],
      {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
      }
    );

  } catch {

    return value;
  }
}


// ============================================================
// RENDER HISTORY
// ============================================================

function renderTradeHistory(
  data
) {

  ensureHistorySection();

  const history =
    Array.isArray(
      data.trade_history
    )
      ? data.trade_history
      : [];


  const statistics =
    data.statistics || {};


  const today =
    new Date()
      .toLocaleDateString(
        "en-CA"
      );


  const todayHistory =
    history.filter(
      trade =>
        trade.date === today
    );


  // ----------------------------------------------------------
  // ALL-TIME STATISTICS
  // ----------------------------------------------------------

  const allTime =
    statistics.all_time || {};


  const allTimeTotal =
    Number(
      allTime.total_trades
      ?? history.length
    );


  const allTimeWinning =
    Number(
      allTime.winning_trades
      ?? history.filter(
        trade =>
          Number(trade.pnl) > 0
      ).length
    );


  const allTimeLosing =
    Number(
      allTime.losing_trades
      ?? history.filter(
        trade =>
          Number(trade.pnl) < 0
      ).length
    );


  const allTimeWinRate =
    Number(
      allTime.win_rate
      ?? (
        allTimeTotal > 0
          ? (
              allTimeWinning
              / allTimeTotal
              * 100
            )
          : 0
      )
    );


  const allTimePnl =
    Number(
      allTime.pnl
      ?? history.reduce(
        (sum, trade) =>
          sum + Number(
            trade.pnl || 0
          ),
        0
      )
    );


  // ----------------------------------------------------------
  // TODAY STATISTICS
  // ----------------------------------------------------------

  const todayStats =
    statistics.today || {};


  const todayTotal =
    Number(
      todayStats.total_trades
      ?? todayHistory.length
    );


  const todayWinning =
    Number(
      todayStats.winning_trades
      ?? todayHistory.filter(
        trade =>
          Number(trade.pnl) > 0
      ).length
    );


  const todayLosing =
    Number(
      todayStats.losing_trades
      ?? todayHistory.filter(
        trade =>
          Number(trade.pnl) < 0
      ).length
    );


  const todayWinRate =
    Number(
      todayStats.win_rate
      ?? (
        todayTotal > 0
          ? (
              todayWinning
              / todayTotal
              * 100
            )
          : 0
      )
    );


  const todayPnl =
    Number(
      todayStats.pnl
      ?? todayHistory.reduce(
        (sum, trade) =>
          sum + Number(
            trade.pnl || 0
          ),
        0
      )
    );


  // ----------------------------------------------------------
  // UPDATE ALL-TIME BOX
  // ----------------------------------------------------------

  if ($("all-history-total")) {

    $("all-history-total")
      .textContent =
      allTimeTotal;
  }


  if ($("all-history-winning")) {

    $("all-history-winning")
      .textContent =
      allTimeWinning;
  }


  if ($("all-history-losing")) {

    $("all-history-losing")
      .textContent =
      allTimeLosing;
  }


  if ($("all-history-winrate")) {

    $("all-history-winrate")
      .textContent =
      allTimeWinRate.toFixed(1)
      + "%";
  }


  if ($("all-history-pnl")) {

    $("all-history-pnl")
      .textContent =
      money(allTimePnl);
  }


  // ----------------------------------------------------------
  // UPDATE TODAY BOX
  // ----------------------------------------------------------

  if ($("today-history-total")) {

    $("today-history-total")
      .textContent =
      todayTotal;
  }


  if ($("today-history-winning")) {

    $("today-history-winning")
      .textContent =
      todayWinning;
  }


  if ($("today-history-losing")) {

    $("today-history-losing")
      .textContent =
      todayLosing;
  }


  if ($("today-history-winrate")) {

    $("today-history-winrate")
      .textContent =
      todayWinRate.toFixed(1)
      + "%";
  }


  if ($("today-history-pnl")) {

    $("today-history-pnl")
      .textContent =
      money(todayPnl);
  }


  // ----------------------------------------------------------
  // TOTAL SAVED COUNT
  // ----------------------------------------------------------

  if ($("history-saved-count")) {

    $("history-saved-count")
      .textContent =
      Number(
        data.history_count
        ?? history.length
      );
  }


  // ----------------------------------------------------------
  // SELECT HISTORY
  // ----------------------------------------------------------

  const visibleHistory =
    historyView === "today"
      ? todayHistory
      : history;


  const body =
    $("trade-history-body");


  if (!body) {

    return;
  }


  if (
    visibleHistory.length === 0
  ) {

    const message =
      historyView === "today"
        ? "No completed trades today."
        : "No completed trades yet.";


    body.innerHTML = `

      <tr>

        <td
          colspan="9"
          class="history-empty"
        >
          ${message}
        </td>

      </tr>

    `;

    return;
  }


  body.innerHTML =
    visibleHistory
      .map(
        trade => {

          const direction =
            trade.direction || "--";


          const pnl =
            Number(
              trade.pnl || 0
            );


          const pnlClass =
            pnl >= 0
              ? "history-profit"
              : "history-loss";


          const directionClass =
            direction === "LONG"
              ? "history-long"
              : "history-short";


          return `

            <tr>

              <td>
                ${formatTradeDate(
                  trade.exit_time
                )}
              </td>


              <td>
                ${formatTradeTime(
                  trade.entry_time
                )}
              </td>


              <td>
                ${formatTradeTime(
                  trade.exit_time
                )}
              </td>


              <td class="${directionClass}">
                ${direction}
              </td>


              <td>
                ${number(
                  trade.entry_price
                )}
              </td>


              <td>
                ${number(
                  trade.exit_price
                )}
              </td>


              <td>
                ${trade.size ?? 0}
              </td>


              <td>
                ${trade.reason || "--"}
              </td>


              <td class="${pnlClass}">
                ${money(pnl)}
              </td>

            </tr>

          `;
        }
      )
      .join("");
}


// ============================================================
// UPDATE DASHBOARD
// ============================================================

function updateDashboard(
  data
) {

  window.__lastDashboardData =
    data;


  /*
   * Process status.
   * This remains separate from trading status.
   */
  setStatus(
    data.bot_running === true
  );


  /*
   * Trading START / STOP status.
   */
  updateBotControls(
    data
  );


  if ($("symbol")) {

    $("symbol").textContent =
      data.symbol || "XAUTUSD";
  }


  if ($("current-price")) {

    $("current-price").textContent =
      number(
        data.current_price
      );
  }


  if ($("today-high")) {

    $("today-high").textContent =
      number(
        data.high
      );
  }


  if ($("today-low")) {

    $("today-low").textContent =
      number(
        data.low
      );
  }


  if ($("wallet-balance")) {

    $("wallet-balance").textContent =
      money(
        data.balance
      );
  }


  const position =
    data.position || {};


  if ($("pos-direction")) {

    $("pos-direction").textContent =
      position.direction || "FLAT";
  }


  if ($("pos-size")) {

    $("pos-size").textContent =
      position.size ?? 0;
  }


  if ($("pos-entry")) {

    $("pos-entry").textContent =
      number(
        position.entry_price
      );
  }


  if ($("pos-sl")) {

    $("pos-sl").textContent =
      number(
        position.stop_loss
      );
  }


  if ($("pos-pnl")) {

    $("pos-pnl").textContent =
      money(
        position.unrealized_pnl
      );
  }


  const ready =
    data.session &&
    data.session.ready;


  if ($("session-ready")) {

    $("session-ready").textContent =
      ready
        ? "YES"
        : "NO";
  }


  if ($("last-update")) {

    $("last-update").textContent =
      "Last update: "
      + new Date()
        .toLocaleTimeString();
  }


  renderTradeHistory(
    data
  );
}


// ============================================================
// LOAD DASHBOARD
// ============================================================

async function loadDashboard() {

  try {

    const response =
      await fetch(
        "/api/dashboard",
        {
          cache: "no-store"
        }
      );


    if (!response.ok) {

      throw new Error(
        "HTTP "
        + response.status
      );
    }


    const data =
      await response.json();


    updateDashboard(
      data
    );

  } catch (error) {

    console.error(
      "Dashboard error:",
      error
    );


    setStatus(false);


    if ($("last-update")) {

      $("last-update").textContent =
        "Dashboard connection failed";
    }
  }
}


// ============================================================
// START
// ============================================================

ensureBotControls();

ensureHistorySection();

loadDashboard();


setInterval(
  loadDashboard,
  3000
);
