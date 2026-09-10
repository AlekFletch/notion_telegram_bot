"""Преобразование текста и форматирования Telegram в блоки Notion.

Главная тонкость: Telegram отдаёт offset/length сущностей в кодовых единицах
UTF-16, а не в символах Python. На кириллице это ещё совпадает, но любой эмодзи
вне BMP (например, 📥) занимает две единицы — и наивная нарезка по индексам
Python съезжает, ломая жирный шрифт и ссылки. Поэтому всё начинается с карты
«смещение UTF-16 → индекс Python».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: Лимит Notion на длину одного текстового фрагмента.
TEXT_LIMIT = 2000
#: Лимит Notion на количество фрагментов внутри одного блока.
RICH_TEXT_LIMIT = 100
#: Лимит Notion на количество блоков в одном запросе.
BLOCKS_PER_REQUEST = 100

#: Сущности Telegram, которые становятся отдельным блоком Notion.
BLOCK_LEVEL = frozenset({"pre", "blockquote", "expandable_blockquote"})

#: Сущности, задающие оформление внутри абзаца.
ANNOTATIONS = {
    "bold": {"bold": True},
    "italic": {"italic": True},
    "underline": {"underline": True},
    "strikethrough": {"strikethrough": True},
    "code": {"code": True},
    # В Notion нет спойлера — ближайшее по смыслу «приглушённое» оформление.
    "spoiler": {"color": "gray_background"},
}

#: Сущности, которые сами по себе являются ссылкой.
AUTOLINK = frozenset({"url", "text_link", "email", "phone_number"})

_DEFAULT_ANNOTATIONS = {
    "bold": False,
    "italic": False,
    "strikethrough": False,
    "underline": False,
    "code": False,
    "color": "default",
}

#: Языки, которые Notion принимает в блоке code.
_NOTION_LANGUAGES = frozenset({
    "bash", "c", "c#", "c++", "clojure", "coffeescript", "css", "dart", "diff",
    "docker", "elixir", "elm", "erlang", "f#", "fortran", "go", "graphql",
    "groovy", "haskell", "html", "java", "javascript", "json", "julia",
    "kotlin", "latex", "less", "lisp", "lua", "makefile", "markdown", "matlab",
    "mermaid", "nix", "objective-c", "ocaml", "pascal", "perl", "php",
    "plain text", "powershell", "prolog", "protobuf", "python", "r", "ruby",
    "rust", "sass", "scala", "scheme", "scss", "shell", "sql", "swift",
    "typescript", "vb.net", "verilog", "vhdl", "webassembly", "xml", "yaml",
})

_LANGUAGE_ALIASES = {
    "py": "python", "python3": "python", "js": "javascript", "node": "javascript",
    "ts": "typescript", "sh": "shell", "zsh": "shell", "bat": "shell",
    "cmd": "shell", "ps1": "powershell", "yml": "yaml", "md": "markdown",
    "cpp": "c++", "cs": "c#", "csharp": "c#", "golang": "go", "rb": "ruby",
    "rs": "rust", "kt": "kotlin", "objc": "objective-c", "postgres": "sql",
    "postgresql": "sql", "mysql": "sql", "sqlite": "sql", "1c": "plain text",
    "dockerfile": "docker", "htm": "html", "tex": "latex",
}

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


# --------------------------------------------------------------------------
# UTF-16
# --------------------------------------------------------------------------

def utf16_map(text: str) -> list[int]:
    """Карта: смещение в UTF-16 → индекс символа в строке Python.

    Длина результата — utf16_len + 1; последний элемент указывает за конец
    строки, чтобы конечные смещения сущностей разыменовывались без проверок.
    """
    mapping: list[int] = []
    for index, char in enumerate(text):
        mapping.append(index)
        if ord(char) > 0xFFFF:  # суррогатная пара занимает две единицы
            mapping.append(index)
    mapping.append(len(text))
    return mapping


@dataclass(frozen=True)
class Span:
    """Сущность Telegram, пересчитанная в индексы символов Python."""

    start: int
    end: int
    type: str
    url: str | None = None
    language: str | None = None


def to_spans(text: str, entities: Sequence[Any] | None) -> list[Span]:
    """Пересчитать сущности Telegram в индексы Python."""
    if not entities:
        return []

    mapping = utf16_map(text)
    last = len(mapping) - 1
    spans: list[Span] = []

    for entity in entities:
        kind = getattr(entity, "type", None)
        kind = getattr(kind, "value", kind)  # aiogram отдаёт str-enum
        offset = getattr(entity, "offset", 0)
        length = getattr(entity, "length", 0)
        if not kind or length <= 0:
            continue

        start_u16 = min(max(offset, 0), last)
        end_u16 = min(max(offset + length, 0), last)
        start, end = mapping[start_u16], mapping[end_u16]
        if end <= start:
            continue

        url = getattr(entity, "url", None)
        fragment = text[start:end]
        if kind == "url":
            url = fragment
        elif kind == "email":
            url = "mailto:" + fragment
        elif kind == "phone_number":
            url = "tel:" + fragment.replace(" ", "")

        spans.append(
            Span(start, end, kind, url=url, language=getattr(entity, "language", None))
        )

    spans.sort(key=lambda s: (s.start, -s.end))
    return spans


# --------------------------------------------------------------------------
# rich_text
# --------------------------------------------------------------------------

def _chunks(text: str, limit: int = TEXT_LIMIT) -> Iterable[str]:
    for start in range(0, len(text), limit):
        yield text[start:start + limit]


def _text_item(content: str, annotations: dict[str, Any], link: str | None) -> dict:
    item: dict[str, Any] = {
        "type": "text",
        "text": {"content": content},
        "annotations": {**_DEFAULT_ANNOTATIONS, **annotations},
    }
    if link:
        item["text"]["link"] = {"url": link}
    return item


def rich_text(
    text: str,
    spans: Sequence[Span],
    start: int = 0,
    end: int | None = None,
) -> list[dict]:
    """Собрать rich_text для участка [start, end) с учётом оформления."""
    end = len(text) if end is None else end
    if end <= start:
        return []

    inline = [
        s for s in spans
        if s.type not in BLOCK_LEVEL and s.start < end and s.end > start
    ]

    # Границы участков с однородным оформлением.
    points = {start, end}
    for span in inline:
        points.add(max(span.start, start))
        points.add(min(span.end, end))
    boundaries = sorted(points)

    items: list[dict] = []
    for left, right in zip(boundaries, boundaries[1:]):
        content = text[left:right]
        if not content:
            continue

        annotations: dict[str, Any] = {}
        link: str | None = None
        for span in inline:
            if span.start > left or span.end < right:
                continue
            annotations.update(ANNOTATIONS.get(span.type, {}))
            if link is None and span.type in AUTOLINK and span.url:
                link = span.url

        for chunk in _chunks(content):
            items.append(_text_item(chunk, annotations, link))

    return items


def _split_rich(items: list[dict]) -> list[list[dict]]:
    """Notion не принимает больше 100 фрагментов в блоке — режем на несколько."""
    if len(items) <= RICH_TEXT_LIMIT:
        return [items]
    return [
        items[i:i + RICH_TEXT_LIMIT]
        for i in range(0, len(items), RICH_TEXT_LIMIT)
    ]


# --------------------------------------------------------------------------
# Блоки
# --------------------------------------------------------------------------

def paragraph(items: list[dict]) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": items}}


def heading(text: str, level: int = 2) -> dict:
    key = "heading_" + str(level)
    return {"object": "block", "type": key, key: {"rich_text": [_text_item(text, {}, None)]}}


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def callout(text: str, emoji: str = "⚠️") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [_text_item(text[:TEXT_LIMIT], {}, None)],
            "icon": {"type": "emoji", "emoji": emoji},
            "color": "gray_background",
        },
    }


def bookmark(url: str) -> dict:
    return {"object": "block", "type": "bookmark", "bookmark": {"url": url}}


def code_block(text: str, language: str | None) -> list[dict]:
    lang = (language or "").strip().lower()
    lang = _LANGUAGE_ALIASES.get(lang, lang)
    if lang not in _NOTION_LANGUAGES:
        lang = "plain text"
    items = [_text_item(chunk, {}, None) for chunk in _chunks(text)]
    return [
        {"object": "block", "type": "code", "code": {"rich_text": part, "language": lang}}
        for part in _split_rich(items)
    ]


def quote_block(items: list[dict]) -> list[dict]:
    return [
        {"object": "block", "type": "quote", "quote": {"rich_text": part}}
        for part in _split_rich(items)
    ]


def _paragraphs(text: str, spans: Sequence[Span], start: int, end: int) -> list[dict]:
    """Разбить обычный участок текста на абзацы по переводам строк."""
    blocks: list[dict] = []
    cursor = start
    for line in text[start:end].split("\n"):
        line_start, line_end = cursor, cursor + len(line)
        cursor = line_end + 1  # +1 за сам перевод строки
        if not line.strip():
            continue
        for part in _split_rich(rich_text(text, spans, line_start, line_end)):
            blocks.append(paragraph(part))
    return blocks


def _outermost(spans: Sequence[Span]) -> list[Span]:
    """Убрать вложенные блочные сущности, оставив только внешние."""
    result: list[Span] = []
    for span in spans:
        if result and span.start < result[-1].end:
            continue
        result.append(span)
    return result


def message_blocks(text: str, entities: Sequence[Any] | None = None) -> list[dict]:
    """Тело сообщения Telegram в виде блоков Notion."""
    if not text:
        return []

    spans = to_spans(text, entities)
    block_spans = _outermost([s for s in spans if s.type in BLOCK_LEVEL])

    blocks: list[dict] = []
    cursor = 0
    for span in block_spans:
        if span.start > cursor:
            blocks.extend(_paragraphs(text, spans, cursor, span.start))
        if span.type == "pre":
            blocks.extend(code_block(text[span.start:span.end], span.language))
        else:
            blocks.extend(quote_block(rich_text(text, spans, span.start, span.end)))
        cursor = span.end

    if cursor < len(text):
        blocks.extend(_paragraphs(text, spans, cursor, len(text)))

    return blocks


def plain_paragraphs(text: str) -> list[dict]:
    """Простой текст без форматирования — например, тело статьи."""
    blocks: list[dict] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for chunk in _chunks(line):
            blocks.append(paragraph([_text_item(chunk, {}, None)]))
    return blocks


def batched(blocks: Sequence[dict], size: int = BLOCKS_PER_REQUEST) -> list[list[dict]]:
    return [list(blocks[i:i + size]) for i in range(0, len(blocks), size)]


# --------------------------------------------------------------------------
# Ссылки и заголовок
# --------------------------------------------------------------------------

def first_url(text: str, entities: Sequence[Any] | None = None) -> str | None:
    """Первая ссылка сообщения: сперва из сущностей, затем поиском по тексту."""
    for span in to_spans(text or "", entities):
        if span.type in {"url", "text_link"} and span.url:
            return span.url
    match = _URL_RE.search(text or "")
    if not match:
        return None
    # Отрезаем знаки препинания, прилипшие к концу ссылки в живом тексте.
    return match.group(0).rstrip(".,;:!?)]}«»")


def _first_line(text: str) -> str:
    """Первая непустая строка со схлопнутыми пробелами.

    Без усечения: единственный лимит длины живёт в build_title, иначе строка
    режется дважды и параметр limit начинает врать.
    """
    for line in (text or "").split("\n"):
        line = " ".join(line.split())
        if line:
            return line
    return ""


_KIND_EMOJI = {
    "Фото": "\U0001f4f7",
    "Видео": "\U0001f3ac",
    "Файл": "\U0001f4ce",
    "Аудио": "\U0001f3a7",
    "Альбом": "\U0001f5bc",
    "Ссылка": "\U0001f517",
    "Текст": "\U0001f4dd",
}


def build_title(
    *,
    kind: str,
    text: str = "",
    article_title: str | None = None,
    filename: str | None = None,
    source: str | None = None,
    limit: int = 120,
) -> str:
    """Заголовок записи без участия AI.

    Приоритет: заголовок статьи → первая строка текста → имя файла →
    описательная заглушка вида «📷 Фото из Канал «X»».
    """
    for candidate in (article_title, _first_line(text), filename):
        candidate = " ".join((candidate or "").split())
        if candidate:
            if len(candidate) <= limit:
                return candidate
            return candidate[:limit - 1].rstrip() + "…"

    emoji = _KIND_EMOJI.get(kind, "\U0001f4dd")
    if source:
        return f"{emoji} {kind} из {source}"
    return f"{emoji} {kind}"
