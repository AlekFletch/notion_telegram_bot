"""Проверка связки с Notion до запуска бота.

Создаёт в базе одну тестовую запись: текст с форматированием, картинку,
закладку. Проверяет дедупликацию. По ходу выясняет, какую именно JSON-форму
блока принимает File Upload API этого воркспейса — в документации она описана
неоднозначно, и лучше выяснить это здесь, чем на живом сообщении.

    python -m scripts.smoke_notion
"""
from __future__ import annotations

import asyncio
import base64
import sys
from datetime import datetime, timezone

from bot import formatting as fmt
from bot import notion as api
from bot.config import load_settings
from bot.notion import NotionClient, NotionError

# Прозрачный PNG 1x1 — самый маленький валидный файл для проверки загрузки.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

KEY = "smoke:notion-check"

#: Формы блока с загруженным файлом. Первая — основная, вторая — запасная.
BLOCK_SHAPES = {
    "file_upload": lambda upload_id: {"type": "file_upload", "file_upload": {"id": upload_id}},
    "file_upload_id": lambda upload_id: {"type": "file", "file": {"file_upload_id": upload_id}},
}


def ok(message: str) -> None:
    print(f"  [ok] {message}")


def fail(message: str) -> None:
    print(f"  [!!] {message}")


async def main() -> int:
    settings = load_settings()
    notion = NotionClient(settings.notion_token, settings.notion_database_id)

    try:
        print("1. Доступ к базе")
        try:
            await notion.prepare()
        except NotionError as exc:
            fail(str(exc))
            if exc.status == 404:
                fail(
                    "База не видна интеграции. Открой страницу «Telegram Inbox» "
                    "в Notion → меню ⋯ → Connections → добавь свою интеграцию."
                )
            elif exc.status == 401:
                fail("Токен отклонён. Проверь NOTION_TOKEN в .env")
            return 1
        ok(f"data source {notion.data_source_id}")
        ok(f"лимит файла {notion.max_upload_bytes / 1024 / 1024:.0f} МБ")

        print("2. Загрузка файла")
        uploaded = await notion.upload("smoke.png", "image/png", PNG_1PX)
        ok(f"upload id {uploaded.id}")

        print("3. Форма блока с файлом")
        working_shape = None
        for name, build in BLOCK_SHAPES.items():
            payload = build(uploaded.id)
            block = {"object": "block", "type": "image", "image": payload}
            try:
                page_id, _ = await notion.create_page(
                    {"Название": api.title_property("проверка формы блока")}, [block]
                )
            except NotionError as exc:
                fail(f"{name}: {exc.notion_message[:120]}")
                continue
            working_shape = name
            ok(f"работает форма «{name}»")
            await notion._request("PATCH", f"/blocks/{page_id}", json={"archived": True})
            break

        if working_shape is None:
            fail("ни одна форма блока не принята — загрузка файлов работать не будет")
            return 1
        if working_shape != "file_upload":
            fail(
                "Внимание: bot/notion.py собирает блок формой «file_upload». "
                f"Здесь работает «{working_shape}» — нужно поправить file_block()."
            )

        print("4. Полноценная запись")
        text = "Проверка бота: жирный, ссылка и код."
        entities = [
            _Entity("bold", 16, 6),
            _Entity("text_link", 24, 6, url="https://www.notion.so/"),
            _Entity("code", 33, 3),
        ]
        blocks = fmt.message_blocks(text, entities)
        blocks.append(api.file_block("image", uploaded.id) if working_shape == "file_upload"
                      else {"object": "block", "type": "image", "image": BLOCK_SHAPES[working_shape](uploaded.id)})
        blocks.append(fmt.divider())
        blocks.append(fmt.bookmark("https://www.notion.so/"))

        properties = {
            "Название": api.title_property("✅ Проверка связи Telegram → Notion"),
            "Дата": api.date_property(datetime.now(timezone.utc)),
            "Источник": api.text_property("scripts/smoke_notion.py"),
            "Тип": api.select_property("Текст"),
            "Ссылка": api.url_property("https://www.notion.so/"),
            "TG key": api.text_property(KEY),
            "Обработано": api.checkbox_property(False),
        }
        page_id, page_url = await notion.create_page(properties, blocks, icon="✅")
        ok(f"страница создана: {page_url}")

        print("5. Дедупликация")
        found = await notion.find_by_key(KEY)
        if found:
            ok("запись находится по ключу — повторы отсекутся")
        else:
            fail("запись не находится по ключу «TG key» — проверь имя свойства в базе")
            return 1

        print("\nВсё работает. Тестовую запись можно удалить в Notion:")
        print(f"  {page_url}")
        return 0

    finally:
        await notion.aclose()


class _Entity:
    """Заглушка сущности Telegram для проверки форматирования."""

    def __init__(self, type: str, offset: int, length: int, url: str | None = None):
        self.type = type
        self.offset = offset
        self.length = length
        self.url = url
        self.language = None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
