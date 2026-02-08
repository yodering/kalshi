from __future__ import annotations

def build_edge_decay_alerts(
    *,
    open_positions: list[dict[str, object]],
    current_signals: list[dict[str, object]],
    edge_decay_alert_threshold_bps: int,
    active_market_tickers: set[str] | None = None,
) -> list[str]:
    signal_by_ticker = {
        str(row.get("market_ticker")): row
        for row in current_signals
        if row.get("market_ticker") is not None
    }
    sides_by_ticker: dict[str, set[str]] = {}
    for position in open_positions:
        ticker = str(position.get("market_ticker"))
        side = str(position.get("side", "")).lower()
        if not ticker:
            continue
        if side not in {"yes", "no"}:
            continue
        sides_by_ticker.setdefault(ticker, set()).add(side)
    hedged_tickers = {ticker for ticker, sides in sides_by_ticker.items() if len(sides) > 1}

    alerts: list[str] = []
    no_signal_notified: set[str] = set()
    for position in open_positions:
        ticker = str(position.get("market_ticker"))
        side = str(position.get("side", "")).lower()
        if not ticker or side not in {"yes", "no"}:
            continue
        # If both YES and NO are open, this is likely an arb/boxed position.
        # Directional edge-decay alerts are not actionable for that state.
        if ticker in hedged_tickers:
            continue
        current_signal = signal_by_ticker.get(ticker)
        if current_signal is None:
            if active_market_tickers is not None and ticker not in active_market_tickers:
                continue
            if ticker in no_signal_notified:
                continue
            alerts.append(
                f"⚠️ No current signal for {ticker} while a position is open."
            )
            no_signal_notified.add(ticker)
            continue

        direction = str(current_signal.get("direction", "flat"))
        edge_bps_value = current_signal.get("edge_bps")
        edge_bps = float(edge_bps_value) if edge_bps_value is not None else 0.0
        expected_direction = "buy_yes" if side == "yes" else "buy_no"
        if direction in {"buy_yes", "buy_no"} and direction != expected_direction:
            alerts.append(
                f"🔴 Signal flipped on {ticker}: open side={side.upper()} current={direction} edge={round(edge_bps, 2)} bps"
            )
            continue

        if abs(edge_bps) < edge_decay_alert_threshold_bps:
            alerts.append(
                f"⚠️ Edge decayed on {ticker}: current edge={round(edge_bps, 2)} bps (< {edge_decay_alert_threshold_bps} bps)"
            )
    return alerts
