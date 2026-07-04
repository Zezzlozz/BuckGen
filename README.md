# BuckGen — Resource-Acquiring Agent

**Minimal Environment for Resource-Acquiring Agent** — an autonomous Python bot that
scans GitHub for bounties, executes DeFi operations, farms testnet airdrops, and
generates LLM-powered bounty solutions — all with human-in-the-loop approval.

```ascii
┌─────────────────────────────────────────────────────────┐
│                      BuckGen Agent                       │
├─────────────────────────────────────────────────────────┤
│  ┌─────────┐  ┌──────────┐  ┌─────────┐  ┌──────────┐  │
│  │ Bounty  │  │  DeFi    │  │ Airdrop │  │  System   │  │
│  │ Scanner │  │  Engine  │  │  Farmer │  │  Monitor  │  │
│  ├─────────┤  ├──────────┤  ├─────────┤  ├──────────┤  │
│  │ Gitcoin │  │ 1inch    │  │Faucet   │  │Circuit   │  │
│  │ Issues  │  │ Swaps    │  │Claims   │  │Breaker   │  │
│  │ Labels  │  │ CEX-CEX  │  │Batch    │  │Health    │  │
│  │ LLM     │  │ Arbitrage│  │Wallets  │  │Scheduler │  │
│  │ Scoring │  │ Budget   │  │Register │  │Telegram  │  │
│  └─────────┘  └──────────┘  └─────────┘  └──────────┘  │
│       │             │            │             │         │
│       ▼             ▼            ▼             ▼         │
│  ┌──────────────────────────────────────────────────┐   │
│  │              FastAPI Server (:8000)               │   │
│  │  ┌──────────┐  ┌──────────┐  ┌────────────────┐ │   │
│  │  │ Wallet   │  │   RPC    │  │   APScheduler  │ │   │
│  │  │ Keyring  │  │  Client  │  │   (5 cron jobs) │ │   │
│  │  └──────────┘  └──────────┘  └────────────────┘ │   │
│  └──────────────────────────────────────────────────┘   │
│       │                                                  │
│       ▼                                                  │
│  ┌──────────────────────────────────────────────────┐   │
│  │    SQLite/PostgreSQL  (8 tables)                  │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

---

## Tech Stack

| Layer | Technology | Version |
|---|---|---|
| Language | Python | 3.11.9 |
| Web framework | FastAPI | 0.115.0+ |
| ASGI server | uvicorn | 0.30.0+ |
| Database ORM | SQLAlchemy | 2.0.35+ |
| Scheduler | APScheduler | 3.10.4+ |
| Blockchain | web3.py | 7.8.0+ |
| Exchange | ccxt | 4.5.63+ |
| LLM | OpenAI-compatible (Zen) | >=1.0 |
| Encryption | cryptography (AES-256-GCM) | 44.0.0+ |
| HTTP client | httpx | 0.28.1+ |
| Testing | pytest | >=8.0 |
| Linting | ruff | >=0.5 |

---

## Table of Contents

- [1. Quick Start](#1-quick-start)
- [2. Render Deployment](#2-render-deployment)
- [3. Configuration Reference](#3-configuration-reference)
- [4. Module Architecture](#4-module-architecture)
- [5. API Reference](#5-api-reference)
- [6. Scheduler Jobs](#6-scheduler-jobs)
- [7. Database Schema](#7-database-schema)
- [8. Security Model](#8-security-model)
- [9. Testing](#9-testing)
- [10. Operations](#10-operations)
- [11. Troubleshooting](#11-troubleshooting)
- [12. Changelog](#12-changelog)

---

## 1. Quick Start

### Prerequisites

- Python 3.11+
- Git
- A 12- or 24-word BIP39 seed phrase (for wallet derivation)
- (Optional) GitHub token with no permissions — for 5000 req/hr vs 60 unauthenticated

### Setup

```bash
# 1. Clone the repository
git clone https://github.com/Zezzlozz/BuckGen.git
cd BuckGen

# 2. Create and activate a virtual environment
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
# source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your secrets (see Configuration Reference below)

# 5. Run the server
uvicorn app.main:app --reload --port 8000

# 6. Verify it's running
curl http://localhost:8000/health
# {"status":"ok","scheduler_running":true}

# 7. Check the web dashboard
# Open http://localhost:8000/dashboard in your browser

# 8. View the bounty review UI
# Open http://localhost:8000/review
```

### Verifying Setup

```bash
# Health check (no auth required)
curl http://localhost:8000/health

# Config dump
curl http://localhost:8000/config

# List wallets (seeded on first startup if SEED_PHRASE is set)
curl http://localhost:8000/wallets

# System status
curl http://localhost:8000/system/status

# With API key (set API_KEY in .env):
curl -H "X-API-Key: your-key" http://localhost:8000/system/status
```

---

## 2. Render Deployment

### Blueprint Deploy

1. Fork/push this repository to GitHub
2. Connect your GitHub repo to Render Dashboard
3. Render will detect `render.yaml` and create:
   - A **Web Service** (Docker, free tier)
   - A **Persistent Disk** (1 GB, mounted at `/app/data`)

### Required Secrets

These **must** be set in the Render Dashboard → Environment (they are `sync: false`
in `render.yaml` and cannot live in the repo):

| Variable | Purpose |
|---|---|
| `SEED_PHRASE` | BIP39 seed phrase (12 or 24 words) |
| `BUCK_ENC_PASSWORD` | Password to decrypt `buck_enc:`-prefixed seed |
| `ETH_RPC_URL` | Ethereum RPC endpoint |
| `BASE_RPC_URL` | Base RPC endpoint |
| `ARBITRUM_RPC_URL` | Arbitrum RPC endpoint |
| `POLYGON_RPC_URL` | Polygon RPC endpoint |
| `BSC_RPC_URL` | BSC RPC endpoint |
| `GITHUB_TOKEN` | GitHub token (5000 req/hr) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (from @BotFather) |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID (from @userinfobot) |
| `ZEN_API_KEY` | OpenCode Zen API key (for LLM) |
| `BINANCE_TRADE_KEY` | Binance trade API key |
| `BINANCE_TRADE_SECRET` | Binance trade API secret |
| `KRAKEN_TRADE_KEY` | Kraken trade API key |
| `KRAKEN_TRADE_SECRET` | Kraken trade API secret |
| `BYBIT_TRADE_KEY` | Bybit trade API key |
| `BYBIT_TRADE_SECRET` | Bybit trade API secret |
| `API_KEY` | API authentication key (X-API-Key header) |

### Render-Specific Settings

The `render.yaml` already configures:
- `DATABASE_URL=sqlite:///data/buckgen.db` (on 1 GB persisted disk)
- `DEBUG=false`
- `DAILY_GAS_CAP_EUR=5.0`
- `STOP_LOSS_EUR=20.0`
- Cron schedules for bounty scan, price check, and airdrop farming

### After Deploy

```bash
# Liveness check
curl https://your-app.onrender.com/health

# System status
curl -H "X-API-Key: your-key" https://your-app.onrender.com/system/status

# View the dashboard
# Open https://your-app.onrender.com/dashboard
```

---

## 3. Configuration Reference

### 3.1 Core Settings

| Env Var | Default | Description |
|---|---|---|
| `SEED_PHRASE` | `""` | BIP39 seed phrase (plain or `buck_enc:<payload>`) |
| `BUCK_ENC_PASSWORD` | `""` | Password to decrypt `buck_enc:` seed |
| `API_KEY` | `""` | API auth key (required in production) |
| `DEBUG` | `false` | Dev mode — bypasses API key auth |
| `DATABASE_URL` | `sqlite:///./data/buckgen.db` | Database connection string |

### 3.2 RPC Endpoints

| Env Var | Default | Description |
|---|---|---|
| `ETH_RPC_URL` | `https://ethereum.publicnode.com` | Ethereum RPC |
| `BASE_RPC_URL` | `https://base.publicnode.com` | Base RPC |
| `ARBITRUM_RPC_URL` | `https://arbitrum.publicnode.com` | Arbitrum RPC |
| `POLYGON_RPC_URL` | `https://polygon-bor.publicnode.com` | Polygon RPC |
| `BSC_RPC_URL` | `https://bsc.publicnode.com` | BSC RPC |

### 3.3 Exchange API Keys

| Env Var | Default | Description |
|---|---|---|
| `BINANCE_API_KEY` | `""` | Read-only Binance API key |
| `BINANCE_SECRET` | `""` | Read-only Binance API secret |
| `BINANCE_TRADE_KEY` | `""` | Trade-capable Binance key |
| `BINANCE_TRADE_SECRET` | `""` | Trade-capable Binance secret |
| `KRAKEN_TRADE_KEY` | `""` | Kraken trade API key |
| `KRAKEN_TRADE_SECRET` | `""` | Kraken trade API secret |
| `BYBIT_TRADE_KEY` | `""` | Bybit trade API key |
| `BYBIT_TRADE_SECRET` | `""` | Bybit trade API secret |

### 3.4 LLM Providers

| Env Var | Default | Description |
|---|---|---|
| `ZEN_API_KEY` | `""` | OpenCode Zen API key (primary LLM) |
| `ZEN_BASE_URL` | `https://opencode.ai/zen/v1` | Zen API base URL |
| `ZEN_MODEL_SCORE` | `deepseek-v4-flash-free` | Model for bounty scoring |
| `ZEN_MODEL_SUBMIT` | `deepseek-v4-flash-free` | Model for solution generation |
| `ZEN_MODEL_CODE` | `north-mini-code-free` | Model for code tasks |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL (fallback) |
| `OLLAMA_MODEL` | `qwen2.5-coder:7b` | Ollama fallback model |
| `LLM_CACHE_TTL` | `1800` | LLM response cache TTL (seconds) |
| `LLM_TEMPERATURE` | `0.1` | Default LLM temperature |
| `LLM_SCORE_MAX_TOKENS` | `128` | Max tokens for scoring responses |

### 3.5 Network & Timeouts

| Env Var | Default | Description |
|---|---|---|
| `HTTP_TIMEOUT` | `30.0` | Generic HTTP client timeout (seconds) |
| `LLM_TIMEOUT` | `60.0` | LLM provider timeout (seconds) |
| `HTTP_PROXY` | `""` | HTTP proxy URL (optional) |
| `HTTPS_PROXY` | `""` | HTTPS proxy URL (optional) |

### 3.6 Trading & Arbitrage

| Env Var | Default | Description |
|---|---|---|
| `SLIPPAGE` | `0.5` | Default slippage % for 1inch swaps |
| `MIN_PROFIT_THRESHOLD_PCT` | `0.8` | Min profit % to report an arb opportunity |
| `ARB_RISK_PCT` | `0.02` | Max loss estimate as fraction of capital (2%) |
| `TRADE_RESERVE_PCT` | `0.05` | Balance reserve fraction (5%) |
| `CONFIDENCE_THRESHOLD` | `0.6` | Min confidence to notify arb ops |
| `MAX_ARB_NOTIFICATIONS` | `3` | Max arb ops to notify per run |
| `INCH_API_KEY` | `""` | 1inch Business API key |
| `EUR_USD_FALLBACK` | `0.92` | Fallback EUR/USD rate |
| `ETH_USD_FALLBACK` | `2000.0` | Fallback ETH/USD price |
| `EXCHANGE_FEES_FALLBACK` | `0.002` | Fallback taker fee (0.2%) |

### 3.7 Gas & RPC Thresholds

| Env Var | Default | Description |
|---|---|---|
| `GAS_THRESHOLD_ETH` | `0.0005` | Min ETH equiv for "has_gas" |
| `GAS_LIMIT_TRANSFER` | `21000` | Gas limit for ETH transfer |
| `GAS_LIMIT_TX` | `100000` | Gas limit for general tx |
| `GAS_LIMIT_SWAP` | `200000` | Fallback gas limit for 1inch swaps |

### 3.8 Bounty & ROI Thresholds

| Env Var | Default | Description |
|---|---|---|
| `ROI_ALERT_THRESHOLD` | `80.0` | $/hr ROI to ping via Telegram |
| `BOUNTY_SCORE_THRESHOLD` | `0.6` | Min LLM score to notify high-value bounty |
| `MIN_BOUNTY_SUBMIT_SCORE` | `0.7` | Min score for auto-submit |
| `MAX_BOUNTY_SUBMISSIONS` | `3` | Max submissions per run |
| `MAX_BOUNTY_RESULTS` | `100` | Max bounties to fetch per scan |
| `GITCOIN_PER_PAGE` | `100` | GitHub API per_page limit |

### 3.9 Airdrop & Faucet

| Env Var | Default | Description |
|---|---|---|
| `FAUCET_CIRCUIT_BREAKER` | `5` | Failures before skipping faucet |
| `WALLETS_PER_CHAIN` | `3` | Wallets per chain for faucet claims |
| `MAX_AIRDROP_RESULTS` | `10` | Max airdrop results per scan |
| `AIRDROP_BASELINE_SCORE` | `0.3` | Baseline heuristic score |
| `AIRDROP_POSITIVE_SIGNAL` | `0.2` | Bonus per positive signal match |
| `AIRDROP_NEGATIVE_SIGNAL` | `0.3` | Penalty per negative signal match |

### 3.10 Budget Cost Estimates

| Env Var | Default | Description |
|---|---|---|
| `DAILY_GAS_CAP_EUR` | `5.0` | Daily gas spending cap (EUR) |
| `STOP_LOSS_EUR` | `20.0` | Total stop-loss limit (EUR) |
| `COST_FAUCET_CLAIM` | `0.001` | Estimated EUR per faucet claim |
| `COST_LLM_SUBMIT` | `0.01` | Estimated EUR per LLM submission |
| `COST_TESTNET_TRANSFER` | `0.001` | Estimated EUR per testnet transfer |
| `COST_TESTNET_DEPLOY` | `0.002` | Estimated EUR per contract deploy |
| `COST_TESTNET_NFT` | `0.001` | Estimated EUR per NFT mint |

### 3.11 Circuit Breaker & System

| Env Var | Default | Description |
|---|---|---|
| `CIRCUIT_BREAKER_THRESHOLD` | `5` | Consecutive failures before circuit opens |
| `CIRCUIT_BREAKER_RESET_SEC` | `3600` | Circuit auto-reset (seconds) |
| `ERROR_WINDOW_SEC` | `3600` | Error tracking window (seconds) |

### 3.12 Scoring & Heuristics

| Env Var | Default | Description |
|---|---|---|
| `HEURISTIC_BASELINE_SCORE` | `0.5` | Baseline heuristic bounty score |
| `KEYWORD_BONUS_WEIGHT` | `0.15` | Keyword match bonus |
| `META_BONUS_WEIGHT` | `0.1` | Metadata keyword bonus |
| `LARGE_REWARD_BONUS` | `0.1` | Score bonus for reward >$100 |
| `SMALL_REWARD_BONUS` | `0.05` | Score bonus for reward >$10 |

### 3.13 Wallet Batch Defaults

| Env Var | Default | Description |
|---|---|---|
| `BATCH_WALLET_COUNT` | `5` | Wallets per batch create |
| `BATCH_WALLET_START_INDEX` | `10` | Start index (avoids hot wallets 0-9) |

### 3.14 Cron Schedules

| Env Var | Default | Description |
|---|---|---|
| `CRON_BOUNTY_SCAN` | `0 */2 * * *` | Bounty scan — every 2 hours |
| `CRON_PRICE_CHECK` | `*/30 * * * *` | Price check — every 30 min |
| `CRON_AIRDROP` | `0 */3 * * *` | Airdrop farm — every 3 hours |

---

## 4. Module Architecture

### 4.1 `rpc.py` — Multi-Chain RPC Client

**File:** `app/modules/rpc.py`

Manages Web3 connections to 5 EVM chains (Ethereum, Base, Arbitrum, Polygon, BSC).
Caches connections, falls back through multiple public endpoints if primary is unreachable.

**Key functions:**

| Function | Returns | Description |
|---|---|---|
| `get_web3(chain)` | `Web3 \| None` | Get/create cached Web3 connection |
| `get_balance(address, chain)` | `WalletBalance` | Check native token balance |
| `get_balances_multi(address)` | `dict` | Check balance across all 5 chains |
| `check_all_chains()` | `dict[str, ChainStatus]` | Health + gas for all chains |
| `estimate_gas(chain, tx)` | `int \| None` | Estimate gas for a transaction |
| `summary()` | `dict` | RPC summary (used by `/chains` endpoint) |

**Chain config:** Each chain has a primary URL (from settings) and 3 fallback URLs.
The connection cache verifies cached connections are still alive before returning them.

**Edge cases:**
- If all RPC endpoints fail for a chain, `get_web3` returns `None`
- Each chain's native symbol differs (ETH, MATIC, BNB) and is returned in `WalletBalance.symbol`
- `has_gas` is `True` if balance > `GAS_THRESHOLD_ETH` (default 0.0005 native tokens)

---

### 4.2 `wallet.py` — HD Wallet Manager

**File:** `app/modules/wallet.py`

Derives EVM wallets from a BIP39 seed phrase using BIP44 derivation
(`m/44'/60'/0'/0/i`). Private keys are held in an in-memory keyring and
zeroed on shutdown. Only public addresses are persisted to the database.

**Key functions:**

| Function | Returns | Description |
|---|---|---|
| `get_seed_phrase()` | `str` | Get decrypted seed phrase from config |
| `derive_wallet(index, chain)` | `LocalAccount` | Derive wallet at path index |
| `sync_wallet_to_db(db, index, chain, wallet_type)` | `Wallet` | Persist wallet to DB |
| `derive_and_sync_batch(db, chains, count)` | `list[Wallet]` | Batch create wallets |
| `zero_keyring()` | `None` | Zero all private keys (shutdown) |
| `get_private_key(address)` | `str \| None` | Get key for an address (case-insensitive) |

**Chain configs:** (chain_id, coin_type, symbol, explorer URL) for all 5 chains.
All use coin_type 60' (EVM-compatible).

**Security:**
- Seed phrase is AES-256-GCM encrypted at rest (via `buck_enc:` prefix)
- Private keys stored in a Python dict, never written to disk
- `zero_keyring()` overwrites all keys on shutdown
- Only public addresses visible in the database and API

---

### 4.3 `prices.py` — Price Feeds & Arbitrage Detection

**File:** `app/modules/prices.py`

Fetches real-time tickers from 8 centralized exchanges (Binance, Coinbase, Kraken,
Bybit, KuCoin, OKX, Gate.io, MEXC) plus CoinGecko reference prices. Detects
arbitrage opportunities across exchanges by comparing bid/ask prices.

**Arbitrage strategies:**
1. **CEX-CEX** — spot price differences across exchanges (parallel fetch)
2. **CEX-DEX** — exchange price vs CoinGecko volume-weighted average
3. **Cross-chain** — same asset on different chains (requires bridge — future)

**Key functions:**

| Function | Returns | Description |
|---|---|---|
| `fetch_ticker(exchange, pair)` | `Ticker \| None` | Fetch single ticker |
| `fetch_all_tickers(pairs, exchanges)` | `dict` | Fetch all tickers (parallel ThreadPool) |
| `fetch_all_coingecko(ids)` | `dict[str, CoinGeckoPrice]` | CoinGecko reference prices |
| `find_arbitrage_opportunities(tickers, capital_eur)` | `list[ArbOpportunity]` | Detect arb gaps |
| `check_all_prices(capital_eur)` | `dict` | Full price+arb check (used by jobs) |
| `get_exchange_fee(exchange_name)` | `float` | Live taker fee from ccxt (1h cache) |
| `get_price_history(db, pair, exchange, hours)` | `list` | Historical snapshots |
| `store_ticker_snapshots(db, tickers)` | `int` | Persist tickers for trend analysis |

**Coverage:** 92 trading pairs (top coins by volume), 8 exchanges.

**Confidence scoring:**
- Volume >100k: confidence 0.5 (0.5 base)
- Volume 10k-100k: confidence 0.5 (0.8 base)
- Volume <10k: confidence 0.5 (0.6 base)
- Boosted or reduced based on volume tiers

---

### 4.4 `gitcoin.py` — GitHub Bounty Scanner

**File:** `app/modules/gitcoin.py`

Scans GitHub Issues using multiple label-based queries (e.g., `bounty`,
`reward`, `paid`) to find bounty-labelled issues across the entire GitHub
ecosystem. Extracts reward amounts from issue titles, bodies, and labels.

**Key functions:**

| Function | Returns | Description |
|---|---|---|
| `fetch_open_bounties(max_bounties)` | `list[dict]` | Search GitHub for bounty issues |
| `normalize_bounty(raw)` | `dict` | Normalize raw issue to standard format |
| `parse_reward(text)` | `(float, str)` | Extract reward + currency from text |
| `detect_experience_level(text)` | `str` | Detect beginner/intermediate/advanced |

**Search queries (hardcoded):**
- `label:bounty` + `reward`
- `label:paid` + `bounty`
- `label:bug` + `reward` (assumes bug bounties)
- `label:contest`
- `label:sponsor` + `reward`

**Rate limiting:** Respects GitHub API rate limits. With `GITHUB_TOKEN` set,
5000 requests/hour. Without it, only 60 requests/hour (likely insufficient).

---

### 4.5 `bounty_review.py` — Human-in-the-Loop Approval Gate

**File:** `app/modules/bounty_review.py`

Dual-gate approval system for bounty submissions. Every bounty solution
requires both approval AND explicit confirmation before anything is posted
to GitHub. Prevents accidental posting and gives you full control.

**Key functions:**

| Function | Returns | Description |
|---|---|---|
| `research_bounty(db, bounty_id)` | `dict` | Generate ROI briefing via LLM |
| `prepare_draft(db, bounty_id)` | `dict` | Generate draft solution |
| `save_draft(db, bounty_id, text)` | `dict` | Save edited draft |
| `approve(db, bounty_id)` | `dict` | Gate 1: mark APPROVED |
| `post_approved(db, bounty_id, confirm)` | `dict` | Gate 2: post with confirm=true |
| `rank_by_roi(db, limit, min_roi)` | `list` | Bounties sorted by $/hr |
| `build_digest(db, limit)` | `str` | Markdown digest of ROI queue |

**Flow:**
```
Research   →  Draft   →  Approve   →  Post?confirm=true
   |             |            |              |
   v             v            v              v
ROI Briefing   Solution    Gate 1       Gate 2 (final)
+ Heuristic    (LLM-gen)   (sets        (posts to GitHub)
                          APPROVED
                          status)
```

If a draft is edited after approval, approval is revoked and must be re-granted.

---

### 4.6 `airdrop.py` — Airdrop Discovery & Faucet Farming

**File:** `app/modules/airdrop.py`

Discovers airdrop/testnet opportunities from GitHub, creates disposable wallets,
claims testnet faucets, and registers for airdrops. The primary on-chain task
automation module.

**Key functions:**

| Function | Returns | Description |
|---|---|---|
| `discover_airdrops(max_results)` | `list[AirdropOpportunity]` | Scan GitHub for airdrop leads |
| `batch_create_wallets(db, count, chains, start_index)` | `list[Wallet]` | Create batch of disposable wallets |
| `claim_faucet(db, chain, wallet_index)` | `bool` | Claim testnet faucet for a wallet |
| `claim_all_faucets(db, chain, wallets_per_chain)` | `dict` | Claim faucets for all wallets |
| `farm_opportunities(db)` | `dict` | Full farm cycle (discover → wallets → faucets → register) |
| `register_wallets_github(db, opportunity)` | `int` | Register wallets via GitHub comment |
| `register_wallets_dework(db, opportunity)` | `int` | Register wallets via Dework |

**Faucet registry:** 29 pre-configured faucets covering Sepolia, Goerli, Base Sepolia,
Arbitrum Sepolia, Polygon Mumbai, BSC Testnet, and more.

**Circuit breaker:** After `FAUCET_CIRCUIT_BREAKER` (default 5) consecutive failures,
a faucet is skipped on future runs.

---

### 4.7 `defi.py` — DeFi Execution (Swaps & CEX Arb)

**File:** `app/modules/defi.py`

Executes on-chain token swaps via the 1inch Aggregator API and CEX-CEX arbitrage
via ccxt. Handles wallet derivation, transaction building, budget checks, and
revenue tracking.

**Key functions:**

| Function | Returns | Description |
|---|---|---|
| `get_swap_quote(chain, from_token, to_token, amount_wei)` | `SwapQuote \| None` | Quote from 1inch |
| `execute_swap(db, chain, wallet_index, ...)` | `SwapResult` | Execute token swap (dry-run safe) |
| `get_trade_exchange(name)` | `ccxt.Exchange \| None` | Get cached trade exchange instance |
| `execute_cex_arbitrage(db, opportunity, ...)` | `dict` | Execute CEX-CEX arb |
| `execute_arbitrage(db, chain, opportunity, capital_eur, confirm)` | `dict` | Route to CEX or on-chain |
| `_get_eth_price_usd()` | `float` | Live ETH price (CoinGecko → ccxt → fallback) |
| `_get_eur_usd_rate()` | `float` | Live EUR/USD (CoinGecko → fallback) |

**Supported chains:** Ethereum, Base, Arbitrum, Polygon, BSC

**1inch API v5 endpoints:**
```
ethereum:  https://api.1inch.com/swap/v5.2/1
base:      https://api.1inch.com/swap/v5.2/8453
arbitrum:  https://api.1inch.com/swap/v5.2/42161
polygon:   https://api.1inch.com/swap/v5.2/137
bsc:       https://api.1inch.com/swap/v5.2/56
```

**Security:**
- All operations go through `can_spend()` budget guard
- Swaps default to dry-run (`confirm=false`) — set `confirm=true` to submit
- CEX trade keys are loaded only when needed, never stored in memory permanently
- Budget is checked before every transaction

---

### 4.8 `submit_bounty.py` — LLM Solution Generator

**File:** `app/modules/submit_bounty.py`

Generates LLM-powered solutions for bounties using tiered parameters based on
reward size. Posts comments to GitHub Issues via the GitHub API.

**Key functions:**

| Function | Returns | Description |
|---|---|---|
| `generate_solution(bounty, task_type)` | `str \| None` | LLM-generate solution with tiered params |
| `github_request(method, url, json_data)` | `dict \| None` | Authenticated GitHub API call |
| `submit_bounty(db, bounty_id)` | `dict` | Submit solution for a single bounty |
| `submit_top_bounties(db, max_submissions, min_score)` | `dict` | Submit top N qualifying bounties |

**Tiered LLM parameters (by reward):**
- Reward >= 1000: temperature 0.8, max_tokens 4000, context 1000 (premium)
- Reward >= 100: temperature 0.7, max_tokens 2000, context 600 (standard)
- Reward < 100: temperature 0.5, max_tokens 1200, context 400 (economy)

**Solution validation (before posting):**
- Must be >= 100 characters
- Must be <= 5000 characters
- Must contain a code fence (```)
- Budget check: `can_spend(db, COST_LLM_SUBMIT)`

---

### 4.9 `system.py` — Health Monitor & Circuit Breaker

**File:** `app/modules/system.py`

Per-module success/failure tracking with automatic circuit breaker that opens
after `CIRCUIT_BREAKER_THRESHOLD` consecutive failures (default 5) and
auto-resets after `CIRCUIT_BREAKER_RESET_SEC` (default 1 hour).

**Key functions/classes:**

| Function | Returns | Description |
|---|---|---|
| `monitor` (instance) | `SystemMonitor` | Global singleton |
| `monitor.register(name)` | `None` | Register a module for tracking |
| `monitor.record_success(name)` | `None` | Record successful operation |
| `monitor.record_error(name, msg)` | `None` | Record error (increments counter) |
| `monitor.is_circuit_open(name)` | `bool` | Check if circuit is open |
| `monitor.reset_module(name)` | `bool` | Manually reset circuit breaker |
| `monitor.recover_all()` | `dict` | Attempt to recover all degraded modules |
| `monitor.get_summary()` | `dict` | System summary (ok/degraded/down counts) |
| `monitor.get_detailed()` | `dict` | Per-module detailed state |

**Module states:** `ok` → `degraded` (3 consecutive errors) → `circuit_open` (5 errors)

**Recovery actions:**
- `rpc` module: clears Web3 connection cache
- `prices` module: clears exchange instance cache
- All others: reset error counters

**Alerting:** When >2 modules are down, sends a Telegram alert.

---

### 4.10 `zksync_era.py` — Testnet On-Chain Tasks

**File:** `app/modules/zksync_era.py`

Performs testnet on-chain operations to establish airdrop eligibility:
self-transfers, test contract deployments, and NFT minting. Despite the name,
this handles ALL testnets (not just zkSync Era).

**Key functions:**

| Function | Returns | Description |
|---|---|---|
| `send_self_transfer(db, chain, wallet_index)` | `dict` | Send ETH to self (creates tx history) |
| `deploy_test_contract(db, chain, wallet_index)` | `dict` | Deploy minimal test contract |
| `mint_test_nft(db, chain, wallet_index)` | `dict` | Mint testnet NFT |
| `run_testnet_actions(db, chain, wallet_index)` | `dict` | Run all 3 tasks on one chain |
| `run_all_testnets(db, wallet_index)` | `dict` | Run all tasks on all testnets |

**Supported testnets:** Sepolia, Base Sepolia, Arbitrum Sepolia, Polygon Amoy,
BSC Testnet, Linea Sepolia, Scroll Sepolia, zkSync Sepolia, Optimism Sepolia,
Blast Sepolia.

**Gas limits:** Transfer: 21,000 | Deploy: 150,000 | NFT mint: 200,000

---

## 5. API Reference

All endpoints are served from FastAPI at `http://localhost:8000` or your Render
deployment URL.

**Auth header:** `X-API-Key: <your-api-key>`
**Content-Type:** `application/json`

### 5.1 Health & Status

#### `GET /health`
Liveness check. No auth required.

```bash
curl http://localhost:8000/health
```
```json
{"status": "ok", "scheduler_running": true}
```

#### `GET /config`
Non-sensitive config dump (no secrets).

```bash
curl http://localhost:8000/config
```
```json
{
  "db_url": "sqlite:///./data/buckgen.db",
  "cron_bounty": "0 */2 * * *",
  "cron_price": "*/30 * * * *",
  "cron_airdrop": "0 */3 * * *",
  "daily_gas_cap_eur": 5.0,
  "stop_loss_eur": 20.0
}
```

#### `GET /system/status`
High-level system status (modules, chains, scheduler, errors).

```bash
curl -H "X-API-Key: your-key" http://localhost:8000/system/status
```
```json
{
  "modules_ok": 6,
  "modules_total": 6,
  "modules_degraded": 0,
  "modules_down": 0,
  "errors_last_hour": 0,
  "scheduler_running": true,
  "chains_connected": 5,
  "chains_total": 5
}
```

#### `GET /system/health`
Per-module detailed health.

```bash
curl -H "X-API-Key: your-key" http://localhost:8000/system/health
```
```json
{
  "modules": [
    {
      "name": "bounties",
      "status": "ok",
      "total_errors": 0,
      "last_success_ago": "2m ago",
      "last_error_ago": null,
      "last_error_msg": null
    }
  ]
}
```

---

### 5.2 Wallets

#### `GET /wallets`
List all active wallets.

```bash
curl http://localhost:8000/wallets
```
```json
{
  "count": 6,
  "wallets": [
    {
      "address": "0x1234...5678",
      "chain": "ethereum",
      "wallet_type": "hot",
      "derivation_path": "m/44'/60'/0'/0/0",
      "balance_wei": "1000000000000000000",
      "is_active": true,
      "last_used": "2026-07-04T12:00:00+00:00"
    }
  ]
}
```

#### `GET /wallets/{address}/balance`
Live balance on a specific chain.

```bash
curl "http://localhost:8000/wallets/0x1234...5678/balance?chain=ethereum"
```
```json
{
  "address": "0x1234...5678",
  "chain": "ethereum",
  "balance_wei": 1000000000000000000,
  "balance": 1.0,
  "symbol": "ETH",
  "has_gas": true,
  "error": ""
}
```

#### `GET /wallets/{address}/balances`
Balance across ALL chains.

```bash
curl "http://localhost:8000/wallets/0x1234...5678/balances"
```

#### `GET /chains`
Health + gas info for all configured chains.

```bash
curl http://localhost:8000/chains
```
```json
{
  "ethereum": {"connected": true, "block": 21456789, "gas_price_gwei": 12.5},
  "base": {"connected": true, "block": 12345678, "gas_price_gwei": 0.01},
  "arbitrum": {"connected": true, "block": 234567890, "gas_price_gwei": 0.1},
  "polygon": {"connected": true, "block": 56789012, "gas_price_gwei": 50.0},
  "bsc": {"connected": true, "block": 34567890, "gas_price_gwei": 3.0}
}
```

---

### 5.3 Airdrops

#### `GET /airdrops/discover`
Scan GitHub for airdrop/testnet opportunities.

```bash
curl http://localhost:8000/airdrops/discover
```

#### `POST /airdrops/farm`
Run the full airdrop farming cycle (requires API key).

```bash
curl -X POST -H "X-API-Key: your-key" http://localhost:8000/airdrops/farm
```
```json
{
  "status": "ok",
  "summary": {
    "airdrops_discovered": 3,
    "wallets_created": 9,
    "faucet_claims_attempted": 12,
    "faucet_claims_succeeded": 8,
    "registrations": 2
  },
  "errors": []
}
```

---

### 5.4 Prices & Arbitrage

#### `GET /prices/tickers`
Live tickers from all exchanges.

```bash
curl http://localhost:8000/prices/tickers
```

#### `GET /prices/coingecko`
CoinGecko reference prices.

```bash
curl http://localhost:8000/prices/coingecko
```

#### `GET /prices/arbitrage`
Detect arbitrage opportunities.

```bash
curl "http://localhost:8000/prices/arbitrage?capital=500"
```
```json
{
  "summary": {
    "pairs_checked": 92,
    "exchanges_checked": 8,
    "tickers_fetched": 736,
    "opportunities_found": 2
  },
  "top_opportunities": [
    {
      "pair": "ETH/USDT",
      "buy_at": "binance",
      "buy_price": 3400.50,
      "sell_at": "kraken",
      "sell_price": 3412.80,
      "net_profit_pct": 0.32,
      "estimated_profit_eur": 1.52,
      "confidence": 0.8,
      "volume": 125000.0
    }
  ],
  "errors": []
}
```

#### `GET /prices/history/{pair}`
Price history for a trading pair.

```bash
curl "http://localhost:8000/prices/history/ETH/USDT?exchange=binance&hours=24"
```

---

### 5.5 Trading (DeFi)

#### `POST /trade/swap`
Execute a token swap via 1inch. Defaults to dry-run (`confirm=false`).
Set `confirm=true` to actually submit the transaction.

```bash
curl -X POST -H "X-API-Key: your-key" \
  "http://localhost:8000/trade/swap?chain=ethereum&wallet_index=0&from_token=ETH&to_token=USDC&amount_wei=100000000000000000&confirm=false"
```
```json
{
  "success": true,
  "tx_hash": null,
  "chain": "ethereum",
  "from_amount": "0.1",
  "to_amount": "340.50",
  "gas_used_wei": "150000",
  "error": ""
}
```

#### `POST /trade/arbitrage`
Execute the best detected arbitrage opportunity. Defaults to dry-run.

```bash
curl -X POST -H "X-API-Key: your-key" \
  "http://localhost:8000/trade/arbitrage?chain=ethereum&capital_eur=500&confirm=true"
```

#### `GET /trade/quote`
Get a 1inch swap quote without executing.

```bash
curl "http://localhost:8000/trade/quote?chain=ethereum&from_token=ETH&to_token=USDC&amount_wei=100000000000000000"
```
```json
{
  "success": true,
  "from_amount": "0.1",
  "to_amount": "340.50",
  "estimated_gas": 150000,
  "price_impact": 0.05
}
```

---

### 5.6 Bounties

#### `GET /bounties/top`
Top-scoring open bounties.

```bash
curl "http://localhost:8000/bounties/top?min_score=0.7&limit=5"
```
```json
{
  "count": 3,
  "bounties": [
    {
      "id": 1,
      "title": "Implement token swap feature",
      "reward": 500.0,
      "currency": "USD",
      "score": 0.85,
      "url": "https://github.com/..."
    }
  ]
}
```

#### `GET /bounties/review-queue`
Bounties ranked by ROI ($/hour).

```bash
curl -H "X-API-Key: your-key" \
  "http://localhost:8000/bounties/review-queue?limit=20&min_roi=50"
```

#### `GET /bounties/digest`
Markdown digest of the ROI queue.

```bash
curl "http://localhost:8000/bounties/digest?limit=15"
```

#### `GET /bounties/{id}/detail`
Full bounty record with briefing and draft.

```bash
curl http://localhost:8000/bounties/1/detail
```

#### `POST /bounties/{id}/research`
Generate ROI briefing (requires API key).

```bash
curl -X POST -H "X-API-Key: your-key" http://localhost:8000/bounties/1/research
```

#### `POST /bounties/{id}/draft`
Generate draft solution for your review.

```bash
curl -X POST -H "X-API-Key: your-key" http://localhost:8000/bounties/1/draft
```

#### `POST /bounties/{id}/draft/save`
Save an edited draft. Re-opens approval.

```bash
curl -X POST -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{"text": "def solve():\n    print(\"solution\")"}' \
  http://localhost:8000/bounties/1/draft/save
```

#### `POST /bounties/{id}/approve`
Approve for posting (Gate 1).

```bash
curl -X POST -H "X-API-Key: your-key" http://localhost:8000/bounties/1/approve
```
```json
{"success": true, "status": "approved", "bounty_id": 1}
```

#### `POST /bounties/{id}/post`
Post the approved draft. Requires BOTH approval AND `confirm=true` (Gate 2).

```bash
curl -X POST -H "X-API-Key: your-key" \
  "http://localhost:8000/bounties/1/post?confirm=true"
```
```json
{
  "success": true,
  "status": "submitted",
  "comment_url": "https://github.com/...",
  "message": "Solution posted to GitHub"
}
```

#### `POST /bounties/submit-top`
Deprecated: auto-submit is disabled. This now researches top bounties into your review queue.

```bash
curl -X POST -H "X-API-Key: your-key" \
  "http://localhost:8000/bounties/submit-top?max_subs=3&min_score=0.7"
```

---

### 5.7 On-Chain Tasks (Testnet)

#### `POST /tasks/self-transfer`
Send a self-transfer on testnet (airdrop eligibility).

```bash
curl -X POST -H "X-API-Key: your-key" \
  "http://localhost:8000/tasks/self-transfer?chain=sepolia&wallet_index=10"
```

#### `POST /tasks/deploy`
Deploy a test contract.

```bash
curl -X POST -H "X-API-Key: your-key" \
  "http://localhost:8000/tasks/deploy?chain=sepolia&wallet_index=10"
```

#### `POST /tasks/run-all`
Run all on-chain tasks on all testnets.

```bash
curl -X POST -H "X-API-Key: your-key" \
  "http://localhost:8000/tasks/run-all?wallet_index=10"
```

---

### 5.8 Revenue & P&L

#### `GET /revenue/summary`
P&L summary across all modules.

```bash
curl "http://localhost:8000/revenue/summary?hours=168"
```
```json
{
  "total_revenue_eur": 1500.0,
  "total_spend_eur": 42.50,
  "total_profit_eur": 1457.50,
  "total_roi_pct": 3429.41,
  "per_module": [
    {"module": "bounties", "revenue_eur": 1500.0, "spend_eur": 0.5, "profit_eur": 1499.5, "roi_pct": 299900.0}
  ],
  "top_sources": [
    {"source": "https://github.com/...", "amount_eur": 500.0, "module": "bounties"}
  ]
}
```

#### `GET /revenue/module/{module}`
P&L for a specific module. Valid modules: `bounties`, `arbitrage`, `airdrops`, `tasks`, `defi`.

```bash
curl "http://localhost:8000/revenue/module/bounties?hours=720"
```

#### `POST /bounties/{id}/mark-paid`
Mark a bounty as PAID and record revenue.

```bash
curl -X POST -H "X-API-Key: your-key" \
  "http://localhost:8000/bounties/1/mark-paid?actual_reward=500"
```

---

### 5.9 System Management

#### `POST /system/reset/{module_name}`
Manually reset a module's circuit breaker.

```bash
curl -X POST -H "X-API-Key: your-key" \
  http://localhost:8000/system/reset/bounties
```

#### `POST /system/recover`
Attempt to recover all degraded/down modules.

```bash
curl -X POST -H "X-API-Key: your-key" http://localhost:8000/system/recover
```

---

### 5.10 Web Interfaces

#### `GET /review`
Human-in-the-loop bounty review dashboard (HTML).

```
Open in browser: http://localhost:8000/review
```

#### `GET /dashboard`
Agent monitoring dashboard (HTML with JS, auto-refreshes every 30s).

```
Open in browser: http://localhost:8000/dashboard
```

---

### 5.11 Error Handling

All endpoints return consistent error responses:

```json
{"success": false, "error": "Human-readable error message"}
```

HTTP status codes:
- `200` — Success
- `400` — Bad request (invalid params)
- `401` — Invalid/missing API key
- `404` — Resource not found
- `500` — Internal server error (sanitized, no traceback)
- `503` — API_KEY not configured in production

Unhandled exceptions are caught by a global handler and return a sanitized
`500` response with no stack trace exposure.

---

## 6. Scheduler Jobs

| Job | ID | Cron | Purpose | Misfire Grace |
|---|---|---|---|---|
| Bounty scan | `scan_bounties` | Every 2h (`0 */2 * * *`) | Search GitHub for new bounty issues, LLM-score, persist | 300s |
| Price check | `check_prices` | Every 30min (`*/30 * * * *`) | Fetch tickers from 8 CEXes + CoinGecko, detect arb | 120s |
| Airdrop farm | `farm_airdrops` | Every 3h (`0 */3 * * *`) | Discover airdrops, create wallets, claim faucets | 300s |
| Gas balance | `check_gas_balances` | Every 2h at :30 (`30 */2 * * *`) | Check wallet balances, alert on low gas | 300s |
| Self-heal | `self_heal` | Every 30min at :05/:35 (`5,35 * * * *`) | Health check, recover degraded modules, alert if needed | 120s |

Jobs are staggered to avoid contention at :00 marks.

**Error handling:** Each job has a try/except/finally block that:
1. Logs the error
2. Sends a Telegram alert via `notify_error()`
3. Records the error in the module's circuit breaker via `monitor.record_error()`
4. Rolls back the DB session on failure

---

## 7. Database Schema

8 tables in SQLite (or PostgreSQL). All IDs are auto-incrementing integers.

### `bounties`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | PK |
| `platform` | Enum | "github", "dework", "layer3", "hackquest" |
| `external_id` | String(255) | Unique per platform |
| `title` | String(512) | Bounty title |
| `description` | Text | Full issue body |
| `reward_amount` | Float | USD/EUR amount |
| `reward_currency` | String(32) | "USD", "ETH", "USDC" |
| `experience_level` | String(64) | "beginner", "intermediate", "advanced" |
| `url` | String(1024) | Original issue URL |
| `status` | Enum | open → researched → drafted → approved → applied → paid/expired/failed |
| `score` | Float | LLM-assigned quality score (0-1) |
| `roi_score` | Float | Expected $ per hour |
| `effort_hours` | Float | Estimated effort in hours |
| `payout_confidence` | Float | 0-1 likelihood of real payout |
| `briefing` | Text | Private research writeup |
| `draft_solution` | Text | Draft solution (not posted) |
| `approved_at` | DateTime | When you approved |
| `created_at` | DateTime | Auto |
| `updated_at` | DateTime | Auto on update |

**Indexes:** `(status)`, `(score)`, `(status, score)`, unique `(platform, external_id)`

### `wallets`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | PK |
| `address` | String(128) | Unique, indexed |
| `wallet_type` | Enum | "hot", "cold", "disposable" |
| `derivation_path` | String(128) | BIP44 path |
| `chain` | String(32) | Default chain |
| `balance_wei` | String(64) | Cached balance (string to avoid precision loss) |
| `is_active` | Boolean | Soft delete |
| `created_at` | DateTime | Auto |
| `last_used_at` | DateTime | Nullable |

**Indexes:** `(address)`, `(chain, wallet_type, is_active)`, `(is_active)`

### `transactions`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | PK |
| `wallet_id` | Integer | FK to wallets |
| `chain` | String(32) | |
| `tx_hash` | String(256) | Unique, nullable |
| `tx_type` | String(64) | "claim", "transfer", "swap", "gas" |
| `amount_wei` | String(64) | |
| `gas_used_wei` | String(64) | |
| `status` | String(32) | "pending", "confirmed", "failed" |
| `memo` | String(512) | |
| `created_at` | DateTime | Auto |
| `confirmed_at` | DateTime | Nullable |

**Indexes:** `(wallet_id)`, `(tx_type)`, `(status)`, `(chain, status)`

### `price_snapshots`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | PK |
| `pair` | String(32) | e.g. "BTC/USDT" |
| `exchange` | String(32) | |
| `bid` | Float | |
| `ask` | Float | |
| `last` | Float | |
| `volume` | Float | |
| `recorded_at` | DateTime | Auto |

**Indexes:** `(pair, recorded_at)`

### `budget`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | PK |
| `date` | String(16) | YYYY-MM-DD |
| `category` | String(64) | "gas", "api", "proxy" |
| `amount_eur` | Float | |
| `memo` | String(256) | |
| `created_at` | DateTime | Auto |

**Indexes:** `(date)`

### `revenue`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | PK |
| `module` | String(64) | "bounties", "arbitrage", "airdrops" |
| `amount_eur` | Float | |
| `currency` | String(16) | |
| `source` | String(255) | URL or description |
| `memo` | String(512) | |
| `earned_at` | DateTime | Auto |

**Indexes:** `(earned_at)`, `(module)`

### `blacklist`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer | PK |
| `source_type` | String(64) | "platform", "contract", "user" |
| `source_id` | String(255) | |
| `reason` | String(512) | |
| `failed_attempts` | Integer | |
| `created_at` | DateTime | Auto |

**Indexes:** Unique `(source_type, source_id)`

---

## 8. Security Model

### Seed Encryption

The BIP39 seed phrase can be stored in two ways:

1. **Plain text** (dev only): `SEED_PHRASE="word1 word2 ... word12"` in `.env`
2. **Encrypted** (production): `SEED_PHRASE="buck_enc:<base64>"` with `BUCK_ENC_PASSWORD`

Encrypted seed is AES-256-GCM with a random 12-byte nonce and 16-byte auth tag.
The encryption function is in `app/utils/crypto.py`.

To encrypt a seed phrase:
```bash
python -m app.encrypt_env "your seed phrase" "your password"
# Output: buck_enc:<base64 payload>
```

### API Authentication

- All write/execution endpoints require `X-API-Key` header matching `API_KEY` in settings
- Auth is **fail-closed**: if `API_KEY` is unset in production, ALL requests get `503`
- Constant-time comparison (`hmac.compare_digest`) to prevent timing attacks
- Debug mode (`DEBUG=true`) bypasses API key auth (local dev only)

### Wallet Security

- Private keys derived from seed phrase at runtime, stored in Python dict
- `zero_keyring()` overwrites all keys on application shutdown
- Only public addresses are stored in the database
- Private keys are never logged or exposed via API
- Different derivation indices for hot wallets (0-9) vs disposable wallets (10+)

### Budget Guards

All spend-capable operations go through `can_spend()` before execution:
- **Daily gas cap** (`DAILY_GAS_CAP_EUR`, default 5.0 EUR)
- **Stop-loss** (`STOP_LOSS_EUR`, default 20.0 EUR total)
- Budget is checked synchronously before each transaction

### Exchange API Separation

Trade-capable API keys are **separate** from read-only keys:
- `BINANCE_API_KEY` (read-only) vs `BINANCE_TRADE_KEY` (trade/withdraw)
- Trade keys are only loaded when executing an arbitrage trade
- Principle of least privilege: read keys for price monitoring, trade keys for execution

### Dry-Run Mode

Every operation that can move funds defaults to dry-run (`confirm=false`):
- Swaps: `POST /trade/swap?confirm=false` (preview only)
- Arbitrage: `POST /trade/arbitrage?confirm=false` (preview only)
- Bounty posting: `POST /bounties/{id}/post?confirm=false` (preview only)

Set `confirm=true` to actually execute.

---

## 9. Testing

### Running Tests

```bash
# Run all 398 unit tests
pytest tests/unit/ -v

# Run with short traceback (cleaner output)
pytest tests/unit/ -v --tb=short

# Run a specific test file
pytest tests/unit/test_defi.py -v

# Run a specific test class
pytest tests/unit/test_defi.py::TestExecuteCexArbitrage -v

# Run integration tests (requires live config)
pytest tests/integration_test.py -v
```

### Test Structure

```
tests/
├── integration_test.py          # Full end-to-end (config → DB → wallets → prices → P&L)
├── test_airdrop.py              # Ad-hoc: live airdrop discovery
├── test_bounty_review.py        # Approval-gate invariants
├── test_gitcoin.py              # Ad-hoc: live GitHub bounty fetch
├── test_prices.py               # Ad-hoc: live price/arb scan
├── test_trade_safety.py         # Safety-gate invariants (dry-run, auth)
├── test_wallet_rpc.py           # Ad-hoc: wallet + RPC connectivity
└── unit/
    ├── conftest.py              # Fixtures: in-memory SQLite per test
    ├── test_airdrop.py          # Airdrop discovery + batch wallets
    ├── test_blacklist.py        # Blacklist CRUD
    ├── test_budget.py           # Spend recording + caps
    ├── test_config.py           # Settings + encryption roundtrip
    ├── test_crypto.py           # AES-256-GCM encrypt/decrypt
    ├── test_db_models.py        # init_db, table creation, CRUD
    ├── test_defi.py             # Trade exchanges, arb execution, budget guard
    ├── test_gitcoin.py          # Bounty fetch, pagination, normalization
    ├── test_jobs.py             # All 5 scheduler jobs
    ├── test_main.py             # All 25+ FastAPI endpoints via TestClient
    ├── test_notify.py           # Telegram send + notification functions
    ├── test_pnl.py              # Revenue recording + P&L queries
    ├── test_prices.py           # Ticker fetch, normalization, arb detection
    ├── test_rpc.py              # Web3 caching, chain checks, balances
    ├── test_scorer.py           # LLM scoring, heuristic fallback, parsing
    ├── test_submit_bounty.py    # Solution gen, GitHub post, budget checks
    ├── test_system.py           # Module registration, circuit breaker
    ├── test_wallet.py           # BIP44 derivation, keyring, DB sync
    └── test_zksync.py           # Transfers, contract deploy, NFT mint
```

### Conftest Fixtures

`tests/unit/conftest.py` provides:
- Clean in-memory SQLite database per test
- `init_db()` called automatically
- `session` fixture for direct DB access
- Default env var overrides (e.g., `SEED_PHRASE`, `DEBUG=False`)
- Sample budget entry fixture

### Writing Tests

1. Use `conftest.py` fixtures for database access
2. Mock external services (HTTP, Web3, ccxt) with `unittest.mock.patch`
3. Prefer synchronous tests where possible; use `pytest.mark.asyncio` for async
4. Test both success paths and error/edge cases
5. Verify budget guard behavior (can_spend/record_spend)
6. Test circuit breaker state transitions

---

## 10. Operations

### Health Endpoints

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /health` | Liveness (scheduler running) | None |
| `GET /system/status` | High-level status | API key |
| `GET /system/health` | Per-module detailed health | API key |
| `GET /chains` | Chain connectivity + gas | None |
| `GET /config` | Non-sensitive config dump | None |

### Circuit Breaker

Each module has an automatic circuit breaker that:
1. **Tracks consecutive errors** per module
2. **Degrades** after 3 consecutive failures (status → `degraded`)
3. **Opens circuit** after 5 consecutive failures (status → `circuit_open`)
4. **Auto-resets** after 1 hour (`CIRCUIT_BREAKER_RESET_SEC`)
5. **Triggers recovery** on next `self_heal` cycle

To manually reset a module's circuit breaker:
```bash
curl -X POST -H "X-API-Key: your-key" http://localhost:8000/system/reset/bounties
```

### Encrypting Secrets

The `encrypt_env` CLI tool encrypts any string with AES-256-GCM:

```bash
# Encrypt a seed phrase
python -m app.encrypt_env "word1 word2 ... word12" "my password"
# Output: buck_enc:6XNIB7oP1V61Ir0umv2w4XkM+FUfTpLz...

# Usage in .env:
# SEED_PHRASE="buck_enc:6XNIB7oP1V61Ir0umv2w4XkM+FUfTpLz..."
# BUCK_ENC_PASSWORD="my password"
```

### Backups

The database is at `data/buckgen.db` (SQLite) or your configured `DATABASE_URL`.

```bash
# Backup SQLite database
cp data/buckgen.db data/buckgen.db.backup

# Or use the app.backup/ directory created during configuration changes
```

### Monitoring

- **Dashboard**: `GET /dashboard` — browser-based monitoring with auto-refresh
- **Review UI**: `GET /review` — bounty review dashboard
- **Telegram alerts**: High-value bounties, arbitrage opportunities, low gas, circuit breaker changes, system errors

### Logging

- Log level: `DEBUG` when `DEBUG=true`, else `INFO`
- Format: `2026-07-04 12:00:00 [buckgen.module] LEVEL message`
- Logs include module prefix for filtering: `[buckgen.defi]`, `[buckgen.rpc]`, etc.
- No secrets or private keys are logged (addresses may be truncated)

---

## 11. Troubleshooting

### "Server misconfigured: API_KEY not set"

Set `API_KEY` in `.env`, or run with `DEBUG=true` for local development.

### "No SEED_PHRASE configured"

Set `SEED_PHRASE` in `.env`. You can encrypt it with `python -m app.encrypt_env`.

### Health check fails on Render

1. Check the Render Dashboard → Logs for startup errors
2. Verify all `sync: false` secrets are set in Render Dashboard → Environment
3. Ensure `SEED_PHRASE` or `BUCK_ENC_PASSWORD` is configured
4. Check that `API_KEY` is set (required for production)

### Wallets not showing up

Wallets are only created on first startup if `SEED_PHRASE` is set. To trigger
manual seeding, you can call one of the farm endpoints or restart the service.

### "Low gas" alerts

Fund the wallets with testnet ETH from the faucets listed in the dashboard.
Some testnet faucets require a social media post or are rate-limited.

### Arbitrage finds no opportunities

1. Check exchange API keys are valid (read-only keys for price feeds)
2. Ensure `MIN_PROFIT_THRESHOLD_PCT` is set low enough (0.8% default)
3. Verify at least one exchange has trade keys for execution
4. Check CoinGecko API is reachable (no proxy issues)

### Bounty scanner finds no bounties

1. Check `GITHUB_TOKEN` is set (60 req/hr without it is very limiting)
2. Verify the GitHub API is reachable
3. Check that `MAX_BOUNTY_RESULTS` isn't set too low
4. Check logs for rate limit errors

### Database locked (SQLite)

SQLite doesn't handle concurrent writes well. If you see `database is locked`:
1. Reduce scheduler job overlap in schedules
2. Consider switching to PostgreSQL via `DATABASE_URL`

### LLM not responding

1. Check `ZEN_API_KEY` is set (primary provider)
2. Check `OLLAMA_BASE_URL` if using local fallback
3. Check `LLM_TIMEOUT` — increase if your model is slow
4. Verify `LLM_CACHE_TTL` — cached responses never retry

---

## 12. Changelog

### v0.1.0 — Initial Release

- GitHub Issues bounty scanner with LLM scoring
- 1inch Aggregator swap execution on 5 EVM chains
- CEX-CEX arbitrage detection and execution (Binance, Kraken, Bybit)
- Multi-exchange price feeds (8 CEXes + CoinGecko)
- Testnet airdrop farming with 29 faucets
- HD wallet management with AES-256-GCM encrypted seed
- Human-in-the-loop bounty approval flow
- Telegram alerts for high-value opportunities and errors
- Circuit breaker with automatic recovery
- Scheduler with 5 cron jobs
- FastAPI web server with browser dashboard
- Render Blueprint deployment with 1 GB persisted disk
- 398 unit tests, 0 failures

**Notable commits:**
- `3bd1f4d` — Hardcoded values remediation (Phase A/B/C): all thresholds, timeouts, fees, and budget costs configurable via env vars
- `5f4dd3c` — Remove applied patch file
- `50ed0c8` — Add human-in-the-loop ROI bounty review; disable auto-post
- `3e10bdc` — P0 security + reliability: solution validation, faucet circuit breaker, RPC fallbacks, rate limit tracking, LLM cache

---

*Generated from source on 2026-07-04.*
