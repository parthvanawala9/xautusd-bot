// ============================================================
// XAUTUSD DASHBOARD
// ============================================================
//
// Dashboard reads live state directly from GitHub Pages storage.
// Do NOT change bot.py or the XAUTUSD trading engine.
//
// ============================================================

const API_BASE_URL = ".";

let refreshTimer = null;


// ============================================================
// FORMAT MONEY
// ============================================================

function money(value) {

    if (
        value === null ||
        value === undefined ||
        value === ""
    ) {
        return "--";
    }

    const numberValue = Number(value);

    if (Number.isNaN(numberValue)) {
        return "--";
    }

    return "$" + numberValue.toLocaleString(
        "en-US",
        {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }
    );
}


// ============================================================
// FORMAT NUMBER
// ============================================================

function number(value) {

    if (
        value === null ||
        value === undefined ||
        value === ""
    ) {
        return "--";
    }

    const numberValue = Number(value);

    if (Number.isNaN(numberValue)) {
        return "--";
    }

    return numberValue.toLocaleString(
        "en-US",
        {
            maximumFractionDigits: 4
        }
    );
}


// ============================================================
// P&L
// ============================================================

function setPnl(
    element,
    value
) {

    element.textContent =
        money(value);

    element.classList.remove(
        "profit",
        "loss"
    );

    if (Number(value) > 0) {

        element.classList.add(
            "profit"
        );

    }

    if (Number(value) < 0) {

        element.classList.add(
            "loss"
        );
    }
}


// ============================================================
// MESSAGE
// ============================================================

function showMessage(
    text
) {

    const message =
        document.getElementById(
            "message"
        );

    if (!message) {
        return;
    }

    message.textContent =
        text || "";

    message.classList.add(
        "show"
    );

    setTimeout(
        () => {

            message.classList.remove(
                "show"
            );

        },
        3000
    );
}


// ============================================================
// BOT STATUS
// ============================================================

function updateBotStatus(
    running
) {

    const status =
        document.getElementById(
            "botStatus"
        );

    const statusCard =
        document.getElementById(
            "botStatusCard"
        );

    if (!status) {
        return;
    }

    if (running) {

        status.className =
            "status running";

        status.innerHTML =
            '<span class="status-dot"></span> BOT RUNNING';

        if (statusCard) {

            statusCard.textContent =
                "RUNNING";

            statusCard.classList.remove(
                "loss"
            );

            statusCard.classList.add(
                "profit"
            );
        }

    } else {

        status.className =
            "status stopped";

        status.innerHTML =
            '<span class="status-dot"></span> BOT STOPPED';

        if (statusCard) {

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
}


// ============================================================
// POSITION
// ============================================================

function updatePosition(
    position
) {

    position =
        position || {};

    const direction =
        position.direction || "FLAT";

    const badge =
        document.getElementById(
            "positionBadge"
        );

    if (badge) {

        badge.textContent =
            direction;

        badge.className =
            direction === "LONG"
                ? "position-long"
                : direction === "SHORT"
                    ? "position-short"
                    : "position-flat";
    }


    const directionElement =
        document.getElementById(
            "direction"
        );

    if (directionElement) {

        directionElement.textContent =
            direction;
    }


    const sizeElement =
        document.getElementById(
            "positionSize"
        );

    if (sizeElement) {

        sizeElement.textContent =
            number(
                position.size
            );
    }


    const entryElement =
        document.getElementById(
            "entryPrice"
        );

    if (entryElement) {

        entryElement.textContent =
            number(
                position.entry_price
            );
    }


    const stopElement =
        document.getElementById(
            "stopLoss"
        );

    if (stopElement) {

        stopElement.textContent =
            number(
                position.stop_loss
            );
    }


    const unrealizedElement =
        document.getElementById(
            "unrealizedPnl"
        );

    if (unrealizedElement) {

        setPnl(
            unrealizedElement,
            position.unrealized_pnl
        );
    }
}


// ============================================================
// TRADE HISTORY
// ============================================================

function renderTrades(
    trades
) {

    const table =
        document.getElementById(
            "tradeTable"
        );

    if (!table) {
        return;
    }

    if (
        !trades ||
        trades.length === 0
    ) {

        table.innerHTML = `
            <tr>
                <td
                    colspan="6"
                    class="empty"
                >
                    No trades yet
                </td>
            </tr>
        `;

        return;
    }


    table.innerHTML =
        trades.map(
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
                            Number(
                                trade.pnl
                            ) > 0
                                ? "profit"
                                : Number(
                                    trade.pnl
                                ) < 0
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


// ============================================================
// TIME
// ============================================================

function formatTime(
    timestamp
) {

    if (!timestamp) {
        return "--";
    }

    const date =
        new Date(
            timestamp
        );

    if (
        Number.isNaN(
            date.getTime()
        )
    ) {

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


// ============================================================
// DASHBOARD API / DATA LOAD
// ============================================================

async function loadDashboard() {

    try {

        const response =
            await fetch(
                "./dashboard.json",
                {
                    cache: "no-store"
                }
            );


        if (!response.ok) {

            throw new Error(
                "Dashboard Data HTTP " +
                response.status
            );
        }


        const data =
            await response.json();


        if (
            data.success === false
        ) {

            throw new Error(
                data.error ||
                "Dashboard error"
            );
        }


        // ----------------------------------------------------
        // BOT STATUS
        // ----------------------------------------------------

        updateBotStatus(
            data.bot_running ?? true
        );


        // ----------------------------------------------------
        // MARKET PRICE
        // ----------------------------------------------------

        const currentPrice =
            document.getElementById(
                "currentPrice"
            );

        if (currentPrice) {

            currentPrice.textContent =
                number(
                    data.current_price
                );
        }


        // ----------------------------------------------------
        // BALANCE
        // ----------------------------------------------------

        const balance =
            document.getElementById(
                "balance"
            );

        if (balance) {

            balance.textContent =
                money(
                    data.balance
                );
        }


        // ----------------------------------------------------
        // TOTAL P&L
        // ----------------------------------------------------

        const totalPnl =
            document.getElementById(
                "totalPnl"
            );

        if (totalPnl) {

            setPnl(
                totalPnl,
                data.total_pnl
            );
        }


        // ----------------------------------------------------
        // TODAY P&L
        // ----------------------------------------------------

        const todayPnl =
            document.getElementById(
                "todayPnl"
            );

        if (todayPnl) {

            setPnl(
                todayPnl,
                data.today_pnl
            );
        }


        // ----------------------------------------------------
        // WIN RATE
        // ----------------------------------------------------

        const winRate =
            document.getElementById(
                "winRate"
            );

        if (winRate) {

            const value =
                Number(
                    data.statistics?.win_rate
                );

            winRate.textContent =
                Number.isNaN(value)
                    ? "--"
                    : value.toFixed(1) + "%";
        }


        // ----------------------------------------------------
        // STATISTICS
        // ----------------------------------------------------

        const totalTrades =
            document.getElementById(
                "totalTrades"
            );

        if (totalTrades) {

            totalTrades.textContent =
                data.statistics?.total_trades ??
                "--";
        }


        const winningTrades =
            document.getElementById(
                "winningTrades"
            );

        if (winningTrades) {

            winningTrades.textContent =
                data.statistics?.winning_trades ??
                "--";
        }


        const losingTrades =
            document.getElementById(
                "losingTrades"
            );

        if (losingTrades) {

            losingTrades.textContent =
                data.statistics?.losing_trades ??
                "--";
        }


        // ----------------------------------------------------
        // POSITION
        // ----------------------------------------------------

        updatePosition(
            data.position
        );


        // ----------------------------------------------------
        // TRADES
        // ----------------------------------------------------

        renderTrades(
            data.trades
        );


        // ----------------------------------------------------
        // LAST UPDATED
        // ----------------------------------------------------

        const lastUpdated =
            document.getElementById(
                "lastUpdated"
            );

        if (lastUpdated) {

            lastUpdated.textContent =
                new Date().toLocaleTimeString(
                    "en-IN",
                    {
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit"
                    }
                );
        }


    } catch (error) {

        console.error(
            "Dashboard error:",
            error
        );


        const lastUpdated =
            document.getElementById(
                "lastUpdated"
            );

        if (lastUpdated) {

            lastUpdated.textContent =
                "Connection error";
        }
    }
}


// ============================================================
// BOT CONTROLS (PLACEHOLDERS FOR STATIC DEPLOYMENT)
// ============================================================

async function startBot() {
    showMessage("Bot state managed directly via server workflow.");
}

async function stopBot() {
    showMessage("Bot state managed directly via server workflow.");
}

async function stopAndExit() {
    showMessage("Manual position exit must be executed on Exchange directly.");
}


// ============================================================
// START
// ============================================================

loadDashboard();

refreshTimer =
    setInterval(
        loadDashboard,
        5000
    );
