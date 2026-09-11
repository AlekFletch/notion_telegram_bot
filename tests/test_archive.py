"""Архив вложений в приватном канале Telegram.

Главное, что здесь проверяется: видео уходит в канал пересылкой, а не
скачиванием — иначе ролик больше 20 МБ сохранить невозможно, Bot API его
просто не отдаёт.
"""
from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import Video

from bot import archive
from bot.pipeline import Saver
from tests.test_pipeline import make_message, photo
from tests.test_saver import FakeBot, FakeNotion, blocks_of, settings, title_of

ARCHIVE = -1001234567890


def video(unique: str = "v1", size: int = 50 * 1024 * 1024, name: str = "clip.mp4") -> Video:
    return Video(
        file_id=f"vid_{unique}",
        file_unique_id=f"vu_{unique}",
        width=1280,
        height=720,
        duration=30,
        file_name=name,
        mime_type="video/mp4",
        file_size=size,
    )


def callouts(notion: FakeNotion) -> list[dict]:
    return [b for b in blocks_of(notion) if b["type"] == "callout"]


def callout_text(block: dict) -> str:
    return block["callout"]["rich_text"][0]["text"]["content"]


def callout_link(block: dict) -> str | None:
    link = block["callout"]["rich_text"][0]["text"].get("link")
    return link["url"] if link else None


# --------------------------------------------------------------------------
# Ссылки
# --------------------------------------------------------------------------

def test_private_channel_link_drops_the_100_prefix():
    assert archive.tg_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"


def test_public_channel_link_uses_username():
    assert archive.tg_link(-1001234567890, 42, "useful") == "https://t.me/useful/42"


def test_link_for_a_chat_without_the_prefix_is_impossible():
    assert archive.tg_link(555, 42) is None


# --------------------------------------------------------------------------
# Видео
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_video_goes_to_the_archive_without_being_downloaded():
    """Ради этого всё и затевалось: 50 МБ через getFile получить нельзя."""
    notion = FakeNotion()
    saver = Saver(notion, settings(archive_chat_id=ARCHIVE))
    bot = FakeBot()
    message = make_message(7, video=video(), caption="важный ролик")

    result = await saver.save(bot, [message])

    assert bot.downloaded == [], "видео не должно скачиваться"
    assert notion.uploads == []
    assert bot.forwarded == [{"chat_id": ARCHIVE, "from_chat_id": 555, "message_id": 7}]

    card = callouts(notion)[0]
    assert callout_link(card) == "https://t.me/c/1234567890/1001"
    assert "clip.mp4" in callout_text(card)
    assert result.notes == [], "это штатный путь, предупреждать не о чем"
    assert title_of(notion) == "важный ролик"


@pytest.mark.asyncio
async def test_without_archive_video_behaves_as_before():
    notion = FakeNotion()
    saver = Saver(notion, settings())  # archive_chat_id = 0
    bot = FakeBot()

    result = await saver.save(bot, [make_message(7, video=video())])

    assert bot.forwarded == []
    # Старое поведение: 50 МБ отсекаются по размеру ещё до getFile, и в
    # заметке остаётся жалоба вместо видео.
    assert bot.downloaded == []
    assert any("20 МБ" in note for note in result.notes)


@pytest.mark.asyncio
async def test_unreachable_archive_falls_back_to_the_old_path():
    """Бота выгнали из канала — запись всё равно должна сохраниться."""
    notion = FakeNotion()
    saver = Saver(notion, settings(archive_chat_id=ARCHIVE))
    bot = FakeBot()
    bot.forward_error = TelegramForbiddenError(method=None, message="bot is not a member")
    message = make_message(7, video=video(), caption="важный ролик")

    result = await saver.save(bot, [message])

    assert notion.created, "страница должна быть создана"
    assert title_of(notion) == "важный ролик"
    text = callout_text(callouts(notion)[0])
    assert "не может писать в канал-архив" in text
    assert result.notes


@pytest.mark.asyncio
async def test_small_video_still_goes_to_the_archive():
    """Влезающий в лимиты ролик тоже уходит в канал: правило одно для всех."""
    notion = FakeNotion()
    saver = Saver(notion, settings(archive_chat_id=ARCHIVE))
    bot = FakeBot(size=512, payload=b"v" * 512)

    await saver.save(bot, [make_message(7, video=video(size=512))])

    assert notion.uploads == []
    assert len(bot.forwarded) == 1


# --------------------------------------------------------------------------
# Остальные вложения: архив только как запасной путь
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_photo_that_fits_stays_inside_the_page():
    notion = FakeNotion()
    saver = Saver(notion, settings(archive_chat_id=ARCHIVE))
    bot = FakeBot(size=512, payload=b"p" * 512)

    await saver.save(bot, [make_message(7, photo=photo("a", size=512))])

    assert bot.forwarded == [], "архив трогать незачем — файл влез"
    assert len(notion.uploads) == 1


@pytest.mark.asyncio
async def test_oversized_photo_gets_a_link_instead_of_a_complaint():
    notion = FakeNotion(max_upload=1024)
    saver = Saver(notion, settings(archive_chat_id=ARCHIVE))
    bot = FakeBot(size=9 * 1024 * 1024)

    result = await saver.save(bot, [make_message(7, photo=photo("a", size=9 * 1024 * 1024))])

    assert len(bot.forwarded) == 1
    card = callouts(notion)[0]
    assert callout_link(card) == "https://t.me/c/1234567890/1001"
    assert "не загружен" not in callout_text(card)
    assert result.notes == []


@pytest.mark.asyncio
async def test_file_rejected_by_notion_gets_a_link():
    notion = FakeNotion()
    notion.upload_error = "unsupported file type"
    saver = Saver(notion, settings(archive_chat_id=ARCHIVE))
    bot = FakeBot(size=512, payload=b"z" * 512)

    result = await saver.save(bot, [make_message(7, photo=photo("a", size=512))])

    assert len(bot.forwarded) == 1
    assert callout_link(callouts(notion)[0]) == "https://t.me/c/1234567890/1001"
    assert result.notes == []


@pytest.mark.asyncio
async def test_both_paths_failing_explains_both_reasons():
    notion = FakeNotion(max_upload=1024)
    saver = Saver(notion, settings(archive_chat_id=ARCHIVE))
    bot = FakeBot(size=9 * 1024 * 1024)
    bot.forward_error = TelegramForbiddenError(method=None, message="bot is not a member")

    result = await saver.save(bot, [make_message(7, photo=photo("a", size=9 * 1024 * 1024))])

    text = callout_text(callouts(notion)[0])
    assert "лимита Notion" in text
    assert "в архив тоже не ушло" in text
    assert result.notes


@pytest.mark.asyncio
async def test_album_forwards_every_message_that_did_not_fit():
    notion = FakeNotion(max_upload=1024)
    saver = Saver(notion, settings(archive_chat_id=ARCHIVE))
    bot = FakeBot(size=9 * 1024 * 1024)
    messages = [
        make_message(7, media_group_id="MG", photo=photo("a", size=9 * 1024 * 1024)),
        make_message(8, media_group_id="MG", photo=photo("b", size=9 * 1024 * 1024)),
    ]

    await saver.save(bot, messages)

    assert [f["message_id"] for f in bot.forwarded] == [7, 8]
    assert len(callouts(notion)) == 2
