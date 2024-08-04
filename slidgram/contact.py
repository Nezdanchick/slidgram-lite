import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pyrogram.enums import UserStatus
from pyrogram.types import Message, User
from slidge.contact import LegacyContact, LegacyRoster
from slixmpp.exceptions import XMPPError

from .avatar import SetAvatarMixin
from .errors import tg_to_xmpp_errors, tg_to_xmpp_errors_it
from .reactions import ReactionsMixin
from .tg_msg import TelegramMessageSenderMixin

if TYPE_CHECKING:
    from .gateway import Gateway
    from .session import Session


class Roster(LegacyRoster[int, "Contact"]):
    session: "Session"

    async def jid_username_to_legacy_id(self, jid_username: str) -> int:
        try:
            return int(jid_username)
        except ValueError:
            raise XMPPError("bad-request", f"Not an integer: {jid_username}")

    @tg_to_xmpp_errors_it
    async def fill(self):
        assert self.session.tg.me is not None
        for user in await self.session.tg.get_contacts():
            if user.id == self.session.tg.me.id:
                continue
            yield await self.by_legacy_id(user.id)


class Contact(
    ReactionsMixin, TelegramMessageSenderMixin, SetAvatarMixin, LegacyContact[int]
):
    xmpp: "Gateway"
    session: "Session"
    legacy_id: int

    async def get_tg_user(self) -> User:
        return await self.session.tg.get_user(self.legacy_id)

    @tg_to_xmpp_errors
    async def update_info(self):
        user = await self.get_tg_user()

        self.name = user.full_name
        self.is_friend = user.is_contact

        await self.update_chat_photo(user.photo)

        self.update_tg_status(user)

        if user.is_bot:
            self.client_type = "bot"
        else:
            self.client_type = "phone"

        self.set_vcard(
            given=user.first_name,
            surname=user.last_name,
            full_name=user.full_name,
            phone=user.phone_number,
        )

        if user.is_contact:
            self.session.create_task(self.add_to_roster())

    def update_tg_status(self, user: User):
        if user.status is None:
            self.offline()
            return

        if user.status == UserStatus.ONLINE:
            self.online(last_seen=user.last_online_date)
        elif user.status == UserStatus.RECENTLY:
            self.away(last_seen=user.last_online_date)
        elif user.last_online_date is not None:
            now = datetime.now()
            if now - user.last_online_date < timedelta(hours=1):
                self.away(last_seen=user.last_online_date)
            else:
                self.extended_away(last_seen=user.last_online_date)
        else:
            self.offline()

    async def send_tg_msg(
        self, message: Message, carbon=False, correction=False, archive_only=False
    ) -> None:
        await super().send_tg_msg(message, carbon=carbon, correction=correction)


log = logging.getLogger(__name__)
