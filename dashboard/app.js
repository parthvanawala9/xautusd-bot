let refreshTimer = null;


function money(value) {

    if (value === null || value === undefined) {
        return "--";
    }

    const number = Number(value);

    if (Number.isNaN(number)) {
        return "--";
    }

    return "$" + number.toLocaleString(
        "en-US",
        {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }
    );
}


function number(value) {

    if (value === null || value === undefined) {
        return "--";
    }

    return Number(value).toLocaleString(
        "en-US",
        {
            maximumFractionDigits: 4
        }
    );
}


function setPnl(element, value) {

    element.textContent = money(value);

    element.classList.remove(
        "profit",
        "loss"
    );

    if (Number(value) > 0) {
        element.classList.add("profit");
    }

    if (Number(value) < 0) {
        element.classList.add("loss");
    }
}


function showMessage(text) {

    const message =
        document.getElementById("message");

    message.textContent = text;

    message.classList.add("show");

    setTimeout(() => {
        message.classList.remove("show");
    }, 3000);
}


function updateBotStatus(running) {

    const status =
        document.getElementById("botStatus");

    const statusCard =
        document.getElementById("botStatusCard");

    if (running) {

        status.className =
            "status running";

        status.innerHTML =
            '<span class="status-dot"></span> BOT RUNNING';

        statusCard.textContent =
            "RUNNING";

        statusCard.classList.remove(
            "loss"
        );

        statusCard.classList.add(
            "profit"
        );

    } else {

        status.className =
            "status stopped";

        status.innerHTML =
            '<span class="status-dot"></span> BOT STOPPED';

        statusCard.textContent =
            "STOPPED";

        statusCard.classList.remove(
            "profit"
        );

        statusCard.classList.add(
            "loss"
        );
    }
}


function updatePosition(position) {

    const direction =
        position.direction || "FLAT";

    const badge =
        document.getElementById(
            "positionBadge"
        );

    badge.textContent = direction;

    badge.className =
        direction === "LONG"
            ? "position-long"
            : direction === "SHORT"
                ? "position-short"
                : "position-flat";


    document.getElementById(
        "direction"
    ).textContent = direction;


    document.getElementById(
        "positionSize"
    ).textContent = number(
        position.size
    );


    document.getElementById(
        "entryPrice"
    ).textContent = number(
        position.entry_price
    );


    document.getElementById(
        "stopLoss"
    ).textContent = number(
        position.stop_loss
    );


    setPnl(
        document.getElementById(
            "unrealizedPnl"
        ),
        position.unrealized_pnl
    );
}


function renderTrades(trades) {

    const table =
        document.getElementById(
            "tradeTable"
        );

    if (!trades || trades.length === 0) {

        table.innerHTML = `
            <tr>
                <td colspan="6" class="empty">
                    No trades yet
                </td>
            </tr>
        `;

        return;
    }


    table.innerHTML = trades.map(
        trade => {

            const side =
                String(
                    trade.side || ""
                ).toUpperCase();

            const sideClass =
                side === "BUY"
                    ? "side-buy"
                    : "side-sell";

            return `
                <tr>

                    <td>
                        ${formatTime(
                            trade.timestamp
                        )}
                    </td>

                    <td class="${sideClass}">
                        ${side || "--"}
                    </td>

                    <td>
                        ${number(
                            trade.price
                        )}
                    </td>

                    <td>
                        ${number(
                            trade.size
                        )}
                    </td>

                    <td>
                        ${money(
                            trade.commission
                        )}
                    </td>

                    <td class="${
                        Number(trade.pnl) > 0
                            ? "profit"
                            : Number(trade.pnl) < 0
                                ? "loss"
                                : ""
                    }">
                        ${money(
                            trade.pnl
                        )}
                    </td>

                </tr>
            `;
        }
    ).join("");
}


function formatTime(timestamp) {

    if (!timestamp) {
        return "--";
    }

    const date =
        new Date(timestamp);

    if (Number.isNaN(
        date.getTime()
    )) {
        return timestamp;
    }

    return date.toLocaleString(
        "en-IN",
        {
            day: "2-digit",
            month: "short",
            hour: "2-digit",
            minute: "2-digit"
        }
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

        const data =
            await response.json();


        if (!data.success) {
            throw new Error(
                data.error || "Dashboard error"
            );
        }


        updateBotStatus(
            data.bot_running
        );


        document.getElementById(
            "currentPrice"
        ).textContent =
            number(
                data.current_price
            );


        document.getElementById(
            "balance"
        ).textContent =
            money(
                data.balance
            );


        setPnl(
            document.getElementById(
                "totalPnl"
            ),
            data.total_pnl
        );


        setPnl(
            document.getElementById(
                "todayPnl"
            ),
            data.today_pnl
        );


        document.getElementById(
            "winRate"
        ).textContent =
            Number(
                data.statistics.win_rate
            ).toFixed(1) + "%";


        document.getElementById(
            "totalTrades"
        ).textContent =
            data.statistics.total_trades;


        document.getElementById(
            "winningTrades"
        ).textContent =
            data.statistics.winning_trades;


        document.getElementById(
            "losingTrades"
        ).textContent =
            data.statistics.losing_trades;


        updatePosition(
            data.position
        );


        renderTrades(
            data.trades
        );


        document.getElementById(
            "lastUpdated"
        ).textContent =
            new Date().toLocaleTimeString(
                "en-IN",
                {
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit"
                }
            );

    } catch (error) {

        console.error(error);

        document.getElementById(
            "lastUpdated"
        ).textContent =
            "Connection error";
    }
}


async function startBot() {

    try {

        const response =
            await fetch(
                "/api/start",
                {
                    method: "POST"
                }
            );

        const data =
            await response.json();

        showMessage(
            data.message
        );

        await loadDashboard();

    } catch (error) {

        showMessage(
            "Unable to start bot."
        );
    }
}


async function stopBot() {

    if (
        !confirm(
            "Stop the bot? Existing position remains open."
        )
    ) {
        return;
    }

    try {

        const response =
            await fetch(
                "/api/stop",
                {
                    method: "POST"
                }
            );

        const data =
            await response.json();

        showMessage(
            data.message
        );

        await loadDashboard();

    } catch (error) {

        showMessage(
            "Unable to stop bot."
        );
    }
}


async function stopAndExit() {

    if (
        !confirm(
            "STOP BOT AND EXIT THE EXISTING POSITION AT MARKET?"
        )
    ) {
        return;
    }

    try {

        const response =
            await fetch(
                "/api/stop-exit",
                {
                    method: "POST"
                }
            );

        const data =
            await response.json();

        showMessage(
            data.message
        );

        await loadDashboard();

    } catch (error) {

        showMessage(
            "Unable to stop and exit."
        );
    }
}


loadDashboard();

refreshTimer =
    setInterval(
        loadDashboard,
        3000
    );
