# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


from pathlib import Path

from pyrogram import Client

from anony import config, logger


class Userbot(Client):
    def __init__(self):
        """
        Initializes the userbot with multiple clients.

        This method sets up clients for the userbot using predefined session strings.
        Each client is assigned a unique name based on the key in the `clients` dictionary.
        """
        self.clients = []
        self.reload_from_config()

    def available_slots(self) -> list[int]:
        return [
            slot
            for slot, key in enumerate(("SESSION1", "SESSION2", "SESSION3"), start=1)
            if getattr(config, key) and self.client_for_slot(slot) in self.clients
        ]

    def client_for_slot(self, slot: int) -> Client | None:
        return {
            1: self.one,
            2: self.two,
            3: self.three,
        }.get(slot)

    def reload_from_config(self) -> None:
        clients = (("one", "SESSION1"), ("two", "SESSION2"), ("three", "SESSION3"))
        for num, (key, string_key) in enumerate(clients, start=1):
            name = f"AnonyUB{num}"
            session = getattr(config, string_key)
            session_name = name
            if config.SESSION_PATH:
                session_dir = Path(config.SESSION_PATH)
                session_name = str(session_dir / name)
            setattr(
                self,
                key,
                self.build_client(session_name, session),
            )

    def build_client(self, session_name: str, session: str | None = None) -> Client:
        return Client(
            name=session_name,
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session,
        )

    async def boot_client(self, num: int, ub: Client):
        """
        Boot a client and perform initial setup.
        Args:
            num (int): The client number to boot (1, 2, or 3).
            ub (Client): The userbot client instance.
        Raises:
            SystemExit: If the client fails to send a message in the log group.
        """
        clients = {
            1: self.one,
            2: self.two,
            3: self.three,
        }
        client = clients[num]
        await client.start()
        try:
            await client.send_message(config.LOGGER_ID, "Assistant Started")
        except Exception as e:
            raise SystemExit(f"Assistant {num} failed to send message in log group.\n\nError: {e}")

        client.id = ub.me.id
        client.name = ub.me.first_name
        client.username = ub.me.username
        client.mention = ub.me.mention
        client.session_slot = num
        self.clients.append(client)

        logger.info(f"Assistant {num} started as @{client.username}")

    async def add_session(self, session: str) -> int:
        for num, attr in enumerate(("one", "two", "three"), start=1):
            key = f"SESSION{num}"
            if getattr(config, key):
                continue

            config.apply_runtime_config({key: session})
            session_name = f"AnonyUB{num}"
            if config.SESSION_PATH:
                session_name = str(Path(config.SESSION_PATH) / session_name)
            client = self.build_client(session_name, session)
            setattr(self, attr, client)
            await self.boot_client(num, client)
            return num

        raise ValueError("All assistant session slots are already configured.")

    async def boot(self):
        """
        Asynchronously starts the assistants.
        """
        if not config.LOGGER_ID:
            logger.info("Log group is not configured yet; skipping assistant startup.")
            return
        if config.SESSION1:
            await self.boot_client(1, self.one)
        if config.SESSION2:
            await self.boot_client(2, self.two)
        if config.SESSION3:
            await self.boot_client(3, self.three)

    async def exit(self):
        """
        Asynchronously stops the assistants.
        """
        for client in list(self.clients):
            try:
                await client.stop()
            except Exception:
                logger.exception("Could not stop assistant %s cleanly.", getattr(client, "name", "unknown"))
        self.clients.clear()
        logger.info("Assistants stopped.")
