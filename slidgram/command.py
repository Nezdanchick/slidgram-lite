from datetime import datetime

from aiotdlib import api as tgapi
from slidge import FormField
from slidge.core.command import Command, CommandAccess, Confirmation, Form, TableResult
from slixmpp import JID

from slidgram.session import Session


class SessionCommandMixin:
    INSTRUCTIONS: str = NotImplemented

    async def run(self, session, ifrom: JID, *args):
        assert session is not None
        tg_sessions = (await session.tg.api.get_active_sessions()).sessions
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
                        {"label": f"{i}: {s.country} ({s.region})", "value": str(i)}
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
                "ip",
                "country",
                "region",
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
            items=items,
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


def fmt_timestamp(t: int):
    return datetime.fromtimestamp(t).isoformat(timespec="minutes")
