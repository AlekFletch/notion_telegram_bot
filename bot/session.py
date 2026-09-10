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
from contextlib import contextmanager
from contextvars import ContextVar

from aiohttp import ClientTimeout

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


#: Флаг «эта отправка одноразовая». ContextVar копируется в дочернюю задачу
#: при её создании, поэтому переключение внутри задачи не задевает соседние.
_single_attempt: ContextVar[bool] = ContextVar("single_attempt", default=False)


@contextmanager
def no_retry():
    """Отключить повторы для вызовов, которые незачем догонять.

    Пример — служебное «Сохраняю…»: если оно не ушло сразу, то доедет уже
    бессмысленным и повиснет на экране, потому что редактировать его будет
    поздно.
    """
    token = _single_attempt.set(True)
    try:
        yield
    finally:
        _single_attempt.reset(token)


def backoff(attempt: int) -> float:
    """Растущая пауза со случайным разбросом, чтобы не долбить сервер в такт."""
    return min(2 ** attempt, 16) * (0.5 + random.random() / 2)


class RetryingSession(AiohttpSession):
    """AiohttpSession, повторяющая запрос при сетевом сбое."""

    def __init__(
        self,
        *args,
        attempts: int = DEFAULT_ATTEMPTS,
        connect_timeout: float = 10.0,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._attempts = attempts
        self._connect_timeout = connect_timeout

    def _deadline(self, timeout) -> ClientTimeout:
        """Таймаут запроса с отдельно ограниченной фазой подключения.

        aiogram отдаёт таймаут одним числом, а число aiohttp понимает как общий
        лимит — коннект при этом не ограничен ничем. Когда канал до Telegram
        фильтруется, подключение висит целую минуту, и повторы теряют смысл.
        Само поле self.timeout при этом обязано остаться числом: aiogram
        складывает его с таймаутом опроса (int(session.timeout + polling)).
        """
        total = float(timeout if timeout is not None else self.timeout)
        return ClientTimeout(
            total=total,
            connect=self._connect_timeout,
            sock_connect=self._connect_timeout,
        )

    async def make_request(self, bot, method, timeout=None):  # type: ignore[override]
        name = type(method).__name__
        attempts = 1 if _single_attempt.get() else self._attempts
        deadline = self._deadline(timeout)
        last: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await super().make_request(bot, method, timeout=deadline)

            except TelegramRetryAfter as exc:
                # Флуд-контроль Telegram: сервер прямо говорит, сколько ждать.
                last = exc
                if attempt == attempts:
                    break
                log.warning("Telegram %s: флуд-контроль, пауза %s с", name, exc.retry_after)
                await asyncio.sleep(exc.retry_after)

            except RETRIABLE as exc:
                last = exc
                if attempt == attempts:
                    break
                delay = backoff(attempt)
                log.warning(
                    "Telegram %s: %s. Повтор %d из %d через %.1f с",
                    name, type(exc).__name__, attempt, attempts - 1, delay,
                )
                await asyncio.sleep(delay)

        assert last is not None
        if attempts > 1:
            log.error("Telegram %s: не удалось за %d попыток", name, attempts)
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
