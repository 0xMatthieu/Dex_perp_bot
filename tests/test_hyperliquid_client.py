from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from dex_perp_bot.config import HyperliquidCredentials
from dex_perp_bot.exchanges.base import BalanceParsingError
from dex_perp_bot.exchanges.hyperliquid import HyperliquidClient


@pytest.fixture
def credentials() -> HyperliquidCredentials:
    return HyperliquidCredentials(api_key="key")


def test_hyperliquid_balance_parsing(credentials):
    fake_balance = {
        "USDC": {"total": "100.5", "free": "40.25"},
        "info": {"withdrawable": "40.25"},
    }

    with patch("dex_perp_bot.exchanges.hyperliquid.HyperliquidSync") as sync_cls:
        instance = MagicMock()
        instance.fetch_balance.return_value = fake_balance
        sync_cls.return_value = instance

        client = HyperliquidClient(credentials)
        balance = client.get_wallet_balance()

    assert balance.total == Decimal("100.5")
    assert balance.available == Decimal("40.25")
    assert balance.raw == fake_balance


def test_hyperliquid_missing_fields(credentials):
    fake_balance = {"info": {}}

    with patch("dex_perp_bot.exchanges.hyperliquid.HyperliquidSync") as sync_cls:
        instance = MagicMock()
        instance.fetch_balance.return_value = fake_balance
        sync_cls.return_value = instance

        client = HyperliquidClient(credentials)
        with pytest.raises(BalanceParsingError):
            client.get_wallet_balance()

