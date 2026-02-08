from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

import requests

from .config import Settings
from .models import AlertEvent, PaperTradeOrder, SignalRecord

logger = logging.getLogger(__name__)


def _is_actionable(signal: SignalRecord) -> bool:
    return signal.direction in {"buy_yes", "buy_no"} and signal.market_ticker is not None


def _signal_sort_key(signal: SignalRecord) -> float:
    return abs(signal.edge_bps or 0.0)


def _format_edge_bps(value: float | None) -> str:
    edge = value or 0.0
    sign = "+" if edge > 0 else ""
    return f"{sign}{round(edge, 2)} bps"


def _format_prob(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{round(value * 100, 1)}%"


def _signal_icon(signal_type: str) -> str:
    signal_type_norm = signal_type.lower()
    if signal_type_norm == "weather":
        return "🌤️"
    if signal_type_norm == "btc":
        return "₿"
    return "📊"


def _direction_label(direction: str) -> str:
    direction_norm = direction.lower()
    if direction_norm == "buy_yes":
        return "🟢 BUY YES"
    if direction_norm == "buy_no":
        return "🔴 BUY NO"
    return direction.replace("_", " ").upper()


def _order_status_icon(status: str) -> str:
    status_norm = status.lower()
    if status_norm == "submitted":
        return "✅"
    if status_norm == "simulated":
        return "🧪"
    if status_norm == "failed":
        return "❌"
    return "ℹ️"


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session = requests.Session()

    def is_enabled(self) -> bool:
        return (
            self.settings.telegram_enabled
            and bool(self.settings.telegram_bot_token)
            and bool(self.settings.telegram_chat_id)
        )

    def notify(
        self,
        now_utc: datetime,
        signals: list[SignalRecord],
        paper_orders: list[PaperTradeOrder],
    ) -> list[AlertEvent]:
        if not self.is_enabled():
            return []
        events: list[AlertEvent] = []
        signal_event = self._send_signal_digest(now_utc, signals)
        if signal_event is not None:
            events.append(signal_event)
        if self.settings.telegram_notify_execution_events and paper_orders:
            order_event = self._send_paper_execution_digest(now_utc, paper_orders)
            if order_event is not None:
                events.append(order_event)
        return events

    def _send_signal_digest(
        self, now_utc: datetime, signals: list[SignalRecord]
    ) -> AlertEvent | None:
        selected = list(signals)
        if self.settings.telegram_notify_actionable_only:
            selected = [
                signal
                for signal in selected
                if _is_actionable(signal)
                and abs(signal.edge_bps or 0.0) >= self.settings.telegram_min_edge_bps
            ]
        if not selected:
            return None
        selected.sort(key=_signal_sort_key, reverse=True)
        top_signals = selected[:5]
        lines = [
            "🧠 Kalshi Bot Signal Digest",
            f"🕒 {now_utc.isoformat()}",
            f"📊 Total={len(signals)} | Sent={len(selected)} | MinEdge={self.settings.telegram_min_edge_bps} bps",
            "",
        ]
        for idx, signal in enumerate(top_signals, start=1):
            icon = _signal_icon(signal.signal_type)
            direction = _direction_label(signal.direction)
            lines.append(
                (
                    f"{idx}) {icon} {signal.signal_type.upper()} • {signal.market_ticker}\n"
                    f"   {direction} | edge={_format_edge_bps(signal.edge_bps)}\n"
                    f"   🤖 model={_format_prob(signal.model_probability)} | 🏛️ market={_format_prob(signal.market_probability)}"
                )
            )
        message = "\n".join(lines)
        status, metadata = self._send_message(message)
        return AlertEvent(
            channel="telegram",
            event_type="signal_digest",
            market_ticker=None,
            message=message,
            status=status,
            metadata=metadata,
            created_at=now_utc,
        )

    def _send_paper_execution_digest(
        self, now_utc: datetime, orders: list[PaperTradeOrder]
    ) -> AlertEvent | None:
        if not orders:
            return None
        submitted = sum(1 for order in orders if order.status == "submitted")
        simulated = sum(1 for order in orders if order.status == "simulated")
        failed = sum(1 for order in orders if order.status == "failed")
        lines = [
            "🤖 Kalshi Bot Paper Executions",
            f"🕒 {now_utc.isoformat()}",
            f"📦 Orders={len(orders)} | ✅ Submitted={submitted} | 🧪 Simulated={simulated} | ❌ Failed={failed}",
            "",
        ]
        for idx, order in enumerate(orders[:5], start=1):
            reason_suffix = ""
            if order.status == "failed" and order.reason:
                reason_text = order.reason.replace("\n", " ").strip()
                if len(reason_text) > 140:
                    reason_text = f"{reason_text[:137]}..."
                reason_suffix = f"\n   ⚠️ reason={reason_text}"
            direction = _direction_label(order.direction)
            status_icon = _order_status_icon(order.status)
            lines.append(
                (
                    f"{idx}) {status_icon} {order.market_ticker}\n"
                    f"   {direction} | side={order.side.upper()} | qty={order.count} | px={order.limit_price_cents}c | status={order.status.upper()}"
                    f"{reason_suffix}"
                )
            )
        message = "\n".join(lines)
        status, metadata = self._send_message(message)
        return AlertEvent(
            channel="telegram",
            event_type="paper_execution_digest",
            market_ticker=None,
            message=message,
            status=status,
            metadata=metadata,
            created_at=now_utc,
        )

    def _send_message(self, message: str) -> tuple[str, dict[str, Any]]:
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.settings.telegram_chat_id, "text": message}
        try:
            response = self._session.post(url, json=payload, timeout=15)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                return "sent", {"note": "non_dict_response"}
            return "sent", {"ok": bool(body.get("ok", True))}
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            logger.warning("telegram_send_failed status=%s", status)
            return "failed", {"error": f"http_{status}"}
        except requests.RequestException:
            logger.warning("telegram_send_failed", exc_info=True)
            return "failed", {"error": "request_exception"}
