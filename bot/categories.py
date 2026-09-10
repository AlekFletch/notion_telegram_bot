"""Категории записей.

Список повторяет разделы рабочего пространства Notion. Чтобы поменять —
правь CATEGORIES здесь и не забудь привести в соответствие варианты
свойства «Категория» в самой базе, иначе Notion отклонит запись.

Порядок важен: он же порядок кнопок в Telegram.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Имя свойства в базе Notion.
PROPERTY = "Категория"


@dataclass(frozen=True)
class Category:
    name: str   # ровно как вариант свойства в Notion
    emoji: str

    @property
    def button(self) -> str:
        return f"{self.emoji} {self.name}"


CATEGORIES: tuple[Category, ...] = (
    Category("ЦЭО АПК — работа", "🏛️"),
    Category("1С и разработка", "⚙️"),
    Category("Веб и IT", "🌐"),
    Category("Здоровье и дыхание", "🫁"),
    Category("Психология и саморазвитие", "🧠"),
    Category("Книги и чтение", "📚"),
    Category("Бизнес и продажи", "💼"),
    Category("Софт и загрузки", "💾"),
    Category("Личное", "🏠"),
)


def by_index(index: int) -> Category | None:
    if 0 <= index < len(CATEGORIES):
        return CATEGORIES[index]
    return None


def index_of(name: str) -> int | None:
    for position, category in enumerate(CATEGORIES):
        if category.name == name:
            return position
    return None
