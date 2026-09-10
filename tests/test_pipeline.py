"""Тесты разбора сообщений: источник, ссылка на оригинал, тип записи."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram.types import (
    Chat,
    Document,
    Message,
    MessageOriginChannel,
    MessageOriginHiddenUser,
    MessageOriginUser,
    PhotoSize,
    User,
)

from bot import pipeline, tg_files

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
ME = Chat(id=555, type="private")


def make_message(message_id: int = 1, **kwargs) -> Message:
    return Message(message_id=message_id, date=NOW, chat=ME, **kwargs)


def channel_origin(*, username: str | None, chat_id: int = -1001234567890, message_id: int = 42):
    return MessageOriginChannel(
        type="channel",
        date=NOW,
        chat=Chat(id=chat_id, type="channel", title="Полезное", username=username),
        message_id=message_id,
    )


def photo(unique: str = "abc", size: int = 1000) -> list[PhotoSize]:
    return [
        PhotoSize(file_id=f"small_{unique}", file_unique_id=f"su_{unique}", width=90, height=90, file_size=100),
        PhotoSize(file_id=f"big_{unique}", file_unique_id=f"bu_{unique}", width=1280, height=720, file_size=size),
    ]


# --------------------------------------------------------------------------
# Ключ дедупликации
# --------------------------------------------------------------------------

def test_key_uses_message_id_for_single_message():
    assert pipeline.message_key(make_message(7)) == "555:7"


def test_album_messages_share_one_key():
    first = make_message(7, media_group_id="MG1", photo=photo("a"))
    second = make_message(8, media_group_id="MG1", photo=photo("b"))
    assert pipeline.message_key(first) == pipeline.message_key(second) == "555:MG1"


# --------------------------------------------------------------------------
# Источник
# --------------------------------------------------------------------------

def test_own_note_has_no_forward_origin():
    assert pipeline.describe_source(make_message(text="заметка")) == "Моя заметка"


def test_public_channel_source_includes_username():
    message = make_message(forward_origin=channel_origin(username="useful"))
    assert pipeline.describe_source(message) == "Канал «Полезное» · @useful"


def test_private_channel_source_without_username():
    message = make_message(forward_origin=channel_origin(username=None))
    assert pipeline.describe_source(message) == "Канал «Полезное»"


def test_user_source_uses_full_name():
    origin = MessageOriginUser(
        type="user",
        date=NOW,
        sender_user=User(id=9, is_bot=False, first_name="Иван", last_name="Петров", username="ivan"),
    )
    assert pipeline.describe_source(make_message(forward_origin=origin)) == "Иван Петров · @ivan"


def test_hidden_user_is_labelled_explicitly():
    origin = MessageOriginHiddenUser(type="hidden_user", date=NOW, sender_user_name="Аноним")
    assert pipeline.describe_source(make_message(forward_origin=origin)) == "Скрытый отправитель (Аноним)"


# --------------------------------------------------------------------------
# Ссылка на оригинал
# --------------------------------------------------------------------------

def test_public_channel_link():
    message = make_message(forward_origin=channel_origin(username="useful", message_id=42))
    assert pipeline.original_link(message) == "https://t.me/useful/42"


def test_private_channel_link_uses_internal_form():
    message = make_message(forward_origin=channel_origin(username=None, chat_id=-1001234567890))
    assert pipeline.original_link(message) == "https://t.me/c/1234567890/42"


def test_no_link_for_forwarded_user_message():
    origin = MessageOriginUser(
        type="user", date=NOW, sender_user=User(id=9, is_bot=False, first_name="Иван")
    )
    assert pipeline.original_link(make_message(forward_origin=origin)) is None


def test_no_link_for_own_note():
    assert pipeline.original_link(make_message(text="привет")) is None


# --------------------------------------------------------------------------
# Текст альбома
# --------------------------------------------------------------------------

def test_album_caption_is_found_on_any_message():
    messages = [
        make_message(1, media_group_id="MG", photo=photo("a")),
        make_message(2, media_group_id="MG", photo=photo("b"), caption="Подпись альбома"),
    ]
    text, _ = pipeline.collect_text(messages)
    assert text == "Подпись альбома"


def test_album_without_caption_gives_empty_text():
    messages = [make_message(1, media_group_id="MG", photo=photo("a"))]
    assert pipeline.collect_text(messages) == ("", [])


# --------------------------------------------------------------------------
# Тип записи
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "media_labels,url,expected",
    [
        ([], None, "Текст"),
        ([], "https://example.com", "Ссылка"),
        (["Фото"], None, "Фото"),
        (["Фото"], "https://example.com", "Фото"),
        (["Фото", "Фото"], None, "Альбом"),
    ],
)
def test_kind_selection(media_labels, url, expected):
    media = [
        tg_files.MediaRef(file_id="x", block_kind="image", kind_label=label, filename="f.jpg", mime="image/jpeg")
        for label in media_labels
    ]
    assert pipeline.Saver._kind(media, url) == expected


# --------------------------------------------------------------------------
# Вложения
# --------------------------------------------------------------------------

def test_photo_extracted_at_largest_size():
    media = tg_files.extract_media(make_message(photo=photo("a", size=99_000)))
    assert media is not None
    assert media.file_id == "big_a"
    assert media.block_kind == "image"
    assert media.kind_label == "Фото"


@pytest.mark.parametrize(
    "filename,mime,block_kind,label",
    [
        ("отчёт.pdf", "application/pdf", "pdf", "Файл"),
        ("схема.png", "image/png", "image", "Фото"),
        ("клип.mp4", "video/mp4", "video", "Видео"),
        ("трек.mp3", "audio/mpeg", "audio", "Аудио"),
        ("архив.zip", "application/zip", "file", "Файл"),
        ("данные.xlsx", None, "file", "Файл"),
    ],
)
def test_document_kind_follows_extension(filename, mime, block_kind, label):
    document = Document(file_id="d1", file_unique_id="du1", file_name=filename, mime_type=mime, file_size=1234)
    media = tg_files.extract_media(make_message(document=document))
    assert media is not None
    assert (media.block_kind, media.kind_label) == (block_kind, label)


def test_message_without_attachment_returns_none():
    assert tg_files.extract_media(make_message(text="просто текст")) is None


def test_human_size_formatting():
    ref = tg_files.MediaRef("i", "file", "Файл", "a.bin", "application/octet-stream", size=12 * 1024 * 1024)
    assert ref.human_size == "12.0 МБ"
    assert tg_files.MediaRef("i", "file", "Файл", "a.bin", "x", size=2048).human_size == "2 КБ"
