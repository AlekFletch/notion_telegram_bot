"""Команды бота и обработка пересланных сообщений."""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import categories
from bot.albums import AlbumBuffer
from bot.categories import CATEGORIES
from bot.config import Settings
from bot.notion import NotionClient, NotionError
from bot.pipeline import SaveResult, Saver
from bot.session import no_retry

log = logging.getLogger(__name__)

router = Router(name="inbox")

HELP = (
    "<b>Что я умею</b>\n\n"
    "Перешли мне любое сообщение — текст, ссылку, фото, альбом, файл, "
    "голосовое — и оно станет отдельной страницей в базе Notion.\n\n"
    "• Форматирование, ссылки и код сохраняются\n"
    "• Из ссылки подтягиваю заголовок и текст статьи\n"
    "• Повторная пересылка того же сообщения не создаёт дубль\n"
    "• Видео уезжает в канал-архив, а в Notion попадает ссылка на него — "
    "так сохраняются ролики любого размера\n"
    "• Под каждой записью — кнопки категорий. Можно нажать сразу, "
    "можно позже: сообщение с кнопками никуда не денется\n\n"
    "<b>Команды</b>\n"
    "/last — последняя запись и кнопки категорий к ней\n"
    "/ping — проверить связь с Notion\n"
    "/id — показать твой Telegram ID\n"
    "/help — эта справка"
)


#: Префикс callback_data. Вся строка обязана уместиться в 64 байта:
#: «c:<индекс>:<32 hex страницы>» — с запасом.
CALLBACK_PREFIX = "c"
BUTTONS_PER_ROW = 2

#: Сколько ждать отправку служебного «Сохраняю…», прежде чем махнуть рукой.
STATUS_WAIT = 15.0


def _keyboard(
    url: str,
    page_id: str = "",
    chosen: int | None = None,
) -> InlineKeyboardMarkup | None:
    """Кнопки под сохранённой записью: категории плюс ссылка на Notion.

    Выбранная категория помечается галочкой и остаётся на месте — передумал,
    нажал другую, значение перезапишется.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if page_id:
        short = page_id.replace("-", "")
        buttons = [
            InlineKeyboardButton(
                text=("✓ " + category.name) if index == chosen else category.button,
                callback_data=f"{CALLBACK_PREFIX}:{index}:{short}",
            )
            for index, category in enumerate(CATEGORIES)
        ]
        rows = [
            buttons[i:i + BUTTONS_PER_ROW]
            for i in range(0, len(buttons), BUTTONS_PER_ROW)
        ]

    if url:
        rows.append([InlineKeyboardButton(text="Открыть в Notion", url=url)])

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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
async def ping(message: Message, bot: Bot, settings: Settings, notion: NotionClient) -> None:
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
        f"Статьи по ссылкам: <b>{'да' if settings.fetch_articles else 'нет'}</b>\n"
        f"{await _archive_status(bot, settings)}"
    )


async def _archive_status(bot: Bot, settings: Settings) -> str:
    """Строка для /ping: виден ли боту канал-архив."""
    if not settings.archive_chat_id:
        return (
            "Архив видео: <b>не настроен</b> — видео крупнее 20 МБ сохранить нельзя. "
            "Создай приватный канал, добавь меня админом и впиши его ID "
            "в <code>ARCHIVE_CHAT_ID</code>."
        )

    try:
        chat = await bot.get_chat(settings.archive_chat_id)
    except Exception as exc:  # noqa: BLE001 — нас интересует любой отказ
        log.warning("Архив недоступен: %s", exc)
        return (
            "Архив видео: <b>бот не видит канал</b> — проверь, что он добавлен "
            "туда администратором."
        )

    return f"Архив видео: <b>{html.escape(chat.title or 'канал')}</b>"


@router.channel_post(Command("id"))
async def channel_id(message: Message) -> None:
    """Узнать ID канала-архива, не привлекая сторонних ботов.

    Бот получает посты канала, только если он там администратор — то есть
    ровно в том состоянии, которое и нужно архиву.
    """
    await message.answer(
        f"ID этого канала: <code>{message.chat.id}</code>\n\n"
        "Впиши его в переменную <code>ARCHIVE_CHAT_ID</code>."
    )


@router.message(Command("last"))
async def last_command(message: Message, settings: Settings, notion: NotionClient) -> None:
    """Последняя сохранённая запись с кнопками категорий.

    Спасательный круг на случай, когда ответ бота не дошёл из-за связи:
    запись в Notion уже есть, а кнопки к ней вызываются заново.
    """
    if not _allowed(message, settings):
        await _deny(message)
        return

    try:
        latest = await notion.latest()
    except NotionError as exc:
        await message.answer(_notion_error_text(exc))
        return

    if latest is None:
        await message.answer("В базе пока пусто.")
        return

    chosen = categories.index_of(latest.category) if latest.category else None
    await message.answer(
        f"🗂 Последняя запись: <b>{html.escape(latest.title)}</b>",
        reply_markup=_keyboard(latest.url, latest.page_id, chosen),
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

    # «Сохраняю…» — вещь косметическая, и её нельзя ставить на критический путь:
    # при обрыве связи повторы этого сообщения задерживали само сохранение на
    # минуты. Отправляем параллельно и не ждём.
    status_task = asyncio.create_task(_send_status(primary))

    try:
        result = await saver.save(bot, messages)
    except NotionError as exc:
        await _report(await _status(status_task), primary, _notion_error_text(exc))
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("Сохранение не удалось")
        await _report(
            await _status(status_task), primary,
            f"⚠️ Не удалось сохранить: <code>{html.escape(str(exc)[:300])}</code>",
        )
        return

    await _report(
        await _status(status_task), primary, _success_text(result),
        _keyboard(result.url, result.page_id),
    )


async def _send_status(message: Message) -> Message | None:
    """Одна попытка без повторов: сообщение одноразовое, догонять его нечего."""
    try:
        with no_retry():
            return await message.reply("⏳ Сохраняю в Notion…")
    except Exception:  # noqa: BLE001
        log.info("Статус-сообщение не ушло — не страшно, ответ придёт отдельным")
        return None


async def _status(task: asyncio.Task) -> Message | None:
    """Забрать отправленный статус, чтобы отредактировать его в ответ.

    Если к этому моменту он всё ещё в пути — отменяем: лучше прислать ответ
    новым сообщением, чем оставить на экране вечное «Сохраняю…».
    """
    try:
        return await asyncio.wait_for(task, timeout=STATUS_WAIT)
    except asyncio.TimeoutError:
        task.cancel()
        return None
    except Exception:  # noqa: BLE001
        return None


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


#: Паузы между попытками доставить ответ, секунды. Провалы связи с Telegram
#: длятся минутами, а ответ с кнопками — единственный способ выбрать
#: категорию, поэтому ждём окно связи вместо того, чтобы сдаться за минуту.
DELIVERY_DELAYS = (15, 30, 60, 120, 180, 300)


async def _report(
    status: Message | None,
    fallback: Message,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Доставить ответ, переживая провалы связи.

    Запись в Notion к этому моменту уже сохранена, теряется только ответ —
    поэтому пробуем долго и в фоне, чтобы не держать обработчик.
    """
    async def attempt() -> None:
        if status:
            await status.edit_text(text, reply_markup=keyboard)
        else:
            await fallback.reply(text, reply_markup=keyboard)

    try:
        await attempt()
        return
    except TelegramBadRequest as exc:
        # Дело не в связи: сообщение слишком длинное, разметка битая и т.п.
        # Повторять нечего, но и падать смысла нет — запись уже сохранена.
        log.warning("Telegram отклонил ответ: %s", exc)
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("Ответ не ушёл (%s), буду пробовать ещё", exc)

    asyncio.create_task(_deliver_later(attempt))


async def _deliver_later(attempt) -> None:
    for delay in DELIVERY_DELAYS:
        await asyncio.sleep(delay)
        try:
            await attempt()
            log.info("Ответ доставлен со второй попытки, спустя паузу")
            return
        except TelegramBadRequest as exc:
            log.warning("Ответ отклонён Telegram, повторять не буду: %s", exc)
            return
        except Exception as exc:  # noqa: BLE001
            log.info("Ответ снова не ушёл (%s)", exc)

    log.error(
        "Ответ так и не доставлен. Запись в Notion на месте — "
        "вызови /last, чтобы получить кнопки категорий"
    )


@router.callback_query(F.data.startswith(CALLBACK_PREFIX + ":"))
async def choose_category(
    query: CallbackQuery,
    settings: Settings,
    notion: NotionClient,
) -> None:
    """Нажатие на кнопку категории под сохранённой записью."""
    user = query.from_user
    if not settings.setup_mode and (not user or user.id not in settings.allowed_ids):
        await query.answer("Этот бот личный.", show_alert=True)
        return

    try:
        _, raw_index, page_id = (query.data or "").split(":", 2)
        category = categories.by_index(int(raw_index))
    except (ValueError, TypeError):
        category = None

    if category is None:
        await query.answer("Неизвестная категория", show_alert=True)
        return

    try:
        await notion.set_select(page_id, categories.PROPERTY, category.name)
    except NotionError as exc:
        log.warning("Не удалось проставить категорию: %s", exc)
        await query.answer("Notion не принял категорию", show_alert=True)
        return

    await query.answer(f"Категория: {category.name}")

    # Перерисовываем клавиатуру с галочкой. Если пользователь нажал ту же
    # кнопку повторно, Telegram ответит «message is not modified» — не ошибка.
    url = _url_from(query.message)
    try:
        await query.message.edit_reply_markup(
            reply_markup=_keyboard(url, page_id, int(raw_index))
        )
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            log.warning("Не удалось обновить клавиатуру: %s", exc)


def _url_from(message: Message | None) -> str:
    """Достать ссылку на Notion из уже отправленной клавиатуры."""
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", []) or []:
        for button in row:
            if button.url:
                return button.url
    return ""


async def _deny(message: Message) -> None:
    user = message.from_user
    log.warning(
        "Отказано в доступе: id=%s username=%s",
        getattr(user, "id", None),
        getattr(user, "username", None),
    )
    await message.answer("Этот бот личный и работает только для своего владельца.")
