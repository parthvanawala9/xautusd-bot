const $ = (id) =>
  document.getElementById(id);


function number(value, decimals = 2) {

  if (
    value === null ||
    value === undefined ||
    Number.isNaN(Number(value))
  ) {
    return "--";
  }

  return Number(value).toFixed(decimals);
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


function setStatus(running) {

  const status = $("bot-status");

  if (!status) {
    return;
  }

  if (running) {

    status.textContent = "BOT RUNNING";

    status.className =
      "status online";

  } else {

    status.textContent = "OFFLINE";

    status.className =
      "status offline";
  }
}


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
        <h2>Trade History</h2>
        <p id="history-date">
          Today's completed trades
        </p>
      </div>

    </div>


    <div class="history-summary">

      <div class="history-stat">
        <span>Today Trades</span>
        <strong id="history-total">0</strong>
      </div>

      <div class="history-stat">
        <span>Winning</span>
        <strong id="history-winning">0</strong>
      </div>

      <div class="history-stat">
        <span>Losing</span>
        <strong id="history-losing">0</strong>
      </div>

      <div class="history-stat">
        <span>Win Rate</span>
        <strong id="history-winrate">0.0%</strong>
      </div>

      <div class="history-stat">
        <span>Today P&L</span>
        <strong id="history-today-pnl">$0.00</strong>
      </div>

      <div class="history-stat">
        <span>Total P&L</span>
        <strong id="history-total-pnl">$0.00</strong>
      </div>

    </div>


    <div class="history-table-wrapper">

      <table class="trade-history-table">

        <thead>

          <tr>
            <th>Time</th>
            <th>Trade</th>
            <th>Entry</th>
            <th>Exit</th>
            <th>Size</th>
            <th>Reason</th>
            <th>P&L</th>
          </tr>

        </thead>

        <tbody id="trade-history-body">

          <tr>
            <td colspan="7">
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
      margin-bottom: 16px;
    }

    .trade-history-header h2 {
      margin: 0 0 4px 0;
    }

    .trade-history-header p {
      margin: 0;
      opacity: 0.65;
      font-size: 13px;
    }

    .history-summary {
      display: grid;
      grid-template-columns:
        repeat(6, minmax(100px, 1fr));
      gap: 10px;
      margin-bottom: 18px;
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
      min-width: 720px;
    }

    .trade-history-table th,
    .trade-history-table td {
      padding: 11px 10px;
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

    @media (max-width: 800px) {

      .history-summary {
        grid-template-columns:
          repeat(2, minmax(120px, 1fr));
      }

      .trade-history-card {
        padding: 14px;
      }

    }

  `;

  document.head.appendChild(
    style
  );
}


function formatTradeTime(
  value
) {

  if (!value) {
    return "--";
  }

  try {

    return new Date(
      value
    ).toLocaleTimeString(
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
    new Date().toISOString()
      .slice(0, 10);


  const todayHistory =
    history.filter(
      trade =>
        trade.date === today
    );


  const total =
    Number(
      statistics.total_trades
      ?? todayHistory.length
    );


  const winning =
    Number(
      statistics.winning_trades
      ?? todayHistory.filter(
        trade =>
          Number(trade.pnl) > 0
      ).length
    );


  const losing =
    Number(
      statistics.losing_trades
      ?? todayHistory.filter(
        trade =>
          Number(trade.pnl) < 0
      ).length
    );


  const winRate =
    Number(
      statistics.win_rate
      ?? (
        total > 0
          ? winning / total * 100
          : 0
      )
    );


  const todayPnl =
    Number(
      statistics.today_pnl
      ?? todayHistory.reduce(
        (sum, trade) =>
          sum + Number(
            trade.pnl || 0
          ),
        0
      )
    );


  const totalPnl =
    Number(
      statistics.total_pnl
      ?? history.reduce(
        (sum, trade) =>
          sum + Number(
            trade.pnl || 0
          ),
        0
      )
    );


  const historyTotal =
    $("history-total");

  const historyWinning =
    $("history-winning");

  const historyLosing =
    $("history-losing");

  const historyWinrate =
    $("history-winrate");

  const historyTodayPnl =
    $("history-today-pnl");

  const historyTotalPnl =
    $("history-total-pnl");


  if (historyTotal) {
    historyTotal.textContent =
      total;
  }

  if (historyWinning) {
    historyWinning.textContent =
      winning;
  }

  if (historyLosing) {
    historyLosing.textContent =
      losing;
  }

  if (historyWinrate) {
    historyWinrate.textContent =
      winRate.toFixed(1) + "%";
  }

  if (historyTodayPnl) {
    historyTodayPnl.textContent =
      money(todayPnl);
  }

  if (historyTotalPnl) {
    historyTotalPnl.textContent =
      money(totalPnl);
  }


  const body =
    $("trade-history-body");

  if (!body) {
    return;
  }


  if (history.length === 0) {

    body.innerHTML = `

      <tr>
        <td colspan="7">
          No completed trades yet.
        </td>
      </tr>

    `;

    return;
  }


  body.innerHTML =
    history
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


function updateDashboard(
  data
) {

  setStatus(
    data.bot_running === true
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
      ready ? "YES" : "NO";
  }


  if ($("last-update")) {

    $("last-update").textContent =
      "Last update: " +
      new Date().toLocaleTimeString();
  }


  renderTradeHistory(
    data
  );
}


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
        "HTTP " + response.status
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


ensureHistorySection();

loadDashboard();


setInterval(
  loadDashboard,
  3000
);
