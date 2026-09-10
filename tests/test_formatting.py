"""Тесты конвертера Telegram → Notion.

Основное внимание — смещениям UTF-16: именно там ломается форматирование,
и именно эту поломку невозможно заметить глазами на коротком примере.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from bot import formatting as f


@dataclass
class Entity:
    """Минимальная замена aiogram.types.MessageEntity."""

    type: str
    offset: int
    length: int
    url: str | None = None
    language: str | None = None


def texts(items: list[dict]) -> list[str]:
    return [item["text"]["content"] for item in items]


def bold_parts(items: list[dict]) -> list[str]:
    return [i["text"]["content"] for i in items if i["annotations"]["bold"]]


# --------------------------------------------------------------------------
# UTF-16
# --------------------------------------------------------------------------

def test_utf16_map_counts_surrogate_pairs_twice():
    # "📥" вне BMP: одна позиция Python, две единицы UTF-16.
    mapping = f.utf16_map("a📥b")
    assert mapping == [0, 1, 1, 2, 3]


def test_bold_after_emoji_keeps_alignment():
    """Классический баг: без пересчёта UTF-16 выделение съезжает влево."""
    text = "📥 Входящие: важное"
    # В UTF-16 эмодзи занимает 2 единицы, поэтому "важное" начинается с 13.
    assert text.encode("utf-16-le").__len__() // 2 == 19
    entity = Entity(type="bold", offset=13, length=6)

    items = f.rich_text(text, f.to_spans(text, [entity]))

    assert bold_parts(items) == ["важное"]


def test_multiple_emoji_before_link():
    text = "🔥🔥 читать тут"
    # 2 эмодзи = 4 единицы UTF-16; "читать тут" начинается с 5.
    entity = Entity(type="text_link", offset=5, length=10, url="https://example.com")

    items = f.rich_text(text, f.to_spans(text, [entity]))
    linked = [i for i in items if i["text"].get("link")]

    assert texts(linked) == ["читать тут"]
    assert linked[0]["text"]["link"] == {"url": "https://example.com"}


def test_cyrillic_offsets_unchanged():
    text = "Привет, мир"
    entity = Entity(type="bold", offset=8, length=3)

    items = f.rich_text(text, f.to_spans(text, [entity]))

    assert bold_parts(items) == ["мир"]


# --------------------------------------------------------------------------
# Оформление
# --------------------------------------------------------------------------

def test_overlapping_entities_merge_annotations():
    text = "жирный курсив"
    entities = [
        Entity(type="bold", offset=0, length=13),
        Entity(type="italic", offset=7, length=6),
    ]

    items = f.rich_text(text, f.to_spans(text, entities))
    both = [i for i in items if i["annotations"]["bold"] and i["annotations"]["italic"]]

    assert texts(both) == ["курсив"]


def test_url_entity_becomes_link_without_explicit_url():
    text = "смотри https://example.com/a вот"
    entity = Entity(type="url", offset=7, length=21)

    items = f.rich_text(text, f.to_spans(text, [entity]))
    linked = [i for i in items if i["text"].get("link")]

    assert linked[0]["text"]["link"] == {"url": "https://example.com/a"}


def test_email_and_phone_get_schemes():
    text = "a@b.ru"
    items = f.rich_text(text, f.to_spans(text, [Entity(type="email", offset=0, length=6)]))
    assert items[0]["text"]["link"] == {"url": "mailto:a@b.ru"}


def test_spoiler_maps_to_background_color():
    text = "секрет"
    items = f.rich_text(text, f.to_spans(text, [Entity(type="spoiler", offset=0, length=6)]))
    assert items[0]["annotations"]["color"] == "gray_background"


def test_entity_beyond_text_is_clamped():
    """Битые offset из чужих клиентов не должны ронять обработку."""
    text = "коротко"
    entity = Entity(type="bold", offset=3, length=9999)

    items = f.rich_text(text, f.to_spans(text, [entity]))

    assert "".join(texts(items)) == text
    assert bold_parts(items) == ["отко"]


# --------------------------------------------------------------------------
# Блоки
# --------------------------------------------------------------------------

def test_lines_become_separate_paragraphs():
    blocks = f.message_blocks("первая\n\nвторая\nтретья")
    assert len(blocks) == 3
    assert all(b["type"] == "paragraph" for b in blocks)
    assert blocks[1]["paragraph"]["rich_text"][0]["text"]["content"] == "вторая"


def test_pre_entity_becomes_code_block_with_language():
    text = "смотри код:\nprint(1)\nконец"
    entity = Entity(type="pre", offset=12, length=8, language="py")

    blocks = f.message_blocks(text, [entity])
    kinds = [b["type"] for b in blocks]

    assert kinds == ["paragraph", "code", "paragraph"]
    assert blocks[1]["code"]["language"] == "python"
    assert blocks[1]["code"]["rich_text"][0]["text"]["content"] == "print(1)"


def test_unknown_language_falls_back_to_plain_text():
    blocks = f.code_block("что угодно", "brainfuck")
    assert blocks[0]["code"]["language"] == "plain text"


def test_blockquote_becomes_quote_block():
    text = "цитата тут"
    blocks = f.message_blocks(text, [Entity(type="blockquote", offset=0, length=10)])
    assert [b["type"] for b in blocks] == ["quote"]


def test_nested_block_entities_do_not_duplicate_text():
    text = "внешняя цитата"
    entities = [
        Entity(type="blockquote", offset=0, length=14),
        Entity(type="pre", offset=8, length=6),
    ]

    blocks = f.message_blocks(text, entities)
    rendered = "".join(
        i["text"]["content"]
        for b in blocks
        for i in b[b["type"]]["rich_text"]
    )

    assert rendered == text


# --------------------------------------------------------------------------
# Лимиты Notion
# --------------------------------------------------------------------------

def test_long_line_is_split_into_2000_char_chunks():
    blocks = f.message_blocks("я" * 5000)
    items = blocks[0]["paragraph"]["rich_text"]

    assert [len(i["text"]["content"]) for i in items] == [2000, 2000, 1000]
    assert sum(len(i["text"]["content"]) for i in items) == 5000


def test_block_never_exceeds_100_rich_text_items():
    # 150 чередующихся жирных слов дают заведомо больше 100 фрагментов.
    words = ["слово"] * 150
    text = " ".join(words)
    entities = []
    offset = 0
    for word in words:
        entities.append(Entity(type="bold", offset=offset, length=len(word)))
        offset += len(word) + 1

    blocks = f.message_blocks(text, entities)

    assert len(blocks) > 1
    assert all(len(b["paragraph"]["rich_text"]) <= 100 for b in blocks)


def test_batched_splits_by_hundred():
    assert [len(part) for part in f.batched([{}] * 250)] == [100, 100, 50]


# --------------------------------------------------------------------------
# Заголовок и ссылки
# --------------------------------------------------------------------------

def test_title_prefers_article_title():
    title = f.build_title(kind="Ссылка", text="какой-то текст", article_title="Заголовок статьи")
    assert title == "Заголовок статьи"


def test_title_falls_back_to_first_meaningful_line():
    title = f.build_title(kind="Текст", text="\n  \nПервая строка\nвторая")
    assert title == "Первая строка"


def test_title_falls_back_to_filename_then_placeholder():
    assert f.build_title(kind="Файл", filename="отчёт.pdf") == "отчёт.pdf"
    assert f.build_title(kind="Фото", source="Канал «X»") == "\U0001f4f7 Фото из Канал «X»"


def test_long_title_is_truncated_with_ellipsis():
    title = f.build_title(kind="Текст", text="я" * 300)
    assert len(title) == 120
    assert title.endswith("…")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("читай https://example.com/x сейчас", "https://example.com/x"),
        ("в конце https://example.com/x.", "https://example.com/x"),
        ("нет ссылок", None),
    ],
)
def test_first_url_from_plain_text(text, expected):
    assert f.first_url(text) == expected


def test_first_url_prefers_entity_url_over_visible_text():
    text = "кликни сюда"
    entity = Entity(type="text_link", offset=7, length=4, url="https://real.example/target")
    assert f.first_url(text, [entity]) == "https://real.example/target"


def test_empty_message_yields_no_blocks():
    assert f.message_blocks("") == []
    assert f.message_blocks("   \n  ") == []
