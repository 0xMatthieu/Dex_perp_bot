import unittest
from unittest.mock import MagicMock, patch

import funding


class FundingQueryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()

    def test_hyperliquid_parses_latest_entry(self) -> None:
        response = MagicMock()
        response.json.return_value = [
            {"fundingRate": "0.0001", "time": 1700000000000},
            {"fundingRate": "0.0003", "time": 1700003600000},
        ]
        response.raise_for_status.return_value = None
        self.session.post.return_value = response

        rate = funding.fetch_hyperliquid_funding("ETH-PERP", session=self.session)

        self.assertAlmostEqual(rate.rate, 0.0003)
        self.assertEqual(rate.exchange, "hyperliquid")
        self.assertEqual(rate.market, "ETH-PERP")
        self.assertIsNotNone(rate.timestamp)

    def test_aster_parses_nested_entries(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "data": {
                "entries": [
                    {"funding_rate": "0.00001", "timestamp": 1700000000000},
                    {"fundingRate": 0.00002, "timestamp": 1700007200000},
                ]
            }
        }
        response.raise_for_status.return_value = None
        self.session.get.return_value = response

        rate = funding.fetch_aster_funding("ETH-PERP", session=self.session)

        self.assertAlmostEqual(rate.rate, 0.00002)
        self.assertEqual(rate.exchange, "aster")
        self.assertEqual(rate.market, "ETH-PERP")
        self.assertIsNotNone(rate.timestamp)

    def test_query_funding_once_invokes_both_clients(self) -> None:
        hyper_rate = funding.FundingRate("hyperliquid", "ETH-PERP", 0.01, None, {"fundingRate": 0.01})
        aster_rate = funding.FundingRate("aster", "ETH-PERP", -0.02, None, {"fundingRate": -0.02})

        with patch.object(
            funding, "fetch_hyperliquid_funding", return_value=hyper_rate
        ) as hyper_mock, patch.object(
            funding, "fetch_aster_funding", return_value=aster_rate
        ) as aster_mock:
            rates = funding.query_funding_once(
                hyperliquid_market="ETH-PERP",
                aster_market="ETH-PERP",
            )

        self.assertEqual(rates["hyperliquid"], hyper_rate)
        self.assertEqual(rates["aster"], aster_rate)
        hyper_mock.assert_called_once()
        aster_mock.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
