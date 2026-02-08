from __future__ import annotations

from typing import Any


def normalize_levels(levels: list[Any]) -> list[tuple[int, int]]:
    normalized: list[tuple[int, int]] = []
    for level in levels:
        price: int | None = None
        qty: int | None = None
        if isinstance(level, dict):
            raw_price = level.get("price")
            raw_qty = (
                level.get("quantity")
                if level.get("quantity") is not None
                else level.get("qty")
            )
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            raw_price = level[0]
            raw_qty = level[1]
        else:
            continue
        try:
            price = int(raw_price)
            qty = int(raw_qty)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        normalized.append((price, qty))
    return normalized


def compute_vwap(
    price_levels: list[tuple[int, int]],
    target_qty: int,
    *,
    ascending: bool = True,
) -> tuple[float, int] | None:
    if target_qty <= 0:
        return None
    sorted_levels = sorted(price_levels, key=lambda row: row[0], reverse=not ascending)
    filled = 0
    total_cost = 0
    for price_cents, qty in sorted_levels:
        if qty <= 0:
            continue
        remaining = target_qty - filled
        if remaining <= 0:
            break
        fill_qty = min(qty, remaining)
        total_cost += price_cents * fill_qty
        filled += fill_qty
    if filled <= 0:
        return None
    return (total_cost / filled, filled)


def effective_yes_ask_vwap(orderbook: dict[str, Any], qty: int) -> tuple[float, int] | None:
    no_levels = normalize_levels(list(orderbook.get("no") or []))
    if not no_levels:
        return None
    yes_asks = [(100 - price, depth) for price, depth in no_levels]
    return compute_vwap(yes_asks, qty, ascending=True)


def effective_no_ask_vwap(orderbook: dict[str, Any], qty: int) -> tuple[float, int] | None:
    yes_levels = normalize_levels(list(orderbook.get("yes") or []))
    if not yes_levels:
        return None
    no_asks = [(100 - price, depth) for price, depth in yes_levels]
    return compute_vwap(no_asks, qty, ascending=True)
