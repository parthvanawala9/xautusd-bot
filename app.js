const $ = (id) =>
  document.getElementById(id);

let adminPin = "";
let dashboardLoading = false;


// ============================================================
// FORMATTERS
// ============================================================

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

  return n >= 0
    ? "+$" + n.toFixed(2)
    : "-$" + Math.abs(n).toFixed(2);
}


function moneyClass(value) {

  const n = Number(value);

  if (
    Number.isNaN(n) ||
    n === 0
  ) {
    return "";
  }

  return n > 0
    ? "history-profit"
    : "history-loss";
}


function escapeHtml(value) {

  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}


// ============================================================
// SMALL DASHBOARD STYLES
//
// These styles are injected here so index.html and style.css
// do not need to be changed.
// ============================================================

function ensureDashboardStyles() {

  if (
    document.getElementById(
      "xaut-dashboard-extra-styles"
    )
  ) {
    return;
  }

  const style =
    document.createElement("style");

  style.id =
    "xaut-dashboard-extra-styles";

  style.textContent = `

    .xaut-statistics-section {
      margin-top: 18px;
      border-top: 1px solid #e8ebef;
      padding-top: 18px;
    }

    .xaut-section-title {
      font-size: 18px;
      font-weight: 800;
      margin: 0 0 12px 0;
      color: #111827;
    }

    .xaut-period-grid {
      display: grid;
      grid-template-columns:
        repeat(2, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }

    .xaut-period-card {
      background: #f8fafc;
      border: 1px solid #e7ebf0;
      border-radius: 14px;
      padding: 15px;
    }

    .xaut-period-title {
      font-size: 14px;
      font-weight: 800;
      color: #111827;
      margin-bottom: 12px;
    }

    .xaut-period-stats {
      display: grid;
      grid-template-columns:
        repeat(5, minmax(0, 1fr));
      gap: 8px;
    }

    .xaut-period-stat {
      background: #ffffff;
      border-radius: 10px;
      padding: 10px;
      min-width: 0;
    }

    .xaut-period-stat span {
      display: block;
      font-size: 11px;
      color: #6b7280;
      margin-bottom: 5px;
      line-height: 1.25;
    }

    .xaut-period-stat strong {
      display: block;
      font-size: 14px;
      font-weight: 800;
      color: #111827;
      word-break: break-word;
    }

    .xaut-history-section {
      margin-top: 18px;
    }

    .xaut-history-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }

    .xaut-history-count {
      font-size: 12px;
      color: #6b7280;
      font-weight: 700;
    }

    .xaut-history-wrap {
      width: 100%;
      overflow-x: auto;
      border: 1px solid #e7ebf0;
      border-radius: 12px;
      background: #ffffff;
    }

    .xaut-history-table {
      width: 100%;
      min-width: 820px;
      border-collapse: collapse;
      font-size: 12px;
    }

    .xaut-history-table th {
      text-align: left;
      padding: 11px 10px;
      background: #f8fafc;
      color: #6b7280;
      font-weight: 800;
      white-space: nowrap;
      border-bottom: 1px solid #e7ebf0;
    }

    .xaut-history-table td {
      padding: 11px 10px;
      border-bottom: 1px solid #eef1f4;
      color: #111827;
      white-space: nowrap;
    }

    .xaut-history-table tr:last-child td {
      border-bottom: none;
    }

    .xaut-direction-long {
      font-weight: 800;
      color: #087f3f;
    }

    .xaut-direction-short {
      font-weight: 800;
      color: #b42318;
    }

    .history-profit {
      color: #087f3f !important;
      font-weight: 800;
    }

    .history-loss {
      color: #b42318 !important;
      font-weight: 800;
    }

    .xaut-empty-history {
      border: 1px solid #e7ebf0;
      border-radius: 12px;
      padding: 20px;
      text-align: center;
      color: #6b7280;
      background: #f8fafc;
      font-size: 13px;
      font-weight: 600;
    }

    .xaut-unrealized-live {
      font-weight: 900 !important;
    }

    @media (max-width: 800px) {

      .xaut-period-grid {
        grid-template-columns: 1fr;
      }

      .xaut-period-stats {
        grid-template-columns:
          repeat(2, minmax(0, 1fr));
      }

    }

  `;

  document.head.appendChild(
    style
  );
}


// ============================================================
// API
// ============================================================

async function apiFetch(
  url,
  options = {}
) {

  const requestOptions = {
    ...options,

    headers: {
      ...(options.headers || {}),
      "Content-Type":
        "application/json",
      "Accept":
        "application/json",
      "X-Admin-Pin":
        adminPin
    }
  };

  let response;

  try {

    response = await fetch(
      url,
      requestOptions
    );

  } catch {

    throw new Error(
      "Dashboard server is unreachable."
    );
  }

  const contentType =
    response.headers.get(
      "content-type"
    ) || "";

  let data = null;

  if (
    contentType
      .toLowerCase()
      .includes("application/json")
  ) {

    try {

      data = await response.json();

    } catch {

      throw new Error(
        `Server returned invalid JSON (HTTP ${response.status}).`
      );
    }

  } else {

    let text = "";

    try {

      text = await response.text();

    } catch {

      text = "";
    }

    const cleanText =
      text
        .replace(/<[^>]*>/g, " ")
        .replace(/\s+/g, " ")
        .trim();

    throw new Error(
      cleanText
        ? `Server returned a non-JSON response (HTTP ${response.status}): ${cleanText.slice(0, 180)}`
        : `Server returned a non-JSON response (HTTP ${response.status}).`
    );
  }

  if (
    !response.ok ||
    data?.success === false
  ) {

    throw new Error(
      data?.message ||
      `Request failed (HTTP ${response.status}).`
    );
  }

  return data;
}


// ============================================================
// PIN
// ============================================================

function requestPin() {

  const saved =
    localStorage.getItem(
      "xaut_admin_pin"
    );

  if (saved !== null) {

    adminPin = saved;

    return;
  }

  adminPin =
    window.prompt(
      "Enter dashboard admin PIN:"
    ) || "";

  localStorage.setItem(
    "xaut_admin_pin",
    adminPin
  );
}


// ============================================================
// BOT CONTROL
// ============================================================

async function startBot(
  accountId
) {

  try {

    await apiFetch(
      "/api/bot/start",
      {
        method: "POST",

        body: JSON.stringify({
          account_id:
            accountId
        })
      }
    );

    await loadDashboard(
      true
    );

  } catch (error) {

    alert(
      error.message
    );
  }
}


async function stopBot(
  accountId
) {

  if (
    !window.confirm(
      "STOP BOT will close the open position on this account. Continue?"
    )
  ) {
    return;
  }

  try {

    await apiFetch(
      "/api/bot/stop",
      {
        method: "POST",

        body: JSON.stringify({
          account_id:
            accountId
        })
      }
    );

    await loadDashboard(
      true
    );

  } catch (error) {

    alert(
      error.message
    );
  }
}


// ============================================================
// ADD CLIENT
// ============================================================

function openClientForm() {

  const form =
    $("client-form");

  if (!form) {
    return;
  }

  form.style.display =
    form.style.display === "none"
      ? "block"
      : "none";
}


async function addClient() {

  const name =
    $("client-name")?.value.trim();

  const apiKey =
    $("client-api-key")?.value.trim();

  const apiSecret =
    $("client-api-secret")?.value.trim();

  const start =
    $("client-start")?.value;

  const expiry =
    $("client-expiry")?.value;

  const fee =
    $("client-fee")?.value || 0;


  if (!name) {

    alert(
      "Enter client name."
    );

    return;
  }


  if (!apiKey || !apiSecret) {

    alert(
      "Enter Delta API key and API secret."
    );

    return;
  }


  if (!start || !expiry) {

    alert(
      "Enter subscription start and expiry."
    );

    return;
  }


  try {

    await apiFetch(
      "/api/client/add",
      {
        method: "POST",

        body:
          JSON.stringify({

            name,

            api_key:
              apiKey,

            api_secret:
              apiSecret,

            subscription_start:
              new Date(
                start
              ).toISOString(),

            subscription_expiry:
              new Date(
                expiry
              ).toISOString(),

            subscription_fee:
              Number(fee)
          })
      }
    );


    const form =
      $("client-form");

    if (form) {

      form.style.display =
        "none";
    }


    if ($("client-name")) {

      $("client-name").value =
        "";
    }


    if ($("client-api-key")) {

      $("client-api-key").value =
        "";
    }


    if ($("client-api-secret")) {

      $("client-api-secret").value =
        "";
    }


    if ($("client-start")) {

      $("client-start").value =
        "";
    }


    if ($("client-expiry")) {

      $("client-expiry").value =
        "";
    }


    if ($("client-fee")) {

      $("client-fee").value =
        "";
    }


    await loadDashboard(
      true
    );


    alert(
      "Client account added successfully."
    );

  } catch (error) {

    alert(
      error.message
    );
  }
}


// ============================================================
// CLIENT SUBSCRIPTION UPDATE
// ============================================================

async function updateSubscription(
  accountId
) {

  const start =
    prompt(
      "Subscription start date/time (local):"
    );

  if (!start) {
    return;
  }

  const expiry =
    prompt(
      "Subscription expiry date/time (local):"
    );

  if (!expiry) {
    return;
  }

  const fee =
    prompt(
      "Subscription fee:"
    );


  try {

    await apiFetch(
      "/api/client/subscription",
      {
        method: "POST",

        body:
          JSON.stringify({

            account_id:
              accountId,

            subscription_start:
              new Date(
                start
              ).toISOString(),

            subscription_expiry:
              new Date(
                expiry
              ).toISOString(),

            subscription_fee:
              Number(fee || 0)
          })
      }
    );


    await loadDashboard(
      true
    );

  } catch (error) {

    alert(
      error.message
    );
  }
}


// ============================================================
// DELETE CLIENT
// ============================================================

async function deleteClient(
  accountId
) {

  if (
    !window.confirm(
      "Remove this client account? If a position exists, the system will try to close it first."
    )
  ) {
    return;
  }


  try {

    await apiFetch(
      "/api/client/delete",
      {
        method: "POST",

        body:
          JSON.stringify({
            account_id:
              accountId
          })
      }
    );


    await loadDashboard(
      true
    );

  } catch (error) {

    alert(
      error.message
    );
  }
}


// ============================================================
// STATISTICS
// ============================================================

function renderStatistics(
  statistics
) {

  const stats =
    statistics || {};

  const today =
    stats.today || {};

  const allTime =
    stats.all_time || {};


  return `

    <div class="xaut-statistics-section">

      <div class="xaut-section-title">
        Trading Performance
      </div>


      <div class="xaut-period-grid">

        <div class="xaut-period-card">

          <div class="xaut-period-title">
            Today
          </div>

          <div class="xaut-period-stats">

            <div class="xaut-period-stat">
              <span>Total Trades</span>
              <strong>
                ${Number(
                  today.total_trades || 0
                )}
              </strong>
            </div>


            <div class="xaut-period-stat">
              <span>Winning Trades</span>
              <strong>
                ${Number(
                  today.winning_trades || 0
                )}
              </strong>
            </div>


            <div class="xaut-period-stat">
              <span>Losing Trades</span>
              <strong>
                ${Number(
                  today.losing_trades || 0
                )}
              </strong>
            </div>


            <div class="xaut-period-stat">
              <span>Win Rate</span>
              <strong>
                ${number(
                  today.win_rate || 0,
                  1
                )}%
              </strong>
            </div>


            <div class="xaut-period-stat">

              <span>Today P&L</span>

              <strong
                class="${moneyClass(
                  today.pnl
                )}"
              >
                ${money(
                  today.pnl || 0
                )}
              </strong>

            </div>

          </div>

        </div>


        <div class="xaut-period-card">

          <div class="xaut-period-title">
            All Time
          </div>

          <div class="xaut-period-stats">

            <div class="xaut-period-stat">
              <span>Total Trades</span>
              <strong>
                ${Number(
                  allTime.total_trades || 0
                )}
              </strong>
            </div>


            <div class="xaut-period-stat">
              <span>Winning Trades</span>
              <strong>
                ${Number(
                  allTime.winning_trades || 0
                )}
              </strong>
            </div>


            <div class="xaut-period-stat">
              <span>Losing Trades</span>
              <strong>
                ${Number(
                  allTime.losing_trades || 0
                )}
              </strong>
            </div>


            <div class="xaut-period-stat">
              <span>Win Rate</span>
              <strong>
                ${number(
                  allTime.win_rate || 0,
                  1
                )}%
              </strong>
            </div>


            <div class="xaut-period-stat">

              <span>All-Time P&L</span>

              <strong
                class="${moneyClass(
                  allTime.pnl
                )}"
              >
                ${money(
                  allTime.pnl || 0
                )}
              </strong>

            </div>

          </div>

        </div>

      </div>

    </div>

  `;
}


// ============================================================
// TRADE HISTORY
// ============================================================

function renderTradeHistory(
  history
) {

  const trades =
    Array.isArray(history)
      ? history
      : [];


  if (!trades.length) {

    return `

      <div class="xaut-history-section">

        <div class="xaut-history-header">

          <div class="xaut-section-title">
            Trade History
          </div>

          <div class="xaut-history-count">
            0 trades
          </div>

        </div>


        <div class="xaut-empty-history">
          No trades recorded yet.
        </div>

      </div>

    `;
  }


  return `

    <div class="xaut-history-section">

      <div class="xaut-history-header">

        <div class="xaut-section-title">
          Trade History
        </div>

        <div class="xaut-history-count">
          ${trades.length}
          ${
            trades.length === 1
              ? "trade"
              : "trades"
          }
        </div>

      </div>


      <div class="xaut-history-wrap">

        <table class="xaut-history-table">

          <thead>

            <tr>

              <th>Date</th>
              <th>Direction</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Size</th>
              <th>Stop Loss</th>
              <th>P&L</th>
              <th>Reason</th>

            </tr>

          </thead>


          <tbody>

            ${trades
              .map(
                (
                  trade
                ) => {

                  const direction =
                    String(
                      trade.direction || ""
                    ).toUpperCase();

                  const pnl =
                    Number(
                      trade.pnl || 0
                    );


                  return `

                    <tr>

                      <td>
                        ${escapeHtml(
                          formatDate(
                            trade.exit_time ||
                            trade.entry_time ||
                            trade.date
                          )
                        )}
                      </td>


                      <td>

                        <span
                          class="${
                            direction === "LONG"
                              ? "xaut-direction-long"
                              : "xaut-direction-short"
                          }"
                        >
                          ${escapeHtml(
                            direction ||
                            "--"
                          )}
                        </span>

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
                        ${Number(
                          trade.size || 0
                        )}
                      </td>


                      <td>
                        ${number(
                          trade.stop_loss
                        )}
                      </td>


                      <td
                        class="${moneyClass(
                          pnl
                        )}"
                      >
                        ${money(
                          pnl
                        )}
                      </td>


                      <td>
                        ${escapeHtml(
                          trade.reason ||
                          "--"
                        )}
                      </td>

                    </tr>

                  `;
                }
              )
              .join("")}

          </tbody>

        </table>

      </div>

    </div>

  `;
}


// ============================================================
// UNREALIZED P&L
// ============================================================

function getDisplayedUnrealizedPnl(
  account
) {

  const position =
    account.position || {};

  const raw =
    position.unrealized_pnl;


  // ----------------------------------------------------------
  // First priority:
  // Use the value supplied by the bot/exchange.
  // ----------------------------------------------------------

  if (
    raw !== null &&
    raw !== undefined &&
    !Number.isNaN(Number(raw))
  ) {

    const numeric =
      Number(raw);

    // If exchange supplied a non-zero value, use it.
    if (numeric !== 0) {

      return numeric;
    }

    // If position is flat, zero is correct.
    if (
      Number(position.size || 0)
      === 0
    ) {

      return 0;
    }
  }


  // ----------------------------------------------------------
  // Fallback:
  // Calculate from entry/current price when the exchange
  // response does not provide unrealized P&L.
  //
  // The bot's normal value is still preferred above.
  // ----------------------------------------------------------

  const size =
    Number(
      position.size || 0
    );

  const entry =
    Number(
      position.entry_price
    );

  const current =
    Number(
      account.current_price
    );


  if (
    !size ||
    !Number.isFinite(entry) ||
    !Number.isFinite(current)
  ) {

    return 0;
  }


  // ----------------------------------------------------------
  // The current bot uses the product contract value internally.
  // If the backend has not returned an exchange P&L value,
  // use the account's balance/leverage/size relationship as
  // a conservative display fallback.
  // ----------------------------------------------------------

  const balance =
    Number(
      account.balance
    );

  const leverage =
    Number(
      account.leverage || 50
    );

  const fraction =
    Number(
      account.balance_fraction || 0.10
    );


  if (
    !Number.isFinite(balance) ||
    !Number.isFinite(leverage) ||
    !Number.isFinite(fraction) ||
    balance <= 0 ||
    leverage <= 0 ||
    fraction <= 0
  ) {

    return 0;
  }


  const estimatedNotional =
    balance *
    fraction *
    leverage;


  const estimatedContractValue =
    estimatedNotional /
    (
      entry *
      size
    );


  if (
    !Number.isFinite(
      estimatedContractValue
    ) ||
    estimatedContractValue <= 0
  ) {

    return 0;
  }


  const priceDifference =
    position.direction === "SHORT"
      ? entry - current
      : current - entry;


  return (
    priceDifference *
    size *
    estimatedContractValue
  );
}


// ============================================================
// ACCOUNT CARD
// ============================================================

function renderAccount(
  account
) {

  const running =
    account.bot_enabled === true;

  const primary =
    account.account_type === "primary";

  const subscription =
    account.subscription || {};

  const position =
    account.position || {};


  let subscriptionText =
    "PRIMARY ACCOUNT";


  if (!primary) {

    if (subscription.expired) {

      subscriptionText =
        "SUBSCRIPTION EXPIRED";

    } else if (
      subscription.active
    ) {

      subscriptionText =
        "ACTIVE UNTIL " +
        formatDate(
          subscription.expiry
        );

    } else {

      subscriptionText =
        "SUBSCRIPTION INACTIVE";
    }
  }


  const unrealizedPnl =
    getDisplayedUnrealizedPnl(
      account
    );


  const statistics =
    account.statistics || {};


  const history =
    Array.isArray(
      account.trade_history
    )
      ? account.trade_history
      : [];


  return `

    <section class="card account-card">

      <div class="account-header">

        <div>

          <div class="account-type">

            ${
              primary
                ? "PRIMARY ACCOUNT"
                : "CLIENT ACCOUNT"
            }

          </div>


          <h2>
            ${escapeHtml(
              account.account_name
            )}
          </h2>


          <p>
            ${escapeHtml(
              account.account_id
            )}
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


      <div class="subscription-bar">

        <span>
          ${escapeHtml(
            subscriptionText
          )}
        </span>


        ${
          !primary &&
          subscription.fee !== undefined

            ? `

              <strong>
                Fee: $${number(
                  subscription.fee
                )}
              </strong>

            `

            : ""
        }

      </div>


      <div class="account-stats">


        <div>

          <span>
            Balance
          </span>

          <strong>

            ${
              account.balance === null ||
              account.balance === undefined

                ? "--"

                : "$" +
                  number(
                    account.balance
                  )
            }

          </strong>

        </div>


        <div>

          <span>
            Price
          </span>

          <strong>
            ${number(
              account.current_price
            )}
          </strong>

        </div>


        <div>

          <span>
            Position
          </span>

          <strong>
            ${escapeHtml(
              position.direction ||
              "FLAT"
            )}
          </strong>

        </div>


        <div>

          <span>
            Size
          </span>

          <strong>
            ${position.size ?? 0}
          </strong>

        </div>


        <div>

          <span>
            Entry
          </span>

          <strong>
            ${number(
              position.entry_price
            )}
          </strong>

        </div>


        <div>

          <span>
            Stop Loss
          </span>

          <strong>
            ${number(
              position.stop_loss
            )}
          </strong>

        </div>


        <div>

          <span>
            Unrealized P&L
          </span>

          <strong
            class="xaut-unrealized-live ${
              moneyClass(
                unrealizedPnl
              )
            }"
          >
            ${money(
              unrealizedPnl
            )}
          </strong>

        </div>


        <div>

          <span>
            All-Time P&L
          </span>

          <strong
            class="${moneyClass(
              statistics
                ?.all_time
                ?.pnl
            )}"
          >
            ${money(
              statistics
                ?.all_time
                ?.pnl
            )}
          </strong>

        </div>


      </div>


      <div class="account-actions">


        ${
          running

            ? `

              <button
                class="danger-button"
                onclick="stopBot('${escapeHtml(
                  account.account_id
                )}')"
              >
                ■ STOP BOT
              </button>

            `

            : `

              <button
                class="success-button"
                onclick="startBot('${escapeHtml(
                  account.account_id
                )}')"
              >
                ▶ START BOT
              </button>

            `
        }


        ${
          !primary

            ? `

              <button
                class="secondary-button"
                onclick="updateSubscription('${escapeHtml(
                  account.account_id
                )}')"
              >
                EDIT SUBSCRIPTION
              </button>


              <button
                class="delete-button"
                onclick="deleteClient('${escapeHtml(
                  account.account_id
                )}')"
              >
                DELETE CLIENT
              </button>

            `

            : ""
        }


      </div>


      ${
        !primary

          ? `

            <div class="subscription-details">

              <div>

                <span>
                  Start
                </span>

                <strong>
                  ${
                    formatDate(
                      subscription.start
                    )
                  }
                </strong>

              </div>


              <div>

                <span>
                  Expiry
                </span>

                <strong>
                  ${
                    formatDate(
                      subscription.expiry
                    )
                  }
                </strong>

              </div>

            </div>

          `

          : ""
      }


      ${renderStatistics(
        statistics
      )}


      ${renderTradeHistory(
        history
      )}


    </section>

  `;
}


// ============================================================
// DATE
// ============================================================

function formatDate(
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

      return String(value);
    }

    return date.toLocaleString();

  } catch {

    return String(value);
  }
}


// ============================================================
// DASHBOARD
// ============================================================

async function loadDashboard(
  force = false
) {

  if (
    dashboardLoading &&
    !force
  ) {

    return;
  }


  if (
    dashboardLoading
  ) {

    return;
  }


  dashboardLoading = true;


  try {

    const data =
      await apiFetch(
        "/api/dashboard",
        {
          method: "GET",
          cache: "no-store"
        }
      );


    const accounts =
      Array.isArray(
        data.accounts
      )
        ? data.accounts
        : [];


    const container =
      $("accounts-container");


    if (!container) {
      return;
    }


    container.innerHTML =
      accounts
        .map(
          renderAccount
        )
        .join("");


    const primary =
      accounts.find(
        account =>
          account.account_type === "primary"
      );


    if ($("bot-status")) {

      $("bot-status").textContent =
        data.server_online === false

          ? "OFFLINE"

          : primary?.bot_enabled

            ? "SYSTEM ONLINE"

            : "SYSTEM ONLINE • BOT STOPPED";
    }


    if ($("last-update")) {

      $("last-update").textContent =
        "Last update: " +
        new Date()
          .toLocaleTimeString();
    }


  } catch (error) {

    console.error(
      "Dashboard load error:",
      error
    );


    if ($("last-update")) {

      $("last-update").textContent =
        "Dashboard connection failed";
    }


    if ($("bot-status")) {

      $("bot-status").textContent =
        "CONNECTION ERROR";
    }


  } finally {

    dashboardLoading = false;
  }
}


// ============================================================
// INIT
// ============================================================

ensureDashboardStyles();

requestPin();

loadDashboard();

setInterval(
  () => loadDashboard(),
  3000
);
