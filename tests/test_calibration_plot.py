from __future__ import annotations

import unittest

from kalshi_pipeline.analysis.calibration_plot import generate_calibration_data


class CalibrationPlotTests(unittest.TestCase):
    def test_generate_calibration_data(self) -> None:
        rows = generate_calibration_data(
            predictions=[(0.1, 0), (0.2, 0), (0.8, 1), (0.9, 1)],
            n_bins=5,
        )
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(sum(int(row["count"]) for row in rows), 4)

    def test_generate_calibration_data_empty(self) -> None:
        rows = generate_calibration_data(predictions=[], n_bins=10)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
