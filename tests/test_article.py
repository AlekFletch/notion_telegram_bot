"""Тесты извлечения заголовка страницы.

Заголовок — то, что видно списком в Notion, поэтому склейки вроде
«agentits» вместо «agent its» портят базу заметнее всего остального.
"""
from __future__ import annotations

import pytest

from bot import article


# --------------------------------------------------------------------------
# Мета-теги
# --------------------------------------------------------------------------

def test_og_title_is_preferred():
    html = '<meta property="og:title" content="Настоящий заголовок">'
    assert article._meta_title(html) == "Настоящий заголовок"


def test_og_title_with_reversed_attribute_order():
    html = '<meta content="Заголовок" property="og:title">'
    assert article._meta_title(html) == "Заголовок"


def test_twitter_title_also_works():
    html = "<meta name='twitter:title' content='Из твиттер-карточки'>"
    assert article._meta_title(html) == "Из твиттер-карточки"


def test_html_entities_are_decoded():
    html = '<meta property="og:title" content="Ford &amp; Sons &laquo;Отчёт&raquo;">'
    assert article._meta_title(html) == "Ford & Sons «Отчёт»"


def test_no_meta_title_returns_none():
    assert article._meta_title("<html><body>ничего</body></html>") is None


# --------------------------------------------------------------------------
# <title>
# --------------------------------------------------------------------------

def test_document_title_is_read_and_collapsed():
    html = "<title>\n  Длинный   заголовок\n</title>"
    assert article._document_title(html) == "Длинный заголовок"


def test_document_title_with_attributes():
    assert article._document_title('<title lang="ru">Привет</title>') == "Привет"


# --------------------------------------------------------------------------
# Имя сайта
# --------------------------------------------------------------------------

def test_site_name_prefix_is_stripped():
    title = "Obscura · Give every agent its own browser"
    assert article._strip_site_name(title, "Obscura") == "Give every agent its own browser"


def test_site_name_suffix_is_stripped():
    assert article._strip_site_name("Как жить | Хабр", "Хабр") == "Как жить"


@pytest.mark.parametrize("separator", ["·", "|", "—", "–", "::", "•", "-"])
def test_all_separators_supported(separator):
    title = f"Статья {separator} Сайт"
    assert article._strip_site_name(title, "Сайт") == "Статья"


def test_dash_inside_real_title_is_kept():
    """Отрезаем только точное совпадение с именем сайта."""
    title = "Ремонт — это просто"
    assert article._strip_site_name(title, "Хабр") == "Ремонт — это просто"


def test_title_without_site_name_unchanged():
    assert article._strip_site_name("Просто заголовок", "Сайт") == "Просто заголовок"


def test_unknown_site_name_leaves_title_alone():
    assert article._strip_site_name("A | B", None) == "A | B"


# --------------------------------------------------------------------------
# Сборка целиком
# --------------------------------------------------------------------------

def test_extract_prefers_title_tag_over_mangled_markup():
    """Ровно тот случай, что поймали на obscura.sh."""
    html = """
    <html><head><title>Obscura · Give every agent its own browser</title></head>
    <body><h1><span>Give every agent</span><span>its own browser.</span></h1>
    <article><p>Достаточно длинный абзац текста, чтобы извлекатель счёл
    страницу содержательной и вернул хоть что-нибудь осмысленное.</p></article>
    </body></html>
    """
    result = article._extract("https://obscura.sh/", html, 40_000)

    assert result.title is not None
    assert "agentits" not in result.title
    assert result.title == "Give every agent its own browser"


def test_extract_uses_og_title_when_present():
    html = """
    <html><head>
      <title>Сайт · Служебный заголовок</title>
      <meta property="og:title" content="Правильный заголовок статьи">
    </head><body><article><p>Текст статьи для извлечения, достаточно
    длинный, чтобы не быть отброшенным.</p></article></body></html>
    """
    assert article._extract("https://example.com/", html, 40_000).title == "Правильный заголовок статьи"


def test_domain_is_stripped_when_site_name_unknown():
    """og:site_name есть не везде — тогда ориентируемся на домен."""
    title = "obscura.sh · Give every agent its own browser"
    assert article._strip_site_name(title, None, "https://obscura.sh/x") == "Give every agent its own browser"


def test_domain_first_label_also_matches():
    title = "Obscura | Заголовок"
    assert article._strip_site_name(title, None, "https://www.obscura.sh/") == "Заголовок"


def test_domain_fallback_does_not_touch_unrelated_title():
    title = "Часть первая — часть вторая"
    assert article._strip_site_name(title, None, "https://example.com/") == title


def test_slash_separator_supported():
    assert article._strip_site_name("Все статьи подряд / Хабр", "Хабр") == "Все статьи подряд"


def test_slash_without_spaces_is_not_a_separator():
    """«A/B тесты» — часть заголовка, а не отбивка имени сайта."""
    assert article._strip_site_name("A/B тесты / Хабр", "Хабр") == "A/B тесты"


def test_site_name_stripped_from_og_title_too():
    """У Хабра имя сайта сидит прямо в og:title — очистка нужна и там."""
    html = """
    <html><head>
      <meta property="og:title" content="Все статьи подряд / Хабр">
      <meta property="og:site_name" content="Хабр">
    </head><body><article><p>Достаточно длинный текст статьи, чтобы
    извлекатель вернул содержимое, а не пустоту.</p></article></body></html>
    """
    assert article._extract("https://habr.com/ru/articles/", html, 40_000).title == "Все статьи подряд"
