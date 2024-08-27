from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import TYPE_CHECKING

from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import UserNotParticipant
from pyrogram.types import ChatMember, ChatPermissions, ChatPrivileges, Message
from pyrogram.utils import zero_datetime
from slidge import global_config
from slidge.group import LegacyBookmarks, LegacyMUC, LegacyParticipant, MucType
from slidge.util.types import Hat, HoleBound, MucAffiliation
from slixmpp.exceptions import XMPPError

from .avatar import SetAvatarMixin
from .contact import Contact
from .errors import tg_to_xmpp_errors, tg_to_xmpp_errors_it
from .reactions import ReactionsMixin
from .telegram import Client
from .text_entities import entities_to_xep_0393
from .tg_msg import TelegramMessageSenderMixin

if TYPE_CHECKING:
    from .session import Session


class Bookmarks(LegacyBookmarks[int, "MUC"]):
    session: "Session"

    @property
    def tg(self) -> Client:
        return self.session.tg

    @staticmethod
    async def legacy_id_to_jid_local_part(legacy_id: int):
        return "group" + str(legacy_id)

    async def jid_local_part_to_legacy_id(self, local_part: str):
        try:
            chat_id = int(local_part.replace("group", ""))
        except ValueError:
            raise XMPPError(
                "bad-request",
                (
                    "This does not look like a valid telegram ID, at least not for"
                    " slidge. Do not be like edhelas, do not attempt to join groups you"
                    " had joined through spectrum. "
                ),
            )
        return chat_id

    async def fill(self):
        dialog_it = self.tg.get_dialogs()
        assert dialog_it is not None
        async for dialog in dialog_it:
            if dialog.chat.type in (
                ChatType.GROUP,
                ChatType.SUPERGROUP,
                ChatType.CHANNEL,
            ):
                if (
                    dialog.top_message is not None
                    and dialog.top_message.left_chat_member is not None
                    and dialog.top_message.left_chat_member.is_self
                ):
                    # destroyed groups or groups that have been left
                    continue
                try:
                    muc = await self.by_legacy_id(dialog.chat.id)
                except XMPPError:
                    continue
                await muc.add_to_bookmarks(auto_join=dialog.chat.type == ChatType.GROUP)


class MUC(ReactionsMixin, SetAvatarMixin, LegacyMUC[int, int, "Participant", int]):
    session: "Session"
    legacy_id: int

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pinned_message_ids = list[int]()

    @property
    def tg(self) -> Client:
        return self.session.tg

    @tg_to_xmpp_errors
    async def update_info(self):
        try:
            await self.tg.get_chat_member(self.legacy_id, "me")
        except UserNotParticipant:
            raise XMPPError(
                "subscription-required", "You are not a member of this group."
            )
        chat = await self.tg.get_chat(self.legacy_id)

        self.name = chat.title
        self.n_participants = chat.members_count

        if hasattr(chat, "description"):
            self.description = chat.description
        if hasattr(chat, "pinned_message"):
            self.set_tg_pinned_message(chat.pinned_message)

        if chat.type == ChatType.SUPERGROUP:
            self.type = MucType.CHANNEL_NON_ANONYMOUS
        elif chat.type == ChatType.CHANNEL:
            self.type = MucType.CHANNEL
            self.name += " (channel)"
        elif chat.type == ChatType.GROUP:
            self.type = MucType.GROUP
        else:
            raise XMPPError(
                "bad-request", f"This is not a telegram group, but a {chat.type}"
            )

        await self.update_chat_photo(chat.photo)

    @tg_to_xmpp_errors_it
    async def fill_participants(self):
        if self.type == MucType.CHANNEL:
            me = await self.get_user_participant()
            me.role = "visitor"
            yield me
            return

        me = None  # type:ignore
        me_member = None
        it = self.tg.get_chat_members(self.legacy_id, limit=100)
        assert it is not None
        async for member in it:
            if member.user is None:
                self.log.debug("Member but no user: %s", member)
                continue

            participant = await self.get_participant_by_legacy_id(member.user.id)
            participant.update_tg_member(member)
            if participant.is_user:
                me = participant
                me_member = member
                continue
            yield participant

        if me is None or me_member is None:
            me = await self.get_user_participant()
            me_member = await self.tg.get_chat_member(self.legacy_id, "me")
        me.update_tg_member(me_member)
        yield me

    @tg_to_xmpp_errors
    async def backfill(
        self,
        after: HoleBound | None = None,
        before: HoleBound | None = None,
    ):
        now = datetime.now()
        self.log.info("Fetching history between %s and %s", after, before)
        it = self.tg.get_chat_history(
            chat_id=self.legacy_id,
            limit=0,
            offset_id=0 if before is None else before.id,  # type:ignore
            min_id=0 if after is None else after.id,  # type:ignore
        )
        assert it is not None
        async for msg in it:
            self.log.debug("Fetched history message from %s", msg.date)
            if (now - msg.date).days > global_config.MAM_MAX_DAYS:
                break
            sender, _ = await self.session.get_sender(msg)
            await sender.send_tg_msg(msg, archive_only=True)

    @tg_to_xmpp_errors
    async def on_set_affiliation(
        self,
        contact: Contact,  # type:ignore
        affiliation: MucAffiliation,
        reason: str | None,
        nickname: str | None,
    ):
        member = await self.tg.get_chat_member(self.legacy_id, contact.legacy_id)

        if affiliation == "outcast":
            await self._on_ban(member, contact)

        elif affiliation == "member":
            await self._on_set_member(member, contact)

        elif affiliation in ("admin", "owner"):
            await self._on_set_admin(member, contact)

    @tg_to_xmpp_errors
    async def on_kick(
        self,
        contact: Contact,  # type:ignore
        reason: str | None,
        nickname: str | None,
    ):
        member = await self.tg.get_chat_member(self.legacy_id, contact.legacy_id)
        await self._on_ban(
            member, contact, datetime.now(tz=timezone.utc) + timedelta(minutes=5)
        )

    async def _on_ban(
        self, member: ChatMember, contact: Contact, until: datetime = zero_datetime()
    ) -> None:
        if member.status == ChatMemberStatus.BANNED:
            raise XMPPError(
                "bad-request",
                f"{contact.name} is already banned from {self.name}.",
            )
        elif member.status == ChatMemberStatus.OWNER:
            raise XMPPError(
                "bad-request",
                f"{contact.name} is the owner of {self.name}.",
            )
        elif member.status == ChatMemberStatus.LEFT:
            raise XMPPError(
                "bad-request",
                f"{contact.name} has already left {self.name}.",
            )
        if await self.tg.ban_chat_member(self.legacy_id, contact.legacy_id, until):
            participant = await self.get_participant_by_legacy_id(contact.legacy_id)
            participant.affiliation = "outcast"
            participant.role = "none"
        else:
            raise XMPPError("internal-server-error")

    async def _on_set_member(self, member: ChatMember, contact: Contact) -> None:
        if member.status == ChatMemberStatus.BANNED:
            success = await self.tg.unban_chat_member(self.legacy_id, contact.legacy_id)
        elif member.status == ChatMemberStatus.RESTRICTED:
            await self.tg.restrict_chat_member(
                self.legacy_id,
                contact.legacy_id,
                permissions=ChatPermissions(all_perms=True),
            )
            success = True
        elif member.status == ChatMemberStatus.LEFT:
            success = await self.tg.add_chat_members(
                self.legacy_id, [contact.legacy_id]
            )
        elif member.status == ChatMemberStatus.ADMINISTRATOR:
            if self.type == MucType.GROUP:
                success = await self.tg.edit_chat_admin(
                    self.legacy_id, contact.legacy_id, False
                )
            else:
                success = await self.tg.promote_chat_member(
                    self.legacy_id,
                    contact.legacy_id,
                    _NO_PRIVILEGES,
                )
        elif member.status == ChatMemberStatus.MEMBER:
            raise XMPPError(
                "bad-request",
                f"{contact.name} is already a member of {self.name}",
            )
        elif member.status == ChatMemberStatus.OWNER:
            raise XMPPError("bad-request", "The chat owner cannot be demoted")

        if success:
            participant = await self.get_participant_by_legacy_id(contact.legacy_id)
            participant.affiliation = "member"
            participant.role = "participant"
        else:
            raise XMPPError("internal-server-error")

    async def _on_set_admin(self, member: ChatMember, contact: Contact) -> None:
        if member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            raise XMPPError(
                "bad-request", f"This chat member is already {member.status}"
            )
        elif member.status == ChatMemberStatus.LEFT:
            if not await self.tg.add_chat_members(self.legacy_id, [contact.legacy_id]):
                raise XMPPError("internal-server-error")

        if self.type == MucType.GROUP:
            success = await self.tg.edit_chat_admin(
                self.legacy_id, contact.legacy_id, True
            )
        else:
            success = await self.tg.promote_chat_member(
                self.legacy_id, contact.legacy_id, _ALL_PRIVILEGES
            )

        if success:
            participant = await self.get_participant_by_legacy_id(contact.legacy_id)
            participant.affiliation = "admin"
            participant.role = "moderator"
        else:
            raise XMPPError("internal-server-error")

    @tg_to_xmpp_errors
    async def on_set_config(
        self,
        name: str | None,
        description: str | None,
    ):
        name = name or ""
        description = description or ""
        if name != self.name:
            if await self.tg.set_chat_title(self.legacy_id, name):
                self.name = name
            else:
                raise XMPPError(
                    "internal-server-error", "While trying to rename %s", self.name
                )
        if description != self.description:
            if await self.tg.set_chat_description(self.legacy_id, description):
                self.description = description
            else:
                raise XMPPError(
                    "internal-server-error",
                    "While trying to change the description of %s",
                    self.name,
                )

    @tg_to_xmpp_errors
    async def on_destroy_request(self, reason: str | None):
        if self.type == MucType.CHANNEL_NON_ANONYMOUS:
            success = await self.tg.delete_supergroup(self.legacy_id)
        elif self.type == MucType.CHANNEL:
            success = await self.tg.delete_channel(self.legacy_id)
        else:
            await self.tg.leave_chat(self.legacy_id, delete=True)
            success = True

        if not success:
            raise XMPPError("internal-server-error", "The group could not be deleted")

    @tg_to_xmpp_errors
    async def on_avatar(self, data: bytes | None, mime: str | None) -> None:
        if not data:
            await self.tg.delete_chat_photo(self.legacy_id)
            return

        await self.tg.set_chat_photo(self.legacy_id, photo=BytesIO(data))

    async def on_set_subject(self, subject: str) -> None:
        raise XMPPError(
            "feature-not-implemented",
            "Telegram groups don't have subject but pinned messages, "
            "and they cannot be set from XMPP.",
        )

    def set_tg_pinned_message(self, message: Message | None = None) -> None:
        if message is None:
            self.subject = ""
            self.subject_date = None
            self.subject_setter = None
            return

        self.pinned_message_ids.append(message.id)
        if message.date is None:
            self.subject_date = None
        else:
            if message.edit_date is None:
                self.subject_date = message.date.replace(tzinfo=timezone.utc)
            else:
                self.subject_date = message.edit_date.replace(tzinfo=timezone.utc)
        if message.from_user is None:
            self.subject_setter = None
        else:
            self.subject_setter = (
                message.from_user.username or message.from_user.full_name
            )

        if message.text is not None:
            assert self.tg.me is not None
            self.subject = entities_to_xep_0393(
                message.text, message.entities, self.tg.me.id, self.user_nick
            )
            return

        if message.caption is not None:
            assert self.tg.me is not None
            self.subject = entities_to_xep_0393(
                message.caption, message.caption_entities, self.tg.me.id, self.user_nick
            )
            return

        self.subject = (
            f"The last pinned message is an attachment without caption: {message.media}."
            "This is unsupported by slidgram."
        )

    async def set_tg_pinned_message_ids(
        self, message_ids: list[int], pinned: bool | None
    ) -> None:
        if pinned:
            self.pinned_message_ids.extend(message_ids)
        else:
            for message_id in message_ids:
                try:
                    self.pinned_message_ids.remove(message_id)
                except ValueError:
                    self.log.warning("Unpinned a message we didn't know?")
        if not self.pinned_message_ids:
            self.set_tg_pinned_message()
            return

        message_id = max(self.pinned_message_ids)
        message = await self.tg.get_messages(self.legacy_id, message_id)
        assert isinstance(message, Message)
        self.set_tg_pinned_message(message)

    def serialize_extra_attributes(self) -> dict:
        return {"pinned_messages": self.pinned_message_ids}

    def deserialize_extra_attributes(self, data: dict) -> None:
        self.pinned_message_ids = data.get("pinned_messages", [])


class Participant(TelegramMessageSenderMixin, LegacyParticipant):
    muc: MUC

    async def send_tg_msg(
        self, message: Message, carbon=False, correction=False, archive_only=False
    ) -> None:
        if message.new_chat_photo is not None:
            await self.muc.update_chat_photo(message.new_chat_photo)
        if message.delete_chat_photo is not None and message.delete_chat_photo:
            await self.muc.set_avatar(None)

        if message.new_chat_title is not None:
            self.muc.name = message.new_chat_title
        if message.new_chat_members is not None:
            for tg_user in message.new_chat_members:
                await self.muc.get_participant_by_legacy_id(tg_user.id)
        if (message.group_chat_created is not None and message.group_chat_created) or (
            message.supergroup_chat_created and message.supergroup_chat_created
        ):
            await self.muc.add_to_bookmarks()

        await super().send_tg_msg(
            message, archive_only=archive_only, correction=correction
        )

    def update_tg_member(self, member: ChatMember):
        if member.status == ChatMemberStatus.OWNER:
            self.affiliation = "owner"
            self.role = "moderator"
        elif member.status == ChatMemberStatus.ADMINISTRATOR:
            if is_owner_privileges(member.privileges):
                self.affiliation = "owner"
            else:
                self.affiliation = "admin"
            self.role = "moderator"
        elif member.status == ChatMemberStatus.RESTRICTED:
            self.set_hats([Hat("https://slidge.im/slidgram/hats", "restricted")])
            self.role = "visitor"
        elif member.status == ChatMemberStatus.LEFT:
            self.leave()
        elif member.status == ChatMemberStatus.BANNED:
            self.affiliation = "outcast"
            self.role = "none"
            self.leave()

        if member.custom_title is not None:
            self.set_hats([Hat("https://slidge.im/slidgram/hats", member.custom_title)])


def is_owner_privileges(privileges: ChatPrivileges | None) -> bool:
    return privileges is not None and privileges.can_change_info


_NO_PRIVILEGES = ChatPrivileges(
    can_manage_chat=False,
    can_delete_messages=False,
    can_manage_video_chats=False,  # Groups and supergroups only
    can_restrict_members=False,
    can_promote_members=False,
    can_change_info=False,
    can_post_messages=False,  # Channels only
    can_edit_messages=False,  # Channels only
    can_invite_users=False,
    can_pin_messages=False,  # Groups and supergroups only
    can_manage_topics=False,  # supergroups only.
    can_post_stories=False,  # Channels only
    can_edit_stories=False,  # Channels only
    can_delete_stories=False,  # Channels only
)

_ALL_PRIVILEGES = ChatPrivileges(
    can_manage_chat=True,
    can_delete_messages=True,
    can_manage_video_chats=True,  # Groups and supergroups only
    can_restrict_members=True,
    can_promote_members=True,
    can_change_info=True,
    can_post_messages=True,  # Channels only
    can_edit_messages=True,  # Channels only
    can_invite_users=True,
    can_pin_messages=True,  # Groups and supergroups only
    can_manage_topics=True,  # supergroups only.
    can_post_stories=True,  # Channels only
    can_edit_stories=True,  # Channels only
    can_delete_stories=True,  # Channels only
)
