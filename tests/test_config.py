import pytest

from dex_perp_bot.config import Settings


def test_settings_from_env(monkeypatch):
    env = {
        "HYPERLIQUID_API_KEY": "hk",
        "ASTER_API_KEY": "ak",
        "ASTER_API_SECRET": "as",
        "ASTER_ACCOUNT_ID": "acct-1",
        "ASTER_BASE_URL": "https://api.aster.test",
        "ASTER_BALANCE_ENDPOINT": "/balance",
        "ASTER_RESPONSE_PATH": "data.summary",
        "ASTER_AVAILABLE_FIELDS": "availableBalance,freeCollateral",
        "ASTER_TOTAL_FIELDS": "totalCollateral",
        "ASTER_TIMEOUT": "5",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    settings = Settings.from_env(load_env_file=False)

    assert settings.hyperliquid.api_key == "hk"
    assert settings.aster.base_url == "https://api.aster.test"
    assert settings.aster.balance_endpoint == "/balance"
    assert settings.aster.response_path == ("data", "summary")
    assert settings.aster.available_fields == ("availableBalance", "freeCollateral")
    assert settings.aster.total_fields == ("totalCollateral",)
    assert settings.aster.request_timeout == pytest.approx(5.0)


def test_settings_missing_env(monkeypatch):
    monkeypatch.delenv("HYPERLIQUID_API_KEY", raising=False)
    monkeypatch.setenv("ASTER_API_KEY", "ak")
    monkeypatch.setenv("ASTER_API_SECRET", "as")
    monkeypatch.setenv("ASTER_ACCOUNT_ID", "acct-1")
    monkeypatch.setenv("ASTER_BASE_URL", "https://api.aster.test")

    with pytest.raises(ValueError):
        Settings.from_env(load_env_file=False)

