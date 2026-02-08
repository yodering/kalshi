from __future__ import annotations

from datetime import date, datetime
import unittest

from kalshi_pipeline.collectors.weather import _extract_daily_max, _measurement_window


class WeatherWindowTests(unittest.TestCase):
    def test_measurement_window_dst_starts_at_one_am(self) -> None:
        target_date = date(2026, 7, 8)
        start, end = _measurement_window(target_date, "America/New_York")
        self.assertEqual(start.hour, 1)
        self.assertEqual(end.hour, 1)

    def test_measurement_window_standard_starts_at_midnight(self) -> None:
        target_date = date(2026, 2, 8)
        start, end = _measurement_window(target_date, "America/New_York")
        self.assertEqual(start.hour, 0)
        self.assertEqual(end.hour, 0)

    def test_extract_daily_max_respects_dst_window(self) -> None:
        target_date = date(2026, 7, 8)
        times = [
            "2026-07-08T00:00",
            "2026-07-08T01:00",
            "2026-07-08T12:00",
        ]
        temps = [99.0, 80.0, 85.0]
        # 00:00 should be excluded in DST window, so max should be 85.
        max_temp = _extract_daily_max(
            hourly_values=temps,
            hourly_times=times,
            target_date=target_date,
            tz_name="America/New_York",
        )
        self.assertEqual(max_temp, 85.0)


if __name__ == "__main__":
    unittest.main()
