import logging
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote

import aiohttp
import pyrogram.raw.types as pyro_raw_types
from PIL import Image
from pyrogram.enums import ChatAction, ChatType, MessageServiceType
from pyrogram.errors import FileReferenceExpired
from pyrogram.raw.base import Peer, SendMessageAction, Update
from pyrogram.raw.base.contacts import ImportedContacts
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
from slidge.util.types import Mention, RecipientType, Sticker
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
        self.tg = TelegramClient(self.user_jid.bare)

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

    async def on_invalid_key(self) -> None:
        self.send_gateway_message(
            "Your telegram session is not valid anymore. "
            "Maybe you disconnected slidgram from another telegram client? "
            "Please go through the registration process again."
        )
        await self.xmpp.unregister_user(self.user)
        raise XMPPError("not-authorized", "Your credentials are not valid anymore")

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

    # The following three chat states have no equivalent in telegram. if we don't override this
    # slidge will send a msg/error/feature-not-implemented for clients which actually send
    # those, such as psi. A lot of clients will just dismiss such errors, but some (again, psi)
    # will display them. Since chat states are effectively supported for "composing", "paused",
    # and "active" when the message has a body, it makes sense to not reply "feature-not-implemented",
    # especially since contacts advertise support for chat states in their disco#features.
    # This could (maybe should) be improved in slidge core, but this fix is good enough for now.
    async def on_active(self, *_args, **_kwargs):
        pass

    async def on_inactive(self, *_args, **_kwargs):
        pass

    async def on_gone(self, *_args, **_kwargs):
        pass

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
            chat.legacy_id,
            legacy_msg_id,
            text,
            entities=entities,
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
        file_name = unquote(url.split("/")[-1])
        content_type = http_response.content_type
        # we cannot use TemporaryFile() because pyrofork checks whether fp is
        # an io.IOBase instance, which it is not, despite implementing seek(),
        # tell(), and read()
        with (
            TemporaryDirectory() as tmp_dir,
            (Path(tmp_dir) / file_name).open("ab+") as fp,
        ):
            async for chunk in http_response.content:
                fp.write(chunk)
            if content_type.startswith("audio"):
                message = await self.tg.send_audio(
                    chat.legacy_id,
                    fp,
                    file_name=file_name,  # pyrofork includes the full path without that
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
            elif content_type.startswith("video"):
                message = await self.tg.send_video(
                    chat.legacy_id,
                    fp,
                    file_name=file_name,
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
            elif content_type.startswith("image"):
                message = await self.tg.send_photo(
                    chat.legacy_id,
                    fp,
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
            else:
                message = await self.tg.send_document(
                    chat.legacy_id,
                    fp,
                    file_name=file_name,
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
        if message is None:
            raise XMPPError(
                "internal-server-error", "Telegram did not confirm this message"
            )
        return message.id

    @tg_to_xmpp_errors
    async def on_sticker(
        self,
        chat: Recipient,
        sticker: Sticker,
        *,
        reply_to_msg_id: int | None = None,
        **_kwargs,
    ) -> int:
        stickers = self.user.legacy_module_data.get("stickers", {})
        assert isinstance(stickers, dict)
        h = sticker.hashes["sha_512"]
        assert isinstance(h, str)
        if (file_id := stickers.get(h)) is None:
            self.log.debug("Uploading a new sticker")
            return await self.__new_sticker(chat, sticker, reply_to_msg_id)
        self.log.debug("Reusing a previous sticker")
        assert isinstance(file_id, str)
        try:
            message = await self.tg.send_sticker(
                chat.legacy_id,
                file_id,
                reply_to_message_id=reply_to_msg_id,  # type:ignore
            )
        except FileReferenceExpired:
            self.log.warning("Sticker has expired, sending it again")
            return await self.__new_sticker(chat, sticker, reply_to_msg_id)
        assert message is not None
        return message.id

    async def __new_sticker(
        self, chat: Recipient, sticker: Sticker, reply_to_msg_id: int | None
    ) -> int:
        stickers = self.user.legacy_module_data.get("stickers", {})
        if sticker.content_type != "image/webp" and (
            (img := Image.open(sticker.path)).format != "WEBP"
        ):
            with BytesIO() as fp:
                await self.xmpp.loop.run_in_executor(None, img.save, fp, "WEBP")
                fp.flush()
                fp.seek(0)
                fp.name = "xmpp-sticker.webp"
                message = await self.tg.send_sticker(
                    chat.legacy_id,
                    fp,
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
        else:
            message = await self.tg.send_sticker(
                chat.legacy_id,
                str(sticker.path),
                reply_to_message_id=reply_to_msg_id,  # type:ignore
            )
        assert message is not None
        if message.sticker is None:
            self.log.warning("%s was not sent as a sticker.", sticker.path)
            return message.id
        stickers[sticker.hashes["sha_512"]] = message.sticker.file_id  # type:ignore
        self.legacy_module_data_update({"stickers": stickers})
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
        # TODO: use pyrogram's filters, eg:
        #  https://pyrofork.mayuri.my.id/main/api/filters.html#pyrogram.filters.left_chat_member
        if (
            isinstance(sender, Participant)
            and sender.is_user
            and message.service == MessageServiceType.LEFT_CHAT_MEMBERS
        ):
            # after leaving, we cache deleted message events, and they re-spawn
            # the MUC in slidge's DB if these message could be resolved.
            # Removing them from the cache solves the issue.
            self.tg.message_cache.remove_chat(sender.muc.legacy_id)
            await self.bookmarks.remove(sender.muc)
            return
        await sender.send_tg_msg(message, carbon=carbon)

    @catch_peer_id_invalid
    async def _on_tg_edit(self, _tg: TelegramClient, message: Message) -> None:
        if message.edit_hide:
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
                self.log.debug(
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
        self, update: pyro_raw_types.UpdateUserTyping, _users, _chats
    ) -> None:
        actor = await self.contacts.by_legacy_id(update.user_id)
        self._send_action(actor, update.action)

    @catch_peer_id_invalid
    async def _on_tg_UpdateChatUserTyping(
        self, update: pyro_raw_types.UpdateChatUserTyping, _users, _chats
    ) -> None:
        muc = await self.bookmarks.by_legacy_id(-update.chat_id)
        if isinstance(update.from_id, pyro_raw_types.PeerUser):
            actor = await muc.get_participant_by_legacy_id(update.from_id.user_id)
        else:
            self.log.warning("Unknown peer: %s", update)
            return
        self._send_action(actor, update.action)

    @catch_peer_id_invalid
    async def _on_tg_UpdateChannelUserTyping(
        self, update: pyro_raw_types.UpdateChannelUserTyping, _users, _chats
    ) -> None:
        # TESTME
        muc = await self.bookmarks.by_legacy_id(get_channel_id(update.channel_id))
        if isinstance(update.from_id, pyro_raw_types.PeerUser):
            actor = await muc.get_participant_by_legacy_id(update.from_id.user_id)
        else:
            self.log.warning("Unknown peer: %s", update)
            return
        self._send_action(actor, update.action)

    def _send_action(
        self, actor: Contact | Participant, action: SendMessageAction
    ) -> None:
        if isinstance(action, _COMPOSING_TYPES):
            actor.composing()
        elif isinstance(action, pyro_raw_types.SendMessageCancelAction):
            actor.paused()
        else:
            self.log.warning("Unknown action: %s for %s", action, actor)

    @catch_peer_id_invalid
    async def _on_tg_UpdateReadHistoryOutbox(
        self, update: pyro_raw_types.UpdateReadHistoryOutbox, _users, _chats
    ) -> None:
        actor = await self._get_actor_by_peer(update.peer)
        actor.displayed(update.max_id)

    @catch_peer_id_invalid
    async def _on_tg_UpdateReadHistoryInbox(
        self, update: pyro_raw_types.UpdateReadHistoryInbox, _users, _chats: list[Chat]
    ) -> None:
        actor = await self._get_actor_by_peer(update.peer, user=True)
        actor.displayed(update.max_id, carbon=True)

    @catch_peer_id_invalid
    async def _on_tg_UpdateReadChannelInbox(
        self, update: pyro_raw_types.UpdateReadChannelInbox, _users, _chats
    ) -> None:
        muc = await self.bookmarks.by_legacy_id(get_channel_id(update.channel_id))
        part = await muc.get_user_participant()
        part.displayed(update.max_id)

    @catch_peer_id_invalid
    async def _on_tg_UpdatePinnedMessages(
        self, update: pyro_raw_types.UpdatePinnedMessages, _users, _chats
    ) -> None:
        muc = await self._get_muc_by_peer(update.peer)
        if muc is None:
            return

        await muc.set_tg_pinned_message_ids(update.messages, update.pinned)

    @catch_peer_id_invalid
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

    @catch_peer_id_invalid
    async def _on_tg_UpdateChannel(
        self,
        update: pyro_raw_types.UpdateChannel,
        _users: dict[int, User],
        chats: dict[int, pyro_raw_types.Channel],
    ) -> None:
        for channel in chats.values():
            if channel.left:
                muc = await self.bookmarks.by_legacy_id(
                    get_channel_id(update.channel_id)
                )
                self.tg.message_cache.remove_chat(muc.legacy_id)
                await self.bookmarks.remove(muc)

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
        if update.sender_business_bot is not None:
            return (
                await muc.get_participant_by_legacy_id(update.sender_business_bot.id),
                False,
            )
        if update.sender_chat or update.chat.type == ChatType.CHANNEL:
            return muc.get_system_participant(), False

        raise RuntimeError(f"Unable to determine who sent this: {update}")

    async def _get_actor_by_peer(self, peer: Peer, user=False) -> Contact | Participant:
        if isinstance(peer, pyro_raw_types.PeerUser):
            return await self.contacts.by_legacy_id(peer.user_id)
        elif isinstance(peer, pyro_raw_types.PeerChat):
            muc = await self.bookmarks.by_legacy_id(-peer.chat_id)
        elif isinstance(peer, PeerChannel):
            muc = await self.bookmarks.by_legacy_id(get_channel_id(peer.channel_id))
        else:
            raise RuntimeError("Invalid peer", peer)
        if user:
            return await muc.get_user_participant()
        return muc.get_system_participant()

    async def _get_muc_by_peer(self, peer: Peer) -> MUC | None:
        if isinstance(peer, pyro_raw_types.PeerUser):
            return None
        if isinstance(peer, pyro_raw_types.PeerChat):
            return await self.bookmarks.by_legacy_id(-peer.chat_id)
        if isinstance(peer, PeerChannel):
            return await self.bookmarks.by_legacy_id(get_channel_id(peer.channel_id))
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
