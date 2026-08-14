# XAUTUSD Delta Auto-Trading Bot

This bot implements the strategy discussed in ChatGPT:

- XAUTUSD
- IST timezone
- Trading day boundary: 05:30 IST
- Opening candle: 05:30-05:45 IST
- After 05:45, if flat:
  - break opening HIGH -> MARKET LONG
  - break opening LOW -> MARKET SHORT
- Initial/current same-day SL:
  - LONG -> current trading-day LOW
  - SHORT -> current trading-day HIGH
- The SL is NOT a conventional trailing stop.
- The day High/Low is the strategy reference and remains the same extreme until a new extreme is made.
- If SL closes a position, the bot immediately reverses with a MARKET order.
- At a new 05:30 trading-day boundary, an existing position is NOT closed and no new entry is taken.
  The bot starts using the new day's extreme for that position's SL as the new day's range develops.
- Friday trade is allowed.
- Saturday 05:00 IST -> force square-off.
- Saturday and Sunday -> no entries.
- Monday 05:30-05:45 -> new opening candle.
- Monday 05:45 -> new entries allowed.
- Position sizing target: 10% of current account equity as margin at 50x leverage.
- SL orders are STOP-MARKET, reduce-only, triggered by LAST TRADED PRICE.
- Actual entries/reversals are MARKET orders.

## Important
The code defaults to Delta India TESTNET and LIVE_TRADING=false.

Do not put API secrets into ChatGPT. Put them only in your local `.env` file.

## Run

1. Install Python 3.10+.
2. Open a terminal in this folder.
3. Install:
   `pip install -r requirements.txt`
4. Copy `.env.example` to `.env`.
5. Enter your Delta TESTNET API key and secret in `.env`.
6. Keep:
   `DELTA_BASE_URL=https://cdn-ind.testnet.deltaex.org`
   `LIVE_TRADING=false`
7. Run:
   `python bot.py`

After the testnet connection and strategy behavior have been verified, the environment can be changed to:
`https://api.india.delta.exchange`
and LIVE_TRADING can be enabled.

The bot prints the actual XAUTUSD product response at startup so contract size/lot metadata can be verified before live execution.
