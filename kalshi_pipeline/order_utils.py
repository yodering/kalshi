from __future__ import annotations

from typing import Any


def as_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_order_status(raw_status: object) -> str:
    status = str(raw_status or "").strip().lower()
    if not status:
        return "submitted"
    if status in {"resting", "open", "pending", "submitted"}:
        return "submitted"
    if status in {"partially_filled", "partially-filled"}:
        return "partially_filled"
    if status in {"filled", "executed", "complete", "completed", "matched"}:
        return "filled"
    if status in {"canceled", "cancelled", "expired", "voided"}:
        return "canceled"
    if status in {"failed", "rejected", "error"}:
        return "failed"
    return status


def extract_order_status(payload: dict[str, Any]) -> str:
    candidates = [
        payload.get("status"),
        payload.get("order_status"),
    ]
    order_obj = payload.get("order")
    if isinstance(order_obj, dict):
        candidates.extend([order_obj.get("status"), order_obj.get("order_status")])
    for candidate in candidates:
        normalized = normalize_order_status(candidate)
        if normalized:
            return normalized
    return "submitted"


def extract_queue_positions(payload: dict[str, Any]) -> dict[str, int]:
    mapping: dict[str, int] = {}

    def record(key: object, value: object) -> None:
        if key is None:
            return
        position = as_int(value)
        if position is None:
            return
        text = str(key).strip()
        if not text:
            return
        mapping[text] = position

    def visit_node(node: object, parent_key: str | None = None) -> None:
        if isinstance(node, dict):
            queue_position = node.get("queue_position")
            if queue_position is None:
                queue_position = node.get("position")
            if queue_position is not None:
                aliases = [
                    parent_key,
                    node.get("order_id"),
                    node.get("external_order_id"),
                    node.get("market_ticker"),
                    node.get("ticker"),
                ]
                for alias in aliases:
                    record(alias, queue_position)
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    visit_node(value, str(key))
                elif key in {"queue_position", "position"}:
                    record(parent_key or key, value)
        elif isinstance(node, list):
            for item in node:
                visit_node(item, parent_key)

    root = payload.get("queue_positions") if isinstance(payload, dict) else None
    if root is None:
        root = payload
    visit_node(root)
    return mapping
