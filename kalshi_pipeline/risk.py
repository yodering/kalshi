from __future__ import annotations

from .config import Settings
from .models import SignalRecord


def kelly_fraction(model_prob: float, market_price_cents: int, side: str) -> float:
    p = max(0.0, min(1.0, model_prob))
    price = max(1, min(99, int(market_price_cents)))

    if side == "yes":
        win = 100 - price
        loss = price
        edge = p * win - (1 - p) * loss
    else:
        win = price
        loss = 100 - price
        edge = (1 - p) * win - p * loss

    if win <= 0 or edge <= 0:
        return 0.0
    return edge / win


def compute_order_size(
    *,
    signal: SignalRecord,
    side: str,
    market_price_cents: int,
    settings: Settings,
    current_exposure_dollars: float,
    bankroll_dollars: float | None,
    fill_probability: float | None = None,
) -> int:
    if signal.model_probability is None:
        return 0

    confidence = signal.confidence if signal.confidence is not None else 0.0
    confidence = max(0.0, min(1.0, confidence))
    if bankroll_dollars is None or bankroll_dollars <= 0:
        bankroll_dollars = settings.paper_trade_max_portfolio_exposure_dollars

    if settings.paper_trade_sizing_mode == "fixed":
        return settings.paper_trade_contract_count

    kelly = kelly_fraction(
        model_prob=signal.model_probability,
        market_price_cents=market_price_cents,
        side=side,
    )
    if kelly <= 0:
        return 0

    # Scale Kelly by empirical fill probability so thin books are sized down.
    fill_prob = settings.paper_trade_default_fill_probability
    if fill_probability is not None:
        fill_prob = fill_probability
    fill_prob = max(0.0, min(1.0, float(fill_prob)))
    kelly *= fill_prob
    if kelly <= 0:
        return 0

    target_dollars = bankroll_dollars * kelly * settings.kelly_fraction_scale * confidence
    target_dollars = min(target_dollars, settings.paper_trade_max_position_dollars)
    remaining_exposure = max(
        0.0, settings.paper_trade_max_portfolio_exposure_dollars - current_exposure_dollars
    )
    target_dollars = min(target_dollars, remaining_exposure)
    if target_dollars <= 0:
        return 0

    contract_cost = market_price_cents / 100.0
    if contract_cost <= 0:
        return 0
    contracts = int(target_dollars / contract_cost)
    return max(1, contracts) if contracts > 0 else 0
