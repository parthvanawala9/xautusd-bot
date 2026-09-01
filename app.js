const $ = (id) =>
  document.getElementById(id);

let adminPin = "";
let dashboardLoading = false;


// ============================================================
// XAUTUSD CONTRACT SETTINGS
// ============================================================
//
// Delta XAUTUSD:
// 1 lot = 0.001 XAUT
//
// If the backend sends contract_value, we use it.
// Otherwise XAUTUSD fallback is 0.001.
//

const DEFAULT_XAUT_CONTRACT_VALUE = 0.001;


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


function escapeHtml(value) {

  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}


// ============================================================
// P&L HELPERS
// ============================================================

function getContractValue(account) {

  const possibleValues = [

    account?.contract_value,

    account?.contract_value_usd,

    account?.contract_unit_value,

    account?.position?.contract_value,

    account?.position?.contract_value_usd,

    account?.position?.contract_unit_value

  ];


  for (const value of possibleValues) {

    const n = Number(value);

    if (
      Number.isFinite(n) &&
      n > 0
    ) {

      return n;
    }
  }


  return DEFAULT_XAUT_CONTRACT_VALUE;
}


function calculateFallbackUnrealizedPnl(
  account
) {

  const position =
    account?.position || {};


  const direction =
    String(
      position.direction || ""
    ).toUpperCase();


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
    !Number.isFinite(size) ||
    size === 0
  ) {

    return 0;
  }


  if (
    direction !== "LONG" &&
    direction !== "SHORT"
  ) {

    return 0;
  }


  if (
    !Number.isFinite(entry) ||
    !Number.isFinite(current)
  ) {

    return 0;
  }


  const contractValue =
    getContractValue(account);


  let pnl = 0;


  if (
    direction === "LONG"
  ) {

    pnl =
      (
        current -
        entry
      ) *
      Math.abs(size) *
      contractValue;

  } else {

    pnl =
      (
        entry -
        current
      ) *
      Math.abs(size) *
      contractValue;
  }


  if (
    !Number.isFinite(pnl)
  ) {

    return 0;
  }


  return pnl;
}


function getLiveUnrealizedPnl(
  account
) {

  const position =
    account?.position || {};


  const size =
    Number(
      position.size || 0
    );


  if (
    !Number.isFinite(size) ||
    size === 0
  ) {

    return 0;
  }


  /*
   * First try the value supplied by the bot.
   *
   * IMPORTANT:
   * Delta can sometimes return 0 while the position
   * is still open. In that case we calculate it locally.
   */

  const exchangePnl =
    Number(
      position.unrealized_pnl
    );


  if (
    Number.isFinite(exchangePnl) &&
    exchangePnl !== 0
  ) {

    return exchangePnl;
  }


  /*
   * Backend value is missing/zero.
   * Calculate from entry/current/size.
   */

  return calculateFallbackUnrealizedPnl(
    account
  );
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


  if (
    !apiKey ||
    !apiSecret
  ) {

    alert(
      "Enter Delta API key and API secret."
    );

    return;
  }


  if (
    !start ||
    !expiry
  ) {

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
// RUNNING TRADE
// ============================================================

function renderRunningTrade(
  account
) {

  const position =
    account.position || {};


  const size =
    Number(
      position.size || 0
    );


  if (
    size === 0 ||
    !position.direction ||
    position.direction === "FLAT"
  ) {

    return `

      <div class="running-trade-card">

        <div class="running-trade-header">

          <div>

            <h3>
              Running Trade
            </h3>

            <span class="running-trade-status">
              NO OPEN TRADE
            </span>

          </div>

        </div>


        <div class="running-trade-empty">

          No trade is currently running on this account.

        </div>

      </div>

    `;
  }


  const direction =
    String(
      position.direction
    ).toUpperCase();


  const directionClass =
    direction === "LONG"
      ? "running-long"
      : "running-short";


  const unrealizedPnl =
    getLiveUnrealizedPnl(
      account
    );


  const pnlClass =
    unrealizedPnl > 0
      ? "trade-profit"
      : unrealizedPnl < 0
        ? "trade-loss"
        : "trade-flat";


  return `

    <div class="running-trade-card">

      <div class="running-trade-header">

        <div>

          <div class="running-trade-label">
            RUNNING TRADE
          </div>

          <h3>
            ${escapeHtml(
              direction
            )}
          </h3>

        </div>


        <div class="running-trade-live">
          ● LIVE
        </div>

      </div>


      <div class="running-trade-grid">

        <div>

          <span>
            Direction
          </span>

          <strong class="${directionClass}">
            ${escapeHtml(
              direction
            )}
          </strong>

        </div>


        <div>

          <span>
            Size
          </span>

          <strong>
            ${number(
              size,
              0
            )}
          </strong>

        </div>


        <div>

          <span>
            Entry Price
          </span>

          <strong>
            ${number(
              position.entry_price
            )}
          </strong>

        </div>


        <div>

          <span>
            Current Price
          </span>

          <strong>
            ${number(
              account.current_price
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
            Unrealized P&amp;L
          </span>

          <strong class="${pnlClass}">

            ${money(
              unrealizedPnl
            )}

          </strong>

        </div>

      </div>

    </div>

  `;
}


// ============================================================
// PERFORMANCE
// ============================================================

function renderPerformance(
  account
) {

  const statistics =
    account.statistics || {};


  const today =
    statistics.today || {};


  const allTime =
    statistics.all_time || {};


  return `

    <div class="performance-section">

      <h3>
        Trading Performance
      </h3>


      <div class="performance-grid">


        <div class="performance-card">

          <h4>
            Today
          </h4>


          <div class="performance-stats">

            <div>

              <span>
                Total Trades
              </span>

              <strong>
                ${today.total_trades ?? 0}
              </strong>

            </div>


            <div>

              <span>
                Winning Trades
              </span>

              <strong>
                ${today.winning_trades ?? 0}
              </strong>

            </div>


            <div>

              <span>
                Losing Trades
              </span>

              <strong>
                ${today.losing_trades ?? 0}
              </strong>

            </div>


            <div>

              <span>
                Win Rate
              </span>

              <strong>
                ${number(
                  today.win_rate,
                  1
                )}%
              </strong>

            </div>


            <div>

              <span>
                Today P&amp;L
              </span>

              <strong>
                ${money(
                  today.pnl
                )}
              </strong>

            </div>

          </div>

        </div>


        <div class="performance-card">

          <h4>
            All Time
          </h4>


          <div class="performance-stats">

            <div>

              <span>
                Total Trades
              </span>

              <strong>
                ${allTime.total_trades ?? 0}
              </strong>

            </div>


            <div>

              <span>
                Winning Trades
              </span>

              <strong>
                ${allTime.winning_trades ?? 0}
              </strong>

            </div>


            <div>

              <span>
                Losing Trades
              </span>

              <strong>
                ${allTime.losing_trades ?? 0}
              </strong>

            </div>


            <div>

              <span>
                Win Rate
              </span>

              <strong>
                ${number(
                  allTime.win_rate,
                  1
                )}%
              </strong>

            </div>


            <div>

              <span>
                All-Time P&amp;L
              </span>

              <strong>
                ${money(
                  allTime.pnl
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
  account
) {

  const history =
    Array.isArray(
      account.trade_history
    )
      ? account.trade_history
      : [];


  const position =
    account.position || {};


  const hasRunningTrade =
    Number(
      position.size || 0
    ) !== 0;


  let rows = "";


  if (history.length > 0) {

    rows =
      history
        .map(
          (trade) => {

            const pnl =
              Number(
                trade.pnl || 0
              );


            const pnlClass =
              pnl > 0
                ? "trade-profit"
                : pnl < 0
                  ? "trade-loss"
                  : "trade-flat";


            return `

              <div class="trade-history-row">

                <div>

                  <span>
                    Date
                  </span>

                  <strong>
                    ${escapeHtml(
                      trade.date || "--"
                    )}
                  </strong>

                </div>


                <div>

                  <span>
                    Direction
                  </span>

                  <strong>
                    ${escapeHtml(
                      trade.direction || "--"
                    )}
                  </strong>

                </div>


                <div>

                  <span>
                    Entry
                  </span>

                  <strong>
                    ${number(
                      trade.entry_price
                    )}
                  </strong>

                </div>


                <div>

                  <span>
                    Exit
                  </span>

                  <strong>
                    ${number(
                      trade.exit_price
                    )}
                  </strong>

                </div>


                <div>

                  <span>
                    Size
                  </span>

                  <strong>
                    ${trade.size ?? 0}
                  </strong>

                </div>


                <div>

                  <span>
                    Reason
                  </span>

                  <strong>
                    ${escapeHtml(
                      trade.reason || "--"
                    )}
                  </strong>

                </div>


                <div>

                  <span>
                    P&amp;L
                  </span>

                  <strong class="${pnlClass}">
                    ${money(
                      pnl
                    )}
                  </strong>

                </div>

              </div>

            `;
          }
        )
        .join("");

  } else {

    rows = `

      <div class="trade-history-empty">

        No closed trades yet.

      </div>

    `;
  }


  let runningTradeBanner = "";


  if (
    hasRunningTrade
  ) {

    const runningPnl =
      getLiveUnrealizedPnl(
        account
      );


    const runningPnlClass =
      runningPnl > 0
        ? "trade-profit"
        : runningPnl < 0
          ? "trade-loss"
          : "trade-flat";


    runningTradeBanner = `

      <div class="running-history-banner">

        <strong>
          ● RUNNING TRADE
        </strong>


        <span>

          ${escapeHtml(
            position.direction ||
            "POSITION"
          )}

          • Entry

          ${number(
            position.entry_price
          )}

          • Size

          ${position.size ?? 0}

          • Current

          ${number(
            account.current_price
          )}

          • Unrealized

          <strong class="${runningPnlClass}">

            ${money(
              runningPnl
            )}

          </strong>

        </span>

      </div>

    `;
  }


  return `

    <div class="trade-history-section">

      <div class="trade-history-title">

        <h3>
          Trade History
        </h3>

        <span>

          ${history.length}

          closed trade${
            history.length === 1
              ? ""
              : "s"
          }

        </span>

      </div>


      ${runningTradeBanner}


      <div class="trade-history-list">

        ${rows}

      </div>

    </div>

  `;
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


  const unrealizedPnl =
    getLiveUnrealizedPnl(
      account
    );


  const unrealizedPnlClass =
    unrealizedPnl > 0
      ? "trade-profit"
      : unrealizedPnl < 0
        ? "trade-loss"
        : "trade-flat";


  let subscriptionText =
    "PRIMARY ACCOUNT";


  if (!primary) {

    if (
      subscription.expired
    ) {

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


        <div class="${
          running
            ? "account-running"
            : "account-stopped"
        }">

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
            Unrealized P&amp;L
          </span>

          <strong class="${unrealizedPnlClass}">

            ${money(
              unrealizedPnl
            )}

          </strong>

        </div>


        <div>

          <span>
            All-Time P&amp;L
          </span>

          <strong>

            ${money(
              account.statistics
                ?.all_time
                ?.pnl
            )}

          </strong>

        </div>

      </div>


      <!-- ====================================================
           RUNNING TRADE
           ==================================================== -->

      ${renderRunningTrade(
        account
      )}


      <!-- ====================================================
           BOT ACTIONS
           ==================================================== -->

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

                  ${formatDate(
                    subscription.start
                  )}

                </strong>

              </div>


              <div>

                <span>
                  Expiry
                </span>

                <strong>

                  ${formatDate(
                    subscription.expiry
                  )}

                </strong>

              </div>


            </div>

          `

          : ""
      }


      <!-- ====================================================
           PERFORMANCE
           ==================================================== -->

      ${renderPerformance(
        account
      )}


      <!-- ====================================================
           TRADE HISTORY
           ==================================================== -->

      ${renderTradeHistory(
        account
      )}


    </section>

  `;
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

requestPin();

loadDashboard();


setInterval(
  () =>
    loadDashboard(),
  3000
);
