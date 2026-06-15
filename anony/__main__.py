# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import asyncio
import importlib
import logging
import signal
import traceback
from contextlib import suppress

from anony import anon, app, config, db, logger, stop, tasks, thumb, userbot, yt
from anony.core.commands import sync_command_menus
from anony.core.health import health
from anony.plugins import all_modules


def deployment_runtime_settings(settings: dict) -> dict:
    if not config.MANAGED_SETUP or not config.DEPLOYMENT_ID:
        return settings

    runtime_deployment_id = settings.get("DEPLOYMENT_ID")
    if settings and runtime_deployment_id != config.DEPLOYMENT_ID:
        logger.warning(
            "Ignoring runtime config for mismatched deployment id: expected=%s got=%s",
            config.DEPLOYMENT_ID,
            runtime_deployment_id or "none",
        )
        return {}

    return {key: value for key, value in settings.items() if key != "DEPLOYMENT_ID"}


async def idle():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()

async def main():
    loop = asyncio.get_running_loop()
    default_exception_handler = loop.get_exception_handler()

    def log_background_exception(event_loop, context):
        logger.error(
            "Unhandled background task error: %s",
            context.get("message", "unknown background task failure"),
            exc_info=context.get("exception"),
        )
        if default_exception_handler:
            default_exception_handler(event_loop, context)

    loop.set_exception_handler(log_background_exception)
    health.start()
    await db.connect()
    stored_runtime_settings = await db.get_all_config()
    if (
        config.MANAGED_SETUP
        and config.DEPLOYMENT_ID
        and stored_runtime_settings
        and not stored_runtime_settings.get("DEPLOYMENT_ID")
    ):
        logger.warning(
            "Runtime configuration has no deployment identity; adopting it for deployment %s.",
            config.DEPLOYMENT_ID,
        )
        await db.set_config("DEPLOYMENT_ID", config.DEPLOYMENT_ID)
        stored_runtime_settings["DEPLOYMENT_ID"] = config.DEPLOYMENT_ID
    if (
        config.MANAGED_SETUP
        and config.DEPLOYMENT_ID
        and config.OWNER_ID
        and not stored_runtime_settings
    ):
        await db.set_config("DEPLOYMENT_ID", config.DEPLOYMENT_ID)
        await db.set_config("OWNER_ID", config.OWNER_ID)
        await db.add_sudo(config.OWNER_ID)
        stored_runtime_settings = await db.get_all_config()

    runtime_settings = deployment_runtime_settings(stored_runtime_settings)

    if runtime_settings:
        config.apply_runtime_config(runtime_settings)
        app.owner = config.OWNER_ID
        app.logger = config.LOGGER_ID
        if app.owner:
            app.sudoers.add(app.owner)
        userbot.reload_from_config()

    await app.boot()
    await userbot.boot()
    await anon.boot()
    await thumb.start()

    for module in all_modules:
        importlib.import_module(f"anony.plugins.{module}")
    logger.info(f"Loaded {len(all_modules)} modules.")

    if config.COOKIES_URL:
        await yt.save_cookies(config.COOKIES_URL)
    if yt.api:
        await yt.api.get_session()

    sudoers = await db.get_sudoers()
    app.sudoers.update(sudoers)
    app.bl_users.update(await db.get_blacklisted())
    logger.info(f"Loaded {len(app.sudoers)} sudo users.")
    menu_warnings = await sync_command_menus()
    if menu_warnings:
        logger.warning("Command menus registered with %d warning(s).", len(menu_warnings))

    health.mark_healthy()
    tasks.append(
        asyncio.create_task(
            anon.maintenance_queue_worker(),
            name="maintenance-queue-restore",
        )
    )
    await idle()
    await stop("termination signal received")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        health.fatal("keyboard interrupt")
    except BaseException as exc:
        health.fatal(f"{type(exc).__name__}: {exc}")
        logging.getLogger(__name__).critical(
            "Fatal deployed bot error:\n%s", traceback.format_exc()
        )
        raise
