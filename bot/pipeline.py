"""Сборка страницы Notion из одного или нескольких сообщений Telegram."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import Message

from bot import formatting as fmt
from bot import notion as api
from bot import tg_files
from bot.article import Article, fetch_article
from bot.config import Settings
from bot.notion import NotionClient

log = logging.getLogger(__name__)

_ICONS = {
    "Фото": "\U0001f4f7",
    "Видео": "\U0001f3ac",
    "Файл": "\U0001f4ce",
    "Аудио": "\U0001f3a7",
    "Альбом": "\U0001f5bc",
    "Ссылка": "\U0001f517",
    "Текст": "\U0001f4dd",
}


@dataclass
class SaveResult:
    """Что сообщить пользователю после сохранения."""

    url: str
    title: str
    kind: str
    duplicate: bool = False
    notes: list[str] = field(default_factory=list)


def message_key(message: Message) -> str:
    """Ключ дедупликации: альбом считается одной записью."""
    suffix = message.media_group_id or message.message_id
    return f"{message.chat.id}:{suffix}"


def describe_source(message: Message) -> str:
    """Человекочитаемый источник сообщения."""
    origin = getattr(message, "forward_origin", None)

    if origin is None:
        return "Моя заметка"

    kind = getattr(origin, "type", None)
    kind = getattr(kind, "value", kind)

    if kind == "channel":
        chat = origin.chat
        title = chat.title or "канал"
        if chat.username:
            return f"Канал «{title}» · @{chat.username}"
        return f"Канал «{title}»"

    if kind == "chat":
        chat = origin.sender_chat
        return f"Чат «{chat.title or 'без названия'}»"

    if kind == "user":
        user = origin.sender_user
        name = " ".join(filter(None, [user.first_name, user.last_name])) or "без имени"
        if user.username:
            return f"{name} · @{user.username}"
        return name

    if kind == "hidden_user":
        return f"Скрытый отправитель ({origin.sender_user_name})"

    return "Telegram"


def original_link(message: Message) -> str | None:
    """Ссылка на исходное сообщение, если её вообще можно построить."""
    origin = getattr(message, "forward_origin", None)
    kind = getattr(getattr(origin, "type", None), "value", getattr(origin, "type", None))
    if kind != "channel":
        return None

    chat = origin.chat
    message_id = origin.message_id
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"

    # Приватный канал: ссылка вида t.me/c/<internal_id>/<msg> откроется
    # у того, кто на канал подписан — то есть у владельца бота.
    internal = str(chat.id)
    if internal.startswith("-100"):
        return f"https://t.me/c/{internal[4:]}/{message_id}"
    return None


def _message_text(message: Message) -> str:
    return message.text or message.caption or ""


def _message_entities(message: Message):
    return message.entities or message.caption_entities or []


def collect_text(messages: list[Message]) -> tuple[str, list]:
    """Текст альбома живёт в подписи к одному из сообщений — найдём его."""
    for message in messages:
        text = _message_text(message)
        if text:
            return text, _message_entities(message)
    return "", []


def _valid_url(url: str | None) -> str | None:
    if url and url.startswith(("http://", "https://")):
        return url
    return None


class Saver:
    """Превращает сообщения Telegram в страницы Notion."""

    def __init__(self, notion: NotionClient, settings: Settings):
        self._notion = notion
        self._settings = settings

    async def save(self, bot: Bot, messages: list[Message]) -> SaveResult:
        primary = messages[0]
        key = message_key(primary)

        existing = await self._notion.find_by_key(key)
        if existing:
            return SaveResult(
                url=existing,
                title="",
                kind="",
                duplicate=True,
            )

        text, entities = collect_text(messages)
        media = [ref for ref in (tg_files.extract_media(m) for m in messages) if ref]
        url = _valid_url(fmt.first_url(text, entities))
        source = describe_source(primary)
        kind = self._kind(media, url)

        article: Article | None = None
        if url and self._settings.fetch_articles:
            article = await fetch_article(
                url,
                timeout=self._settings.article_timeout,
                max_chars=self._settings.article_max_chars,
            )

        title = fmt.build_title(
            kind=kind,
            text=text,
            article_title=article.title if article else None,
            filename=media[0].filename if media else None,
            source=source,
        )

        blocks, notes = await self._body(bot, messages, text, entities, url, article, media)

        properties = {
            "Название": api.title_property(title),
            "Дата": api.date_property(primary.date),
            "Источник": api.text_property(source),
            "Тип": api.select_property(kind),
            "Ссылка": api.url_property(url),
            "Оригинал в TG": api.url_property(original_link(primary)),
            "TG key": api.text_property(key),
            "Обработано": api.checkbox_property(False),
        }

        head, *tail = fmt.batched(blocks) or [[]]
        page_id, page_url = await self._notion.create_page(
            properties, head, icon=_ICONS.get(kind)
        )
        for part in tail:
            await self._notion.append_blocks(page_id, part)

        log.info("Сохранено «%s» (%s) → %s", title, kind, page_url)
        return SaveResult(url=page_url, title=title, kind=kind, notes=notes)

    # ------------------------------------------------------------------

    @staticmethod
    def _kind(media: list[tg_files.MediaRef], url: str | None) -> str:
        if len(media) > 1:
            return "Альбом"
        if media:
            return media[0].kind_label
        if url:
            return "Ссылка"
        return "Текст"

    async def _body(
        self,
        bot: Bot,
        messages: list[Message],
        text: str,
        entities: list,
        url: str | None,
        article: Article | None,
        media: list[tg_files.MediaRef],
    ) -> tuple[list[dict], list[str]]:
        blocks: list[dict] = fmt.message_blocks(text, entities)
        notes: list[str] = []

        for ref in media:
            block, note = await self._media_block(bot, ref)
            blocks.append(block)
            if note:
                notes.append(note)

        if url:
            blocks.append(fmt.divider())
            blocks.append(fmt.bookmark(url))

        if article and article.description and not article.has_text:
            blocks.extend(fmt.plain_paragraphs(article.description))

        if article and article.has_text:
            blocks.append(fmt.heading("Содержимое статьи"))
            blocks.extend(fmt.plain_paragraphs(article.text or ""))
        elif article and article.error:
            note = f"Текст статьи не сохранён: {article.error}"
            blocks.append(fmt.callout(note, "\U0001f517"))
            notes.append(note)

        return blocks, notes

    async def _media_block(self, bot: Bot, ref: tg_files.MediaRef) -> tuple[dict, str | None]:
        result = await tg_files.download(bot, ref, self._notion.max_upload_bytes)

        if not result.ok:
            note = f"{ref.filename}: {result.reason}"
            return fmt.callout(f"Файл не загружен — {note}", "\U0001f4e6"), note

        try:
            uploaded = await self._notion.upload(ref.filename, ref.mime, result.data or b"")
        except api.NotionError as exc:
            note = f"{ref.filename}: Notion отклонил загрузку ({exc.notion_message})"
            log.warning(note)
            return fmt.callout(f"Файл не загружен — {note}", "\U0001f4e6"), note

        return api.file_block(ref.block_kind, uploaded.id), None
