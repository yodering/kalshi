from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from ..db import PostgresStore


@dataclass(frozen=True)
class AccuracyReport:
    market_type: str
    days: int
    n_signals: int
    brier_score: float | None
    market_brier_score: float | None
    log_loss: float | None
    edge_reliability: float | None
    hit_rate: float | None
    avg_pnl_per_contract: float | None
    total_pnl: float | None
    sharpe_ratio: float | None
    calibration_curve: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _signal_type_for_market_type(market_type: str) -> str | None:
    normalized = market_type.strip().lower()
    if normalized in {"all", "*", ""}:
        return None
    if normalized in {"weather", "kxhighny"}:
        return "weather"
    if normalized in {"btc", "btc_15m", "kxbtc15m"}:
        return "btc"
    return None


def _compute_sharpe(avg_pnl: float | None, n_signals: int) -> float | None:
    if avg_pnl is None or n_signals <= 1:
        return None
    # We only have aggregate stats in-db. Use a conservative proxy with sqrt(n).
    scale = math.sqrt(float(n_signals))
    return round((avg_pnl / 100.0) * scale, 4)


def generate_accuracy_report(
    store: PostgresStore, market_type: str = "all", days: int = 30
) -> AccuracyReport:
    signal_type = _signal_type_for_market_type(market_type)
    metrics = store.get_accuracy_metrics(days=days, signal_type=signal_type)
    curve = store.get_calibration_curve(days=days, signal_type=signal_type)
    n_signals = int(metrics.get("n_signals") or 0)
    avg_pnl = metrics.get("avg_pnl_per_contract")
    sharpe_ratio = _compute_sharpe(
        float(avg_pnl) if avg_pnl is not None else None,
        n_signals,
    )
    return AccuracyReport(
        market_type=market_type,
        days=days,
        n_signals=n_signals,
        brier_score=metrics.get("brier_score"),  # type: ignore[arg-type]
        market_brier_score=metrics.get("market_brier_score"),  # type: ignore[arg-type]
        log_loss=metrics.get("log_loss"),  # type: ignore[arg-type]
        edge_reliability=metrics.get("edge_reliability"),  # type: ignore[arg-type]
        hit_rate=metrics.get("hit_rate"),  # type: ignore[arg-type]
        avg_pnl_per_contract=avg_pnl,  # type: ignore[arg-type]
        total_pnl=metrics.get("total_pnl"),  # type: ignore[arg-type]
        sharpe_ratio=sharpe_ratio,
        calibration_curve=curve,
    )
