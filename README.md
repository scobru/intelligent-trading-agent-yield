<img src="static/icon.svg" alt="" width="88" height="88" align="left">

# Yield Agent (Base passive yield)

<br clear="left">

**English** · [Italiano](README.it.md)

> ⚠️ **Experimental software, not financial advice.** The bot trades real money on Base and can lose some or all of the capital you give it. Start with paper trading or dry-run; when you go live, use a dedicated wallet and only amounts you can afford to lose. See the **Disclaimer** section at the bottom.

An agent that looks for **passive yield on Base**: it finds the lending
markets and vaults that pay interest (Aave, Compound, Morpho, Moonwell, Fluid,
Euler...), deposits idle capital there and moves it only when a better pool
really pays back the cost of moving.

It is part of the [Intelligent Trading](https://github.com/scobru/intelligent-trading)
suite of agents for Base. Structure, dashboard, Telegram notifications,
dry-run/paper modes and the LLM decision cycle are shared with its siblings.
The goal is different: no price speculation here, just the best
risk-adjusted interest.

---

## 🧠 How a cycle works

1. **Discovery** — every Base pool from [DefiLlama Yields](https://defillama.com/yields)
   (base APY, reward APY, 30-day mean, TVL, exposure, IL risk).

2. **Screening** — only pools that are:
   - from an allowlisted protocol (lending and vaults with a track record);
   - single-asset with no impermanent loss (no LPs);
   - on an asset the bot knows how to hold (`DEPOSIT_ASSETS`, USDC only by default);
   - with credible TVL, history and APY, not inflated by emissions or an outlier.

   Every rejection keeps its reason and shows up in the dashboard.

3. **On-chain recognition** — the DefiLlama id is not enough to know where to
   deposit. For each candidate the bot recognizes the contract and checks the
   underlying asset on-chain:

   | Family | Recognition | Typical protocols |
   |---|---|---|
   | Aave V3 | `getReserveData(asset)` → aToken → `UNDERLYING_ASSET_ADDRESS()` | Aave |
   | ERC-4626 | `asset()` + `convertToAssets()` + TVL consistent with DefiLlama | Morpho, Fluid, Euler, Spark... |
   | Compound V3 | `baseToken()` | Compound (Comet) |
   | Compound V2 fork | `underlying()` + `exchangeRateStored()` | Moonwell |

   Only recognized pools are **executable**; the others stay in the dashboard
   for monitoring and the model cannot pick them.

4. **Portfolio** — balances are read on-chain (aTokens, vault shares, Comet
   balance, cTokens). Entry price, APY and TVL at entry live in
   `positions.json`. A manual deposit into a recognized pool is adopted; a
   position withdrawn outside the bot disappears from the registry.

5. **Automatic exits** — if a pool's TVL collapses compared to entry
   (`EXIT_TVL_DROP_PCT`, default 40%) or the pool ends up on the blocklist, the
   bot exits *before* consulting the model.

6. **Decision** — the LLM (via OpenRouter) receives the universe, positions and
   limits, and answers with `deposit` / `withdraw` / `rebalance` / `hold`.

7. **Execution** — approve + deposit/withdraw on the recognized contract.
   If the vault's asset is not USDC (with `DEPOSIT_ASSETS` extended to WETH or
   cbBTC) the bot buys/sells the asset on Uniswap V3.

---

## 🛡️ Guardrails before every transaction

| Limit | Default | Variable |
|---|---|---|
| No transaction is signed | **on** | `DRY_RUN=true` |
| Max share in a single pool | 40% of the portfolio | `MAX_POOL_PCT` |
| Max share on a single protocol | 60% | `MAX_PROTOCOL_PCT` |
| Pools held at the same time | 4 | `MAX_POSITIONS` |
| Amount per deposit | $10 – $2500 | `MIN_DEPOSIT_USD`, `MAX_DEPOSIT_USD` |
| Minimum improvement to move | +1 APY point | `MIN_APY_IMPROVEMENT` |
| Minimum time in a pool | 24h | `MIN_HOLD_HOURS` |
| The move must pay back within | 14 days | `MAX_BREAKEVEN_DAYS` |
| ETH reserve for gas | 0.001 ETH | `MIN_ETH_RESERVE` |
| Max gas price | 0.5 gwei | `MAX_GAS_PRICE_GWEI` |

The model **cannot bypass them**: they are enforced by the executor, not by
the prompt. A signal outside the limits is rejected and the reason is stored
in the DB, shown in the dashboard and sent to Telegram.

The break-even check for a rebalance is explicit: estimated cost
(transactions × `EST_TX_COST_USD` + slippage of any swaps) divided by the
daily gain from the APY difference. Moving $50 for half a point does not pass;
moving $2000 for two points does.

### "Effective" APY

Pools are compared on **base APY + rewards × `REWARD_APY_WEIGHT`**
(default 0.5). Rewards are emission tokens that must be claimed and sold — the
bot does not do that on its own yet — and they can stop overnight. For
ranking, today's APY is also capped at 1.5 times the 30-day mean: a
utilization spike should not make capital move.

### Lending loop (leveraged staking yield)

With `ENABLE_LOOPING=true` the agent can amplify the staking yield of
ETH-pegged liquid staking tokens (**wstETH** and **cbETH**) on Aave V3:

- **Mechanics**: supply wstETH/cbETH as collateral, borrow WETH, buy more LST
  with it and supply again, **in rounds**, up to the target leverage (default
  `LOOP_MAX_LEVERAGE=2.2x`). Each borrow stays within the LTV of the collateral
  accumulated so far: a single borrow of (leverage − 1) × capital would be
  rejected by Aave.
- **Data from the chain**: supply and borrow rates, LTV and liquidation
  threshold (of the right e-mode category, detected automatically), supply cap
  and available WETH, Aave oracle prices. Staking yield comes from DefiLlama
  (Lido and Coinbase pools on Ethereum), with `staking_yield` in `config.py`
  as a fallback.
- **Effective leverage** = the lowest of `LOOP_MAX_LEVERAGE`, the leverage that
  keeps the HF at `LOOP_TARGET_HEALTH_FACTOR` and the leverage reachable with
  the LTV.
- **Net APY formula**:
  $$\text{Net APY} = (\text{Staking Yield} + \text{Supply APY}) \times L - \text{Borrow APY} \times (L - 1)$$
- **Position value** = collateral − debt (Aave oracle prices), not the
  collateral alone.
- **Safety and health factor**:
  - Initial target: `LOOP_TARGET_HEALTH_FACTOR=1.55`.
  - Automatic exit: if the HF drops below `LOOP_MIN_HEALTH_FACTOR=1.30` the
    loop is unwound without waiting for the LLM cycle. The lower of the Aave
    account HF and the loop-only HF is used.
  - Unwinding goes in rounds (withdraw LST → swap to WETH → repay) without ever
    taking the HF below the threshold, then everything goes back to USDC.
- **Risk**: collateral and debt are both in ETH, so the ETH price does not move
  the loop toward liquidation (an LST depeg or a spiking borrow rate does), but
  **the dollar value of the loop follows ETH**.
- Off by default (`ENABLE_LOOPING=false`).

---

## 🚀 Getting started

```bash
cp .env.example .env     # fill in wallet, RPC and OpenRouter key
pip install -r requirements.txt
python main.py           # one cycle, in dry-run
python dashboard.py      # dashboard at http://localhost:3000
python -m unittest discover -s tests   # offline tests
```

With Docker:

```bash
docker compose up --build
```

### Deploy on CapRover

`captain-definition` at the root, a single process.

1. **Create the app** and connect the repo (or `caprover deploy` from the folder).
2. **App Configs → Container HTTP Port: `3000`.**
3. **Persistent Directories** → map `/app/data`. The SQLite database,
   `positions.json` and the paper state go there automatically. Without a
   volume the bot finds its deposits on-chain again (and adopts them), but it
   loses entry APY and TVL and the history.
4. **Environment Variables**: at least `OPENROUTER_API_KEY`, `WALLET_ADDRESS`,
   `PRIVATE_KEY`, `BASE_RPC_URL`. Keep `DRY_RUN=true` for the first cycles.
5. **Enable HTTPS** if you expose the dashboard.

The image is light and builds in a few seconds. The interval between cycles is
`TRADING_INTERVAL`, default **3600s**: yields change slowly and every cycle
queries the RPC and the LLM.

### The three modes

| Mode | `.env` | What happens |
|---|---|---|
| **Dry-run** (default) | `DRY_RUN=true` | Reads the real wallet, decides, validates the limits and shows the transaction plan without signing it. |
| **Paper** | `PAPER_TRADING=true` | Virtual portfolio (`PAPER_START_USDC`, default $1000): deposits earn **each pool's current real APY**, cycle after cycle. Watch the rotation strategy work without capital. |
| **Live** | `DRY_RUN=false` | Signs and sends the transactions. |

In paper, positions are kept in USD: for a WETH or cbBTC vault the yield is
simulated but not the asset's price swings. Every transaction costs
`PAPER_GAS_USD` and swaps pay the default slippage. To start over, delete
`paper_portfolio.json` and `paper_positions.json`.

**Always start in dry-run or paper.** When live, use a dedicated wallet.

### ⛽ Auto-refuel (ETH → USDC)

If the wallet holds too little USDC (< `USDC_AUTO_SWAP_THRESHOLD`, default
$5.0) but has native ETH, the agent automatically converts the excess ETH into
USDC on Uniswap V3 at the start of the cycle, always keeping the ETH needed for
gas (`ETH_GAS_RESERVE`, default 0.003 ETH). Sending only ETH to the wallet is
enough to make the bot operational.

```bash
# Quick check or manual refuel
python main.py --refuel

# Inspect wallet balances with the dedicated tool
python tools/refuel.py --status
```

---

## 🎯 Specific pools

```bash
# considered even if the protocol is not allowlisted
# (DefiLlama id or vault address; the other filters still apply)
POOL_WATCHLIST="0xVaultAddress"

# never touched; if held, the bot exits it on the next cycle
POOL_BLOCKLIST="0xVaultAddress"
```

A watchlisted vault becomes executable only if on-chain recognition accepts it
(right asset, consistent TVL).

## 📱 Telegram and dashboard

Commands: `/status`, `/positions`, `/pools`, `/last`, `/run`. They are accepted
**only** from the chat set in `TELEGRAM_CHAT_ID`, because `/run` starts a
cycle that can sign transactions.

For the same reason the dashboard's "Run cycle now" button stays disabled
until you set `DASHBOARD_RUN_TOKEN`; the browser asks for it once and
remembers it.

#### A consistent dashboard across the suite

All the agents in the suite share the same design system:
`static/dashboard.css` and `static/dashboard.js` are **identical in every
repository** (if you change them, copy them to the others). Every page has the
same structure: header with a mode badge (`LIVE` / `PAPER` / `DRY-RUN`), paper
trading panel, KPIs, equity curve, positions and last AI decision, bot-specific
sections, operation history and errors. Only the accent color and the icon
change.

#### Wallet and gas

Below the header, outside paper trading, the dashboard shows the bot's wallet:
ETH for gas (with its dollar value), free USDC, address with a Basescan link
and a status: **OK**, **RUNNING LOW** (below `GAS_WARN_ETH`) or **TOP UP NOW**
(below the minimum reserve). The same warning appears in the cycle's Telegram
report.

## 📁 Structure

| File | Role |
|---|---|
| `main.py` | the cycle: discovery → recognition → risk exits → decision → execution |
| `config.py` | Base contracts, screening and risk thresholds |
| `yield_scanner.py` | DefiLlama Yields, screening, scoring |
| `protocols.py` | Aave V3, ERC-4626, Compound V3, cToken and Aave loop adapters |
| `loop_math.py` | looping math: leverage, health factor, rounds, e-mode |
| `yield_manager.py` | portfolio state, limits, deposits/withdrawals/rebalances |
| `positions.py` | position registry (entry, APY, TVL) |
| `paper.py` | virtual portfolio that earns the real APY |
| `yield_agent.py` | LLM call, JSON schema, fallback to `hold` |
| `base_client.py` | RPC, ERC-20, transaction sending, dry-run, gas cap |
| `uniswap.py` | USDC ↔ WETH/cbBTC/LST swaps when needed |
| `http_client.py` | pacing, retries and caching for public APIs |
| `db_utils.py` | SQLite: snapshots, scans, operations, errors |
| `dashboard.py`, `dashboard_auth.py` | web dashboard and token check for its commands |
| `static/dashboard.css`, `static/dashboard.js` | design system shared with the sibling bots |
| `telegram_bot.py` | notifications and commands |

---

## ⚠️ Disclaimer

This software is experimental and provided "as is", without warranty of any
kind (see the MIT license). It is not financial advice nor an invitation to
invest.

- **You can lose money.** Bugs, wrong model decisions, slippage, protocol
  exploits, manipulated oracles and liquidations can cause the loss of some or
  all of your capital.
- **Decisions are made by an LLM.** It can be wrong or behave unpredictably:
  the executor's limits reduce the damage, they do not eliminate it. Past
  results, paper ones included, do not guarantee future ones.
- **Start with paper or dry-run.** When live, use a wallet dedicated to the
  bot, with amounts you can afford to lose, and never reuse that private key
  elsewhere.
- **Protect your keys.** The private key belongs only in the deployment's
  environment variables: never commit it. Without `DASHBOARD_RUN_TOKEN` the
  dashboard commands stay disabled: set it to a long random value before
  exposing the dashboard to the Internet.
- **Laws and taxes.** You are responsible for complying with the rules and tax
  obligations of your country.

**Risks specific to this bot.** Passive yield is **not risk-free**: a protocol
exploit, a manipulated oracle or a bad liquidation can cause the loss of the
deposited capital; a market at 100% utilization can block withdrawals for hours
or days; a stablecoin can lose its peg. Screening and diversification limits
reduce the exposure, they do not eliminate it. No guarantees, no promised
returns.

## 📜 License

MIT.
