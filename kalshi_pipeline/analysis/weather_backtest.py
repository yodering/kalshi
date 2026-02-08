from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
import math
from typing import Any

from ..config import Settings
from ..db import PostgresStore


def _clamp_prob(value: float) -> float:
    return max(1e-6, min(1 - 1e-6, float(value)))


def compute_brier_score(predictions: list[tuple[float, int]]) -> float | None:
    if not predictions:
        return None
    total = 0.0
    for probability, outcome in predictions:
        p = max(0.0, min(1.0, float(probability)))
        y = 1.0 if int(outcome) else 0.0
        total += (p - y) ** 2
    return total / float(len(predictions))


def _compute_log_loss(predictions: list[tuple[float, int]]) -> float | None:
    if not predictions:
        return None
    total = 0.0
    for probability, outcome in predictions:
        p = _clamp_prob(probability)
        y = 1 if int(outcome) else 0
        total += -math.log(p if y == 1 else (1.0 - p))
    return total / float(len(predictions))


def _calibration_table(
    predictions: list[tuple[float, int]],
    *,
    bins: int = 10,
) -> tuple[list[dict[str, Any]], float | None]:
    if not predictions:
        return [], None
    bucket_values: dict[int, list[tuple[float, int]]] = {idx: [] for idx in range(1, bins + 1)}
    for probability, outcome in predictions:
        p = max(0.0, min(1.0, float(probability)))
        bucket = min(bins, max(1, int(p * bins) + 1))
        bucket_values[bucket].append((p, int(outcome)))

    output: list[dict[str, Any]] = []
    max_error = 0.0
    has_error = False
    for bucket in range(1, bins + 1):
        rows = bucket_values[bucket]
        if not rows:
            continue
        avg_pred = sum(item[0] for item in rows) / float(len(rows))
        actual_rate = sum(item[1] for item in rows) / float(len(rows))
        error = abs(avg_pred - actual_rate)
        max_error = max(max_error, error)
        has_error = True
        output.append(
            {
                "bucket": bucket,
                "count": len(rows),
                "avg_predicted": round(avg_pred, 6),
                "actual_rate": round(actual_rate, 6),
                "abs_error": round(error, 6),
            }
        )
    return output, (max_error if has_error else None)


@dataclass(frozen=True)
class WeatherCalibrationReport:
    days: int
    n_brackets: int
    resolved_days: int
    model_brier: float | None
    market_brier: float | None
    brier_advantage: float | None
    model_log_loss: float | None
    market_log_loss: float | None
    edge_hit_rate: float | None
    edge_miss_rate: float | None
    sim_pnl_cents: float
    calibration_table: list[dict[str, Any]]
    max_calibration_error: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_weather_calibration(
    store: PostgresStore,
    *,
    days: int = 30,
    edge_threshold: float = 0.05,
) -> WeatherCalibrationReport:
    rows = store.get_weather_backtest_rows(days=days)
    if not rows:
        return WeatherCalibrationReport(
            days=days,
            n_brackets=0,
            resolved_days=0,
            model_brier=None,
            market_brier=None,
            brier_advantage=None,
            model_log_loss=None,
            market_log_loss=None,
            edge_hit_rate=None,
            edge_miss_rate=None,
            sim_pnl_cents=0.0,
            calibration_table=[],
            max_calibration_error=None,
        )

    model_predictions: list[tuple[float, int]] = []
    market_predictions: list[tuple[float, int]] = []
    edge_positive_total = 0
    edge_positive_hits = 0
    edge_positive_misses = 0
    sim_pnl_cents = 0.0
    resolved_dates: set[date] = set()

    for row in rows:
        model_prob = row.get("model_prob")
        market_prob = row.get("market_prob")
        outcome = int(row.get("actual_outcome") or 0)
        target_date = row.get("target_date")
        if isinstance(target_date, date):
            resolved_dates.add(target_date)
        if model_prob is None:
            continue

        model_predictions.append((float(model_prob), outcome))
        if market_prob is not None:
            market_predictions.append((float(market_prob), outcome))

        edge = row.get("edge")
        if edge is not None and float(edge) > 0:
            edge_positive_total += 1
            if outcome == 1:
                edge_positive_hits += 1
            else:
                edge_positive_misses += 1

        if edge is not None and market_prob is not None and float(edge) >= edge_threshold:
            price_cents = float(market_prob) * 100.0
            if outcome == 1:
                sim_pnl_cents += 100.0 - price_cents
            else:
                sim_pnl_cents -= price_cents

    model_brier = compute_brier_score(model_predictions)
    market_brier = compute_brier_score(market_predictions)
    model_log_loss = _compute_log_loss(model_predictions)
    market_log_loss = _compute_log_loss(market_predictions)
    calibration_table, max_calibration_error = _calibration_table(model_predictions)

    brier_advantage = None
    if model_brier is not None and market_brier is not None:
        brier_advantage = market_brier - model_brier

    edge_hit_rate = (
        (edge_positive_hits / float(edge_positive_total))
        if edge_positive_total > 0
        else None
    )
    edge_miss_rate = (
        (edge_positive_misses / float(edge_positive_total))
        if edge_positive_total > 0
        else None
    )

    return WeatherCalibrationReport(
        days=days,
        n_brackets=len(model_predictions),
        resolved_days=len(resolved_dates),
        model_brier=model_brier,
        market_brier=market_brier,
        brier_advantage=brier_advantage,
        model_log_loss=model_log_loss,
        market_log_loss=market_log_loss,
        edge_hit_rate=edge_hit_rate,
        edge_miss_rate=edge_miss_rate,
        sim_pnl_cents=round(sim_pnl_cents, 2),
        calibration_table=calibration_table,
        max_calibration_error=max_calibration_error,
    )


def check_weather_live_gates(
    report: WeatherCalibrationReport, settings: Settings
) -> dict[str, bool]:
    brier_advantage = report.brier_advantage if report.brier_advantage is not None else -1.0
    max_calibration_error = (
        report.max_calibration_error
        if report.max_calibration_error is not None
        else float("inf")
    )
    return {
        "min_resolved_days": report.resolved_days >= settings.weather_live_gate_min_resolved_days,
        "min_brier_advantage": brier_advantage >= settings.weather_live_gate_min_brier_advantage,
        "min_sim_profit_cents": report.sim_pnl_cents >= settings.weather_live_gate_min_sim_profit_cents,
        "max_calibration_error": max_calibration_error <= settings.weather_live_gate_max_calibration_error,
    }
