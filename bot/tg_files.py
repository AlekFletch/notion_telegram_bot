"""Разбор вложений Telegram и их скачивание.

Два независимых потолка, которые важно не путать:
  * Bot API отдаёт через getFile файлы не больше 20 МБ — это ограничение
    Telegram, обойти его обычным ботом нельзя;
  * Notion принимает файл не больше лимита воркспейса (5 МБ на free-плане).
Файл сохраняется, только если проходит оба.
"""
from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

log = logging.getLogger(__name__)

#: Жёсткий предел Bot API на скачивание файла.
BOT_API_FILE_LIMIT = 20 * 1024 * 1024

#: Расширение → (тип блока Notion, MIME). Notion сверяет расширение с типом
#: блока, поэтому имя файла всегда должно оканчиваться чем-то осмысленным.
_EXTRA_MIME = {
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".webp": "image/webp",
    ".webm": "video/webm",
    ".m4a": "audio/mp4",
    ".heic": "image/heic",
    ".avif": "image/avif",
    ".tgs": "application/gzip",
}

#: Расширения, которые Notion принимает в блоках соответствующего типа.
_IMAGE_EXT = frozenset({".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp", ".ico", ".avif"})
_VIDEO_EXT = frozenset({".amv", ".asf", ".avi", ".f4v", ".flv", ".gifv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".qt", ".wmv", ".webm"})
_AUDIO_EXT = frozenset({".aac", ".adts", ".mid", ".midi", ".mp3", ".mpga", ".m4a", ".m4b", ".oga", ".ogg", ".wav", ".wma"})


@dataclass
class MediaRef:
    """Вложение сообщения, приведённое к виду, понятному Notion."""

    file_id: str
    block_kind: str          # image / video / audio / pdf / file
    kind_label: str          # значение свойства «Тип» в базе
    filename: str
    mime: str
    size: int = 0

    @property
    def human_size(self) -> str:
        if self.size <= 0:
            return "размер неизвестен"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.0f} КБ"
        return f"{self.size / 1024 / 1024:.1f} МБ"


@dataclass
class Download:
    """Результат попытки скачать вложение."""

    data: bytes | None = None
    reason: str | None = None   # почему не получилось, текстом для карточки

    @property
    def ok(self) -> bool:
        return self.data is not None


def extract_media(message: Message) -> MediaRef | None:
    """Достать из сообщения единственное вложение, если оно есть."""
    if message.photo:
        largest = message.photo[-1]  # последний размер — самый крупный
        return MediaRef(
            file_id=largest.file_id,
            block_kind="image",
            kind_label="Фото",
            filename=f"photo_{largest.file_unique_id}.jpg",
            mime="image/jpeg",
            size=largest.file_size or 0,
        )

    if message.video:
        video = message.video
        return MediaRef(
            file_id=video.file_id,
            block_kind="video",
            kind_label="Видео",
            filename=video.file_name or f"video_{video.file_unique_id}.mp4",
            mime=video.mime_type or "video/mp4",
            size=video.file_size or 0,
        )

    if message.animation:
        anim = message.animation
        return MediaRef(
            file_id=anim.file_id,
            block_kind="video",
            kind_label="Видео",
            filename=anim.file_name or f"animation_{anim.file_unique_id}.mp4",
            mime=anim.mime_type or "video/mp4",
            size=anim.file_size or 0,
        )

    if message.video_note:
        note = message.video_note
        return MediaRef(
            file_id=note.file_id,
            block_kind="video",
            kind_label="Видео",
            filename=f"video_note_{note.file_unique_id}.mp4",
            mime="video/mp4",
            size=note.file_size or 0,
        )

    if message.voice:
        voice = message.voice
        return MediaRef(
            file_id=voice.file_id,
            block_kind="audio",
            kind_label="Аудио",
            filename=f"voice_{voice.file_unique_id}.oga",
            mime=voice.mime_type or "audio/ogg",
            size=voice.file_size or 0,
        )

    if message.audio:
        audio = message.audio
        name = audio.file_name or f"{audio.performer or 'audio'} - {audio.title or audio.file_unique_id}.mp3"
        return MediaRef(
            file_id=audio.file_id,
            block_kind="audio",
            kind_label="Аудио",
            filename=name,
            mime=audio.mime_type or "audio/mpeg",
            size=audio.file_size or 0,
        )

    if message.sticker:
        sticker = message.sticker
        if sticker.is_animated or sticker.is_video:
            # .tgs и .webm-стикеры Notion не покажет — кладём как файл.
            ext = ".webm" if sticker.is_video else ".tgs"
            kind, label = ("video", "Видео") if sticker.is_video else ("file", "Файл")
        else:
            ext, kind, label = ".webp", "image", "Фото"
        return MediaRef(
            file_id=sticker.file_id,
            block_kind=kind,
            kind_label=label,
            filename=f"sticker_{sticker.file_unique_id}{ext}",
            mime=_EXTRA_MIME.get(ext, "application/octet-stream"),
            size=sticker.file_size or 0,
        )

    if message.document:
        document = message.document
        name = document.file_name or f"file_{document.file_unique_id}"
        mime = document.mime_type or _guess_mime(name)
        return MediaRef(
            file_id=document.file_id,
            block_kind=_block_kind_for(name, mime),
            kind_label=_label_for(name, mime),
            filename=name,
            mime=mime,
            size=document.file_size or 0,
        )

    return None


def _extension(filename: str) -> str:
    _, _, ext = filename.rpartition(".")
    return f".{ext.lower()}" if ext and ext != filename else ""


def _guess_mime(filename: str) -> str:
    ext = _extension(filename)
    if ext in _EXTRA_MIME:
        return _EXTRA_MIME[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _block_kind_for(filename: str, mime: str) -> str:
    """Тип блока Notion. Notion сверяет расширение с типом, поэтому решает оно."""
    ext = _extension(filename)
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _AUDIO_EXT:
        return "audio"
    return "file"


def _label_for(filename: str, mime: str) -> str:
    kind = _block_kind_for(filename, mime)
    return {"image": "Фото", "video": "Видео", "audio": "Аудио"}.get(kind, "Файл")


async def download(bot: Bot, media: MediaRef, max_bytes: int) -> Download:
    """Скачать вложение, если оно проходит по обоим лимитам."""
    limit = min(max_bytes, BOT_API_FILE_LIMIT)

    if media.size and media.size > limit:
        return Download(reason=_limit_reason(media, max_bytes))

    try:
        file = await bot.get_file(media.file_id)
    except TelegramBadRequest as exc:
        if "too big" in str(exc).lower():
            return Download(reason=f"Telegram не отдаёт боту файлы больше 20 МБ ({media.human_size})")
        log.warning("getFile не удался для %s: %s", media.filename, exc)
        return Download(reason="Telegram не отдал файл")

    size = file.file_size or media.size
    if size and size > limit:
        media.size = size
        return Download(reason=_limit_reason(media, max_bytes))

    if not file.file_path:
        return Download(reason="Telegram не вернул путь к файлу")

    try:
        buffer = await bot.download_file(file.file_path)
    except Exception as exc:  # noqa: BLE001 — сеть, таймауты, битые ссылки
        log.warning("Скачивание %s не удалось: %s", media.filename, exc)
        return Download(reason="не удалось скачать файл из Telegram")

    data = buffer.read() if hasattr(buffer, "read") else bytes(buffer)
    media.size = len(data)

    if len(data) > max_bytes:
        return Download(reason=_limit_reason(media, max_bytes))

    return Download(data=data)


def _limit_reason(media: MediaRef, notion_limit: int) -> str:
    if media.size > BOT_API_FILE_LIMIT:
        return f"{media.human_size} — Telegram не отдаёт боту файлы больше 20 МБ"
    return (
        f"{media.human_size} — больше лимита Notion "
        f"({notion_limit / 1024 / 1024:.0f} МБ на текущем тарифе)"
    )
