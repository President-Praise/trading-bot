"""
telegram_alerts.py
~~~~~~~~~~~~~~~~~~
Non-blocking Telegram notification queue.

Messages are enqueued synchronously and dispatched by an asyncio worker
with a 1-second gap between sends.  Telegram failures never crash the bot.

Two public methods are available:
  send(message)               — plain HTML text message
  send_chart(photo_bytes,     — chart PNG + caption (uses sendPhoto)
             caption)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

import credentials
from utils.logger import get_logger, log_event

# Fix #12: bound the queue so put_nowait actually raises QueueFull
_QUEUE_MAXSIZE = 200


@dataclass
class _TextItem:
    """A plain text message to deliver via sendMessage."""
    message: str


@dataclass
class _PhotoItem:
    """A chart image + caption to deliver via sendPhoto."""
    photo_bytes: bytes
    caption:     str


# Internal queue item type
_QueueItem = _TextItem | _PhotoItem


class TelegramAlerter:
    """
    Thread-safe async message queue for Telegram notifications.

    Usage
    -----
    alerter = TelegramAlerter()
    await alerter.start()                    # starts the background worker once
    alerter.send("Hello!")                   # non-blocking text enqueue
    alerter.send_chart(png_bytes, caption)   # non-blocking chart enqueue
    """

    def __init__(self):
        self._queue:   asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._running: bool          = False
        self._task:    asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self.logger = get_logger('telegram')

    # ── Lifecycle ─────────────────────────────────────────

    async def start(self):
        """Start the background queue worker (idempotent)."""
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task    = asyncio.create_task(self._worker())
        log_event(self.logger, logging.INFO, '------', 'Telegram alerter started')

    async def stop(self):
        """Drain the queue and stop the worker gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session is not None:
            await self._session.close()
            self._session = None
        log_event(self.logger, logging.INFO, '------', 'Telegram alerter stopped')

    # ── Public API ────────────────────────────────────────

    def send(self, message: str):
        """
        Enqueue a plain HTML text *message* for delivery.
        Non-blocking — safe to call from synchronous or asynchronous code.
        """
        try:
            self._queue.put_nowait(_TextItem(message))
        except asyncio.QueueFull:
            log_event(self.logger, logging.WARNING, '------',
                      'Telegram queue full — text message dropped')

    def send_chart(self, photo_bytes: bytes, caption: str):
        """
        Enqueue a chart image (PNG bytes) together with an HTML *caption*.
        The image is sent via Telegram's sendPhoto endpoint so it renders
        inline in the chat — identical in appearance to the Deriv screenshot.
        Non-blocking — safe to call from synchronous or asynchronous code.
        """
        try:
            self._queue.put_nowait(_PhotoItem(photo_bytes, caption))
        except asyncio.QueueFull:
            log_event(self.logger, logging.WARNING, '------',
                      'Telegram queue full — chart image dropped')

    # ── Worker ────────────────────────────────────────────

    
    async def _worker(self):
        """Deliver queued items with a 1-second inter-message delay."""
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if isinstance(item, _TextItem):
                await self._send_text(item.message)
            elif isinstance(item, _PhotoItem):
                await self._send_photo(item.photo_bytes, item.caption)

            self._queue.task_done()
            await asyncio.sleep(1)

    # ── HTTP delivery ─────────────────────────────────────

    async def _send_text(self, message: str):
        """
        POST *message* to the Telegram Bot API via sendMessage.
        Sends to all chat IDs in credentials.TELEGRAM_CHAT_ID.
        All exceptions are caught — Telegram failures must never crash the bot.
        """
        if self._session is None:
            log_event(self.logger, logging.WARNING, '------',
                      'Telegram session not initialised — message dropped')
            return

        chat_ids = credentials.TELEGRAM_CHAT_ID
        if isinstance(chat_ids, str):
            chat_ids = [chat_ids]

        for chat_id in chat_ids:
            url = (
                f'https://api.telegram.org/bot{credentials.TELEGRAM_BOT_TOKEN}/sendMessage'
            )
            payload = {
                'chat_id':    chat_id,
                'text':       message,
                'parse_mode': 'HTML',
            }
            try:
                async with self._session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log_event(self.logger, logging.WARNING, '------',
                                f'Telegram API returned {resp.status}: {body[:200]}')
            except aiohttp.ClientError as exc:
                log_event(self.logger, logging.WARNING, '------',
                        f'Telegram network error: {exc}')
            except asyncio.TimeoutError:
                log_event(self.logger, logging.WARNING, '------',
                        'Telegram request timed out')
            except Exception as exc:  # noqa: BLE001
                log_event(self.logger, logging.WARNING, '------',
                        f'Unexpected Telegram error: {exc}')

    async def _send_photo(self, photo_bytes: bytes, caption: str):
        """
        POST a chart PNG to Telegram via sendPhoto with an HTML caption.
        Sends to all chat IDs in credentials.TELEGRAM_CHAT_ID.
        All exceptions are caught — Telegram failures must never crash the bot.
        """
        if self._session is None:
            log_event(self.logger, logging.WARNING, '------',
                      'Telegram session not initialised — photo dropped')
            return

        chat_ids = credentials.TELEGRAM_CHAT_ID
        if isinstance(chat_ids, str):
            chat_ids = [chat_ids]

        for chat_id in chat_ids:
            url = (
                f'https://api.telegram.org/bot{credentials.TELEGRAM_BOT_TOKEN}/sendPhoto'
            )
            try:
                form = aiohttp.FormData()
                form.add_field('chat_id',    chat_id)
                form.add_field('caption',    caption)
                form.add_field('parse_mode', 'HTML')
                form.add_field(
                    'photo',
                    photo_bytes,
                    filename='chart.png',
                    content_type='image/png',
                )
                async with self._session.post(
                    url,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=30),  # larger timeout for upload
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log_event(self.logger, logging.WARNING, '------',
                                f'Telegram sendPhoto returned {resp.status}: {body[:200]}')
            except aiohttp.ClientError as exc:
                log_event(self.logger, logging.WARNING, '------',
                        f'Telegram photo network error: {exc}')
            except asyncio.TimeoutError:
                log_event(self.logger, logging.WARNING, '------',
                        'Telegram sendPhoto request timed out')
            except Exception as exc:  # noqa: BLE001
                log_event(self.logger, logging.WARNING, '------',
                        f'Unexpected Telegram photo error: {exc}')

    # ── Legacy alias (kept for backward compatibility) ────
    # Any existing call to ._send() in tests or external code still works.
    _send = _send_text
