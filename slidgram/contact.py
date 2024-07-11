import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional, Union

import aiotdlib.api as tgapi
from slidge import LegacyContact, LegacyRoster, global_config
from slixmpp.exceptions import XMPPError

from . import config
from .util import AvailableEmojisMixin, TelegramToXMPPMixin

if TYPE_CHECKING:
    from .session import Session


class Contact(TelegramToXMPPMixin, AvailableEmojisMixin, LegacyContact[int]):
    DISCO_TYPE = "phone"
    session: "Session"

    UNKNOWN_RETRY_DELAY = 5
    UNKNOWN_RETRY_MAX_DELAY = 600
    UNKNOWN_MAX_ATTEMPTS = 10

    @property
    def chat_id(self):
        return self.legacy_id

    async def get_telegram_user(self, force_update: bool = False):
        return await self.session.tg.get_user(self.legacy_id, force_update=force_update)

    def update_status(self, status: tgapi.UserStatus):
        if self.legacy_id == self.session.contacts.user_legacy_id:
            # FIXME: This shouldn't happen but apparently it does
            return

        if isinstance(status, tgapi.UserStatusLastMonth):
            self.extended_away(
                (
                    "Offline since last month"
                    if global_config.LAST_SEEN_FALLBACK
                    else None
                ),
                last_seen=datetime.now() - timedelta(days=31),
            )
        elif isinstance(status, tgapi.UserStatusLastWeek):
            self.extended_away(
                "Offline since last week" if global_config.LAST_SEEN_FALLBACK else None,
                last_seen=datetime.now() - timedelta(days=7),
            )
        elif isinstance(status, tgapi.UserStatusOffline):
            task = _online_expires_task.get(self.legacy_id)
            if task is not None and task.done():
                # we've never seen the contact online, so we use the was_online timestamp
                self.away(last_seen=datetime.fromtimestamp(status.was_online))
        elif isinstance(status, tgapi.UserStatusOnline):
            task = _online_expires_task.get(self.legacy_id)
            if task is not None:
                task.cancel()
            self.online()
            _online_expires_task[self.legacy_id] = self.xmpp.loop.create_task(
                _expire_online(self.session, self.legacy_id, status.expires),
                name=str(self.legacy_id),
            )
            _online_expires_task[self.legacy_id].add_done_callback(_remove_task)
        elif isinstance(status, tgapi.UserStatusRecently):
            self.away(
                "Last seen recently" if global_config.LAST_SEEN_FALLBACK else None,
                last_seen=datetime.now(),
            )

    async def __fetch_avatar(self, user: tgapi.User):
        if photo := user.profile_photo:
            file = photo.big if config.BIG_AVATARS else photo.small
            if path := await self.session.tg.get_local_path(file):
                await self.set_avatar(path, photo.id)

    async def __fetch_profile(self):
        for i in range(self.UNKNOWN_MAX_ATTEMPTS):
            wait = min(self.UNKNOWN_RETRY_DELAY * i, self.UNKNOWN_RETRY_MAX_DELAY)
            self.log.debug("Waiting %s seconds before retrying to fetch my profile")
            await asyncio.sleep(wait)
            user = await self.get_telegram_user(force_update=True)
            if not isinstance(user.type_, tgapi.UserTypeUnknown):
                self.log.debug("Oh cool, now I'm not unknown anymore")
                await self.update_info(user)
                return
            self.log.debug("I'm still unknown!")
        self.log.warning("Giving up on trying to fetch details of an 'unknown user'")

    async def update_info(
        self, user: Optional[tgapi.User] = None, force_user_update=False
    ):
        if user is None:
            user = await self.get_telegram_user()

        full_name = " ".join([user.first_name, user.last_name]).strip()
        if usernames := user.usernames:
            self.name = usernames.editable_username
        elif full_name:
            # it might just be a whitespace at this stage, so we don't set it,
            # the participant ID will be displayed
            self.name = full_name
        elif isinstance(user.type_, tgapi.UserTypeUnknown):
            self.name = f"Unknown user #{self.legacy_id}"
            self.session.create_task(self.__fetch_profile())
        elif isinstance(user.type_, tgapi.UserTypeDeleted):
            self.name = f"Deleted user #{self.legacy_id}"
        else:
            self.log.error("Could not set name for %s", user)

        if photo := user.profile_photo:
            if self.avatar != photo.id:
                self.session.create_task(self.__fetch_avatar(user))
            else:
                self.log.debug("Cached photo is OK")
        else:
            self.log.debug("No avatar")
            self.avatar = None

        if isinstance(user.type_, tgapi.UserTypeBot) or user.id == 777000:
            # 777000 is not marked as bot, it's the "Telegram" contact, which gives
            # confirmation codes and announces telegram-related stuff
            self.DISCO_TYPE = "bot"

        if p := user.phone_number:
            phone = "+" + p
        else:
            phone = None
        self.set_vcard(
            given=user.first_name,
            surname=user.last_name,
            phone=phone,
            full_name=full_name,
        )

        self.is_friend = user.is_contact or self.DISCO_TYPE == "bot"
        if self.is_friend:
            await self.add_to_roster()
            self.update_status(user.status)

    async def on_friend_request(self, text=None):
        tg_user = await self.get_telegram_user()
        if tg_user.is_contact:
            return
        await self.session.tg.api.add_contact(
            contact=tgapi.Contact.model_construct(
                user_id=tg_user.id,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                phone_number=tg_user.phone_number,
            ),
            share_phone_number=False,
        )
        await self.accept_friend_request("I am your contact on telegram")

    async def on_friend_delete(self, text=None):
        tg_user = await self.get_telegram_user()
        if not tg_user.is_contact:
            return
        await self.session.tg.api.remove_contacts(user_ids=[self.legacy_id])


class Roster(LegacyRoster[int, Contact]):
    session: "Session"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.__fill_task: Optional[asyncio.Task] = None

    async def jid_username_to_legacy_id(self, jid_username: str) -> int:
        try:
            tg_id = int(jid_username)
        except ValueError:
            raise XMPPError("bad-request", "This is not a telegram user ID")
        else:
            if tg_id > 0:
                await self.session.tg.get_user(user_id=tg_id)
                return tg_id
            else:
                raise XMPPError("bad-request", "This looks like a telegram group ID")

    async def fill(self):
        if self.__fill_task is not None:
            self.__fill_task.cancel()
        self.__fill_task = self.session.xmpp.loop.create_task(
            self.session.tg.api.get_contacts()
        )


def _remove_task(task: asyncio.Task):
    try:
        _online_expires_task.pop(int(task.get_name()))
    except KeyError:
        pass
    log.debug("Tasks: %s", _online_expires_task)


async def _expire_online(session, legacy_id: int, timestamp: Union[int, float]):
    now = time.time()
    how_long = timestamp - now
    log.debug("Online status expires in %s seconds", how_long)
    await asyncio.sleep(how_long)
    contact = await session.contacts.by_legacy_id(legacy_id)
    contact.away(last_seen=datetime.fromtimestamp(timestamp))


_online_expires_task = dict[int, asyncio.Task]()

log = logging.getLogger(__name__)
