"""
Unit tests for Application Configuration (app/config.py).

Tests cover:
  - Settings constructor reads env vars and applies defaults
  - encrypt_seed produces buck_enc: prefixed output
  - validate detects missing critical settings
  - Decryption of encrypted seed phrase
"""

from unittest.mock import patch

# Note: the global `settings` singleton is created at import time in app/config.py
# with env vars set by conftest.py.  Here we test the Settings class directly by
# constructing fresh instances with monkeypatched environments.


class TestSettingsDefaults:
    """Settings() — default values when no env vars are set."""

    def _make_settings(self, monkeypatch, **overrides):
        """Create a Settings instance with selective env overrides.

        Clears known env vars before applying overrides so user's real env
        does not leak into tests.
        """
        _CLEAR = [
            "SEED_PHRASE",
            "GITHUB_TOKEN",
            "BINANCE_API_KEY",
            "BINANCE_SECRET",
            "BINANCE_TRADE_KEY",
            "BINANCE_TRADE_SECRET",
            "KRAKEN_TRADE_KEY",
            "KRAKEN_TRADE_SECRET",
            "BYBIT_TRADE_KEY",
            "BYBIT_TRADE_SECRET",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "OLLAMA_BASE_URL",
            "OLLAMA_MODEL",
            "ZEN_API_KEY",
            "ZEN_BASE_URL",
            "ZEN_MODEL_SCORE",
            "ZEN_MODEL_SUBMIT",
            "ZEN_MODEL_CODE",
            "DEBUG",
            "ETH_RPC_URL",
            "BASE_RPC_URL",
            "ARBITRUM_RPC_URL",
            "POLYGON_RPC_URL",
            "BSC_RPC_URL",
            "BUCK_ENC_PASSWORD",
        ]
        for key in _CLEAR:
            monkeypatch.delenv(key, raising=False)
        for k, v in overrides.items():
            monkeypatch.setenv(k, str(v))
        from app.config import Settings

        return Settings()

    def test_seed_phrase_default_empty(self, monkeypatch):
        s = self._make_settings(monkeypatch)
        assert s.SEED_PHRASE == ""

    def test_github_token_default_empty(self, monkeypatch):
        s = self._make_settings(monkeypatch)
        assert s.GITHUB_TOKEN == ""

    def test_daily_gas_cap_default(self, monkeypatch):
        s = self._make_settings(monkeypatch)
        assert s.DAILY_GAS_CAP_EUR == 5.0

    def test_stop_loss_default(self, monkeypatch):
        s = self._make_settings(monkeypatch)
        assert s.STOP_LOSS_EUR == 20.0

    def test_ollama_defaults(self, monkeypatch):
        s = self._make_settings(monkeypatch)
        assert s.OLLAMA_BASE_URL == "http://localhost:11434"
        assert s.OLLAMA_MODEL == "qwen2.5-coder:7b"

    def test_zen_defaults(self, monkeypatch):
        s = self._make_settings(monkeypatch)
        assert s.ZEN_API_KEY == ""
        assert s.ZEN_BASE_URL == "https://opencode.ai/zen/v1"
        assert s.ZEN_MODEL_SCORE == "deepseek-v4-flash-free"
        assert s.ZEN_MODEL_SUBMIT == "deepseek-v4-flash-free"
        assert s.ZEN_MODEL_CODE == "north-mini-code-free"

    def test_debug_default_false(self, monkeypatch):
        s = self._make_settings(monkeypatch)
        assert s.DEBUG is False

    def test_debug_true_from_env(self, monkeypatch):
        s = self._make_settings(monkeypatch, DEBUG="1")
        assert s.DEBUG is True

    def test_rpc_url_defaults(self, monkeypatch):
        s = self._make_settings(monkeypatch)
        assert "publicnode.com" in s.ETH_RPC_URL
        assert "publicnode.com" in s.BASE_RPC_URL
        assert "publicnode.com" in s.ARBITRUM_RPC_URL

    def test_reads_custom_env(self, monkeypatch):
        s = self._make_settings(monkeypatch, GITHUB_TOKEN="ghp_custom")
        assert s.GITHUB_TOKEN == "ghp_custom"

    def test_trade_keys_default_empty(self, monkeypatch):
        s = self._make_settings(monkeypatch)
        assert s.BINANCE_TRADE_KEY == ""


class TestEncryptSeed:
    """encrypt_seed — produces buck_enc: prefix with encrypted payload."""

    def test_returns_prefixed_string(self):
        from app.config import Settings

        s = Settings()
        with patch("app.config.encrypt_env", return_value="base64payload=="):
            result = s.encrypt_seed("mypassword")
            assert result == "buck_enc:base64payload=="

    def test_delegates_to_encrypt_env(self):
        from app.config import Settings

        s = Settings()
        with patch("app.config.encrypt_env") as mock_enc:
            s.encrypt_seed("testpass")
            mock_enc.assert_called_once_with(s.SEED_PHRASE, "testpass")


class TestValidate:
    """validate — returns list of missing critical settings."""

    def test_empty_seed_is_missing(self, monkeypatch):
        monkeypatch.setenv("SEED_PHRASE", "")
        monkeypatch.delenv("BUCK_ENC_PASSWORD", raising=False)
        from app.config import Settings

        s = Settings()
        missing = s.validate()
        assert "SEED_PHRASE" in missing

    def test_empty_eth_rpc_is_missing(self, monkeypatch):
        monkeypatch.setenv("ETH_RPC_URL", "")
        from app.config import Settings

        s = Settings()
        missing = s.validate()
        assert "ETH_RPC_URL" in missing

    def test_no_missing_when_configured(self, monkeypatch):
        monkeypatch.setenv("SEED_PHRASE", "my seed phrase")
        monkeypatch.setenv("ETH_RPC_URL", "https://eth.example.com")
        from app.config import Settings

        s = Settings()
        missing = s.validate()
        assert missing == []


class TestDecryptSeed:
    """Settings constructor decrypts buck_enc: prefixed seed."""

    def test_decrypts_encrypted_seed(self, monkeypatch):
        """When BUCK_ENC_PASSWORD is set and SEED_PHRASE has buck_enc: prefix."""
        monkeypatch.setenv("BUCK_ENC_PASSWORD", "secret")
        monkeypatch.setenv("SEED_PHRASE", "buck_enc:ciphertext==")
        with patch("app.config.decrypt_env", return_value="decrypted seed") as mock_dec:
            from app.config import Settings

            s = Settings()
            mock_dec.assert_called_once_with("ciphertext==", "secret")
            assert s.SEED_PHRASE == "decrypted seed"

    def test_plain_seed_not_decrypted(self, monkeypatch):
        """Without buck_enc: prefix, seed is used as-is even with password set."""
        monkeypatch.setenv("BUCK_ENC_PASSWORD", "secret")
        monkeypatch.setenv("SEED_PHRASE", "my plain seed")
        with patch("app.config.decrypt_env") as mock_dec:
            from app.config import Settings

            s = Settings()
            mock_dec.assert_not_called()
            assert s.SEED_PHRASE == "my plain seed"
