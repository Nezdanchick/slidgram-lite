import asyncio
import functools
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

import aiotdlib
from aiotdlib import api as tgapi
from aiotdlib.api import BaseObject
from aiotdlib.client import RequestResult
from slidge.contact.roster import ContactIsUser
from slixmpp.exceptions import XMPPError

from . import config
from .group import MUC, NotAMember, Participant

if TYPE_CHECKING:
    from .contact import Contact
    from .session import Session


def get_base_kwargs(user_reg_form: dict):
    return dict(
        phone_number=user_reg_form["phone"],
        api_id=user_reg_form.get("api_id") or config.API_ID,
        api_hash=user_reg_form.get("api_hash") or config.API_HASH,
        database_encryption_key=config.TDLIB_KEY,
        files_directory=config.TDLIB_PATH,
    )


class Timeout(asyncio.TimeoutError, XMPPError):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        XMPPError.__init__(
            self, "remote-server-timeout", "Telegram did not respond in time"
        )


# since aiotdlib relies on its exceptions for flow control in certain calls,
# we maintain the original exceptions as a parent
class BadRequest(tgapi.BadRequest, XMPPError):
    def __init__(self, base: tgapi.BadRequest):
        self.code = base.code
        self.message = base.message
        XMPPError.__init__(self, "bad-request", self.message)


class Unauthorized(tgapi.Unauthorized, XMPPError):
    def __init__(self, base: tgapi.Unauthorized):
        self.code = base.code
        self.message = base.message
        XMPPError.__init__(self, "not-authorized", self.message)


class NotFound(tgapi.NotFound, XMPPError):
    def __init__(self, base: tgapi.NotFound):
        self.code = base.code
        self.message = base.message
        XMPPError.__init__(self, "item-not-found", self.message)


class CredentialsValidation(aiotdlib.Client):
    def __init__(self, registration_form: dict):
        super().__init__(**get_base_kwargs(registration_form))
        self.code_future: asyncio.Future[
            str
        ] = asyncio.get_running_loop().create_future()
        self.password = registration_form.get("password")

    async def _auth_get_code(self, code_type: str = "SMS"):
        return await self.code_future

    async def _auth_get_password(self):
        return self.password

    async def get_main_list_chats(self, limit=0):
        # do not prefetch any chats, unlike aiotdlib's default behaviour
        r = await self.cache.get_main_chat_list(limit)
        return r


class TelegramClient(aiotdlib.Client):
    def __init__(self, session: "Session"):
        super().__init__(
            parse_mode=aiotdlib.ClientParseMode.MARKDOWN,
            **get_base_kwargs(session.user.registration_form),
        )
        self.session = session
        self.contacts = session.contacts
        self.bookmarks = session.bookmarks
        self.log = self.session.log

        async def input_(prompt):
            self.session.send_gateway_status(f"Action required: {prompt}")
            return await session.input(prompt)

        self.input = input_
        self._auth_get_code = functools.partial(input_, "Enter code")  # type:ignore
        self._auth_get_password = functools.partial(  # type:ignore
            input_, "Enter 2FA password:"
        )
        self._auth_get_first_name = functools.partial(  # type:ignore
            input_, "Enter first name:"
        )
        self._auth_get_last_name = functools.partial(  # type:ignore
            input_, "Enter last name:"
        )

        self.add_event_handler(self.dispatch_update, tgapi.API.Types.ANY)
        self.ready = asyncio.Event()

    async def get_main_list_chats(self, limit=10):
        # only fetch 10 chats instead of aiotdlib's default of 100,
        # because it seems to take a while for some users
        r = await self.cache.get_main_chat_list(limit)
        return r

    async def request(  # type:ignore
        self,
        query: BaseObject,
        *,
        request_id: str = None,  # type:ignore
        request_timeout: int = 60,
    ) -> Optional[RequestResult]:
        try:
            return await super().request(  # type:ignore
                query, request_id=request_id, request_timeout=request_timeout
            )
        except asyncio.TimeoutError:
            raise XMPPError("remote-server-timeout", "Telegram did not respond in time")
        except tgapi.BadRequest as e:
            raise BadRequest(e)
        except tgapi.Unauthorized as e:
            raise Unauthorized(e)
        except tgapi.NotFound as e:
            raise NotFound(e)
        except RuntimeError as e:
            raise XMPPError("internal-server-error", str(e))

    async def dispatch_update(self, _self, update: tgapi.Update):
        if update.ID == "ok":
            return
        try:
            handler = getattr(self, "handle_" + update.ID[6:])
        except AttributeError:
            self.session.log.debug("No handler for %s, ignoring", update.ID)
        except IndexError:
            self.session.log.debug("Ignoring weird event: %s", update.ID)
        else:
            try:
                await handler(update)
            except ContactIsUser:
                pass
            except NotAMember as e:
                self.session.log.debug(
                    "Ignoring update because member status is %s", e.status.ID
                )

    async def handle_NewMessage(self, update: tgapi.UpdateNewMessage):
        msg = update.message
        if msg.is_outgoing:
            if msg.sending_state is not None:
                return
            if msg.id in self.session.sent:
                return

        sender = await self.__get_contact_or_participant(msg)
        await sender.send_tg_message(msg)

    async def handle_UserStatus(self, update: tgapi.UpdateUserStatus):
        if update.user_id == await self.get_my_id():
            return
        contact = await self.contacts.by_legacy_id(update.user_id)
        contact.update_status(update.status)

    async def handle_ChatReadOutbox(self, update: tgapi.UpdateChatReadOutbox):
        if await self.is_private_chat(update.chat_id):
            contact = await self.contacts.by_legacy_id(update.chat_id)
            contact.displayed(update.last_read_outbox_message_id)
        else:
            # telegram does not have individual read markers for groups,
            # this means "at least someone has read"
            # mapping to the room itself is not great, but is what seems more natural
            muc = await self.bookmarks.by_legacy_id(update.chat_id)
            p = muc.get_system_participant()
            p.displayed(update.last_read_outbox_message_id)

    async def handle_ChatAction(self, action: tgapi.UpdateChatAction):
        sender = action.sender_id
        if not isinstance(sender, tgapi.MessageSenderUser):
            self.log.debug("Ignoring action: %s", action)
            return

        chat_id = action.chat_id
        user_id = sender.user_id
        if chat_id == user_id:
            composer: Union[
                "Contact", "Participant"
            ] = await self.contacts.by_legacy_id(chat_id)
        else:
            muc: MUC = await self.bookmarks.by_legacy_id(chat_id)
            composer = await muc.participant_by_tg_user(await self.get_user(user_id))

        if isinstance(action.action, tgapi.ChatActionTyping):
            composer.composing()
        elif isinstance(action.action, tgapi.ChatActionCancel):
            composer.paused()

    async def handle_ChatReadInbox(self, action: tgapi.UpdateChatReadInbox):
        if not await self.is_private_chat(action.chat_id):
            return

        session = self.session
        msg_id = action.last_read_inbox_message_id
        self.log.debug(
            "Self read mark for %s and we sent %s", msg_id, session.sent_read_marks
        )
        try:
            session.sent_read_marks.remove(msg_id)
        except KeyError:
            # slidge didn't send this read mark, so it comes from the official tg client
            contact = await session.contacts.by_legacy_id(action.chat_id)
            contact.displayed(msg_id, carbon=True)

    async def __get_contact_or_participant(self, msg: tgapi.Message):
        session = self.session
        chat_id = msg.chat_id
        if await self.is_private_chat(chat_id):
            return await self.session.contacts.by_legacy_id(chat_id)
        muc = await session.bookmarks.by_legacy_id(chat_id)
        participant = await muc.participant_by_sender_id(msg.sender_id)
        return participant

    async def handle_MessageContent(self, action: tgapi.UpdateMessageContent):
        new_content = action.new_content
        if isinstance(new_content, tgapi.MessagePhoto):
            # Happens when the user send a picture, looks safe to ignore
            self.log.debug("Ignoring message photo update")
            return
        if not isinstance(new_content, tgapi.MessageText):
            self.log.warning("Ignoring message update: %s", new_content)
            return
        if new_content.web_page:
            self.log.debug("Ignoring update with web_page")
            return

        session = self.session
        corrected_msg_id = action.message_id
        chat_id = action.chat_id

        fut = session.user_correction_futures.pop(action.message_id, None)
        if fut is not None:
            self.log.debug("User correction confirmation received")
            fut.set_result(None)
            return

        try:
            msg = await self.api.get_message(chat_id, corrected_msg_id)
        except NotFound:
            self.log.debug("Ignoring update of message that cannot be found anymore.")
            return
        sender = await self.__get_contact_or_participant(msg)
        await sender.send_tg_message(msg, correction=True)

    async def handle_User(self, action: tgapi.UpdateUser):
        u = action.user
        if u.id == await self.get_my_id():
            return
        await self.session.contacts.by_legacy_id(u.id)

    async def handle_NewChat(self, action: tgapi.UpdateNewChat):
        if isinstance(action.chat.type_, tgapi.ChatTypePrivate):
            if action.chat.id == await self.get_my_id():
                return
            contact = await self.session.contacts.by_legacy_id(action.chat.id)
            user: tgapi.User = await contact.get_telegram_user()
            if not isinstance(user.type_, tgapi.UserTypeRegular):
                return
            if not user.is_contact and action.chat.last_message:
                contact.send_friend_request(
                    "We have a direct chat, do you want to add me as a Telegram contact?"
                )
            return
        try:
            if isinstance(action.chat.type_, tgapi.ChatTypeBasicGroup):
                g = await self.session.bookmarks.by_legacy_id(action.chat.id)
                await g.add_to_bookmarks(auto_join=True)
            elif isinstance(action.chat.type_, tgapi.ChatTypeSupergroup):
                await self.session.bookmarks.by_legacy_id(action.chat.id)
        except XMPPError as e:
            self.log.debug("Could not add group", exc_info=e)

    async def handle_MessageInteractionInfo(
        self, update: tgapi.UpdateMessageInteractionInfo
    ):
        if not await self.is_private_chat(update.chat_id):
            return await self.react_group(update)

        contact = await self.session.contacts.by_legacy_id(update.chat_id)
        me = await self.get_my_id()
        if update.interaction_info is None:
            contact.react(update.message_id, [])
            contact.react(update.message_id, [], carbon=True)
            return

        user_reactions = list[str]()
        contact_reactions = list[str]()
        # these sanity checks might not be necessary, but in doubt…
        for reaction in update.interaction_info.reactions:
            if not isinstance(reaction.type_, tgapi.ReactionTypeEmoji):
                continue
            if reaction.total_count == 1:
                if len(reaction.recent_sender_ids) != 1:
                    self.log.warning(
                        "Weird reactions (wrong count): %s",
                        update.interaction_info.reactions,
                    )
                    continue
                sender = reaction.recent_sender_ids[0]
                if isinstance(sender, tgapi.MessageSenderUser):
                    if sender.user_id == me:
                        user_reactions.append(reaction.type_.emoji)
                    elif sender.user_id == contact.legacy_id:
                        contact_reactions.append(reaction.type_.emoji)
                else:
                    self.log.warning(
                        "Weird reactions (neither me nor them): %s",
                        update.interaction_info.reactions,
                    )
            elif reaction.total_count == 2:
                user_reactions.append(reaction.type_.emoji)
                contact_reactions.append(reaction.type_.emoji)
            else:
                self.log.warning(
                    "Weird reactions (empty): %s", update.interaction_info.reactions
                )

        contact.react(update.message_id, contact_reactions)
        contact.react(update.message_id, user_reactions, carbon=True)

    async def react_group(self, update: tgapi.UpdateMessageInteractionInfo):
        muc = await self.bookmarks.by_legacy_id(update.chat_id)
        if update.interaction_info is None:
            while True:
                try:
                    reacter, _ = muc.reactions[update.message_id].pop()
                except KeyError:
                    return
                reacter.react(update.message_id)

        old_reacters = muc.reactions[update.message_id]
        new_reacters = set()
        for reaction in update.interaction_info.reactions:
            if not isinstance(reaction.type_, tgapi.ReactionTypeEmoji):
                continue
            emoji = reaction.type_.emoji

            for sender_id in reaction.recent_sender_ids:
                if isinstance(sender_id, tgapi.MessageSenderUser):
                    reacter = await muc.get_participant_by_legacy_id(sender_id.user_id)
                else:
                    reacter = muc.get_system_participant()
                new_reacters.add((reacter, emoji))

        self.log.debug("Old reacters: %s", old_reacters)
        self.log.debug("New reacters: %s", new_reacters)

        old_all_reacters = {x[0] for x in old_reacters}
        new_all_reacters = {x[0] for x in new_reacters}
        for unreacter in old_all_reacters - new_all_reacters:
            unreacter.react(update.message_id)
        for reacter, emoji in new_reacters - old_reacters:
            reacter.react(update.message_id, emoji)

        muc.reactions[update.message_id] = new_reacters

    async def handle_DeleteMessages(self, update: tgapi.UpdateDeleteMessages):
        if not update.is_permanent:  # tdlib send 'delete from cache' updates apparently
            self.log.debug("Ignoring non permanent delete")
            return

        direct = await self.is_private_chat(update.chat_id)

        if direct:
            contact = await self.session.contacts.by_legacy_id(update.chat_id)
        else:
            muc: "MUC" = await self.session.bookmarks.by_legacy_id(update.chat_id)
            p = muc.get_system_participant()

        for legacy_msg_id in update.message_ids:
            future = self.session.delete_futures.pop(legacy_msg_id, None)
            if future is not None:
                future.set_result(update)
                continue

            if direct:
                if legacy_msg_id in self.session.sent:
                    contact.retract(legacy_msg_id, carbon=True)
                else:
                    contact.retract(legacy_msg_id)
            else:
                p.moderate(legacy_msg_id)

    async def handle_MessageSendSucceeded(
        self, update: tgapi.UpdateMessageSendSucceeded
    ):
        self.session.sent_read_marks.add(update.message.id)
        for _ in range(10):
            try:
                future = self.session.ack_futures.pop(update.message.id)
            except KeyError:
                await asyncio.sleep(0.5)
            else:
                future.set_result(update.message.id)
                return
        self.log.warning("Ignoring Send success for %s", update.message.id)

    async def handle_BasicGroupFullInfo(self, update: tgapi.UpdateBasicGroupFullInfo):
        info = update.basic_group_full_info
        group = await self.get_basic_group(update.basic_group_id)
        muc: MUC = await self.session.bookmarks.by_group_id(group.id)
        await muc.update_info(info)

    async def is_private_chat(self, chat_id: int):
        chat = await self.get_chat(chat_id)
        return isinstance(chat.type_, tgapi.ChatTypePrivate)

    async def get_local_path(self, file: tgapi.File) -> Optional[Path]:
        # we want to limit calls as much as possible during login,
        # because aiotdlib will just raise an Exception if it takes too
        # long
        await self.ready.wait()
        if not file.local.path or not Path(file.local.path).exists():
            try:
                async with self.session.xmpp.download_semaphore:
                    file = await self.session.tg.api.download_file(
                        file_id=file.id,
                        synchronous=True,
                        priority=1,
                        offset=0,
                        limit=0,
                    )
            except Exception as e:
                self.log.error("Could not download %s", file, exc_info=e)
                return None

        return Path(file.local.path)

    async def send_formatted_text(
        self,
        chat_id: int,
        text: tgapi.FormattedText,
        *,
        reply_to_message_id: Optional[int] = None,
    ):
        return await self._Client__send_message(
            chat_id=chat_id,
            content=tgapi.InputMessageText.construct(text=text),
            reply_to_message_id=reply_to_message_id,
        )
