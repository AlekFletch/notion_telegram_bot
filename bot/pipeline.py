"""Сборка страницы Notion из одного или нескольких сообщений Telegram."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import Message

from bot import archive
from bot import formatting as fmt
from bot import notion as api
from bot import social
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


def _post_title(post: social.Post | None) -> str | None:
    """Заголовок поста: своё название либо первая строка подписи."""
    if post is None:
        return None
    if post.title:
        return post.title
    if post.description:
        return post.description.strip().splitlines()[0]
    return None


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

        post = await self._fetch_post(url)
        kind = self._kind(media, url, post)

        # На Instagram и Facebook trafilatura тратить нечего: страницы
        # анонимному запросу не отдают ни текста, ни мета-тегов.
        article: Article | None = None
        if url and self._settings.fetch_articles and post is None:
            article = await fetch_article(
                url,
                timeout=self._settings.article_timeout,
                max_chars=self._settings.article_max_chars,
            )

        title = fmt.build_title(
            kind=kind,
            text=text,
            article_title=_post_title(post) or (article.title if article else None),
            filename=media[0].filename if media else None,
            source=source,
        )

        blocks, notes = await self._body(bot, text, entities, url, article, pairs, post)

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

    async def _fetch_post(self, url: str | None) -> social.Post | None:
        """Скачать пост из соцсети, если ссылка ведёт на поддерживаемый сайт."""
        settings = self._settings
        if not url or not settings.social_download:
            return None
        if not social.is_supported(url, settings.social_domains):
            return None

        return await social.fetch_post(
            url,
            cookies_file=settings.social_cookies_file,
            max_items=settings.social_max_items,
            timeout=settings.social_timeout,
        )

    @staticmethod
    def _kind(
        media: list[tg_files.MediaRef],
        url: str | None = None,
        post: social.Post | None = None,
    ) -> str:
        if len(media) > 1:
            return "Альбом"
        if media:
            return media[0].kind_label
        if post and post.ok:
            if len(post.items) > 1:
                return "Альбом"
            return "Видео" if post.items[0].is_video else "Фото"
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
        post: social.Post | None = None,
    ) -> tuple[list[dict], list[str]]:
        blocks: list[dict] = fmt.message_blocks(text, entities)
        notes: list[str] = []

        for message, ref in pairs:
            block, note = await self._media_block(bot, message, ref)
            blocks.append(block)
            if note:
                notes.append(note)

        if post is not None:
            post_blocks, post_notes = await self._post_blocks(bot, post)
            blocks.extend(post_blocks)
            notes.extend(post_notes)

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

    async def _post_blocks(
        self, bot: Bot, post: social.Post
    ) -> tuple[list[dict], list[str]]:
        """Подпись поста и ссылки на его вложения, уехавшие в архив."""
        blocks: list[dict] = []
        notes: list[str] = []

        if post.uploader:
            blocks.append(fmt.callout(f"Автор: {post.uploader}", "\U0001f464"))

        if post.description:
            blocks.extend(fmt.plain_paragraphs(post.description))

        chat_id = self._settings.archive_chat_id
        caption = f"{post.uploader or 'Пост'} · {post.url}"

        for number, item in enumerate(post.items, start=1):
            stored = await archive.upload(
                bot, chat_id, item.data, item.filename,
                is_video=item.is_video, caption=caption,
            )
            label = f"Вложение {number} из {len(post.items)}" if len(post.items) > 1 else "Вложение"
            if stored.ok:
                icon = "\U0001f3ac" if item.is_video else "\U0001f5bc"
                blocks.append(fmt.link_callout(
                    f"{label} — смотреть в Telegram", stored.url or "", icon
                ))
            else:
                note = f"{label} не сохранено — {stored.reason}"
                blocks.append(fmt.callout(note, "\U0001f4e6"))
                notes.append(note)

        if post.skipped:
            note = f"Вложений пропущено по размеру: {post.skipped}"
            blocks.append(fmt.callout(note, "\U0001f4e6"))
            notes.append(note)

        if not post.ok and post.error:
            note = f"Пост не скачан: {post.error}"
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
