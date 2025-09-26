from decimal import Decimal

import pytest
import responses

from dex_perp_bot.config import AsterConfig, AsterCredentials
from dex_perp_bot.exchanges.aster import AsterClient
from dex_perp_bot.exchanges.base import BalanceParsingError


@pytest.fixture
def credentials() -> AsterCredentials:
    return AsterCredentials(
        api_key="key",
        api_secret="secret",
    )


@pytest.fixture
def config() -> AsterConfig:
    return AsterConfig(
        account_id="acct-1",
        base_url="https://api.aster.test",
        balance_endpoint="/account-summary",
        response_path=("data", "account"),
        available_fields=("availableBalance", "freeCollateral"),
        total_fields=("totalCollateral",),
        request_timeout=5.0,
    )


@responses.activate
def test_aster_balance_parsing(credentials, config):
    responses.add(
        responses.POST,
        "https://api.aster.test/account-summary",
        json={
            "data": {
                "account": {
                    "availableBalance": "50.5",
                    "totalCollateral": "200.75",
                }
            }
        },
        status=200,
    )

    client = AsterClient(credentials, config)
    balance = client.get_wallet_balance()

    assert balance.available == Decimal("50.5")
    assert balance.total == Decimal("200.75")

    req = responses.calls[0].request
    assert req.headers["X-API-KEY"] == "key"
    assert req.headers["X-TIMESTAMP"]
    assert req.headers["X-SIGNATURE"]


@responses.activate
def test_aster_missing_fields(credentials, config):
    responses.add(
        responses.POST,
        "https://api.aster.test/account-summary",
        json={"data": {"account": {"other": "1"}}},
        status=200,
    )

    client = AsterClient(credentials, config)

    with pytest.raises(BalanceParsingError):
        client.get_wallet_balance()

