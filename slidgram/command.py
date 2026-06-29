from typing import TYPE_CHECKING

from slidge.command import Command, CommandAccess, FormField
from slidge.command.base import FormSession
from slidge.command.categories import GROUPS
from slixmpp import JID

if TYPE_CHECKING:
    from .session import Session

class JoinPublicChat(Command["Session"]):
    NAME = "🚪 Join a telegram chat"
    HELP = "Join a public channel, private group or supergroup"
    NODE = CHAT_COMMAND = "join-chat"
    ACCESS = CommandAccess.USER_LOGGED
    INSTRUCTIONS = "Use a tg:// URI or a or a https://t.me URL to join a group"
    CATEGORY = GROUPS

    async def run(
        self, session: "Session | None", ifrom: JID, *args: str
    ) -> FormSession["Session"]:
        return FormSession(
            title=self.NAME,
            instructions=self.INSTRUCTIONS,
            fields=[FormField("query", label="Username, tg:// or t.me URL")],
            handler=self.finish,
        )

    @staticmethod
    async def finish(
        form_values: dict[str, str], session: "Session", _ifrom: JID
    ) -> str:
        chat_name: str = form_values["query"]
        if chat_name.startswith("http://"):
            chat_name = "https://" + chat_name[7:]

        chat_name = chat_name.replace("https://t.me/s/", "https://t.me/")

        chat = await session.tg.join_chat(chat_name)
        muc = await session.bookmarks.by_tg_id(chat.id)
        return f"You can now join '{chat.title}' at xmpp:{muc.jid}?join"
