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

  return "$" + Number(value).toFixed(2);
}


function setStatus(running) {

  const status = $("bot-status");

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


function updateDashboard(data) {

  setStatus(
    data.bot_running === true
  );


  $("symbol").textContent =
    data.symbol || "XAUTUSD";


  $("current-price").textContent =
    number(
      data.current_price
    );


  $("today-high").textContent =
    number(
      data.high
    );


  $("today-low").textContent =
    number(
      data.low
    );


  $("wallet-balance").textContent =
    money(
      data.balance
    );


  const position =
    data.position || {};


  $("pos-direction").textContent =
    position.direction || "FLAT";


  $("pos-size").textContent =
    position.size ?? 0;


  $("pos-entry").textContent =
    number(
      position.entry_price
    );


  $("pos-sl").textContent =
    number(
      position.stop_loss
    );


  $("pos-pnl").textContent =
    money(
      position.unrealized_pnl
    );


  const ready =
    data.session &&
    data.session.ready;


  $("session-ready").textContent =
    ready ? "YES" : "NO";


  $("last-update").textContent =
    "Last update: " +
    new Date().toLocaleTimeString();
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

    $("last-update").textContent =
      "Dashboard connection failed";
  }
}


loadDashboard();


setInterval(
  loadDashboard,
  3000
);
