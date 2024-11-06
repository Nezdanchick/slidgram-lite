from typing import TYPE_CHECKING

from slidge import FormField
from slidge.command import Command, CommandAccess, Form
from slidge.command.categories import GROUPS

if TYPE_CHECKING:
    from .session import Session


class JoinPublicChat(Command):
    NAME = "🚪 Join a telegram chat"
    HELP = "Join a public channel, private group or supergroup"
    NODE = CHAT_COMMAND = "join-chat"
    ACCESS = CommandAccess.USER_LOGGED
    INSTRUCTIONS = "Use a tg:// URI or a or a https://t.me URL to join a group"
    CATEGORY = GROUPS

    async def run(self, _session, _ifrom, *_args):
        return Form(
            title=self.NAME,
            instructions=self.INSTRUCTIONS,
            fields=[FormField("query", label="Username, tg:// or t.me URL")],
            handler=self.finish,  # type:ignore
        )

    @staticmethod
    async def finish(form_values: dict, session: "Session", _ifrom):
        chat_name: str = form_values["query"]
        if chat_name.startswith("http://"):
            chat_name = "https://" + chat_name[7:]

        # The /s/ part is for web preview of telegram chats and is not accepted by pyrofork's API
        chat_name = chat_name.replace("https://t.me/s/", "https://t.me/")

        chat = await session.tg.join_chat(chat_name)
        muc = await session.bookmarks.by_legacy_id(chat.id)
        return f"You can now join '{chat.title}' at xmpp:{muc.jid}?join"
