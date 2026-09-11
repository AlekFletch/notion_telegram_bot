"""Сборка страницы Notion из одного или нескольких сообщений Telegram."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import Message

from bot import archive
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
    page_id: str = ""          # нужен, чтобы потом проставить категорию
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
    return archive.tg_link(chat.id, origin.message_id, chat.username)


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
            return SaveResult(url=existing, title="", kind="", duplicate=True)

        text, entities = collect_text(messages)
        # Пары, а не просто вложения: чтобы переслать файл в архив, нужно
        # исходное сообщение, а не только file_id.
        pairs = [(m, ref) for m in messages if (ref := tg_files.extract_media(m))]
        media = [ref for _, ref in pairs]
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

        blocks, notes = await self._body(bot, text, entities, url, article, pairs)

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
        return SaveResult(
            url=page_url, title=title, kind=kind, page_id=page_id, notes=notes
        )

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
        text: str,
        entities: list,
        url: str | None,
        article: Article | None,
        pairs: list[tuple[Message, tg_files.MediaRef]],
    ) -> tuple[list[dict], list[str]]:
        blocks: list[dict] = fmt.message_blocks(text, entities)
        notes: list[str] = []

        for message, ref in pairs:
            block, note = await self._media_block(bot, message, ref)
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

    async def _media_block(
        self, bot: Bot, message: Message, ref: tg_files.MediaRef
    ) -> tuple[dict, str | None]:
        """Блок для вложения: файл внутри страницы или ссылка на архив.

        Видео идёт в архив сразу — скачивать его почти всегда бессмысленно,
        Bot API не отдаёт больше 20 МБ. Всё остальное сначала пробует попасть
        внутрь страницы, а в архив уходит только если не получилось.
        """
        archived = False
        archive_reason: str | None = None

        if ref.block_kind == "video":
            block, archive_reason = await self._archive_block(bot, message, ref)
            if block:
                return block, None
            archived = True   # второй раз пересылать то же самое незачем

        result = await tg_files.download(bot, ref, self._notion.max_upload_bytes)
        reason = result.reason or ""

        if result.ok:
            try:
                uploaded = await self._notion.upload(
                    ref.filename, ref.mime, result.data or b""
                )
                return api.file_block(ref.block_kind, uploaded.id), None
            except api.NotionError as exc:
                log.warning("Notion отклонил %s: %s", ref.filename, exc.notion_message)
                reason = f"Notion отклонил загрузку ({exc.notion_message})"

        # Внутрь страницы файл не попал — пробуем хотя бы сохранить ссылку.
        if not archived:
            block, archive_reason = await self._archive_block(bot, message, ref)
            if block:
                return block, None

        note = f"{ref.filename}: {reason}"
        # Почему не спас и архив — тоже пишем в заметку, иначе «бот не админ
        # канала» будет видно только в логах Render.
        if archive_reason:
            note = f"{note}; в архив тоже не ушло — {archive_reason}"
        return fmt.callout(f"Файл не загружен — {note}", "\U0001f4e6"), note

    async def _archive_block(
        self, bot: Bot, message: Message, ref: tg_files.MediaRef
    ) -> tuple[dict | None, str | None]:
        """Переслать вложение в канал-архив и вернуть карточку со ссылкой."""
        chat_id = self._settings.archive_chat_id
        if not chat_id:
            return None, None

        stored = await archive.store(bot, message, chat_id)
        if not stored.ok:
            return None, stored.reason

        icon = "\U0001f3ac" if ref.block_kind == "video" else "\U0001f4ce"
        caption = f"{ref.filename} ({ref.human_size}) — смотреть в Telegram"
        return fmt.link_callout(caption, stored.url or "", icon), None
