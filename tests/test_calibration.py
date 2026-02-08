from __future__ import annotations

from datetime import date
import sys
import types
import unittest

# weather_backtest imports db -> psycopg at import time; stub it for unit tests.
if "psycopg" not in sys.modules:
    psycopg_stub = types.ModuleType("psycopg")
    psycopg_stub.OperationalError = Exception
    psycopg_stub.connect = lambda *args, **kwargs: None
    psycopg_stub.types = types.SimpleNamespace(
        json=types.SimpleNamespace(Jsonb=lambda value: value)
    )
    sys.modules["psycopg"] = psycopg_stub

from kalshi_pipeline.analysis.weather_backtest import generate_weather_calibration


class _FakeStore:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def get_weather_backtest_rows(self, *, days: int) -> list[dict[str, object]]:
        return list(self._rows)


def _row(
    *,
    d: date,
    ticker: str,
    model_prob: float,
    market_prob: float,
    outcome: int,
) -> dict[str, object]:
    return {
        "target_date": d,
        "ticker": ticker,
        "model_prob": model_prob,
        "market_prob": market_prob,
        "edge": model_prob - market_prob,
        "actual_outcome": outcome,
    }


class CalibrationTests(unittest.TestCase):
    def test_brier_score_perfect_predictions(self) -> None:
        rows = [
            _row(d=date(2026, 2, 1), ticker="A", model_prob=1.0, market_prob=0.5, outcome=1),
            _row(d=date(2026, 2, 1), ticker="B", model_prob=0.0, market_prob=0.5, outcome=0),
        ]
        report = generate_weather_calibration(_FakeStore(rows), days=30)
        self.assertEqual(report.model_brier, 0.0)

    def test_brier_score_worst_predictions(self) -> None:
        rows = [
            _row(d=date(2026, 2, 2), ticker="A", model_prob=1.0, market_prob=0.5, outcome=0),
            _row(d=date(2026, 2, 2), ticker="B", model_prob=0.0, market_prob=0.5, outcome=1),
        ]
        report = generate_weather_calibration(_FakeStore(rows), days=30)
        self.assertEqual(report.model_brier, 1.0)

    def test_calibration_bins_sum_correctly(self) -> None:
        rows = [
            _row(d=date(2026, 2, 3), ticker=f"T{i}", model_prob=i / 10.0, market_prob=0.5, outcome=i % 2)
            for i in range(1, 9)
        ]
        report = generate_weather_calibration(_FakeStore(rows), days=30)
        self.assertEqual(report.n_brackets, 8)
        self.assertEqual(sum(item["count"] for item in report.calibration_table), 8)

    def test_empty_predictions_handled(self) -> None:
        report = generate_weather_calibration(_FakeStore([]), days=30)
        self.assertEqual(report.n_brackets, 0)
        self.assertIsNone(report.model_brier)
        self.assertEqual(report.calibration_table, [])


if __name__ == "__main__":
    unittest.main()
