const API_BASE = "";

const $ = (id) => document.getElementById(id);


function number(value, decimals = 2) {

  if (
    value === null ||
    value === undefined ||
    Number.isNaN(Number(value))
  ) {
    return "--";
  }

  return Number(value).toLocaleString(
    "en-US",
    {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    }
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

  const prefix = n >= 0 ? "+" : "";

  return `${prefix}$${number(n, 2)}`;
}


function setText(
  id,
  value
) {

  const element = $(id);

  if (element) {
    element.textContent = value;
  }
}


function updateStatus(
  running,
  websocket
) {

  const status = $("bot-status");

  if (!status) return;

  if (running && websocket) {

    status.textContent = "BOT LIVE";

    status.className =
      "status online";

  } else if (running) {

    status.textContent =
      "BOT RUNNING / WS WAITING";

    status.className =
      "status warning";

  } else {

    status.textContent =
      "BOT OFFLINE";

    status.className =
      "status offline";
  }

  setText(
    "websocket-status",
    websocket
      ? "CONNECTED"
      : "DISCONNECTED"
  );

  setText(
    "connection-text",
    websocket
      ? "Live connection"
      : "Waiting for WebSocket"
  );
}


function updatePosition(
  position
) {

  const direction =
    position?.direction || "FLAT";

  const directionElement =
    $("position-direction");

  if (directionElement) {

    directionElement.textContent =
      direction;

    directionElement.className =
      "position-" +
      direction.toLowerCase();
  }

  setText(
    "pos-size",
    number(
      position?.size || 0,
      0
    )
  );

  setText(
    "pos-entry",
    position?.entry_price
      ? number(
          position.entry_price,
          2
        )
      : "--"
  );

  setText(
    "pos-sl",
    position?.stop_loss
      ? number(
          position.stop_loss,
          2
        )
      : "--"
  );

  const pnl =
    Number(
      position?.unrealized_pnl || 0
    );

  setText(
    "pos-pnl",
    money(pnl)
  );

  const pnlElement =
    $("pos-pnl");

  if (pnlElement) {

    pnlElement.classList.remove(
      "profit",
      "loss"
    );

    if (pnl > 0) {
      pnlElement.classList.add(
        "profit"
      );
    }

    if (pnl < 0) {
      pnlElement.classList.add(
        "loss"
      );
    }
  }
}


function updateTrades(
  trades
) {

  const tbody =
    $("trade-history");

  if (!tbody) return;

  tbody.innerHTML = "";

  if (
    !Array.isArray(trades)
    || trades.length === 0
  ) {

    const row =
      document.createElement("tr");

    row.innerHTML =
      `<td colspan="6">
         No closed trades yet
       </td>`;

    tbody.appendChild(row);

    return;
  }


  const recent =
    [...trades]
      .reverse()
      .slice(0, 50);


  recent.forEach(
    (trade) => {

      const row =
        document.createElement("tr");

      const pnl =
        Number(
          trade.pnl || 0
        );

      const pnlClass =
        pnl > 0
          ? "profit"
          : pnl < 0
            ? "loss"
            : "";


      row.innerHTML = `

        <td>
          ${
            trade.time
              ? new Date(
                  trade.time
                ).toLocaleString()
              : "--"
          }
        </td>

        <td>
          ${trade.direction || "--"}
        </td>

        <td>
          ${number(
            trade.entry_price,
            2
          )}
        </td>

        <td>
          ${number(
            trade.exit_price,
            2
          )}
        </td>

        <td>
          ${number(
            trade.size,
            0
          )}
        </td>

        <td class="${pnlClass}">
          ${money(pnl)}
        </td>
      `;

      tbody.appendChild(row);
    }
  );
}


function updateDashboard(
  data
) {

  updateStatus(
    data.bot_running,
    data.websocket_connected
  );

  setText(
    "current-price",
    number(
      data.current_price,
      2
    )
  );

  setText(
    "wallet-balance",
    `$${number(
      data.balance,
      2
    )}`
  );

  setText(
    "day-high",
    number(
      data.high,
      2
    )
  );

  setText(
    "day-low",
    number(
      data.low,
      2
    )
  );

  setText(
    "symbol",
    data.symbol || "XAUTUSD"
  );

  setText(
    "session-start",
    data.session_start
      ? new Date(
          data.session_start
        ).toLocaleString()
      : "--"
  );

  setText(
    "last-update",
    data.last_tick
      ? new Date(
          data.last_tick
        ).toLocaleTimeString()
      : "--"
  );


  const stats =
    data.statistics || {};

  setText(
    "total-trades",
    stats.total_trades || 0
  );

  setText(
    "winning-trades",
    stats.winning_trades || 0
  );

  setText(
    "losing-trades",
    stats.losing_trades || 0
  );

  setText(
    "win-rate",
    `${number(
      stats.win_rate || 0,
      1
    )}%`
  );

  setText(
    "total-pnl",
    money(
      data.total_pnl || 0
    )
  );


  updatePosition(
    data.position || {}
  );

  updateTrades(
    data.trades || []
  );
}


async function loadDashboard() {

  try {

    const response =
      await fetch(
        `${API_BASE}/api/dashboard`,
        {
          cache: "no-store"
        }
      );

    if (!response.ok) {

      throw new Error(
        `HTTP ${response.status}`
      );
    }

    const data =
      await response.json();

    if (!data.success) {

      throw new Error(
        data.error ||
        "Dashboard API error"
      );
    }

    updateDashboard(
      data
    );

  } catch (error) {

    console.error(
      "Dashboard error:",
      error
    );

    const status =
      $("bot-status");

    if (status) {

      status.textContent =
        "DASHBOARD OFFLINE";

      status.className =
        "status offline";
    }

    setText(
      "connection-text",
      "API connection failed"
    );
  }
}


loadDashboard();

setInterval(
  loadDashboard,
  3000
);
