"""Тесты доставки ответа при нестабильной связи.

Запись в Notion к моменту ответа уже сохранена, поэтому терять ответ нельзя:
без него не выбрать категорию. Проверяем, что бот дожидается окна связи.
"""
from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from bot import handlers


@pytest.fixture(autouse=True)
def instant_delays(monkeypatch):
    """Нулевые паузы вместо минутных: проверяем количество попыток, не время.

    Подменять asyncio.sleep целиком нельзя — фоновая задача тогда не получает
    управление, и тест начинает мерить не то, что нужно.
    """
    monkeypatch.setattr(handlers, "DELIVERY_DELAYS", (0, 0, 0))


class Recorder:
    """Сообщение, которое падает первые `fail_times` раз."""

    def __init__(self, fail_times: int = 0, error=None):
        self.fail_times = fail_times
        self.error = error or TelegramNetworkError(method=None, message="нет связи")
        self.calls = 0

    async def reply(self, text, reply_markup=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return self

    async def edit_text(self, text, reply_markup=None):
        return await self.reply(text, reply_markup)


async def drain():
    """Дать фоновым задачам доставки доработать."""
    for _ in range(50):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_reply_delivered_on_first_try():
    target = Recorder()
    await handlers._report(None, target, "готово")
    assert target.calls == 1


@pytest.mark.asyncio
async def test_reply_retried_until_connection_returns():
    """Связь вернулась на третьей попытке — ответ всё-таки доходит."""
    target = Recorder(fail_times=2)

    await handlers._report(None, target, "готово")
    await drain()

    assert target.calls == 3


@pytest.mark.asyncio
async def test_retries_stop_after_budget_is_spent():
    target = Recorder(fail_times=99)

    await handlers._report(None, target, "готово")
    await drain()

    # Первая попытка плюс по одной на каждую паузу.
    assert target.calls == 1 + len(handlers.DELIVERY_DELAYS)


@pytest.mark.asyncio
async def test_bad_request_is_not_retried():
    """Дело не в связи — повторять бессмысленно."""
    target = Recorder(
        fail_times=99,
        error=TelegramBadRequest(method=None, message="message is too long"),
    )

    await handlers._report(None, target, "готово")
    await drain()

    assert target.calls == 1


@pytest.mark.asyncio
async def test_handler_is_not_blocked_by_retries():
    """Повторы идут в фоне: обработчик возвращается сразу."""
    target = Recorder(fail_times=2)

    await asyncio.wait_for(handlers._report(None, target, "готово"), timeout=1.0)
    assert target.calls == 1  # остальное — уже в фоновой задаче

    await drain()
    assert target.calls == 3


@pytest.mark.asyncio
async def test_status_message_is_edited_when_it_exists():
    status = Recorder()
    fallback = Recorder()

    await handlers._report(status, fallback, "готово")

    assert status.calls == 1
    assert fallback.calls == 0
