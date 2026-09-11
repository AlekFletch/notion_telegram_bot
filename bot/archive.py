"""Архив вложений в приватном канале Telegram.

Зачем это нужно: Bot API не отдаёт боту файлы больше 20 МБ, а Notion на
бесплатном плане принимает 5 МБ. Обычное видео не проходит ни то, ни другое —
скачать его нельзя в принципе. Но переслать можно: forwardMessage не качает
файл, Telegram копирует его у себя, и ограничение на скачивание не действует.

Поэтому видео уезжает в канал-архив, а в заметку Notion попадает ссылка на
пересланное сообщение.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile, Message

log = logging.getLogger(__name__)

#: Подпись к сообщению в Telegram — 1024 символа.
CAPTION_LIMIT = 1024


@dataclass
class Stored:
    """Результат попытки отправить вложение в архив."""

    url: str | None = None
    reason: str | None = None   # почему не получилось, текстом для карточки

    @property
    def ok(self) -> bool:
        return bool(self.url)


def tg_link(chat_id: int | str, message_id: int, username: str | None = None) -> str | None:
    """Ссылка на сообщение в Telegram.

    У публичного канала это t.me/<username>/<id>. У приватного — t.me/c/<id>/<msg>,
    где из внутреннего идентификатора отрезан префикс -100; такая ссылка
    открывается у того, кто на канал подписан.
    """
    if username:
        return f"https://t.me/{username}/{message_id}"

    internal = str(chat_id)
    if internal.startswith("-100"):
        return f"https://t.me/c/{internal[4:]}/{message_id}"
    return None


async def store(bot: Bot, message: Message, chat_id: int) -> Stored:
    """Переслать сообщение в канал-архив и вернуть ссылку на копию."""
    if not chat_id:
        return Stored(reason="архив не настроен")

    try:
        copy = await bot.forward_message(
            chat_id=chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramForbiddenError as exc:
        log.warning("Архив недоступен (%s): %s", chat_id, exc)
        return Stored(reason="бот не может писать в канал-архив")
    except TelegramBadRequest as exc:
        text = str(exc).lower()
        if "protected" in text or ("forward" in text and "restrict" in text):
            return Stored(reason="исходный чат запрещает пересылку")
        log.warning("Пересылка в архив не удалась (%s): %s", chat_id, exc)
        return Stored(reason="Telegram не принял пересылку в архив")
    except Exception as exc:  # noqa: BLE001 — сеть отвалилась даже после повторов
        # Как и при скачивании: сообщение важнее вложения.
        log.warning("Пересылка в архив не удалась (%s): %s", chat_id, exc)
        return Stored(reason="не удалось переслать в архив (проблемы со связью)")

    url = tg_link(chat_id, copy.message_id)
    if not url:
        return Stored(reason="не удалось построить ссылку на архив")
    return Stored(url=url)


async def upload(
    bot: Bot,
    chat_id: int,
    data: bytes,
    filename: str,
    *,
    is_video: bool = False,
    caption: str = "",
) -> Stored:
    """Положить в архив файл, скачанный не из Telegram, и вернуть ссылку.

    Отличается от [store](archive.py) тем, что пересылать нечего: файл пришёл
    из внешнего источника, и его надо именно отправить.
    """
    if not chat_id:
        return Stored(reason="архив не настроен")

    payload = BufferedInputFile(data, filename=filename)
    short = caption[:CAPTION_LIMIT] if caption else None

    try:
        if is_video:
            sent = await bot.send_video(
                chat_id, video=payload, caption=short, supports_streaming=True
            )
        else:
            sent = await bot.send_document(chat_id, document=payload, caption=short)
    except TelegramForbiddenError as exc:
        log.warning("Архив недоступен (%s): %s", chat_id, exc)
        return Stored(reason="бот не может писать в канал-архив")
    except TelegramBadRequest as exc:
        log.warning("Отправка в архив не удалась (%s): %s", chat_id, exc)
        if "too large" in str(exc).lower():
            return Stored(reason="файл больше 50 МБ — столько бот отправить не может")
        return Stored(reason="Telegram не принял файл в архив")
    except Exception as exc:  # noqa: BLE001 — сеть отвалилась даже после повторов
        log.warning("Отправка в архив не удалась (%s): %s", chat_id, exc)
        return Stored(reason="не удалось отправить в архив (проблемы со связью)")

    url = tg_link(chat_id, sent.message_id)
    if not url:
        return Stored(reason="не удалось построить ссылку на архив")
    return Stored(url=url)
