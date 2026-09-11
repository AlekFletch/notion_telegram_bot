"""Сквозной офлайн-тест конвейера: сообщение → готовый payload для Notion.

Notion и Telegram подменены заглушками, поэтому тест проверяет именно то,
что мы собираем и отправляем: свойства записи, порядок блоков, поведение
при слишком большом файле и при повторной пересылке.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from aiogram.types import MessageEntity

from bot.config import Settings
from bot.notion import NotionError
from bot.pipeline import Saver
from tests.test_pipeline import channel_origin, make_message, photo

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


class FakeNotion:
    """Записывает то, что бот попытался сохранить."""

    def __init__(self, *, existing: str | None = None, max_upload: int = 5 * 1024 * 1024):
        self.max_upload_bytes = max_upload
        self.existing = existing
        self.created: list[tuple[dict, list[dict]]] = []
        self.appended: list[list[dict]] = []
        self.uploads: list[tuple[str, str, int]] = []
        self.upload_error: str | None = None

    async def find_by_key(self, key, property_name="TG key"):
        return self.existing

    async def create_page(self, properties, children=None, *, icon=None):
        self.created.append((properties, list(children or [])))
        return "page-id", "https://notion.so/page-id"

    async def append_blocks(self, page_id, blocks):
        self.appended.append(list(blocks))

    async def upload(self, filename, content_type, data):
        if self.upload_error:
            raise NotionError(400, "validation_error", self.upload_error)
        self.uploads.append((filename, content_type, len(data)))
        from bot.notion import Uploaded

        return Uploaded(id=f"upload-{len(self.uploads)}", filename=filename)


class FakeFile:
    def __init__(self, path: str, size: int):
        self.file_path = path
        self.file_size = size


class FakeForward:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    """Минимальный Bot: отдаёт заранее заданное содержимое файла.

    Умеет и пересылать в архив — запоминает вызовы, чтобы тест мог проверить,
    что видео ушло в канал, а файл при этом не скачивался.
    """

    def __init__(self, *, size: int = 1024, payload: bytes = b"x" * 1024):
        self.size = size
        self.payload = payload
        self.downloaded: list[str] = []
        self.forwarded: list[dict] = []
        self.forward_error: Exception | None = None

    async def get_file(self, file_id):
        self.downloaded.append(file_id)
        return FakeFile(f"photos/{file_id}.jpg", self.size)

    async def download_file(self, path):
        return io.BytesIO(self.payload)

    async def forward_message(self, chat_id, from_chat_id, message_id):
        if self.forward_error:
            raise self.forward_error
        self.forwarded.append(
            {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
        )
        return FakeForward(1000 + len(self.forwarded))


def settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="123:AA",
        notion_token="ntn_test",
        notion_database_id="db",
        fetch_articles=False,  # без сети
    )
    base.update(overrides)
    return Settings(**base)


def props_of(notion: FakeNotion) -> dict:
    return notion.created[0][0]


def blocks_of(notion: FakeNotion) -> list[dict]:
    return notion.created[0][1]


def title_of(notion: FakeNotion) -> str:
    return props_of(notion)["Название"]["title"][0]["text"]["content"]


# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plain_text_message_fills_properties():
    notion = FakeNotion()
    saver = Saver(notion, settings())
    message = make_message(7, text="Первая строка\nвторая строка")

    result = await saver.save(FakeBot(), [message])

    props = props_of(notion)
    assert title_of(notion) == "Первая строка"
    assert props["Тип"]["select"]["name"] == "Текст"
    assert props["Источник"]["rich_text"][0]["text"]["content"] == "Моя заметка"
    assert props["TG key"]["rich_text"][0]["text"]["content"] == "555:7"
    assert props["Ссылка"]["url"] is None
    assert props["Обработано"]["checkbox"] is False
    assert result.url == "https://notion.so/page-id"
    assert [b["type"] for b in blocks_of(notion)] == ["paragraph", "paragraph"]


@pytest.mark.asyncio
async def test_duplicate_is_not_saved_twice():
    notion = FakeNotion(existing="https://notion.so/already")
    saver = Saver(notion, settings())

    result = await saver.save(FakeBot(), [make_message(7, text="повтор")])

    assert result.duplicate is True
    assert result.url == "https://notion.so/already"
    assert notion.created == []


@pytest.mark.asyncio
async def test_link_message_gets_bookmark_and_url_property():
    notion = FakeNotion()
    saver = Saver(notion, settings())
    message = make_message(7, text="читай https://example.com/post вот")

    await saver.save(FakeBot(), [message])

    assert props_of(notion)["Ссылка"]["url"] == "https://example.com/post"
    assert props_of(notion)["Тип"]["select"]["name"] == "Ссылка"
    kinds = [b["type"] for b in blocks_of(notion)]
    assert kinds[-2:] == ["divider", "bookmark"]


@pytest.mark.asyncio
async def test_photo_is_uploaded_and_attached():
    notion = FakeNotion()
    saver = Saver(notion, settings())
    message = make_message(7, photo=photo("a", size=2048), caption="подпись")

    await saver.save(FakeBot(size=2048, payload=b"y" * 2048), [message])

    assert notion.uploads == [("photo_bu_a.jpg", "image/jpeg", 2048)]
    image_blocks = [b for b in blocks_of(notion) if b["type"] == "image"]
    assert image_blocks[0]["image"]["file_upload"]["id"] == "upload-1"
    assert title_of(notion) == "подпись"


@pytest.mark.asyncio
async def test_oversized_file_becomes_callout_not_failure():
    notion = FakeNotion(max_upload=1024)  # лимит 1 КБ
    saver = Saver(notion, settings())
    message = make_message(7, photo=photo("a", size=9 * 1024 * 1024))

    result = await saver.save(FakeBot(size=9 * 1024 * 1024), [message])

    assert notion.uploads == []  # даже не пытались грузить
    callouts = [b for b in blocks_of(notion) if b["type"] == "callout"]
    assert callouts, "должна остаться заметка о непрогруженном файле"
    text = callouts[0]["callout"]["rich_text"][0]["text"]["content"]
    assert "9.0 МБ" in text and "лимита Notion" in text
    assert result.notes  # пользователю тоже сообщим


@pytest.mark.asyncio
async def test_notion_rejecting_upload_does_not_lose_the_message():
    notion = FakeNotion()
    notion.upload_error = "unsupported file type"
    saver = Saver(notion, settings())
    message = make_message(7, photo=photo("a", size=512), caption="важное")

    result = await saver.save(FakeBot(size=512, payload=b"z" * 512), [message])

    assert notion.created, "страница всё равно должна быть создана"
    assert title_of(notion) == "важное"
    assert any("Notion отклонил загрузку" in note for note in result.notes)


@pytest.mark.asyncio
async def test_album_becomes_single_page_with_all_photos():
    notion = FakeNotion()
    saver = Saver(notion, settings())
    messages = [
        make_message(7, media_group_id="MG", photo=photo("a", size=512)),
        make_message(8, media_group_id="MG", photo=photo("b", size=512), caption="Альбом"),
        make_message(9, media_group_id="MG", photo=photo("c", size=512)),
    ]

    await saver.save(FakeBot(size=512, payload=b"q" * 512), messages)

    assert len(notion.created) == 1
    assert props_of(notion)["Тип"]["select"]["name"] == "Альбом"
    assert len(notion.uploads) == 3
    assert len([b for b in blocks_of(notion) if b["type"] == "image"]) == 3


@pytest.mark.asyncio
async def test_forwarded_channel_message_keeps_source_and_link():
    notion = FakeNotion()
    saver = Saver(notion, settings())
    message = make_message(
        7, text="из канала", forward_origin=channel_origin(username="useful", message_id=42)
    )

    await saver.save(FakeBot(), [message])

    props = props_of(notion)
    assert props["Источник"]["rich_text"][0]["text"]["content"] == "Канал «Полезное» · @useful"
    assert props["Оригинал в TG"]["url"] == "https://t.me/useful/42"


@pytest.mark.asyncio
async def test_long_message_is_split_across_create_and_append():
    notion = FakeNotion()
    saver = Saver(notion, settings())
    # 150 строк → 150 блоков, в create_page влезает только 100.
    text = "\n".join(f"строка {i}" for i in range(150))

    await saver.save(FakeBot(), [make_message(7, text=text)])

    assert len(blocks_of(notion)) == 100
    assert len(notion.appended) == 1
    assert len(notion.appended[0]) == 50


@pytest.mark.asyncio
async def test_formatting_survives_to_notion_payload():
    notion = FakeNotion()
    saver = Saver(notion, settings())
    # Эмодзи вне BMP перед выделением — проверяем сквозной пересчёт UTF-16.
    text = "📥 важное дело"
    message = make_message(
        7, text=text, entities=[MessageEntity(type="bold", offset=3, length=6)]
    )
    await saver.save(FakeBot(), [message])

    items = blocks_of(notion)[0]["paragraph"]["rich_text"]
    bold = [i["text"]["content"] for i in items if i["annotations"]["bold"]]
    assert bold == ["важное"]


@pytest.mark.asyncio
async def test_network_failure_on_media_keeps_the_message():
    """Связь с Telegram отвалилась на вложении — текст всё равно сохраняем.

    Раньше TelegramNetworkError пролетал наружу и убивал всю запись целиком.
    """
    from aiogram.exceptions import TelegramNetworkError

    class BrokenBot(FakeBot):
        async def get_file(self, file_id):
            raise TelegramNetworkError(method=None, message="Request timeout error")

    notion = FakeNotion()
    saver = Saver(notion, settings())
    message = make_message(7, photo=photo("a", size=2048), caption="важный пост")

    result = await saver.save(BrokenBot(), [message])

    assert notion.created, "страница должна быть создана несмотря на сбой сети"
    assert title_of(notion) == "важный пост"
    callouts = [b for b in blocks_of(notion) if b["type"] == "callout"]
    assert callouts, "о непрогруженном файле должна остаться заметка"
    assert "связ" in callouts[0]["callout"]["rich_text"][0]["text"]["content"].lower()
    assert result.notes
