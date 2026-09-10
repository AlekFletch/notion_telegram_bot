"""Команды бота и обработка пересланных сообщений."""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.albums import AlbumBuffer
from bot.config import Settings
from bot.notion import NotionClient, NotionError
from bot.pipeline import SaveResult, Saver

log = logging.getLogger(__name__)

router = Router(name="inbox")

HELP = (
    "<b>Что я умею</b>\n\n"
    "Перешли мне любое сообщение — текст, ссылку, фото, альбом, файл, "
    "голосовое — и оно станет отдельной страницей в базе Notion.\n\n"
    "• Форматирование, ссылки и код сохраняются\n"
    "• Из ссылки подтягиваю заголовок и текст статьи\n"
    "• Повторная пересылка того же сообщения не создаёт дубль\n\n"
    "<b>Команды</b>\n"
    "/ping — проверить связь с Notion\n"
    "/id — показать твой Telegram ID\n"
    "/help — эта справка"
)


def _keyboard(url: str) -> InlineKeyboardMarkup | None:
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть в Notion", url=url)]]
    )


def _allowed(message: Message, settings: Settings) -> bool:
    if settings.setup_mode:
        return True
    user = message.from_user
    return bool(user and user.id in settings.allowed_ids)


@router.message(CommandStart())
async def start(message: Message, settings: Settings) -> None:
    user_id = message.from_user.id if message.from_user else "неизвестен"

    if settings.setup_mode:
        await message.answer(
            "👋 Бот запущен, но <b>ещё не привязан к владельцу</b> — сейчас "
            "он принимает сообщения от кого угодно.\n\n"
            f"Твой Telegram ID: <code>{user_id}</code>\n\n"
            "Впиши его в переменную <code>ALLOWED_USER_IDS</code> и перезапусти "
            "бота — после этого он будет слушать только тебя.",
        )
        return

    if not _allowed(message, settings):
        await _deny(message)
        return

    await message.answer(f"👋 Готов к работе.\n\n{HELP}")


@router.message(Command("help"))
async def help_command(message: Message, settings: Settings) -> None:
    if not _allowed(message, settings):
        await _deny(message)
        return
    await message.answer(HELP)


@router.message(Command("id"))
async def id_command(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "неизвестен"
    await message.answer(f"Твой Telegram ID: <code>{user_id}</code>")


@router.message(Command("ping"))
async def ping(message: Message, settings: Settings, notion: NotionClient) -> None:
    if not _allowed(message, settings):
        await _deny(message)
        return

    try:
        await notion.prepare()
    except NotionError as exc:
        await message.answer(
            "❌ Notion недоступен.\n"
            f"<code>{html.escape(str(exc))}</code>\n\n"
            "Чаще всего это значит, что страница «Telegram Inbox» не подключена "
            "к интеграции: открой её → меню <b>⋯</b> → <b>Connections</b>."
        )
        return

    limit_mb = notion.max_upload_bytes / 1024 / 1024
    await message.answer(
        "✅ Notion на связи.\n"
        f"Лимит на файл: <b>{limit_mb:.0f} МБ</b>\n"
        f"Статьи по ссылкам: <b>{'да' if settings.fetch_articles else 'нет'}</b>"
    )


@router.message(F.text | F.caption | F.photo | F.video | F.document | F.audio | F.voice | F.animation | F.video_note | F.sticker)
async def save_message(
    message: Message,
    bot: Bot,
    settings: Settings,
    saver: Saver,
    album: AlbumBuffer,
) -> None:
    if not _allowed(message, settings):
        await _deny(message)
        return

    async def flush(messages: list[Message]) -> None:
        await _save_and_reply(bot, messages, saver)

    await album.add(message, flush)


async def _save_and_reply(bot: Bot, messages: list[Message], saver: Saver) -> None:
    primary = messages[0]
    status: Message | None = None
    try:
        status = await primary.reply("⏳ Сохраняю в Notion…")
    except Exception:  # noqa: BLE001 — не смогли ответить, но сохранить всё равно надо
        log.warning("Не удалось отправить статус-сообщение", exc_info=True)

    try:
        result = await saver.save(bot, messages)
    except NotionError as exc:
        await _report(status, primary, _notion_error_text(exc))
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("Сохранение не удалось")
        await _report(
            status, primary,
            f"⚠️ Не удалось сохранить: <code>{html.escape(str(exc)[:300])}</code>",
        )
        return

    await _report(status, primary, _success_text(result), _keyboard(result.url))


def _success_text(result: SaveResult) -> str:
    if result.duplicate:
        return "↩️ Это сообщение уже сохранено — дубль создавать не стал."

    lines = [f"✅ Сохранено: <b>{html.escape(result.title)}</b>"]
    if result.notes:
        lines.append("")
        lines.extend(f"⚠️ {html.escape(note)}" for note in result.notes)
    return "\n".join(lines)


def _notion_error_text(exc: NotionError) -> str:
    if exc.status == 404:
        return (
            "❌ Notion не видит базу.\n\n"
            "Открой страницу «Telegram Inbox» в Notion → меню <b>⋯</b> → "
            "<b>Connections</b> → добавь свою интеграцию."
        )
    if exc.status == 401:
        return "❌ Notion отклонил токен. Проверь <code>NOTION_TOKEN</code>."
    return f"❌ Notion вернул ошибку:\n<code>{html.escape(str(exc)[:300])}</code>"


async def _report(
    status: Message | None,
    fallback: Message,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        if status:
            await status.edit_text(text, reply_markup=keyboard)
        else:
            await fallback.reply(text, reply_markup=keyboard)
    except Exception:  # noqa: BLE001
        log.warning("Не удалось отправить ответ пользователю", exc_info=True)


async def _deny(message: Message) -> None:
    user = message.from_user
    log.warning(
        "Отказано в доступе: id=%s username=%s",
        getattr(user, "id", None),
        getattr(user, "username", None),
    )
    await message.answer("Этот бот личный и работает только для своего владельца.")
