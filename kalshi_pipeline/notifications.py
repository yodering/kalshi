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
            f"Kalshi bot signal update ({now_utc.isoformat()})",
            f"signals={len(signals)} selected={len(selected)}",
        ]
        for signal in top_signals:
            lines.append(
                f"{signal.signal_type}:{signal.market_ticker} {signal.direction} edge_bps={round(signal.edge_bps or 0.0, 2)}"
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
            f"Kalshi bot paper executions ({now_utc.isoformat()})",
            f"orders={len(orders)} submitted={submitted} simulated={simulated} failed={failed}",
        ]
        for order in orders[:5]:
            lines.append(
                f"{order.market_ticker} {order.direction} side={order.side} count={order.count} price={order.limit_price_cents} status={order.status}"
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
