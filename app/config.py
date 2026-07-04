"""
Application configuration loader.
Secrets are loaded from environment variables (or .env file).
Seed phrase is optionally encrypted with AES-256-GCM and decrypted at runtime.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from app.utils.crypto import decrypt_env, encrypt_env

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
ENV_ENCRYPTED_FILE = BASE_DIR / ".env.encrypted"

# ---------------------------------------------------------------------------
# Load plain-text .env if it exists (development only)
# ---------------------------------------------------------------------------
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)


# ---------------------------------------------------------------------------
# Configuration holder
# ---------------------------------------------------------------------------
class Settings:
    """Immutable settings populated from env at startup."""

    def __init__(self):
        # -- Encryption password for .env.encrypted -------------------------
        self._enc_password: str | None = os.getenv("BUCK_ENC_PASSWORD")

        # -- Seed phrase (encrypted or plain) -------------------------------
        raw_seed = os.getenv("SEED_PHRASE", "")
        if self._enc_password and raw_seed.startswith("buck_enc:"):
            # encrypted payload — decrypt at runtime
            payload = raw_seed.removeprefix("buck_enc:")
            self.SEED_PHRASE: str = decrypt_env(payload, self._enc_password)
        else:
            self.SEED_PHRASE: str = raw_seed

        # -- GitHub token (for bounty search, 5000 req/hr vs 60 unauthed) ---
        self.GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

        # -- API authentication key -------------------------------------------
        self.API_KEY: str = os.getenv("API_KEY", "")

        # -- Exchange API keys (read-only permissions) ----------------------
        self.BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
        self.BINANCE_SECRET: str = os.getenv("BINANCE_SECRET", "")

        # -- Trade-capable exchange API keys (separate for security) ---------
        # These must have trade/withdraw permissions. Keep separate from
        # read-only keys above to allow principle of least privilege.
        self.BINANCE_TRADE_KEY: str = os.getenv("BINANCE_TRADE_KEY", "")
        self.BINANCE_TRADE_SECRET: str = os.getenv("BINANCE_TRADE_SECRET", "")
        self.KRAKEN_TRADE_KEY: str = os.getenv("KRAKEN_TRADE_KEY", "")
        self.KRAKEN_TRADE_SECRET: str = os.getenv("KRAKEN_TRADE_SECRET", "")
        self.BYBIT_TRADE_KEY: str = os.getenv("BYBIT_TRADE_KEY", "")
        self.BYBIT_TRADE_SECRET: str = os.getenv("BYBIT_TRADE_SECRET", "")

        # -- Proxy (optional — SOCKS5 or HTTP proxy for all outbound traffic)
        self.HTTP_PROXY: str = os.getenv("HTTP_PROXY", "")
        self.HTTPS_PROXY: str = os.getenv("HTTPS_PROXY", "")

        # -- RPC endpoints (publicnode.com is free, no API key needed) -------
        self.ETH_RPC_URL: str = os.getenv(
            "ETH_RPC_URL", "https://ethereum.publicnode.com"
        )
        self.BASE_RPC_URL: str = os.getenv(
            "BASE_RPC_URL", "https://base.publicnode.com"
        )
        self.ARBITRUM_RPC_URL: str = os.getenv(
            "ARBITRUM_RPC_URL", "https://arbitrum.publicnode.com"
        )
        self.POLYGON_RPC_URL: str = os.getenv(
            "POLYGON_RPC_URL", "https://polygon-bor.publicnode.com"
        )
        self.BSC_RPC_URL: str = os.getenv("BSC_RPC_URL", "https://bsc.publicnode.com")

        # -- 1inch API key ----------------------------------------------------
        self.INCH_API_KEY: str = os.getenv("INCH_API_KEY", "")

        # -- Telegram alerts -------------------------------------------------
        self.TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

        # -- Budget ---------------------------------------------------------
        self.DAILY_GAS_CAP_EUR: float = float(os.getenv("DAILY_GAS_CAP_EUR", "5.0"))
        self.STOP_LOSS_EUR: float = float(os.getenv("STOP_LOSS_EUR", "20.0"))

        # -- Schedules (cron expressions) -----------------------------------
        self.CRON_BOUNTY_SCAN: str = os.getenv("CRON_BOUNTY_SCAN", "0 */2 * * *")
        self.CRON_PRICE_CHECK: str = os.getenv("CRON_PRICE_CHECK", "*/30 * * * *")
        self.CRON_AIRDROP: str = os.getenv("CRON_AIRDROP", "0 */3 * * *")

        # -- Database -------------------------------------------------------
        self.DATABASE_URL: str = os.getenv(
            "DATABASE_URL",
            f"sqlite:///{BASE_DIR / 'data' / 'buckgen.db'}",
        )

        # -- LLM: OpenCode Zen (primary) ------------------------------------
        self.ZEN_API_KEY: str = os.getenv("ZEN_API_KEY", "")
        self.ZEN_BASE_URL: str = os.getenv("ZEN_BASE_URL", "https://opencode.ai/zen/v1")
        # Per-task model selection — free models by default
        self.ZEN_MODEL_SCORE: str = os.getenv(
            "ZEN_MODEL_SCORE", "deepseek-v4-flash-free"
        )
        self.ZEN_MODEL_SUBMIT: str = os.getenv(
            "ZEN_MODEL_SUBMIT", "deepseek-v4-flash-free"
        )
        self.ZEN_MODEL_CODE: str = os.getenv("ZEN_MODEL_CODE", "north-mini-code-free")

        # -- LLM: local Ollama (fallback if Zen unavailable) ----------------
        self.OLLAMA_BASE_URL: str = os.getenv(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        )
        self.OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

        # -- Network timeouts ------------------------------------------------
        self.HTTP_TIMEOUT: float = float(os.getenv("HTTP_TIMEOUT", "30.0"))
        self.LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "60.0"))

        # -- Live price fallbacks (used when live fetch fails) ----------------
        self.EUR_USD_FALLBACK: float = float(os.getenv("EUR_USD_FALLBACK", "0.92"))
        self.ETH_USD_FALLBACK: float = float(os.getenv("ETH_USD_FALLBACK", "2000.0"))

        # -- LLM cache -------------------------------------------------------
        self.LLM_CACHE_TTL: int = int(os.getenv("LLM_CACHE_TTL", "1800"))

        # -- Trading defaults ------------------------------------------------
        self.SLIPPAGE: float = float(os.getenv("SLIPPAGE", "0.5"))

        # -- Arbitrage thresholds --------------------------------------------
        self.MIN_PROFIT_THRESHOLD_PCT: float = float(
            os.getenv("MIN_PROFIT_THRESHOLD_PCT", "0.8")
        )
        self.ARB_RISK_PCT: float = float(os.getenv("ARB_RISK_PCT", "0.02"))
        self.TRADE_RESERVE_PCT: float = float(os.getenv("TRADE_RESERVE_PCT", "0.05"))
        self.CONFIDENCE_THRESHOLD: float = float(
            os.getenv("CONFIDENCE_THRESHOLD", "0.6")
        )
        self.MAX_ARB_NOTIFICATIONS: int = int(os.getenv("MAX_ARB_NOTIFICATIONS", "3"))
        self.EXCHANGE_FEES_FALLBACK: float = float(
            os.getenv("EXCHANGE_FEES_FALLBACK", "0.002")
        )

        # -- Gas / RPC -------------------------------------------------------
        self.GAS_THRESHOLD_ETH: float = float(os.getenv("GAS_THRESHOLD_ETH", "0.0005"))
        self.GAS_LIMIT_TRANSFER: int = int(os.getenv("GAS_LIMIT_TRANSFER", "21000"))
        self.GAS_LIMIT_TX: int = int(os.getenv("GAS_LIMIT_TX", "100000"))
        self.GAS_LIMIT_SWAP: int = int(os.getenv("GAS_LIMIT_SWAP", "200000"))

        # -- Bounty / ROI thresholds -----------------------------------------
        self.ROI_ALERT_THRESHOLD: float = float(
            os.getenv("ROI_ALERT_THRESHOLD", "80.0")
        )
        self.BOUNTY_SCORE_THRESHOLD: float = float(
            os.getenv("BOUNTY_SCORE_THRESHOLD", "0.6")
        )
        self.MIN_BOUNTY_SUBMIT_SCORE: float = float(
            os.getenv("MIN_BOUNTY_SUBMIT_SCORE", "0.7")
        )
        self.MAX_BOUNTY_SUBMISSIONS: int = int(os.getenv("MAX_BOUNTY_SUBMISSIONS", "3"))
        self.MAX_BOUNTY_RESULTS: int = int(os.getenv("MAX_BOUNTY_RESULTS", "100"))
        self.GITCOIN_PER_PAGE: int = int(os.getenv("GITCOIN_PER_PAGE", "100"))

        # -- Airdrop / faucet thresholds -------------------------------------
        self.FAUCET_CIRCUIT_BREAKER: int = int(os.getenv("FAUCET_CIRCUIT_BREAKER", "5"))
        self.WALLETS_PER_CHAIN: int = int(os.getenv("WALLETS_PER_CHAIN", "3"))
        self.MAX_AIRDROP_RESULTS: int = int(os.getenv("MAX_AIRDROP_RESULTS", "10"))
        self.AIRDROP_BASELINE_SCORE: float = float(
            os.getenv("AIRDROP_BASELINE_SCORE", "0.3")
        )
        self.AIRDROP_POSITIVE_SIGNAL: float = float(
            os.getenv("AIRDROP_POSITIVE_SIGNAL", "0.2")
        )
        self.AIRDROP_NEGATIVE_SIGNAL: float = float(
            os.getenv("AIRDROP_NEGATIVE_SIGNAL", "0.3")
        )

        # -- Budget cost estimates (testnet / LLM / faucet ops) --------------
        self.COST_FAUCET_CLAIM: float = float(os.getenv("COST_FAUCET_CLAIM", "0.001"))
        self.COST_LLM_SUBMIT: float = float(os.getenv("COST_LLM_SUBMIT", "0.01"))
        self.COST_TESTNET_TRANSFER: float = float(
            os.getenv("COST_TESTNET_TRANSFER", "0.001")
        )
        self.COST_TESTNET_DEPLOY: float = float(
            os.getenv("COST_TESTNET_DEPLOY", "0.002")
        )
        self.COST_TESTNET_NFT: float = float(os.getenv("COST_TESTNET_NFT", "0.001"))

        # -- Circuit breaker / system ----------------------------------------
        self.CIRCUIT_BREAKER_THRESHOLD: int = int(
            os.getenv("CIRCUIT_BREAKER_THRESHOLD", "5")
        )
        self.CIRCUIT_BREAKER_RESET_SEC: int = int(
            os.getenv("CIRCUIT_BREAKER_RESET_SEC", "3600")
        )
        self.ERROR_WINDOW_SEC: int = int(os.getenv("ERROR_WINDOW_SEC", "3600"))

        # -- LLM scoring defaults --------------------------------------------
        self.LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
        self.LLM_SCORE_MAX_TOKENS: int = int(os.getenv("LLM_SCORE_MAX_TOKENS", "128"))
        self.HEURISTIC_BASELINE_SCORE: float = float(
            os.getenv("HEURISTIC_BASELINE_SCORE", "0.5")
        )
        self.KEYWORD_BONUS_WEIGHT: float = float(
            os.getenv("KEYWORD_BONUS_WEIGHT", "0.15")
        )
        self.META_BONUS_WEIGHT: float = float(os.getenv("META_BONUS_WEIGHT", "0.1"))
        self.LARGE_REWARD_BONUS: float = float(os.getenv("LARGE_REWARD_BONUS", "0.1"))
        self.SMALL_REWARD_BONUS: float = float(os.getenv("SMALL_REWARD_BONUS", "0.05"))
        self.BATCH_WALLET_COUNT: int = int(os.getenv("BATCH_WALLET_COUNT", "5"))
        self.BATCH_WALLET_START_INDEX: int = int(
            os.getenv("BATCH_WALLET_START_INDEX", "10")
        )

        # -- Debug ----------------------------------------------------------
        self.DEBUG: bool = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def http_headers(self) -> dict[str, str]:
        """Return headers with a realistic User-Agent for outbound requests."""
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        }

    def proxy_config(self) -> dict[str, str] | None:
        """Return proxy configuration for httpx (None if not configured).

        httpx 0.28+ proxy parameter accepts a dict mapping URL patterns
        (with ``://`` suffix) to proxy URLs, or a single proxy URL string.
        """
        cfg: dict[str, str] = {}
        if self.HTTP_PROXY:
            cfg["http://"] = self.HTTP_PROXY
        if self.HTTPS_PROXY:
            cfg["https://"] = self.HTTPS_PROXY
        return cfg or None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def encrypt_seed(self, password: str) -> str:
        """Return 'buck_enc:<payload>' for use in .env.encrypted."""
        payload = encrypt_env(self.SEED_PHRASE, password)
        return f"buck_enc:{payload}"

    def validate(self) -> list[str]:
        """Return list of missing critical settings."""
        missing = []
        if not self.SEED_PHRASE:
            missing.append("SEED_PHRASE")
        if not self.ETH_RPC_URL:
            missing.append("ETH_RPC_URL")
        return missing


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------
settings = Settings()
