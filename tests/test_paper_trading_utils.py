from __future__ import annotations

import unittest

from kalshi_pipeline.order_utils import extract_queue_positions, normalize_order_status


class PaperTradingUtilsTests(unittest.TestCase):
    def test_normalize_order_status_partially_filled(self) -> None:
        self.assertEqual(normalize_order_status("partially_filled"), "partially_filled")
        self.assertEqual(normalize_order_status("partially-filled"), "partially_filled")
        self.assertEqual(normalize_order_status("resting"), "submitted")
        self.assertEqual(normalize_order_status("filled"), "filled")

    def test_extract_queue_positions_from_nested_payload(self) -> None:
        payload = {
            "queue_positions": [
                {"order_id": "abc", "queue_position": 12},
                {
                    "market_ticker": "KXBTC15M-TEST",
                    "position": 9,
                },
            ]
        }
        result = extract_queue_positions(payload)
        self.assertEqual(result.get("abc"), 12)
        self.assertEqual(result.get("KXBTC15M-TEST"), 9)


if __name__ == "__main__":
    unittest.main()
