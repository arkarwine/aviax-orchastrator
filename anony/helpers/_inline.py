# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


from pyrogram import enums, types

from anony import app, config, lang
from anony.core.lang import lang_codes


MODERATION_HELP = {
    "mod_core": (
        "🛡️ <u><b>Moderation</b></u>\n"
        "<i>For everyday admin action. Requires matching admin permissions.</i>\n\n"
        "/ban, /kick, /unban\n"
        "/mute, /tmute, /unmute\n"
        "/warn, /warns, /resetwarns\n"
        "/setwarnslimit, /setwarnsaction\n"
        "/purge, /pin, /unpin, /unpinall\n"
        "/cleanservice, /antichannelpin"
    ),
    "mod_spam": (
        "🧹 <u><b>Anti-Spam</b></u>\n"
        "<i>Deletes obvious spam and integrates with warnings.</i>\n\n"
        "/antispam on|off\n"
        "/spamfilter word\n"
        "/delspamfilter word\n"
        "/spamfilters\n"
        "/spamallow user|link|forward value\n"
        "/delspamallow user|link|forward value\n"
        "/spamallowlist"
    ),
    "mod_notes": (
        "🗒️ <u><b>Notes & Filters</b></u>\n"
        "<i>Save useful replies and automatic keyword responses.</i>\n\n"
        "/filter \"trigger\" response\n"
        "/delfilter trigger\n"
        "/filters\n"
        "/note \"name\" content\n"
        "/note name\n"
        "/delnote name\n"
        "/notes\n"
        "#name"
    ),
    "mod_welcome": (
        "👋 <u><b>Welcome & Rules</b></u>\n"
        "<i>Welcomes use chat-member updates, so they do not depend only on visible service messages.</i>\n\n"
        "/rules\n"
        "/setrules text\n"
        "/resetrules\n"
        "/welcome on|off\n"
        "/setwelcome Welcome {mention} to {chat}!\n"
        "/resetwelcome\n"
        "/welcomeformat"
    ),
    "mod_mentions": (
        "📣 <u><b>Mentions</b></u>\n"
        "<i>Batch member calls with delay controls to reduce flood risk.</i>\n\n"
        "/all text\n"
        "/callall text\n"
        "/call amount batch text\n"
        "/calladmins text\n"
        "/anybody text\n"
        "/allstatus\n"
        "/stopcall\n"
        "/setall batch|delay|hidden|admins value"
    ),
    "mod_utils": (
        "🧰 <u><b>Group Utilities</b></u>\n"
        "<i>Small group helpers that are useful during moderation.</i>\n\n"
        "/id\n"
        "/info\n"
        "/admins\n"
        "/report"
    ),
}


class Inline:
    def __init__(self):
        self.ikm = types.InlineKeyboardMarkup
        self.ikb = types.InlineKeyboardButton

    def cancel_dl(self, text) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb(
                        text=text,
                        callback_data="cancel_dl",
                        style=enums.ButtonStyle.DANGER,
                    )
                ]
            ]
        )

    def controls(
        self,
        chat_id: int,
        status: str = None,
        timer: str = None,
        remove: bool = False,
    ) -> types.InlineKeyboardMarkup:
        keyboard = []
        if status:
            keyboard.append(
                [
                    self.ikb(
                        text=status,
                        callback_data=f"controls status {chat_id}",
                        style=enums.ButtonStyle.PRIMARY,
                    )
                ]
            )
        elif timer:
            keyboard.append(
                [
                    self.ikb(
                        text=timer,
                        callback_data=f"controls status {chat_id}",
                        style=enums.ButtonStyle.PRIMARY,
                    )
                ]
            )

        if not remove:
            keyboard.append(
                [
                    self.ikb(
                        text="▷",
                        callback_data=f"controls resume {chat_id}",
                        style=enums.ButtonStyle.SUCCESS,
                    ),
                    self.ikb(
                        text="II",
                        callback_data=f"controls pause {chat_id}",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    self.ikb(text="⥁", callback_data=f"controls replay {chat_id}"),
                    self.ikb(
                        text="‣‣I",
                        callback_data=f"controls skip {chat_id}",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    self.ikb(
                        text="▢",
                        callback_data=f"controls stop {chat_id}",
                        style=enums.ButtonStyle.DANGER,
                    ),
                ]
            )
            keyboard.append(
                [
                    self.ikb(
                        text="📋 Queue",
                        callback_data=f"controls queue {chat_id}",
                    ),
                    self.ikb(
                        text="🎵 Song",
                        callback_data=f"song_request {chat_id}",
                    ),
                    self.ikb(
                        text="🔁 Loop",
                        callback_data=f"controls loop {chat_id}",
                    ),
                ]
            )
        return self.ikm(keyboard)

    async def help_markup(
        self,
        _lang: dict,
        back: bool = False,
        user_id: int | None = None,
    ) -> types.InlineKeyboardMarkup:
        if back:
            rows = [
                [
                    self.ikb(
                        text=f"↩️ {_lang['back']}",
                        callback_data="help back",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    self.ikb(
                        text=f"✖️ {_lang['close']}",
                        callback_data="help close",
                        style=enums.ButtonStyle.DANGER,
                    ),
                ]
            ]
        else:
            cbs = [
                "admins",
                "auth",
                "mod",
                "blist",
                "lang",
                "ping",
                "play",
                "queue",
                "stats",
                "sudo",
            ]
            icons = {
                "admins": "🛡️",
                "auth": "🔐",
                "mod": "🧰",
                "blist": "🚫",
                "lang": "🌐",
                "ping": "📶",
                "play": "🎵",
                "queue": "📜",
                "stats": "📊",
                "sudo": "👑",
            }
            labels = {
                "admins": _lang["help_0"],
                "auth": _lang["help_1"],
                "mod": _lang.get("help_mod", "Moderation"),
                "blist": _lang["help_2"],
                "lang": _lang["help_3"],
                "ping": _lang["help_4"],
                "play": _lang["help_5"],
                "queue": _lang["help_6"],
                "stats": _lang["help_7"],
                "sudo": _lang["help_8"],
            }
            buttons = [
                self.ikb(
                    text=f"{icons[cb]} {labels[cb]}",
                    callback_data=f"help {cb}",
                    style=(
                        enums.ButtonStyle.DANGER
                        if cb == "sudo"
                        else enums.ButtonStyle.SUCCESS
                        if cb == "mod"
                        else enums.ButtonStyle.PRIMARY
                    ),
                )
                for cb in cbs
                if (
                    (cb != "sudo" or user_id == app.owner or (user_id is not None and user_id in app.sudoers))
                    and (cb != "mod" or config.MODERATION_ENABLED)
                )
            ]
            rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]

        return self.ikm(rows)

    def moderation_markup(self, _lang: dict) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb("🛡️ Moderation", callback_data="help mod_core", style=enums.ButtonStyle.PRIMARY),
                    self.ikb("🧹 Anti-Spam", callback_data="help mod_spam", style=enums.ButtonStyle.SUCCESS),
                ],
                [
                    self.ikb("🗒️ Notes", callback_data="help mod_notes", style=enums.ButtonStyle.PRIMARY),
                    self.ikb("👋 Welcome", callback_data="help mod_welcome", style=enums.ButtonStyle.PRIMARY),
                ],
                [
                    self.ikb("📣 Mentions", callback_data="help mod_mentions", style=enums.ButtonStyle.SUCCESS),
                    self.ikb("🧰 Utilities", callback_data="help mod_utils", style=enums.ButtonStyle.PRIMARY),
                ],
                [
                    self.ikb(f"↩️ {_lang['back']}", callback_data="help back", style=enums.ButtonStyle.PRIMARY),
                    self.ikb(f"✖️ {_lang['close']}", callback_data="help close", style=enums.ButtonStyle.DANGER),
                ],
            ]
        )

    def moderation_back_markup(self, _lang: dict) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb("↩️ Moderation", callback_data="help mod", style=enums.ButtonStyle.PRIMARY),
                    self.ikb(f"✖️ {_lang['close']}", callback_data="help close", style=enums.ButtonStyle.DANGER),
                ]
            ]
        )

    def moderation_help_text(self, section: str) -> str:
        return MODERATION_HELP.get(
            section,
            "🧰 <b>Moderation tools</b>\n\nChoose a category below.",
        )

    def lang_markup(self, _lang: str) -> types.InlineKeyboardMarkup:
        langs = lang.get_languages()

        buttons = [
            self.ikb(
                text=f"{name} ({code}) {'✔️' if code == _lang else ''}",
                callback_data=f"lang_change {code}",
                style=(
                    enums.ButtonStyle.SUCCESS
                    if code == _lang
                    else enums.ButtonStyle.DEFAULT
                ),
            )
            for code, name in langs.items()
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        return self.ikm(rows)

    def ping_markup(self, text: str) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb(
                        text=text,
                        url=config.SUPPORT_CHAT,
                        style=enums.ButtonStyle.PRIMARY,
                    )
                ]
            ]
        )

    def setup_next_session(self) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb(
                        text="➡️ Next: Assistant Session",
                        url=f"https://t.me/{app.username}?start=addsession",
                        style=enums.ButtonStyle.SUCCESS,
                    )
                ]
            ]
        )

    def session_setup_method(self) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb(
                        text="📱 Phone Number",
                        callback_data="setup_session phone",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    self.ikb(
                        text="🔑 Session String",
                        callback_data="setup_session string",
                        style=enums.ButtonStyle.SUCCESS,
                    ),
                ],
                [
                    self.ikb(
                        text="✖️ Cancel",
                        callback_data="setup_session cancel",
                        style=enums.ButtonStyle.DANGER,
                    )
                ],
            ]
        )

    def play_queued(
        self, chat_id: int, item_id: str, _text: str
    ) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb(
                        text=_text,
                        callback_data=f"controls force {chat_id} {item_id}",
                        style=enums.ButtonStyle.SUCCESS,
                    )
                ]
            ]
        )

    def queue_markup(
        self, chat_id: int, _text: str, playing: bool
    ) -> types.InlineKeyboardMarkup:
        _action = "pause" if playing else "resume"
        return self.ikm(
            [
                [
                    self.ikb(
                        text=_text,
                        callback_data=f"controls {_action} {chat_id} q",
                        style=(
                            enums.ButtonStyle.PRIMARY
                            if playing
                            else enums.ButtonStyle.SUCCESS
                        ),
                    )
                ]
            ]
        )

    def settings_markup(
        self,
        lang: dict,
        admin_only: bool,
        cmd_delete: bool,
        language: str,
        chat_id: int,
    ) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb(
                        text=lang["play_mode"] + " ➜",
                        callback_data="settings",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    self.ikb(
                        text=admin_only,
                        callback_data="settings play",
                        style=(
                            enums.ButtonStyle.SUCCESS
                            if admin_only
                            else enums.ButtonStyle.DANGER
                        ),
                    ),
                ],
                [
                    self.ikb(
                        text=lang["cmd_delete"] + " ➜",
                        callback_data="settings",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    self.ikb(
                        text=cmd_delete,
                        callback_data="settings delete",
                        style=(
                            enums.ButtonStyle.SUCCESS
                            if cmd_delete
                            else enums.ButtonStyle.DANGER
                        ),
                    ),
                ],
                [
                    self.ikb(
                        text=lang["language"] + " ➜",
                        callback_data="settings",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    self.ikb(
                        text=lang_codes[language],
                        callback_data="language",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                ],
            ]
        )

    def start_key(
        self, lang: dict, private: bool = False
    ) -> types.InlineKeyboardMarkup:
        rows = [
            [
                self.ikb(
                    text=f"➕ {lang['add_me']}",
                    url=f"https://t.me/{app.username}?startgroup=true",
                    style=enums.ButtonStyle.SUCCESS,
                )
            ],
            [
                self.ikb(
                    text=f"❔ {lang['help']}",
                    callback_data="help",
                    style=enums.ButtonStyle.PRIMARY,
                ),
                self.ikb(
                    text=f"📊 {lang['help_7']}",
                    callback_data="stats",
                    style=enums.ButtonStyle.SUCCESS,
                ),
            ],
            [
                self.ikb(
                    text=f"💬 {lang['support']}",
                    url=config.SUPPORT_CHAT,
                    style=enums.ButtonStyle.PRIMARY,
                ),
                self.ikb(
                    text=f"📣 {lang['channel']}",
                    url=config.SUPPORT_CHANNEL,
                    style=enums.ButtonStyle.PRIMARY,
                ),
            ],
        ]
        if private:
            rows += [
                [
                    self.ikb(
                        text="👤 Owner",
                        url=config.OWNER_LINK or f"tg://user?id={app.owner}",
                        style=enums.ButtonStyle.PRIMARY,
                    )
                ]
            ]
        else:
            rows += [
                [
                    self.ikb(
                        text=f"🌍 {lang['language']}",
                        callback_data="language",
                        style=enums.ButtonStyle.PRIMARY,
                    )
                ]
            ]
        return self.ikm(rows)

    def stats_markup(self) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb(
                        text="🔄 Refresh",
                        callback_data="stats refresh",
                        style=enums.ButtonStyle.SUCCESS,
                    ),
                    self.ikb(
                        text="✖️ Close",
                        callback_data="stats close",
                        style=enums.ButtonStyle.DANGER,
                    ),
                ]
            ]
        )

    def queue_receipt(
        self, chat_id: int, queue_id: str, owner_id: int, allow_force: bool = True
    ) -> types.InlineKeyboardMarkup:
        rows = [
            [
                self.ikb(
                    text="🔄 Refresh",
                    callback_data=f"queue_request status {chat_id} {queue_id} {owner_id}",
                    style=enums.ButtonStyle.PRIMARY,
                ),
                self.ikb(
                    text="📋 Queue",
                    callback_data=f"queue_request queue {chat_id} {queue_id} {owner_id}",
                ),
                self.ikb(
                    text="🗑 Remove",
                    callback_data=f"queue_request remove {chat_id} {queue_id} {owner_id}",
                    style=enums.ButtonStyle.DANGER,
                ),
            ]
        ]
        if allow_force:
            rows.append(
                [
                    self.ikb(
                        text="▶️ Play Next",
                        callback_data=f"controls force {chat_id} {queue_id}",
                        style=enums.ButtonStyle.SUCCESS,
                    )
                ]
            )
        return self.ikm(rows)

    def maintenance_receipt(
        self, chat_id: int, maintenance_id: str, owner_id: int
    ) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb(
                        text="🔄 Check Status",
                        callback_data=f"maintenance status {chat_id} {maintenance_id} {owner_id}",
                        style=enums.ButtonStyle.PRIMARY,
                    ),
                    self.ikb(
                        text="📋 View Saved",
                        callback_data=f"maintenance queue {chat_id} {maintenance_id} {owner_id}",
                    ),
                ],
                [
                    self.ikb(
                        text="🗑 Remove",
                        callback_data=f"maintenance remove {chat_id} {maintenance_id} {owner_id}",
                        style=enums.ButtonStyle.DANGER,
                    ),
                ],
            ]
        )

    def yt_key(self, link: str) -> types.InlineKeyboardMarkup:
        return self.ikm(
            [
                [
                    self.ikb(text="❐", copy_text=link),
                    self.ikb(
                        text="Youtube",
                        url=link,
                        style=enums.ButtonStyle.DANGER,
                    ),
                ],
            ]
        )
