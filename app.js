const $ = (id) =>
  document.getElementById(id);

let adminPin = "";


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
// API
// ============================================================

async function apiFetch(
  url,
  options = {}
) {

  options.headers = {
    ...(options.headers || {}),
    "Content-Type": "application/json",
    "X-Admin-Pin": adminPin
  };

  const response =
    await fetch(
      url,
      options
    );

  const data =
    await response.json();

  if (!response.ok || data.success === false) {

    throw new Error(
      data.message ||
      "Request failed."
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

  if (saved) {

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

    await loadDashboard();

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

    await loadDashboard();

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


    $("client-form").style.display =
      "none";


    $("client-name").value = "";
    $("client-api-key").value = "";
    $("client-api-secret").value = "";
    $("client-start").value = "";
    $("client-expiry").value = "";
    $("client-fee").value = "";


    await loadDashboard();


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


    await loadDashboard();

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


    await loadDashboard();

  } catch (error) {

    alert(
      error.message
    );
  }
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
            ? `<strong>
                Fee: $${number(
                  subscription.fee
                )}
              </strong>`
            : ""
        }

      </div>


      <div class="account-stats">

        <div>
          <span>Balance</span>
          <strong>
            ${
              account.balance === null
                ? "--"
                : "$" +
                  number(
                    account.balance
                  )
            }
          </strong>
        </div>


        <div>
          <span>Price</span>
          <strong>
            ${number(
              account.current_price
            )}
          </strong>
        </div>


        <div>
          <span>Position</span>
          <strong>
            ${position.direction || "FLAT"}
          </strong>
        </div>


        <div>
          <span>Size</span>
          <strong>
            ${position.size ?? 0}
          </strong>
        </div>


        <div>
          <span>Entry</span>
          <strong>
            ${number(
              position.entry_price
            )}
          </strong>
        </div>


        <div>
          <span>Stop Loss</span>
          <strong>
            ${number(
              position.stop_loss
            )}
          </strong>
        </div>


        <div>
          <span>Unrealized P&L</span>
          <strong>
            ${money(
              position.unrealized_pnl
            )}
          </strong>
        </div>


        <div>
          <span>All-Time P&L</span>
          <strong>
            ${money(
              account.statistics
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
                <span>Start</span>
                <strong>
                  ${
                    formatDate(
                      subscription.start
                    )
                  }
                </strong>
              </div>

              <div>
                <span>Expiry</span>
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

    return new Date(
      value
    ).toLocaleString();

  } catch {

    return value;
  }
}


// ============================================================
// DASHBOARD
// ============================================================

async function loadDashboard() {

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
        a =>
          a.account_type === "primary"
      );


    if ($("bot-status")) {

      $("bot-status").textContent =
        primary?.bot_running
          ? "SYSTEM ONLINE"
          : "OFFLINE";
    }


    if ($("last-update")) {

      $("last-update").textContent =
        "Last update: " +
        new Date()
          .toLocaleTimeString();
    }


  } catch (error) {

    console.error(
      error
    );


    if ($("last-update")) {

      $("last-update").textContent =
        "Dashboard connection failed";
    }
  }
}


// ============================================================
// INIT
// ============================================================

requestPin();

loadDashboard();

setInterval(
  loadDashboard,
  3000
);
