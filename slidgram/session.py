import logging
from io import BytesIO

import aiohttp
from PIL import Image
from pyrogram.enums import ChatAction, ChatType
from pyrogram.raw.base import Peer, SendMessageAction, Update
from pyrogram.raw.base.contacts import ImportedContacts
from pyrogram.raw.types import (
    ChatParticipant,
    ChatParticipantAdmin,
    ChatParticipantCreator,
    ChatParticipantsForbidden,
    PeerChat,
    PeerUser,
    SendMessageCancelAction,
    SendMessageTypingAction,
    UpdateChannelUserTyping,
    UpdateChatParticipants,
    UpdateChatUserTyping,
    UpdatePinnedMessages,
    UpdateReadChannelInbox,
    UpdateReadHistoryInbox,
    UpdateReadHistoryOutbox,
    UpdateUserTyping,
)
from pyrogram.types import (
    Chat,
    ChatMemberUpdated,
    InputPhoneContact,
    Message,
    MessageReactionUpdated,
    PeerChannel,
    User,
)
from pyrogram.utils import get_channel_id
from slidge import BaseSession, FormField, SearchResult
from slidge.util.types import Mention, RecipientType
from slixmpp.exceptions import XMPPError

from .contact import Contact
from .errors import catch_peer_id_invalid, tg_to_xmpp_errors
from .gateway import Gateway
from .group import MUC, Bookmarks, Participant
from .telegram import Client as TelegramClient
from .text_entities import styling_to_entities

Recipient = Contact | MUC


class Session(BaseSession[int, Recipient]):
    xmpp: Gateway
    bookmarks: Bookmarks

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.__init_tg()

    def __init_tg(self):
        self.tg = TelegramClient(self.user_jid.bare, self.user.legacy_module_data)

        # need to be in a different group than other handlers or else it's not used
        self.tg.on_raw_update(group=10)(self._on_tg_raw)  # type:ignore
        self.tg.on_message(group=20)(self._on_tg_msg)  # type:ignore
        self.tg.on_user_status(group=20)(self._on_tg_status)  # type:ignore
        self.tg.on_edited_message(group=20)(self._on_tg_edit)  # type:ignore
        self.tg.on_chat_member_updated(group=20)(self._on_tg_chat_member)  # type:ignore
        self.tg.on_deleted_messages(group=20)(self._on_tg_deleted_msg)  # type:ignore
        # on_reaction is not a standard pyrogram hook, hence the different syntax
        self.tg.on_reaction(self._on_tg_reaction)

    @staticmethod
    def xmpp_to_legacy_msg_id(i: str) -> int:
        return int(i)

    @tg_to_xmpp_errors
    async def login(self):
        await self.tg.start()
        me = self.tg.me
        assert me is not None
        self.contacts.user_legacy_id = me.id
        my_name = me.full_name.strip()
        self.bookmarks.user_nick = my_name
        return f"Connected as {my_name}"

    @tg_to_xmpp_errors
    async def logout(self):
        await self.tg.stop()

    @tg_to_xmpp_errors
    async def on_text(
        self,
        chat: Recipient,
        text: str,
        *,
        reply_to_msg_id: int | None = None,
        mentions: list[Mention] | None = None,
        **_kwargs,
    ) -> int:
        text, entities = await styling_to_entities(text, mentions)
        message = await self.tg.send_message(
            chat.legacy_id,
            text,
            reply_to_message_id=reply_to_msg_id,  # type:ignore
            entities=entities,
        )
        return message.id

    @tg_to_xmpp_errors
    async def on_correct(
        self,
        chat: Recipient,
        text: str,
        legacy_msg_id: int,
        *,
        reply_to_msg_id: int | None = None,
        mentions: list[Mention] | None = None,
        **_kwargs,
    ) -> None:
        text, entities = await styling_to_entities(text, mentions)
        await self.tg.edit_message_text(
            chat.legacy_id, legacy_msg_id, text, entities=entities
        )

    @tg_to_xmpp_errors
    async def on_file(
        self,
        chat: RecipientType,
        url: str,
        *,
        http_response: aiohttp.ClientResponse,
        reply_to_msg_id: int | None = None,
        **_kwargs,
    ) -> int:
        if http_response.content_type.startswith("audio"):
            message = await self.tg.send_audio(chat.legacy_id, url)
        elif http_response.content_type.startswith("video"):
            message = await self.tg.send_video(chat.legacy_id, url)
        elif http_response.content_type.startswith("image"):
            message = await self.tg.send_photo(chat.legacy_id, url)
        else:
            message = await self.tg.send_document(chat.legacy_id, url)
        if message is None:
            raise XMPPError(
                "internal-server-error", "Telegram did not confirm this message"
            )
        return message.id

    @tg_to_xmpp_errors
    async def on_react(
        self,
        chat: Recipient,
        legacy_msg_id: int,
        emojis: list[str],
        thread=None,
    ):
        await self.tg.send_reaction(
            chat.legacy_id,
            legacy_msg_id,
            emoji=emojis,  # type:ignore
        )

    @tg_to_xmpp_errors
    async def on_composing(self, chat: RecipientType, thread=None):
        await self.tg.send_chat_action(chat.legacy_id, ChatAction.TYPING)

    @tg_to_xmpp_errors
    async def on_paused(self, chat: RecipientType, thread=None):
        await self.tg.send_chat_action(chat.legacy_id, ChatAction.CANCEL)

    @tg_to_xmpp_errors
    async def on_displayed(self, chat: RecipientType, legacy_msg_id: int, thread=None):
        await self.tg.read_chat_history(chat.legacy_id, legacy_msg_id)

    @tg_to_xmpp_errors
    async def on_moderate(
        self,
        muc: MUC,  # type:ignore
        legacy_msg_id: int,
        reason: str | None,
    ):
        if (
            await self.tg.delete_messages(muc.legacy_id, [legacy_msg_id], revoke=True)
            == 0
        ):
            raise XMPPError(
                "internal-server-error", "Telegram did not accept this message deletion"
            )
        me = await muc.get_user_participant()
        me.moderate(legacy_msg_id)

    @tg_to_xmpp_errors
    async def on_create_group(  # type:ignore
        self,
        name: str,
        contacts: list[Contact],  # type:ignore
    ) -> int:
        group = await self.tg.create_group(name, [c.legacy_id for c in contacts])
        return group.id

    @tg_to_xmpp_errors
    async def on_invitation(self, contact: Contact, muc: MUC, reason: str | None):
        await self.tg.add_chat_members(muc.legacy_id, contact.legacy_id)

    @tg_to_xmpp_errors
    async def on_retract(
        self,
        chat: Recipient,
        legacy_msg_id: int,
        thread=None,
    ):
        await self.tg.delete_messages(chat.legacy_id, [legacy_msg_id], revoke=True)

    async def on_avatar(
        self,
        bytes_: bytes | None,
        hash_: str | None,
        type_: str | None,
        width: int | None,
        height: int | None,
    ) -> None:
        it = self.tg.get_chat_photos("me")
        assert it is not None
        async for photo in it:
            self.log.debug("Deleting my picture: %s", photo)
            success = await self.tg.delete_profile_photos(photo.file_id)

            if not success:
                raise XMPPError(
                    "internal-server-error", "Couldn't unset telegram avatar"
                )

        if bytes_ is None:
            return

        if not type_ or not any(x in type_.lower() for x in ("jpg", "jpeg")):
            img = Image.open(BytesIO(bytes_))
            self.log.debug("Image needs conversion")
            with BytesIO() as f:
                img_no_alpha = await self.xmpp.loop.run_in_executor(
                    None, img.convert, "RGB"
                )
                await self.xmpp.loop.run_in_executor(None, img_no_alpha.save, f, "JPEG")
                f.flush()
                f.seek(0)
                f.name = "slidge-upload.jpg"
                success = await self.tg.set_profile_photo(photo=f)
        else:
            with BytesIO(bytes_) as f:
                f.flush()
                f.seek(0)
                f.name = "slidge-upload.jpg"
                success = await self.tg.set_profile_photo(photo=f)

        if not success:
            raise XMPPError("internal-server-error", "Couldn't set telegram avatar")

    @tg_to_xmpp_errors
    async def on_search(self, form_values: dict[str, str]) -> SearchResult | None:
        imported: ImportedContacts = await self.tg.import_contacts(
            contacts=[
                InputPhoneContact(
                    form_values["phone"],
                    first_name=form_values["first"],
                    last_name=form_values.get("last", ""),
                )
            ]
        )
        if len(imported.imported) == 0:
            return None

        contact = await self.contacts.by_legacy_id(imported.imported[0].user_id)

        return SearchResult(
            description="This telegram contact has been added to your roster.",
            fields=[
                FormField("name", "Name"),
                FormField("jid", "JID", type="jid-single"),
            ],
            items=[{"user_id": contact.name, "jid": contact.jid}],
        )

    @tg_to_xmpp_errors
    async def on_leave_group(self, chat_id: int):
        await self.tg.leave_chat(chat_id)

    @catch_peer_id_invalid
    async def _on_tg_msg(self, _tg: TelegramClient, message: Message) -> None:
        sender, carbon = await self.get_sender(message)
        await sender.send_tg_msg(message, carbon=carbon)

    @catch_peer_id_invalid
    async def _on_tg_edit(self, _tg: TelegramClient, message: Message) -> None:
        if message.edit_date is None:
            return

        sender, carbon = await self.get_sender(message)
        await sender.send_tg_msg(message, carbon=carbon, correction=True)

    @catch_peer_id_invalid
    async def _on_tg_status(self, _tg: TelegramClient, user: User) -> None:
        if self.tg.is_me(user):
            return
        contact = await self.contacts.by_legacy_id(user.id)
        contact.update_tg_status(user)

    @catch_peer_id_invalid
    async def _on_tg_chat_member(self, _tg, update: ChatMemberUpdated):
        muc = await self.bookmarks.by_legacy_id(update.chat.id)
        part = await muc.get_participant_by_legacy_id(update.new_chat_member.user.id)
        part.update_tg_member(update.new_chat_member)

    # this is a handler for a custom event we added to our pyrogram.Client
    # subclass.

    @catch_peer_id_invalid
    async def _on_tg_reaction(
        self, message: Message, user_id: int, emoji: str | None
    ) -> None:
        emojis = [] if emoji is None else [emoji]

        if message.chat.type in (ChatType.PRIVATE, ChatType.BOT):
            if self.tg.is_me(user_id):
                contact = await self.contacts.by_legacy_id(message.chat.id)
                contact.react(message.id, emojis, carbon=True)
            else:
                contact = await self.contacts.by_legacy_id(user_id)
                contact.react(message.id, emojis)
            return

        muc = await self.bookmarks.by_legacy_id(message.chat.id)
        participant = await muc.get_participant_by_legacy_id(user_id)
        participant.react(message.id, emojis)

    @catch_peer_id_invalid
    async def _on_tg_deleted_msg(self, _tg, messages: list[Message]) -> None:
        for message in messages:
            msg_id = message.id
            message = self.tg.message_cache.get_by_message_id(msg_id)
            if message is None:
                self.log.warning(
                    "Received a message deletion event, but we don't know which chat it belongs to!"
                )
                continue
            sender, carbon = await self.get_sender(message)
            if hasattr(sender, "muc"):
                sender.muc.get_system_participant().moderate(message.id)
            else:
                sender.retract(message.id, carbon=carbon)

    # these are "raw" telegram updates that are not processed at all by
    # pyrogram
    async def _on_tg_raw(
        self, _tg, update: Update, users: dict[int, User], chats: dict[int, Chat]
    ) -> None:
        name = update.QUALNAME.split(".")[-1]
        handler = getattr(self, f"_on_tg_{name}", None)
        if handler is None:
            self.log.debug("No handler for: %s", name)
            return
        try:
            await handler(update, users, chats)
        except Exception as e:
            self.log.exception("Exception raised in %s: %s", handler, e, exc_info=e)

    @catch_peer_id_invalid
    async def _on_tg_UpdateUserTyping(
        self, update: UpdateUserTyping, _users, _chats
    ) -> None:
        actor = await self.contacts.by_legacy_id(update.user_id)
        self._send_action(actor, update.action)

    @catch_peer_id_invalid
    async def _on_tg_UpdateChatUserTyping(
        self, update: UpdateChatUserTyping, _users, _chats
    ) -> None:
        muc = await self.bookmarks.by_legacy_id(-update.chat_id)
        if isinstance(update.from_id, PeerUser):
            actor = await muc.get_participant_by_legacy_id(update.from_id.user_id)
        else:
            self.log.warning("Unknown peer: %s", update)
            return
        self._send_action(actor, update.action)

    @catch_peer_id_invalid
    async def _on_tg_UpdateChannelUserTyping(
        self, update: UpdateChannelUserTyping, _users, _chats
    ) -> None:
        # TESTME
        muc = await self.bookmarks.by_legacy_id(get_channel_id(update.channel_id))
        if isinstance(update.from_id, PeerUser):
            actor = await muc.get_participant_by_legacy_id(update.from_id.user_id)
        else:
            self.log.warning("Unknown peer: %s", update)
            return
        self._send_action(actor, update.action)

    def _send_action(
        self, actor: Contact | Participant, action: SendMessageAction
    ) -> None:
        if isinstance(action, SendMessageTypingAction):
            actor.composing()
        elif isinstance(action, SendMessageCancelAction):
            actor.paused()
        else:
            self.log.warning("Unknown action: %s for %s", action, actor)

    @catch_peer_id_invalid
    async def _on_tg_UpdateReadHistoryOutbox(
        self, update: UpdateReadHistoryOutbox, _users, _chats
    ) -> None:
        actor = await self._get_actor_by_peer(update.peer)
        actor.displayed(update.max_id)

    @catch_peer_id_invalid
    async def _on_tg_UpdateReadHistoryInbox(
        self, update: UpdateReadHistoryInbox, _users, _chats: list[Chat]
    ) -> None:
        actor = await self._get_actor_by_peer(update.peer)
        actor.displayed(update.max_id, carbon=True)

    @catch_peer_id_invalid
    async def _on_tg_UpdateReadChannelInbox(
        self, update: UpdateReadChannelInbox, _users, _chats
    ) -> None:
        pass

    @catch_peer_id_invalid
    async def _on_tg_UpdatePinnedMessages(
        self, update: UpdatePinnedMessages, _users, _chats
    ) -> None:
        muc = await self._get_muc_by_peer(update.peer)
        if muc is None:
            return

        await muc.set_tg_pinned_message_ids(update.messages, update.pinned)

    @catch_peer_id_invalid
    async def _on_tg_UpdateChatParticipants(
        self,
        update: UpdateChatParticipants,
        _users: dict[int, User],
        _chats: dict[int, Chat],
    ) -> None:
        muc = await self.bookmarks.by_legacy_id(-update.participants.chat_id)
        if isinstance(update.participants, ChatParticipantsForbidden):
            self.log.warning(
                "Received ChatParticipantsForbidden: %s", update.participants
            )
            return
        for tg_participant in update.participants.participants:
            participant = await muc.get_participant_by_legacy_id(tg_participant.user_id)
            if isinstance(tg_participant, ChatParticipant):
                participant.affiliation = "member"
                participant.role = "participant"
            elif isinstance(tg_participant, ChatParticipantAdmin):
                participant.affiliation = "admin"
                participant.role = "moderator"
            elif isinstance(tg_participant, ChatParticipantCreator):
                participant.affiliation = "owner"
                participant.role = "moderator"
            else:
                self.log.warning("Unknown participant: %s", tg_participant)

    async def get_sender(
        self,
        update: Message | MessageReactionUpdated,
    ) -> tuple[Contact | Participant, bool]:
        if update.chat.type in (ChatType.PRIVATE, ChatType.BOT):
            if self.tg.is_me(update.from_user):
                return await self.contacts.by_legacy_id(update.chat.id), True
            else:
                return await self.contacts.by_legacy_id(update.from_user.id), False

        muc = await self.bookmarks.by_legacy_id(update.chat.id)
        if update.from_user is not None:
            return await muc.get_participant_by_legacy_id(update.from_user.id), False
        if update.sender_chat:
            return muc.get_system_participant(), True
        if update.sender_business_bot is not None:
            return (
                await muc.get_participant_by_legacy_id(update.sender_business_bot.id),
                False,
            )
        else:
            raise RuntimeError(f"Unable to determine who sent this: {update}")

    async def _get_actor_by_peer(self, peer: Peer) -> Contact | Participant:
        if isinstance(peer, PeerUser):
            return await self.contacts.by_legacy_id(peer.user_id)
        elif isinstance(peer, PeerChat):
            muc = await self.bookmarks.by_legacy_id(-peer.chat_id)
        elif isinstance(peer, PeerChannel):
            muc = await self.bookmarks.by_legacy_id(get_channel_id(peer.channel_id))
        else:
            raise RuntimeError("Invalid peer", peer)
        return muc.get_system_participant()

    async def _get_muc_by_peer(self, peer: Peer) -> MUC | None:
        if isinstance(peer, PeerUser):
            return None
        if isinstance(peer, PeerChat):
            return await self.bookmarks.by_legacy_id(-peer.chat_id)
        if isinstance(peer, PeerChannel):
            return await self.bookmarks.by_legacy_id(get_channel_id(peer.channel_id))
        return None


log = logging.getLogger(__name__)
