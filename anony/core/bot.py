# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import pyrogram
from pathlib import Path

from anony import config, logger


class Bot(pyrogram.Client):
    def __init__(self):
        session_name = "Anony"
        if config.SESSION_PATH:
            session_name = str(Path(config.SESSION_PATH) / session_name)

        super().__init__(
            name=session_name,
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            parse_mode=pyrogram.enums.ParseMode.HTML,
            max_concurrent_transmissions=7,
            link_preview_options=pyrogram.types.LinkPreviewOptions(is_disabled=True),
        )
        self.owner = config.OWNER_ID
        self.logger = config.LOGGER_ID
        self.bl_users = pyrogram.filters.user()
        self.sudoers = pyrogram.filters.user(self.owner)

    async def warn_owner_about_logging(self, reason: str, action: str) -> None:
        if not self.owner:
            logger.warning("Could not send log-group warning because no owner is configured.")
            return
        try:
            await self.send_message(
                self.owner,
                "⚠️ <b>Log group is unavailable after restart.</b>\n\n"
                f"{reason}\n\n"
                f"💡 {action}\n"
                "🎵 The music bot will continue running normally without activity logging.",
            )
        except Exception as exc:
            logger.warning("Could not notify owner about unavailable log group: %s", exc)

    async def boot(self):
        """Start the bot and enable optional log-group delivery when available."""
        await super().start()
        self.id = self.me.id
        self.name = self.me.first_name
        self.username = self.me.username
        self.mention = self.me.mention

        if self.owner:
            self.sudoers.add(self.owner)

        if config.LOGGING_DISABLED:
            logger.info("Activity logging was disabled by the owner; skipping log group check.")
            self.logger = 0
            logger.info(f"Bot started as @{self.username}")
            return

        if not self.logger:
            logger.info("Log group is not configured yet; skipping log group check.")
            await self.warn_owner_about_logging(
                "No log group is currently configured.",
                "Create a group, add me, promote me as admin, then run <code>/setlog</code> there.",
            )
            logger.info(f"Bot started as @{self.username}")
            return

        try:
            await self.send_message(self.logger, "Bot Started")
            get = await self.get_chat_member(self.logger, self.id)
        except Exception as ex:
            logger.warning(
                "Configured log group %s is unreachable; continuing with logging disabled: %s",
                self.logger,
                ex,
            )
            self.logger = 0
            await self.warn_owner_about_logging(
                "I could not reach the configured log group.",
                "Add me back to the log group or check its availability, promote me as admin, then run <code>/setlog</code> there again.",
            )
            logger.info(f"Bot started as @{self.username}")
            return

        if get.status != pyrogram.enums.ChatMemberStatus.ADMINISTRATOR:
            logger.warning(
                "Bot is not an administrator in configured log group %s; continuing with logging disabled.",
                self.logger,
            )
            self.logger = 0
            await self.warn_owner_about_logging(
                "I am not an administrator in the configured log group.",
                "Promote me as admin in the log group, then run <code>/setlog</code> there again.",
            )
        logger.info(f"Bot started as @{self.username}")

    async def exit(self):
        """
        Asynchronously stops the bot.
        """
        await super().stop()
        logger.info("Bot stopped.")
