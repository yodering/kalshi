from __future__ import annotations

import unittest

from kalshi_pipeline.signals.edge_monitor import build_edge_decay_alerts


class EdgeMonitorTests(unittest.TestCase):
    def test_hedged_position_suppresses_decay_alerts(self) -> None:
        open_positions = [
            {"market_ticker": "KXBTC15M-TEST", "side": "yes"},
            {"market_ticker": "KXBTC15M-TEST", "side": "no"},
        ]
        signals = [{"market_ticker": "KXBTC15M-TEST", "direction": "buy_yes", "edge_bps": 20}]
        alerts = build_edge_decay_alerts(
            open_positions=open_positions,
            current_signals=signals,
            edge_decay_alert_threshold_bps=75,
            active_market_tickers={"KXBTC15M-TEST"},
        )
        self.assertEqual(alerts, [])

    def test_no_signal_alert_skips_stale_ticker(self) -> None:
        open_positions = [{"market_ticker": "KXBTC15M-OLD", "side": "no"}]
        alerts = build_edge_decay_alerts(
            open_positions=open_positions,
            current_signals=[],
            edge_decay_alert_threshold_bps=75,
            active_market_tickers={"KXBTC15M-LIVE"},
        )
        self.assertEqual(alerts, [])

    def test_signal_flip_generates_alert(self) -> None:
        open_positions = [{"market_ticker": "KXHIGHNY-TEST", "side": "yes"}]
        signals = [{"market_ticker": "KXHIGHNY-TEST", "direction": "buy_no", "edge_bps": -300}]
        alerts = build_edge_decay_alerts(
            open_positions=open_positions,
            current_signals=signals,
            edge_decay_alert_threshold_bps=75,
            active_market_tickers={"KXHIGHNY-TEST"},
        )
        self.assertEqual(len(alerts), 1)
        self.assertIn("Signal flipped", alerts[0])


if __name__ == "__main__":
    unittest.main()
