from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from typing import Any, Awaitable, Callable

import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)


MessageHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
ErrorHandler = Callable[[Exception], Awaitable[None] | None]
AuthHeadersProvider = Callable[[], dict[str, str]]


class WSManager:
    def __init__(
        self,
        *,
        url: str,
        auth_headers: dict[str, str] | None = None,
        auth_headers_provider: AuthHeadersProvider | None = None,
        on_message: MessageHandler | None = None,
        on_error: ErrorHandler | None = None,
        reconnect_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self.url = url
        self.auth_headers = auth_headers or {}
        self.auth_headers_provider = auth_headers_provider
        self.on_message = on_message
        self.on_error = on_error
        self.reconnect_delay = max(0.5, reconnect_delay)
        self.reconnect_max_delay = max(self.reconnect_delay, reconnect_max_delay)
        self.heartbeat_interval = max(5.0, heartbeat_interval)
        self._ws: WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_wait = self.reconnect_delay
        self._subscriptions: list[dict[str, Any]] = []

    async def connect(self) -> None:
        headers = self.auth_headers
        if self.auth_headers_provider is not None:
            headers = self.auth_headers_provider()
        self._ws = await websockets.connect(
            self.url,
            extra_headers=headers or None,
            ping_interval=None,
            close_timeout=5,
            max_queue=4096,
        )
        logger.info("ws_connected url=%s", self.url)
        self._reconnect_wait = self.reconnect_delay
        for payload in self._subscriptions:
            await self._send_json(payload)

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def subscribe(self, channels: list[str], tickers: list[str] | None = None) -> None:
        payload: dict[str, Any] = {"cmd": "subscribe", "channels": channels}
        if tickers:
            payload["market_tickers"] = tickers
        self._subscriptions.append(payload)
        if self._ws is not None:
            await self._send_json(payload)

    async def unsubscribe(self, sids: list[int]) -> None:
        payload = {"cmd": "unsubscribe", "sids": sids}
        if self._ws is not None:
            await self._send_json(payload)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(payload))

    async def _dispatch_message(self, payload: dict[str, Any]) -> None:
        if self.on_message is None:
            return
        result = self.on_message(payload)
        if inspect.isawaitable(result):
            await result

    async def _dispatch_error(self, exc: Exception) -> None:
        if self.on_error is None:
            return
        result = self.on_error(exc)
        if inspect.isawaitable(result):
            await result

    async def _listen_loop(self) -> None:
        assert self._ws is not None
        async for message in self._ws:
            if isinstance(message, bytes):
                try:
                    payload = json.loads(message.decode("utf-8"))
                except Exception:
                    continue
            else:
                try:
                    payload = json.loads(message)
                except Exception:
                    continue
            if not isinstance(payload, dict):
                continue
            await self._dispatch_message(payload)

    async def _heartbeat(self) -> None:
        while self._running and self._ws is not None:
            await asyncio.sleep(self.heartbeat_interval)
            if self._ws is None:
                return
            try:
                pong_waiter = await self._ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=10)
            except Exception as exc:
                await self._dispatch_error(exc)
                try:
                    await self._ws.close()
                except Exception:
                    pass
                return

    async def _reconnect(self) -> None:
        wait_seconds = min(self._reconnect_wait, self.reconnect_max_delay)
        logger.info("ws_reconnecting url=%s wait_seconds=%s", self.url, wait_seconds)
        await asyncio.sleep(wait_seconds)
        self._reconnect_wait = min(self._reconnect_wait * 2, self.reconnect_max_delay)

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.connect()
                heartbeat_task = asyncio.create_task(self._heartbeat())
                try:
                    await self._listen_loop()
                finally:
                    heartbeat_task.cancel()
                    with contextlib.suppress(Exception):
                        await heartbeat_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ws_loop_failed url=%s", self.url, exc_info=True)
                await self._dispatch_error(exc)
            finally:
                if self._ws is not None:
                    with contextlib.suppress(Exception):
                        await self._ws.close()
                self._ws = None
            if self._running:
                await self._reconnect()

    async def close(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()
