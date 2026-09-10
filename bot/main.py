"""Точка входа. Один и тот же бот работает и на long polling, и на вебхуке."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from bot.albums import AlbumBuffer
from bot.config import Settings, load_settings
from bot.handlers import router
from bot.notion import NotionClient, NotionError
from bot.pipeline import Saver
from bot.session import RetryingSession

log = logging.getLogger("bot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_dispatcher(settings: Settings, notion: NotionClient) -> tuple[Dispatcher, AlbumBuffer]:
    album = AlbumBuffer()
    dispatcher = Dispatcher(
        settings=settings,
        notion=notion,
        saver=Saver(notion, settings),
        album=album,
    )
    dispatcher.include_router(router)
    return dispatcher, album


async def run_polling(bot: Bot, dispatcher: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Режим long polling. Останов — Ctrl+C")
    await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())


async def run_webhook(bot: Bot, dispatcher: Dispatcher, settings: Settings) -> None:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        # Render и UptimeRobot дёргают этот адрес, чтобы сервис не засыпал.
        return web.Response(text="ok")

    app.router.add_get("/", health)

    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=settings.webhook_secret or None,
        # Отвечаем Telegram 200 сразу, сохраняем в фоне: холодный старт Render
        # иначе упирается в таймаут и апдейт приезжает повторно.
        handle_in_background=True,
    ).register(app, path=settings.webhook_path)
    setup_application(app, dispatcher, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)
    await site.start()

    await bot.set_webhook(
        settings.webhook_url,
        secret_token=settings.webhook_secret or None,
        allowed_updates=dispatcher.resolve_used_update_types(),
        drop_pending_updates=False,
    )
    log.info("Вебхук установлен: %s (порт %s)", settings.webhook_url, settings.port)

    try:
        await asyncio.Event().wait()  # держим процесс живым
    finally:
        await runner.cleanup()


async def amain() -> int:
    settings = load_settings()
    setup_logging(settings.log_level)

    if settings.setup_mode:
        log.warning(
            "ALLOWED_USER_IDS пуст — бот принимает сообщения от кого угодно. "
            "Отправь боту /start, узнай свой ID и впиши его в .env"
        )

    bot = Bot(
        token=settings.telegram_bot_token,
        session=RetryingSession(timeout=90.0),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    notion = NotionClient(settings.notion_token, settings.notion_database_id)
    dispatcher, album = build_dispatcher(settings, notion)

    try:
        await notion.prepare()
    except NotionError as exc:
        log.error("Notion недоступен: %s", exc)
        if exc.status == 404:
            log.error(
                "Похоже, интеграции не дали доступ к базе. Открой страницу "
                "«Telegram Inbox» → меню ⋯ → Connections → добавь интеграцию."
            )
        await bot.session.close()
        await notion.aclose()
        return 1

    me = await bot.get_me()
    log.info("Бот @%s запущен в режиме %s", me.username, settings.mode)

    try:
        if settings.mode == "webhook":
            await run_webhook(bot, dispatcher, settings)
        else:
            await run_polling(bot, dispatcher)
    finally:
        await album.shutdown()
        await notion.aclose()
        await bot.session.close()

    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(amain()))
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            raise
        log.info("Остановлено")


if __name__ == "__main__":
    main()
