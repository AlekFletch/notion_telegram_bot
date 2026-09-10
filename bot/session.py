"""Сессия Telegram с автоповтором.

Доступ к api.telegram.org бывает нестабильным: установка нового соединения
срывается по таймауту, хотя на уже открытом запросы проходят за секунду.
Один повтор почти всегда решает дело, поэтому все вызовы Bot API проходят
через эту сессию, а не через стандартную.
"""
from __future__ import annotations

import asyncio
import logging
import random

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

log = logging.getLogger(__name__)

#: Ошибки, которые лечатся повтором.
RETRIABLE = (TelegramNetworkError, TelegramServerError)

DEFAULT_ATTEMPTS = 4


def backoff(attempt: int) -> float:
    """Растущая пауза со случайным разбросом, чтобы не долбить сервер в такт."""
    return min(2 ** attempt, 16) * (0.5 + random.random() / 2)


class RetryingSession(AiohttpSession):
    """AiohttpSession, повторяющая запрос при сетевом сбое."""

    def __init__(self, *args, attempts: int = DEFAULT_ATTEMPTS, **kwargs):
        super().__init__(*args, **kwargs)
        self._attempts = attempts

    async def make_request(self, bot, method, timeout=None):  # type: ignore[override]
        name = type(method).__name__
        last: Exception | None = None

        for attempt in range(1, self._attempts + 1):
            try:
                return await super().make_request(bot, method, timeout=timeout)

            except TelegramRetryAfter as exc:
                # Флуд-контроль Telegram: сервер прямо говорит, сколько ждать.
                last = exc
                if attempt == self._attempts:
                    break
                log.warning("Telegram %s: флуд-контроль, пауза %s с", name, exc.retry_after)
                await asyncio.sleep(exc.retry_after)

            except RETRIABLE as exc:
                last = exc
                if attempt == self._attempts:
                    break
                delay = backoff(attempt)
                log.warning(
                    "Telegram %s: %s. Повтор %d из %d через %.1f с",
                    name, type(exc).__name__, attempt, self._attempts - 1, delay,
                )
                await asyncio.sleep(delay)

        assert last is not None
        log.error("Telegram %s: не удалось за %d попыток", name, self._attempts)
        raise last


async def retry_call(what: str, factory, attempts: int = DEFAULT_ATTEMPTS):
    """Повтор для вызовов мимо make_request — например, скачивания файла.

    factory — функция без аргументов, возвращающая новую корутину на каждую
    попытку: повторно ожидать уже использованную корутину нельзя.
    """
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except RETRIABLE as exc:
            last = exc
            if attempt == attempts:
                break
            delay = backoff(attempt)
            log.warning(
                "%s: %s. Повтор %d из %d через %.1f с",
                what, type(exc).__name__, attempt, attempts - 1, delay,
            )
            await asyncio.sleep(delay)

    assert last is not None
    raise last
