import logging
from io import BytesIO
from typing import TYPE_CHECKING, Never

import pyrogram.raw.types as pyro_raw_types
from PIL import Image
from pyrogram.enums import ChatType, MessageServiceType
from pyrogram.raw.base import (  # type:ignore[attr-defined]
    Peer,
    SendMessageAction,
    Update,
)
from pyrogram.raw.base.contacts import ImportedContacts  # type:ignore[attr-defined]
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
from slidge import BaseSession
from slidge.command import FormField, SearchResult
from slidge.db import GatewayUser
from slixmpp.exceptions import XMPPError

from .errors import (
    ignore_event_on_peer_id_invalid,
    log_error_on_peer_id_invalid,
    tg_to_xmpp_errors,
)
from .telegram import Client as TelegramClient

if TYPE_CHECKING:
    from .contact import Contact, Roster
    from .group import MUC, Bookmarks, Participant


class Session(BaseSession["Contact"]):
    bookmarks: "Bookmarks"
    contacts: "Roster"
    
    # Store active sessions here so http_server can find them
    active_sessions: set["Session"] = set()

    def __init__(self, user: GatewayUser) -> None:
        super().__init__(user)
        self.__init_tg()

    def __init_tg(self) -> None:
        self.tg = TelegramClient(self.user_jid.bare)

        # need to be in a different group than other handlers or else it's not used
        self.tg.on_raw_update(group=10)(self._on_tg_raw)  # type:ignore[misc]
        self.tg.on_message(group=20)(self._on_tg_msg)
        self.tg.on_user_status(group=20)(self._on_tg_status)
        self.tg.on_edited_message(group=20)(self._on_tg_edit)
        self.tg.on_chat_member_updated(group=20)(self._on_tg_chat_member)
        self.tg.on_deleted_messages(group=20)(self._on_tg_deleted_msg)
        # on_reaction is not a standard pyrogram hook, hence the different syntax
        self.tg.on_reaction(self._on_tg_reaction)

    @staticmethod
    def xmpp_to_legacy_msg_id(i: str) -> int:
        return int(i)

    async def on_invalid_key(self) -> Never:
        self.send_gateway_message(
            "Your telegram session is not valid anymore. "
            "Maybe you disconnected slidgram from another telegram client? "
            "Please go through the registration process again."
        )
        await self.xmpp.unregister_user(self.user)
        raise XMPPError("not-authorized", "Your credentials are not valid anymore")

    @tg_to_xmpp_errors
    async def login(self) -> str:
        Session.active_sessions.add(self)
        await self.tg.start()
        me = self.tg.me
        assert me is not None
        self.contacts.user_legacy_id = str(me.id)
        my_name = me.full_name.strip()
        self.bookmarks.user_nick = my_name
        return f"Connected as {my_name}"

    @tg_to_xmpp_errors
    async def logout(self) -> None:
        Session.active_sessions.discard(self)
        await self.tg.stop()

    @tg_to_xmpp_errors
    async def on_create_group(
        self,
        name: str,
        contacts: list["Contact"],
    ) -> str:
        group = await self.tg.create_group(name, [c.tg_id for c in contacts])
        return str(group.id)

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
    async def on_leave_group(self, chat_id: str) -> None:
        await self.tg.leave_chat(int(chat_id))

    @log_error_on_peer_id_invalid
    async def _on_tg_msg(self, _tg: TelegramClient, message: Message) -> None:
        if message.chat is not None and self.tg.is_me(message.chat.id):
            # slidge voluntarily does not support messages to self through the legacy network
            return
        if (
            message.service == MessageServiceType.NEW_CHAT_MEMBERS
            and message.chat
            and message.chat.type == ChatType.SUPERGROUP
        ):
            # maybe handled in ChatMemberUpdated? This logs a few PeerIdInvalid in supergroups
            return
        sender, carbon = await self.get_sender(message)
        # TODO: use pyrogram's filters, eg:
        #  https://pyrofork.mayuri.my.id/main/api/filters.html#pyrogram.filters.left_chat_member
        if (
            sender.is_participant
            and sender.is_user
            and message.service == MessageServiceType.LEFT_CHAT_MEMBERS
        ):
            # after leaving, we cache deleted message events, and they re-spawn
            # the MUC in slidge's DB if these message could be resolved.
            # Removing them from the cache solves the issue.
            self.tg.message_cache.remove_chat(sender.muc.tg_id)
            await self.bookmarks.remove(sender.muc)
            return
        await sender.send_tg_msg(message, carbon=carbon)

    @log_error_on_peer_id_invalid
    async def _on_tg_edit(self, _tg: TelegramClient, message: Message) -> None:
        # Skip edits that carry no text/caption
        if message.text is None and message.caption is None:
            return

        sender, carbon = await self.get_sender(message)

        if carbon and message.edit_hide:
            # When a 1:1 contact reacts to one of our messages, this is
            # triggered with the edit_hide bit on. We don't want to interpret
            # this as *us* modifying the message from a Telegram app.
            return

        await sender.send_tg_msg(message, carbon=carbon, correction=True)

    @ignore_event_on_peer_id_invalid
    async def _on_tg_status(self, _tg: TelegramClient, user: User) -> None:
        if self.tg.is_me(user):
            return
        contact = await self.contacts.by_legacy_id(str(user.id))
        contact.update_tg_status(user)

    @log_error_on_peer_id_invalid
    async def _on_tg_chat_member(
        self, _tg: TelegramClient, update: ChatMemberUpdated
    ) -> None:
        muc = await self.bookmarks.by_tg_id(update.chat.id)
        part = await muc.get_participant_by_tg_id(update.new_chat_member.user.id)
        part.update_tg_member(update.new_chat_member)

    # this is a handler for a custom event we added to our pyrogram.Client
    # subclass.

    @ignore_event_on_peer_id_invalid
    async def _on_tg_reaction(
        self, message: Message, user_id: int, emoji: str | None
    ) -> None:
        emojis = [] if emoji is None else [emoji]

        if message.chat.type in (ChatType.PRIVATE, ChatType.BOT):
            if self.tg.is_me(user_id):
                contact = await self.contacts.by_tg_id(message.chat.id)
                contact.react(str(message.id), emojis, carbon=True)
            else:
                contact = await self.contacts.by_tg_id(user_id)
                contact.react(str(message.id), emojis)
            return

        muc = await self.bookmarks.by_tg_id(message.chat.id)
        participant = await muc.get_participant_by_tg_id(user_id)
        participant.react(str(message.id), emojis)

    @ignore_event_on_peer_id_invalid
    async def _on_tg_deleted_msg(
        self, _tg: TelegramClient, messages: list[Message]
    ) -> None:
        for message in messages:
            msg_id = message.id
            message = self.tg.message_cache.get_by_message_id(msg_id)
            if message is None:
                self.log.debug(
                    "Received a message deletion event, but we don't know which chat it belongs to!"
                )
                continue
            sender, carbon = await self.get_sender(message)
            if hasattr(sender, "muc"):
                sender.muc.get_system_participant().moderate(str(message.id))
            else:
                sender.retract(str(message.id), carbon=carbon)

    # these are "raw" telegram updates that are not processed at all by
    # pyrogram
    async def _on_tg_raw(
        self,
        _tg: TelegramClient,
        update: Update,
        users: dict[int, User],
        chats: dict[int, Chat],
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

    async def _on_tg_UpdateDialogPinned(
        self, update: pyro_raw_types.UpdateDialogPinned, _users: object, chats: object
    ) -> None:
        if isinstance(update.peer, pyro_raw_types.DialogPeerFolder):
            # TODO: investigate what that is
            return

        muc = await self._get_muc_by_peer(update.peer.peer)
        if muc is None:
            return

        await muc.add_to_bookmarks(pin=update.pinned)

    @ignore_event_on_peer_id_invalid
    async def _on_tg_UpdateUserTyping(
        self, update: pyro_raw_types.UpdateUserTyping, _users: object, _chats: object
    ) -> None:
        actor = await self.contacts.by_legacy_id(str(update.user_id))
        self._send_action(actor, update.action)

    @ignore_event_on_peer_id_invalid
    async def _on_tg_UpdateChatUserTyping(
        self,
        update: pyro_raw_types.UpdateChatUserTyping,
        _users: object,
        _chats: object,
    ) -> None:
        muc = await self.bookmarks.by_tg_id(-update.chat_id)
        if isinstance(update.from_id, pyro_raw_types.PeerUser):
            actor = await muc.get_participant_by_tg_id(update.from_id.user_id)
        else:
            self.log.warning("Unknown peer: %s", update)
            return
        self._send_action(actor, update.action)

    @ignore_event_on_peer_id_invalid
    async def _on_tg_UpdateChannelUserTyping(
        self,
        update: pyro_raw_types.UpdateChannelUserTyping,
        _users: object,
        _chats: object,
    ) -> None:
        muc = await self.bookmarks.by_tg_id(get_channel_id(update.channel_id))
        if isinstance(update.from_id, pyro_raw_types.PeerUser):
            actor = await muc.get_participant_by_tg_id(update.from_id.user_id)
        else:
            self.log.warning("Unknown peer: %s", update)
            return
        self._send_action(actor, update.action)

    def _send_action(
        self, actor: "Contact | Participant", action: SendMessageAction
    ) -> None:
        if isinstance(action, _COMPOSING_TYPES):
            actor.composing()
        elif isinstance(action, pyro_raw_types.SendMessageCancelAction):
            actor.paused()
        else:
            self.log.warning("Unknown action: %s for %s", action, actor)

    @ignore_event_on_peer_id_invalid
    async def _on_tg_UpdateReadHistoryOutbox(
        self,
        update: pyro_raw_types.UpdateReadHistoryOutbox,
        _users: object,
        _chats: object,
    ) -> None:
        actor = await self._get_actor_by_peer(update.peer)
        actor.displayed(str(update.max_id))

    @ignore_event_on_peer_id_invalid
    async def _on_tg_UpdateReadHistoryInbox(
        self,
        update: pyro_raw_types.UpdateReadHistoryInbox,
        _users: object,
        _chats: list[Chat],
    ) -> None:
        if isinstance(update.peer, pyro_raw_types.PeerUser) and self.tg.is_me(
            update.peer.user_id
        ):
            # self-message through telegram are not supported
            return
        actor = await self._get_actor_by_peer(update.peer, user=True)
        actor.displayed(str(update.max_id), carbon=True)

    @ignore_event_on_peer_id_invalid
    async def _on_tg_UpdateReadChannelInbox(
        self,
        update: pyro_raw_types.UpdateReadChannelInbox,
        _users: object,
        _chats: object,
    ) -> None:
        muc = await self.bookmarks.by_tg_id(get_channel_id(update.channel_id))
        part = await muc.get_user_participant()
        part.displayed(str(update.max_id))

    @log_error_on_peer_id_invalid
    async def _on_tg_UpdatePinnedMessages(
        self,
        update: pyro_raw_types.UpdatePinnedMessages,
        _users: object,
        _chats: object,
    ) -> None:
        muc = await self._get_muc_by_peer(update.peer)
        if muc is None:
            return

        await muc.set_tg_pinned_message_ids(update.messages, update.pinned)

    @log_error_on_peer_id_invalid
    async def _on_tg_UpdateChatParticipants(
        self,
        update: pyro_raw_types.UpdateChatParticipants,
        _users: dict[int, User],
        _chats: dict[int, Chat],
    ) -> None:
        muc = await self.bookmarks.by_legacy_id(-update.participants.chat_id)
        if isinstance(update.participants, pyro_raw_types.ChatParticipantsForbidden):
            self.log.warning(
                "Received ChatParticipantsForbidden: %s", update.participants
            )
            return
        for tg_participant in update.participants.participants:
            participant = await muc.get_participant_by_legacy_id(tg_participant.user_id)
            if isinstance(tg_participant, pyro_raw_types.ChatParticipant):
                participant.affiliation = "member"
                participant.role = "participant"
            elif isinstance(tg_participant, pyro_raw_types.ChatParticipantAdmin):
                participant.affiliation = "admin"
                participant.role = "moderator"
            elif isinstance(tg_participant, pyro_raw_types.ChatParticipantCreator):
                participant.affiliation = "owner"
                participant.role = "moderator"
            else:
                self.log.warning("Unknown participant: %s", tg_participant)

    @log_error_on_peer_id_invalid
    async def _on_tg_UpdateChannel(
        self,
        update: pyro_raw_types.UpdateChannel,
        _users: dict[int, User],
        chats: dict[int, pyro_raw_types.Channel],
    ) -> None:
        for channel in chats.values():
            if channel.left:
                muc = await self.bookmarks.by_tg_id(get_channel_id(update.channel_id))
                self.tg.message_cache.remove_chat(muc.tg_id)
                await self.bookmarks.remove(muc)

    async def get_sender(
        self,
        update: Message | MessageReactionUpdated,
    ) -> tuple["Contact | Participant", bool]:
        if update.chat.type in (ChatType.PRIVATE, ChatType.BOT):
            if self.tg.is_me(update.from_user):
                return await self.contacts.by_legacy_id(str(update.chat.id)), True
            else:
                return await self.contacts.by_legacy_id(str(update.from_user.id)), False

        muc = await self.bookmarks.by_tg_id(update.chat.id)
        if update.from_user is not None:
            return await muc.get_participant_by_tg_id(update.from_user.id), False
        if update.sender_business_bot is not None:
            return (
                await muc.get_participant_by_tg_id(update.sender_business_bot.id),
                False,
            )
        if update.sender_chat or update.chat.type == ChatType.CHANNEL:
            return muc.get_system_participant(), False

        raise RuntimeError(f"Unable to determine who sent this: {update}")

    async def _get_actor_by_peer(
        self, peer: Peer, user: bool = False
    ) -> "Contact | Participant":
        if isinstance(peer, pyro_raw_types.PeerUser):
            return await self.contacts.by_tg_id(peer.user_id)
        elif isinstance(peer, pyro_raw_types.PeerChat):
            muc = await self.bookmarks.by_tg_id(-peer.chat_id)
        elif isinstance(peer, PeerChannel):
            muc = await self.bookmarks.by_tg_id(get_channel_id(peer.channel_id))
        else:
            raise RuntimeError("Invalid peer", peer)
        if user:
            return await muc.get_user_participant()
        return muc.get_system_participant()

    async def _get_muc_by_peer(self, peer: Peer) -> "MUC | None":
        if isinstance(peer, pyro_raw_types.PeerUser):
            return None
        if isinstance(peer, pyro_raw_types.PeerChat):
            return await self.bookmarks.by_tg_id(-peer.chat_id)
        if isinstance(peer, (PeerChannel, pyro_raw_types.PeerChannel)):
            return await self.bookmarks.by_tg_id(get_channel_id(peer.channel_id))
        return None


_COMPOSING_TYPES = (
    pyro_raw_types.SendMessageTypingAction,
    pyro_raw_types.SendMessageChooseStickerAction,
    pyro_raw_types.SendMessageUploadAudioAction,
    pyro_raw_types.SendMessageUploadDocumentAction,
    pyro_raw_types.SendMessageUploadPhotoAction,
    pyro_raw_types.SendMessageUploadVideoAction,
    pyro_raw_types.SendMessageUploadRoundAction,
)


log = logging.getLogger(__name__)
