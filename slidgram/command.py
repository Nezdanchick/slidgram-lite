from datetime import datetime
from typing import TYPE_CHECKING

from aiotdlib import api as tgapi
from slidge import FormField
from slidge.command import Command, CommandAccess, Confirmation, Form, TableResult
from slixmpp import JID

if TYPE_CHECKING:
    from .session import Session


class SessionCommandMixin:
    INSTRUCTIONS: str = NotImplemented

    async def run(self, session, ifrom: JID, *args):
        assert session is not None
        tg_sessions: list[tgapi.Session] = (
            await session.tg.api.get_active_sessions()
        ).sessions
        if args:
            return await self.step2(
                {"tg-session": args[0]}, session, ifrom, tg_sessions
            )
        return Form(
            title="Telegram sessions",
            instructions=self.INSTRUCTIONS,
            fields=[
                FormField(
                    "tg-session",
                    type="list-single",
                    label="Session",
                    options=[
                        {
                            "label": f"{i}: {s.location} ({s.application_name})",
                            "value": str(i),
                        }
                        for i, s in enumerate(tg_sessions)
                    ],
                )
            ],
            handler=self.step2,  # type:ignore
            handler_args=[tg_sessions],
        )

    async def step2(
        self, form_values, _session, _ifrom, tg_sessions: list[tgapi.Session]
    ):
        raise NotImplementedError


class ListSessions(SessionCommandMixin, Command):
    NAME = "List telegram sessions"
    NODE = CHAT_COMMAND = "tg-sessions"
    ACCESS = CommandAccess.USER_LOGGED
    INSTRUCTIONS = "Pick a session for more details"

    async def step2(
        self, form_values, _session, _ifrom, tg_sessions: list[tgapi.Session]
    ):
        i = int(form_values["tg-session"])
        tg_session = tg_sessions[i]
        items = [
            {"name": n.removesuffix("_"), "value": str(getattr(tg_session, n))}
            for n in [
                "is_current",
                "type_",
                "application_name",
                "ip_address",
                "location",
            ]
        ]
        items.extend(
            {
                "name": n,
                "value": fmt_timestamp(getattr(tg_session, n, 0)),
            }
            for n in ["log_in_date", "last_active_date"]
        )
        return TableResult(
            description=f"Details of telegram session #{i}",
            fields=[FormField("name"), FormField("value")],
            items=items,  # type:ignore
        )


class TerminateSession(SessionCommandMixin, Command):
    NAME = "Terminate a telegram session"
    NODE = CHAT_COMMAND = "terminate-tg-session"
    ACCESS = CommandAccess.USER_LOGGED
    INSTRUCTIONS = "Pick a session to terminate it"

    async def step2(
        self, form_values, session, _ifrom, tg_sessions: list[tgapi.Session]
    ):
        assert session is not None
        i = int(form_values["tg-session"])
        tg_session = tg_sessions[i]
        return Confirmation(
            prompt=(
                f"Are you sure you want to terminate session #{i} "
                f"(last active on {fmt_timestamp(tg_session.last_active_date)})"
            ),
            success="The session has been terminated",
            handler=self.finish,  # type:ignore
            handler_args=[i],
        )

    @staticmethod
    async def finish(session: "Session", _ifrom, session_i: int):
        await session.tg.api.terminate_session(session_i)
        return "Session has been terminated"


class JoinPublicChat(Command):
    NAME = "Join a telegram public chat"
    HELP = "Join a public channel, private group or supergroup"
    NODE = CHAT_COMMAND = "join-chat"
    ACCESS = CommandAccess.USER_LOGGED
    INSTRUCTIONS = "Use a tg:// URI or a or a https://tg.me URL to join a group"

    async def run(self, _session, _ifrom, *_args):
        return Form(
            title=self.NAME,
            instructions=self.INSTRUCTIONS,
            fields=[FormField("query", label="Username, tg:// or t.me URL")],
            handler=self.finish,  # type:ignore
        )

    @staticmethod
    async def finish(form_values: dict, session: "Session", _ifrom):
        query: str = form_values["query"]
        query = query.removeprefix("https://t.me/").removeprefix("tg://resolve?domain=")
        chat = await session.tg.api.search_public_chat(query)
        await session.tg.api.join_chat(chat.id)
        muc = await session.bookmarks.by_legacy_id(chat.id)
        return f"You can now '{chat.title}' at xmpp:{muc.jid}?join"


class SearchPublicChats(Command):
    NAME = "Search telegram public chats"
    HELP = (
        "Searches public chats by looking for specified query in their "
        "username and title. Currently, only supergroups and "
        "channels can be public. Returns a meaningful number of results."
    )
    NODE = CHAT_COMMAND = "search-chats"
    ACCESS = CommandAccess.USER_LOGGED
    INSTRUCTIONS = "Enter search terms"

    async def run(self, _session, _ifrom, *_args):
        return Form(
            title=self.NAME,
            instructions=self.INSTRUCTIONS,
            fields=[FormField("query", label="Query")],
            handler=self.step2,  # type:ignore
        )

    async def step2(self, form_values: dict, session: "Session", _ifrom):
        query: str = form_values["query"]
        tg = session.tg
        resp = await tg.api.search_public_chats(query)
        chats = list[tgapi.Chat]()
        for chat_id in resp.chat_ids:
            chat = await session.tg.get_chat(chat_id)
            session.log.debug("Search result: %s", chat)
            if isinstance(chat.type_, tgapi.ChatTypePrivate):
                continue
            chats.append(chat)
        if not chats:
            return "No results"
        return Form(
            title="Search results",
            instructions="Select the chat you want to join",
            fields=[
                FormField(
                    "chat",
                    label="Group",
                    type="list-single",
                    options=[
                        {"label": chat.title, "value": str(chat.id)} for chat in chats
                    ],
                )
            ],
            handler=self.join,  # type:ignore
        )

    @staticmethod
    async def join(form_values: dict, session: "Session", _ifrom):
        chat_id = int(form_values["chat"])
        await session.tg.api.join_chat(chat_id)
        muc = await session.bookmarks.by_legacy_id(chat_id)
        return f"You can now join the chat at at xmpp:{muc.jid}?join"


def fmt_timestamp(t: int):
    return datetime.fromtimestamp(t).isoformat(timespec="minutes")
