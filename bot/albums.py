"""Склейка альбомов.

Альбом Telegram приходит несколькими отдельными апдейтами с общим
media_group_id — «конца альбома» в API нет. Поэтому копим сообщения и ждём
паузу: не пришло продолжение за debounce секунд — считаем альбом собранным.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from aiogram.types import Message

log = logging.getLogger(__name__)

Flush = Callable[[list[Message]], Awaitable[None]]


class AlbumBuffer:
    def __init__(self, debounce: float = 2.0):
        self._debounce = debounce
        self._groups: dict[str, list[Message]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def add(self, message: Message, flush: Flush) -> None:
        """Одиночное сообщение уходит сразу, часть альбома — ждёт соседей."""
        group_id = message.media_group_id
        if not group_id:
            await flush([message])
            return

        key = f"{message.chat.id}:{group_id}"
        async with self._lock:
            self._groups.setdefault(key, []).append(message)
            timer = self._timers.get(key)
            if timer:
                timer.cancel()
            self._timers[key] = asyncio.create_task(self._wait(key, flush))

    async def _wait(self, key: str, flush: Flush) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return  # пришло ещё одно фото — таймер перезапущен

        async with self._lock:
            messages = self._groups.pop(key, [])
            self._timers.pop(key, None)

        if not messages:
            return

        messages.sort(key=lambda m: m.message_id)
        log.info("Альбом %s собран: %d сообщений", key, len(messages))
        try:
            await flush(messages)
        except Exception:  # noqa: BLE001 — задача фоновая, лог обязателен
            log.exception("Не удалось сохранить альбом %s", key)

    async def shutdown(self) -> None:
        """Отменить незавершённые таймеры при остановке бота."""
        async with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._groups.clear()
