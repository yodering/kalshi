from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING, Any

import requests

from .analysis.accuracy_report import generate_accuracy_report
from .config import Settings
from .models import AlertEvent, PaperTradeOrder, SignalRecord

if TYPE_CHECKING:
    from .pipeline import DataPipeline

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


def _format_pct(value: float | None) -> str:
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
        self._updates_offset = 0

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

    def poll_commands(self, pipeline: "DataPipeline") -> list[AlertEvent]:
        if not self.is_enabled():
            return []
        updates = self._fetch_updates()
        if not updates:
            return []
        events: list[AlertEvent] = []
        configured_chat_id = str(self.settings.telegram_chat_id).strip()
        for update in updates:
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            if not isinstance(chat, dict):
                continue
            chat_id = str(chat.get("id"))
            if configured_chat_id and chat_id != configured_chat_id:
                continue
            text = str(message.get("text", "")).strip()
            if not text:
                continue
            response_text = self._handle_command(text, pipeline)
            if response_text is None:
                continue
            status, metadata = self._send_message(response_text)
            events.append(
                AlertEvent(
                    channel="telegram",
                    event_type="telegram_command",
                    market_ticker=None,
                    message=response_text,
                    status=status,
                    metadata={"request_text": text, **metadata},
                    created_at=datetime.now(timezone.utc),
                )
            )
        return events

    def _handle_command(self, text: str, pipeline: "DataPipeline") -> str | None:
        normalized = text.strip()
        lower = normalized.lower()
        if lower == "confirm live":
            return pipeline.confirm_live_mode()

        if lower == "/status":
            status = pipeline.get_runtime_status()
            return (
                "📡 Bot Status\n"
                f"mode={status['mode']}\n"
                f"paused={status['paused']}\n"
                f"last_poll={status.get('last_poll_at')}\n"
                f"last_metrics={status.get('last_metrics')}"
            )

        if lower == "/pause":
            pipeline.set_paused(True)
            return "⏸️ Trading paused. Data collection is still running."

        if lower == "/resume":
            pipeline.set_paused(False)
            return "▶️ Trading resumed."

        if lower.startswith("/mode"):
            parts = normalized.split()
            if len(parts) == 1:
                return f"Current mode: {pipeline.runtime_mode}"
            requested_mode = parts[1].strip().lower()
            return pipeline.request_mode_change(requested_mode)

        if lower == "/positions":
            positions = pipeline.store.get_open_positions_summary()
            if not positions:
                return "No open submitted positions."
            lines = ["📦 Open Positions"]
            for row in positions[:10]:
                lines.append(
                    f"{row['market_ticker']} side={str(row['side']).upper()} contracts={row['contracts']} avg={round(float(row['avg_price_cents']), 2)}c"
                )
            return "\n".join(lines)

        if lower == "/orders":
            orders = pipeline.store.get_recent_paper_orders(limit=10)
            if not orders:
                return "No recent paper orders."
            lines = ["🧾 Recent Orders"]
            for row in orders[:10]:
                lines.append(
                    f"{row['created_at']} {row['market_ticker']} {str(row['side']).upper()} x{row['count']} @ {row['limit_price_cents']}c -> {row['status']}"
                )
            return "\n".join(lines)

        if lower == "/signals":
            rows = pipeline.store.get_recent_signals(limit=10)
            if not rows:
                return "No recent signals."
            lines = ["🧠 Recent Signals"]
            for row in rows[:10]:
                lines.append(
                    f"{row['created_at']} {row['signal_type']} {row['market_ticker']} {row['direction']} edge={round(float(row['edge_bps'] or 0.0), 2)}bps conf={round(float(row['confidence'] or 0.0), 3)}"
                )
            return "\n".join(lines)

        if lower.startswith("/accuracy"):
            parts = normalized.split()
            days = 30
            if len(parts) >= 2:
                try:
                    days = max(1, int(parts[1]))
                except ValueError:
                    days = 30
            report = generate_accuracy_report(pipeline.store, market_type="all", days=days)
            return (
                f"📈 Accuracy ({days}d)\n"
                f"n_signals={report.n_signals}\n"
                f"brier={report.brier_score}\n"
                f"market_brier={report.market_brier_score}\n"
                f"log_loss={report.log_loss}\n"
                f"edge_reliability={report.edge_reliability}\n"
                f"hit_rate={report.hit_rate}\n"
                f"avg_pnl_per_contract={report.avg_pnl_per_contract}\n"
                f"total_pnl={report.total_pnl}\n"
                f"sharpe_proxy={report.sharpe_ratio}"
            )

        if lower.startswith("/fills"):
            parts = normalized.split()
            days = 30
            if len(parts) >= 2:
                try:
                    days = max(1, int(parts[1]))
                except ValueError:
                    days = 30
            metrics = pipeline.store.get_paper_fill_metrics(days=days)
            avg_fill = metrics["avg_fill_minutes"]
            avg_fill_text = "n/a" if avg_fill is None else str(round(float(avg_fill), 2))
            return (
                f"📦 Fill Metrics ({days}d)\n"
                f"total_orders={metrics['total_orders']}\n"
                f"filled_orders={metrics['filled_orders']}\n"
                f"open_orders={metrics['open_orders']}\n"
                f"canceled_orders={metrics['canceled_orders']}\n"
                f"failed_orders={metrics['failed_orders']}\n"
                f"fill_rate={_format_pct(metrics['fill_rate'])}\n"
                f"avg_fill_minutes={avg_fill_text}"
            )

        if lower == "/balance":
            balance = pipeline.get_balance_snapshot()
            if balance is None:
                return "Balance unavailable for current mode."
            return f"💵 Balance\n{balance}"

        return None

    def notify_operational_alerts(
        self, now_utc: datetime, messages: list[str]
    ) -> list[AlertEvent]:
        if not self.is_enabled() or not messages:
            return []
        events: list[AlertEvent] = []
        for message in messages:
            status, metadata = self._send_message(message)
            events.append(
                AlertEvent(
                    channel="telegram",
                    event_type="operational_alert",
                    market_ticker=None,
                    message=message,
                    status=status,
                    metadata=metadata,
                    created_at=now_utc,
                )
            )
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

    def _fetch_updates(self) -> list[dict[str, Any]]:
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/getUpdates"
        params = {"timeout": 0, "offset": self._updates_offset}
        try:
            response = self._session.get(url, params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException:
            logger.warning("telegram_get_updates_failed", exc_info=True)
            return []
        if not isinstance(payload, dict):
            return []
        updates = payload.get("result", [])
        if not isinstance(updates, list):
            return []
        for update in updates:
            if isinstance(update, dict):
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._updates_offset = max(self._updates_offset, update_id + 1)
        return [update for update in updates if isinstance(update, dict)]

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
