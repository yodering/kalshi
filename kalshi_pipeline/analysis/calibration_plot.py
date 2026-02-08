from __future__ import annotations

from typing import Any


def generate_calibration_data(
    predictions: list[tuple[float, int]], n_bins: int = 10
) -> list[dict[str, Any]]:
    if n_bins <= 0:
        return []
    bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for probability, outcome in predictions:
        p = max(0.0, min(1.0, float(probability)))
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((p, int(outcome)))

    results: list[dict[str, Any]] = []
    for idx, bucket in enumerate(bins):
        if not bucket:
            continue
        probs, outcomes = zip(*bucket)
        predicted_avg = sum(probs) / float(len(probs))
        actual_freq = sum(outcomes) / float(len(outcomes))
        results.append(
            {
                "bin_center": round((idx + 0.5) / float(n_bins), 6),
                "predicted_avg": round(predicted_avg, 6),
                "actual_freq": round(actual_freq, 6),
                "count": len(bucket),
            }
        )
    return results
