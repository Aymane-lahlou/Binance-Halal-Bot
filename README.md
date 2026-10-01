# Binance Halal Bot

Unofficial experimental Binance spot bot built with Freqtrade. Spot ownership only, long-only, no leverage.

> Trademark note: Unofficial project. Not affiliated with, endorsed by, or sponsored by Binance. Binance is a trademark of its respective owner. Freqtrade is a separate open-source project.
>
> Suggested GitHub repo description (copy-paste): `Unofficial Binance spot-only bot inspired by SC Malaysia Shariah list — not a fatwa, not financial advice.`

## Please read this first — disclaimer

I am **not a qadi, mufti, scholar, or student of knowledge**. I don't have deep religious education.

This is just a small personal project I wanted to try: can I configure an open-source trading bot with conservative guardrails that *try* to stay close to what I understood from the Malaysian approach?

This repo is:

- **NOT a fatwa**
- **NOT financial advice**
- **NOT a halal certificate for any coin or trade**

If you are Muslim, please do your own research and **ask a qualified scholar you trust** before trading with real money. Scholars differ on crypto, and my understanding may be wrong or incomplete.

If anything in this code contradicts what your scholar says, follow your scholar, not this repo.

## Why "based on the Malaysia fatwa"?

I am loosely inspired by the public Malaysian position as I understood it — specifically the crypto rulings, not forex in general:

- The Shariah Advisory Council (SAC) of the Securities Commission Malaysia, 233rd meeting (29 June 2020) + 234th meeting (20 July 2020): digital currency without underlying is recognised as *mal*, categorised as *'urud* (goods, not currency, not *ribawi* items), so spot trading is not subject to *bay' al-sarf*. Investment and trading on SC-registered DAX is permissible in principle, subject to conditions.
- SC Malaysia publishes the allowed Shariah-compliant list here: https://www.sc.com.my/digital-assets — currently 15 assets: BTC, ETH, XRP, LTC, BCH, SOL, ADA, LINK, UNI, MATIC, AVAX, DOT, ATOM, WLD, XLM.
- **BNB is NOT on that SC Shariah-compliant list**, so this bot blacklists `BNB/USDT` and treats it as forbidden.
- The Perlis State Fatwa gazetted Bitcoin as permissible (*harus*) as a digital asset, with zakat (2.5% when nisab + haul are met).
- The National Fatwa Council (JAKIM, 98th Muzakarah, Feb 2012) ruled online forex / margin trading impermissible because of: no real *qabd*, leverage / *riba*, *gharar*, and *maysir*.
- Bank Negara Malaysia does not recognise crypto as legal tender. SC regulates it as a digital asset.

What I took from that for this bot — my own interpretation, not a ruling:

1. Only **spot, real ownership**. No futures, no margin, no leverage, no shorts, no CFDs.
2. No interest-based income: no lending, no borrowing, no yield-bearing stables, no Earn / staking rewards in this bot.
3. Trade only from the SC-compliant universe, avoid the rest. See below.
4. If in doubt, skip the trade.

Again: this is my attempt at being careful, not a religious verdict. SharLife (registered SC Shariah advisory) and individual muftis may screen tokens differently. Always check the current SC list — it changes.

## Allowed vs forbidden coins in this bot

Whitelist in `user_data/config.json` — all 9 are on the SC Shariah-compliant list:

`BTC/USDT`, `ETH/USDT`, `SOL/USDT`, `XRP/USDT`, `ADA/USDT`, `LINK/USDT`, `AVAX/USDT`, `DOT/USDT`, `LTC/USDT`

Blacklist / deny-list — forbidden in this bot:

- `BNB/USDT` — explicitly excluded because BNB is **not** on the SC Shariah-compliant list
- Meme / speculative: `DOGE`, `SHIB`, `PEPE`, `FLOKI`, `WIF`
- Lending / interest protocols: `AAVE`, `COMP`, `MKR`, `SKY`, `CREAM`, `VENUS`, `XVS`, `ALPACA`, `JST`, `QI`, `BIFI`, `RDNT`, `EUL`, `GHO`, `MORPHO`, `SPELL`, `MIM`, `ALPHA`, `LQTY`, `STG`, `CVX`, `YFI`, `FXS`, `SNX`, `CRV`
- Yield-bearing synthetic stables: `ENA`, `USDE`, `SUSDE`, `USDD`, `USDX`
- Perp / leverage venues: `DYDX`, `GMX`, `GNS`, `LEVER`
- Liquid staking / yield tokenisation: `LDO`, `RPL`, `JTO`, `SD`, `PENDLE`
- Gambling: `FUN`, `WINK`, `WIN`, `EDG`
- Leveraged tokens by pattern: `.*UP/USDT`, `.*DOWN/USDT`, `.*BULL/USDT`, `.*BEAR/USDT`, `.*3L/USDT`, `.*3S/USDT`, `.*5L/USDT`, `.*5S/USDT`, `.*PERP.*`

This is enforced twice: in `config.json` `pair_blacklist` and hardcoded in the strategy (`non_compliant_bases` + `leveraged_token_patterns` in `confirm_trade_entry` + `bot_start`), so a hand-edited whitelist can't quietly reintroduce them.

## What this bot actually does

- Exchange: Binance **spot** (`binance`, `trading_mode: spot`)
- Strategy: `HalalSpotStrategy` in `user_data/strategies/HalalSpotStrategy.py`
- Long-only (`can_short = False`), leverage pinned to 1.0
- Dry-run enabled by default, 3 max open trades, 15m timeframe + 4h informative
- ATR-based stop, risk-based position sizing (~1% risk per trade), partial take-profit, BTC 4h regime filter, stale-trade timeout
- Startup guardrails: bot refuses to start if `trading_mode` is not `spot`, if margin is set, if `can_short` is true, or if a whitelisted pair is on the deny-list
- Pair deny-list enforced in both `user_data/config.json` and in the strategy (`confirm_trade_entry`), so a hand-edited whitelist can't quietly reintroduce them

What it never does:

- Futures, margin, leverage, short selling
- Lending, borrowing, staking, Earn, withdrawals via the bot
- Live trading unless **you** explicitly set `dry_run: false` and add real API keys

No profitability is claimed. No backtest results are included. Test everything yourself.

## Requirements

- Docker Desktop (on Windows, reboot after install if networking acts up)
- Freqtrade is run via Docker, nothing is installed on your host

## Setup

```powershell
Copy-Item .env.example .env
docker compose pull
docker compose run --rm freqtrade list-strategies --config user_data/config.json
docker compose run --rm freqtrade test-pairlist --config user_data/config.json
```

Download data and backtest:

```powershell
docker compose run --rm freqtrade download-data --config user_data/config.json --pairs BTC/USDT ETH/USDT --exchange binance --days 365 -t 15m
docker compose run --rm freqtrade backtesting --config user_data/config.json --strategy HalalSpotStrategy --timerange 20250101-20250601
```

Start dry-run:

```powershell
docker compose up -d
```

FreqUI:

```text
http://localhost:8080
```

Stop:

```powershell
docker compose down
```

## Secrets — how this repo stays safe to publish

- Real secrets live only in `.env`, which is git-ignored and **never committed**.
- `.env.example` contains only placeholders.
- `user_data/config.json` contains empty keys and `CHANGE_THIS_*` placeholders for the API server. Put real values in `.env`, not in the JSON.
- If you create Binance API keys: spot-trading only, disable withdrawals, never enable futures, margin, Earn, lending, or borrowing permissions.
- Telegram is disabled by default. Only fill `FREQTRADE__TELEGRAM__TOKEN` / `CHAT_ID` in your local `.env` if you want it.

Do not open issues or PRs containing API keys, tokens, or chat IDs.

## Live trading — don't

This bot ships in `dry_run: true` mode for a reason. Do not go live until you have:

1. Backtested on 1-2 years of data
2. Run dry-run for several weeks
3. Re-checked API permissions (spot-only, no withdrawals)
4. Asked a scholar you trust about your specific setup, including exchange, fees, and zakat

To go live later you must deliberately change `dry_run` to `false`. There is no automatic switch.

## Project status

Hobby / learning project. No guarantees, no support promises, no profit expectations. Breaking changes may happen without notice.

If you fork it, please keep this disclaimer and don't present it as a religious ruling either.

*Jazakum Allahu khayran for reading — and again, I'm not a scholar, just trying to learn and build carefully.*
