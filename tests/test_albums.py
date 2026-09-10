"""Тесты буфера альбомов: конца альбома в Telegram API нет, ждём паузу."""
from __future__ import annotations

import asyncio

import pytest

from bot.albums import AlbumBuffer
from tests.test_pipeline import make_message, photo

DEBOUNCE = 0.05


def recorder() -> tuple[list, object]:
    """Асинхронный приёмник: AlbumBuffer ждёт корутину, не обычную функцию."""
    flushed: list[list] = []

    async def flush(messages):
        flushed.append(messages)

    return flushed, flush


@pytest.mark.asyncio
async def test_single_message_flushes_immediately():
    buffer = AlbumBuffer(debounce=10.0)  # заведомо больше времени теста
    flushed, flush = recorder()

    await buffer.add(make_message(1, text="одиночное"), flush)

    assert len(flushed) == 1
    assert len(flushed[0]) == 1


@pytest.mark.asyncio
async def test_album_parts_are_flushed_once_together():
    buffer = AlbumBuffer(debounce=DEBOUNCE)
    flushed, flush = recorder()

    for message_id in (3, 1, 2):
        await buffer.add(
            make_message(message_id, media_group_id="MG", photo=photo(str(message_id))),
            flush,
        )
        await asyncio.sleep(DEBOUNCE / 3)  # части приходят быстрее дебаунса

    await asyncio.sleep(DEBOUNCE * 3)

    assert len(flushed) == 1, "альбом должен сохраниться одной записью"
    assert [m.message_id for m in flushed[0]] == [1, 2, 3], "порядок восстанавливается"


@pytest.mark.asyncio
async def test_two_albums_do_not_mix():
    buffer = AlbumBuffer(debounce=DEBOUNCE)
    flushed, flush = recorder()

    await buffer.add(make_message(1, media_group_id="A", photo=photo("a")), flush)
    await buffer.add(make_message(2, media_group_id="B", photo=photo("b")), flush)
    await asyncio.sleep(DEBOUNCE * 3)

    assert len(flushed) == 2
    assert {m.media_group_id for group in flushed for m in group} == {"A", "B"}


@pytest.mark.asyncio
async def test_flush_error_is_contained():
    """Падение сохранения не должно ронять фоновую задачу молча и навсегда."""
    buffer = AlbumBuffer(debounce=DEBOUNCE)

    async def failing(messages):
        raise RuntimeError("Notion недоступен")

    await buffer.add(make_message(1, media_group_id="MG", photo=photo("a")), failing)
    await asyncio.sleep(DEBOUNCE * 3)

    # Буфер остался работоспособным для следующего альбома.
    flushed, flush = recorder()
    await buffer.add(make_message(2, media_group_id="MG2", photo=photo("b")), flush)
    await asyncio.sleep(DEBOUNCE * 3)
    assert len(flushed) == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_timers():
    buffer = AlbumBuffer(debounce=10.0)
    flushed, flush = recorder()

    await buffer.add(make_message(1, media_group_id="MG", photo=photo("a")), flush)
    await buffer.shutdown()
    await asyncio.sleep(0)

    assert flushed == []
