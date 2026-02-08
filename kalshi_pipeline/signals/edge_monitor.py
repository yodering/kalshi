from __future__ import annotations

def build_edge_decay_alerts(
    *,
    open_positions: list[dict[str, object]],
    current_signals: list[dict[str, object]],
    edge_decay_alert_threshold_bps: int,
) -> list[str]:
    signal_by_ticker = {
        str(row.get("market_ticker")): row
        for row in current_signals
        if row.get("market_ticker") is not None
    }
    alerts: list[str] = []
    for position in open_positions:
        ticker = str(position.get("market_ticker"))
        side = str(position.get("side", "")).lower()
        current_signal = signal_by_ticker.get(ticker)
        if current_signal is None:
            alerts.append(
                f"⚠️ No current signal for {ticker} while a {side.upper()} position is open."
            )
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
