import asyncio
import mimetypes
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional, Union

import aiotdlib.api as tgapi
from slidge import LegacyBookmarks, LegacyMUC, LegacyParticipant, MucType
from slidge.util.types import Mention
from slixmpp.exceptions import XMPPError
from slixmpp.types import MucAffiliation

from . import config
from .text_entities import formatted_text_to_xep_0393
from .util import AvailableEmojisMixin, TelegramToXMPPMixin

if TYPE_CHECKING:
    from .contact import Contact
    from .session import Session


class NotAMember(XMPPError):
    def __init__(self, status: tgapi.ChatMemberStatus):
        super().__init__(
            "not-authorized",
            f"You don't belong to this group, your status is {status.ID}. "
            "Use an official telegram client to change that.",
        )
        self.status = status


class Bookmarks(LegacyBookmarks[int, "MUC"]):
    session: "Session"

    # COMPAT: We prefix with 'group' because movim does not like MUC local parts
    #         starting with a hyphen

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.__fill_task: Optional[asyncio.Task] = None
        self.group_ids = dict[int, int]()

    @staticmethod
    async def legacy_id_to_jid_local_part(legacy_id: int):
        return "group" + str(legacy_id)

    async def by_legacy_id(self, legacy_id: int) -> "MUC":
        muc: MUC = await super().by_legacy_id(legacy_id)
        group = await muc.get_group()
        self.group_ids[group.id] = legacy_id
        return muc

    async def by_group_id(self, group_id: int) -> Optional["MUC"]:
        return await self.by_legacy_id(self.group_ids[group_id])

    async def jid_local_part_to_legacy_id(self, local_part: str):
        try:
            group_id = int(local_part.replace("group", ""))
        except ValueError:
            raise XMPPError(
                "bad-request",
                (
                    "This does not look like a valid telegram ID, at least not for"
                    " slidge. Do not be like edhelas, do not attempt to join groups you"
                    " had joined through spectrum. "
                ),
            )
        info = await self.session.tg.get_chat_info(group_id)
        if isinstance(info, (tgapi.User, tgapi.UserFullInfo, tgapi.SecretChat)):
            raise XMPPError(
                "bad-request", f"This is not a telegram group, but a {type(info)}"
            )
        return group_id

    async def fill(self):
        if self.__fill_task is not None:
            self.__fill_task.cancel()
        self.__fill_task = self.xmpp.loop.create_task(
            self.session.tg.get_main_list_chats_all()
        )


class MUC(AvailableEmojisMixin, LegacyMUC[int, int, "Participant", int]):
    MAX_SUPER_GROUP_PARTICIPANTS = 200
    session: "Session"
    _VALID_MEMBER_STATUSES = (
        tgapi.ChatMemberStatusMember,
        tgapi.ChatMemberStatusAdministrator,
        tgapi.ChatMemberStatusCreator,
        tgapi.ChatMemberStatusRestricted,
    )

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        #                                     tuple[telegram user id, emoji]
        self.reactions = defaultdict[int, set[tuple[int, str]]](set)
        self.__avatar_fetch_task = None

    @property
    def chat_id(self):
        return self.legacy_id

    def serialize_extra_attributes(self) -> Optional[dict]:
        return {"reactions": {k: list(v) for k, v in self.reactions.items()}}

    def deserialize_extra_attributes(self, data: dict) -> None:
        # FIXME: why do we need int(k) here?
        self.reactions.update(
            {
                int(k): {tuple(x) for x in v}
                for k, v in data.get("reactions", {}).items()
            }
        )

    @staticmethod
    def __avatar_id(best: tgapi.File) -> Optional[int]:
        if best.remote.unique_id:
            id_ = best.remote.unique_id
        elif best.remote.id:
            id_ = best.remote.id
        elif best.id:
            id_ = best.id
        else:
            id_ = None
        return id_

    async def __fetch_avatar(self, best: tgapi.File):
        local_path = await self.session.tg.get_local_path(best)
        await self.set_avatar(local_path, self.__avatar_id(best))

    def update_tg_photo(self, photo: Optional[tgapi.ChatPhoto]) -> None:
        if not photo:
            self.avatar = None
            return

        if config.BIG_AVATARS:
            best = max(photo.sizes, key=lambda x: x.width).photo
        else:
            best = min(photo.sizes, key=lambda x: x.width).photo
        if self.__avatar_id(best) != self.avatar:
            if self.__avatar_fetch_task:
                self.__avatar_fetch_task.cancel()
            self.__avatar_fetch_task = self.xmpp.loop.create_task(
                self.__fetch_avatar(best)
            )

    async def get_group(self) -> Union[tgapi.BasicGroup, tgapi.Supergroup]:
        tg = self.session.tg
        chat = await tg.get_chat(self.legacy_id)

        if isinstance(chat.type_, tgapi.ChatTypeBasicGroup):
            return await tg.get_basic_group(chat.type_.basic_group_id)
        elif isinstance(chat.type_, tgapi.ChatTypeSupergroup):
            return await tg.get_supergroup(chat.type_.supergroup_id)
        else:
            raise XMPPError(
                "bad-request", f"This is not a telegram group: {chat.type_}"
            )

    async def get_info(
        self,
    ) -> Union[tgapi.BasicGroupFullInfo, tgapi.SupergroupFullInfo]:
        tg = self.session.tg
        group = await self.get_group()

        if isinstance(group, tgapi.BasicGroup):
            return await tg.get_basic_group_full_info(group.id)
        elif isinstance(group, tgapi.Supergroup):
            return await tg.get_supergroup_full_info(group.id)
        else:
            raise XMPPError("internal-server-error")

    async def update_info(
        self,
        info: Optional[
            Union[tgapi.BasicGroupFullInfo, tgapi.SupergroupFullInfo]
        ] = None,
    ):
        chat = await self.session.tg.get_chat(self.legacy_id)
        group = await self.get_group()
        info = await self.get_info()

        if isinstance(group, tgapi.BasicGroup):
            self.type = MucType.GROUP
        elif isinstance(group, tgapi.Supergroup):
            if info.can_get_members:
                self.type = MucType.CHANNEL_NON_ANONYMOUS
            else:
                self.type = MucType.CHANNEL
        else:
            raise XMPPError("bad-request", f"This is not a telegram group: {chat}")
        if not isinstance(group.status, self._VALID_MEMBER_STATUSES):
            raise NotAMember(group.status)
        self.update_tg_photo(info.photo)
        self.n_participants = group.member_count
        name = chat.title
        if getattr(chat.type_, "is_channel", False):
            name += " (channel)"
        self.name = name
        self.description = info.description
        await self.fill_participants(info)
        self.session.create_task(self.update_subject_from_msg())

    async def update_subject_from_msg(self, msg: Optional[tgapi.Message] = None):
        if msg is None:
            try:
                msg = await self.session.tg.api.get_chat_pinned_message(self.legacy_id)
                self.log.debug("Pinned message: %s", type(msg.content))
            except (XMPPError, tgapi.NotFound):
                # tgapi.NotFound should not be raised here, but apparently is sometimes.
                # possibly race condition on startup :/
                # maybe we have to catch elsewhere too…
                self.log.debug("Pinned message not found?")
                return
        content = msg.content
        if not isinstance(content, (tgapi.MessagePhoto, tgapi.MessageText)):
            return

        sender_id = msg.sender_id
        self.subject_date = datetime.fromtimestamp(msg.date, tz=timezone.utc)
        if isinstance(sender_id, tgapi.MessageSenderUser):
            if sender_id.user_id == await self.session.tg.get_my_id():
                self.subject_setter = await self.get_user_participant()
            else:
                contact = await self.session.contacts.by_legacy_id(sender_id.user_id)
                self.subject_setter = await self.get_participant_by_contact(contact)
        else:
            self.subject_setter = self.get_system_participant()

        if isinstance(content, tgapi.MessagePhoto):
            self.subject = formatted_text_to_xep_0393(content.caption)
        if isinstance(content, tgapi.MessageText):
            self.subject = formatted_text_to_xep_0393(content.text)

    async def fill_participants(
        self,
        info: Optional[
            Union[tgapi.BasicGroupFullInfo, tgapi.SupergroupFullInfo]
        ] = None,
    ):
        self.log.debug("Getting participants")
        chat = await self.session.tg.get_chat(chat_id=self.legacy_id)
        if not isinstance(
            chat.type_, (tgapi.ChatTypeBasicGroup, tgapi.ChatTypeSupergroup)
        ):
            raise XMPPError("item-not-found", text="This is not a valid group ID")

        if info is None:
            info = await self.session.tg.get_chat_info(chat, full=True)
        if isinstance(info, tgapi.BasicGroupFullInfo):
            members = info.members
            read_only = False
        elif isinstance(info, tgapi.SupergroupFullInfo):
            group = await self.session.tg.get_supergroup(chat.type_.supergroup_id)
            read_only = group.is_broadcast_group or group.is_channel
            if info.can_get_members:
                members = (
                    await self.session.tg.api.get_supergroup_members(
                        supergroup_id=chat.type_.supergroup_id,  # type:ignore
                        filter_=None,  # type:ignore
                        offset=0,
                        limit=self.MAX_SUPER_GROUP_PARTICIPANTS,
                        skip_validation=True,
                    )
                ).members
            else:
                if read_only:
                    part = await self.get_user_participant()
                    part.affiliation = "member"
                    part.role = "visitor"
                members = []
        else:
            raise RuntimeError
        self.log.debug("%s participants", len(members))

        old = set(
            c.contact.legacy_id
            for c in await self.get_participants(fill_first=False)
            if c.contact is not None
        )
        self.log.debug("Old participants: %s", old)
        for member in members:
            sender = member.member_id
            if not isinstance(sender, tgapi.MessageSenderUser):
                self.log.debug("Ignoring non-user sender")  # Does this happen?
                continue
            part = await self.participant_by_sender_id(sender)
            if part.contact:
                old.discard(part.contact.legacy_id)
            status = member.status
            if isinstance(status, tgapi.ChatMemberStatusCreator):
                part.role = "moderator"
                part.affiliation = "owner"
            elif isinstance(status, tgapi.ChatMemberStatusAdministrator):
                part.role = "moderator"
                part.affiliation = "admin"
            elif isinstance(status, tgapi.ChatMemberStatusBanned):
                part.role = "none"
                part.affiliation = "outcast"
                part.offline()
            elif isinstance(status, tgapi.ChatMemberStatusMember):
                part.role = "participant"
                part.affiliation = "member"
            elif read_only:
                part.affiliation = "member"
                part.role = "visitor"
        for tg_id in old:
            self.log.debug("Removing %s", tg_id)
            part = await self.get_participant_by_legacy_id(tg_id)
            self.remove_participant(part)

    async def send_text(self, text: str) -> int:
        result = await self.session.tg.send_text(self.legacy_id, text)
        self.log.debug("MUC SEND RESULT: %s", result)
        msg_id = await self.session.wait_for_tdlib_success(result.id)
        self.log.debug("MUC SEND MSG: %s", msg_id)
        return msg_id

    async def participant_by_tg_user(self, user: tgapi.User) -> "Participant":
        return await self.get_participant_by_legacy_id(user.id)

    async def get_tg_chat(self):
        return await self.session.tg.get_chat(self.legacy_id)

    async def backfill(self, oldest_message_id=None, oldest_date=None):
        for m in await self.fetch_history(
            config.GROUP_HISTORY_MAXIMUM_MESSAGES, oldest_message_id
        ):
            part = await self.participant_by_sender_id(m.sender_id)
            await part.send_tg_message(m, archive_only=True)

    async def fetch_history(self, n: int, before: Optional[int] = None):
        tg = self.session.tg
        chat = await self.get_tg_chat()
        m = chat.last_message
        if m is None:
            return []

        messages = (
            [chat.last_message]
            if isinstance(chat.last_message.content, BACKFILLABLE)
            else []
        )
        try:
            async for m in tg.iter_chat_history(
                self.legacy_id,
                limit=n,
                from_message_id=before or 0,  # 0="None" for tdlib in this context
            ):
                if isinstance(m, BACKFILLABLE):
                    messages.append(m)
        except XMPPError as e:
            self.log.warning(
                "Problem fetching history: %s, we could only fetch %s message(s).",
                e.text,
                len(messages),
            )

        return messages

    async def participant_by_sender_id(self, sender_id: tgapi.MessageSender):
        if isinstance(sender_id, tgapi.MessageSenderUser):
            return await self.participant_by_tg_user(
                await self.session.tg.get_user(sender_id.user_id)
            )
        else:
            return self.get_system_participant()

    async def admin_set_avatar(
        self, data: Optional[bytes], mime: Optional[str]
    ) -> Optional[Union[int, str]]:
        if data:
            with tempfile.NamedTemporaryFile(
                suffix=mimetypes.guess_extension(mime) if mime else None
            ) as f:
                f.write(data)
                f.flush()
                response = await self.session.tg.api.set_chat_photo(
                    self.legacy_id,
                    (
                        tgapi.InputChatPhotoStatic(
                            photo=tgapi.InputFileLocal(path=f.name)
                        )
                        if data
                        else None
                    ),
                )
        else:
            response = await self.session.tg.api.set_chat_photo(self.legacy_id, None)
        self.log.debug("Set room avatar response: %s", response)
        return None

    async def on_set_affiliation(
        self,
        contact: "Contact",  # type:ignore
        affiliation: MucAffiliation,
        reason: Optional[str],
        nickname: Optional[str],
    ):
        tg = self.session.tg.api

        try:
            member = await tg.get_chat_member(
                self.legacy_id, tgapi.MessageSenderUser(user_id=contact.legacy_id)
            )
        except tgapi.NotFound:
            if affiliation == "none":
                return
            await tg.add_chat_member(
                self.legacy_id,
                contact.legacy_id,
                forward_limit=0 if affiliation == "outcast" else 100,
            )
            member = await tg.get_chat_member(self.legacy_id, contact.legacy_id)

        await tg.set_chat_member_status(
            self.legacy_id,
            member.member_id,
            status=AFFILIATIONS[affiliation],
        )

    async def on_set_config(
        self,
        name: Optional[str],
        description: Optional[str],
    ):
        await self.session.tg.api.set_chat_title(self.chat_id, title=name or "")
        await self.session.tg.api.set_chat_description(
            self.chat_id, description=description or ""
        )

    async def on_destroy_request(self, reason: Optional[str]):
        await self.session.tg.api.delete_chat(self.legacy_id)

    async def parse_mentions_utf16(self, text: bytes) -> list[Mention]:
        # TODO: move this logic to slidge-style-parser
        participants = {
            p.name: p for p in await self.get_participants(fill_first=False)
        }
        if len(participants) == 0:
            return []

        result = []
        pattern = "|".encode("utf-8").join(
            re.escape(nick.encode("utf-16-le")) for nick in participants
        )
        self.log.debug("text: %s", text)
        self.log.debug("pattern: %s", pattern)
        for match in re.finditer(pattern, text):
            self.log.debug("match: %s", match)
            self.log.debug("group: %s", match.group())
            span = match.span()
            nick = match.group().decode("utf-16-le")
            participant = participants[nick]
            if contact := participant.contact:
                result.append(
                    Mention(contact=contact, start=span[0] // 2, end=span[1] // 2)
                )
        return result


AFFILIATIONS = {
    "admin": tgapi.ChatMemberStatusAdministrator(
        rights=tgapi.ChatAdministratorRights(
            can_manage_chat=True,
            can_change_info=True,
            can_post_messages=True,
            can_edit_messages=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_manage_topics=True,
            can_promote_members=True,
            can_manage_video_chats=True,
            is_anonymous=False,
        )
    ),
    "outcast": tgapi.ChatMemberStatusBanned(
        banned_until_date=(datetime.utcnow() + timedelta(days=1000)).timestamp()
    ),
    "owner": tgapi.ChatMemberStatusCreator(
        custom_title="", is_anonymous=False, is_member=True
    ),
    "member": tgapi.ChatMemberStatusMember(),
    "none": tgapi.ChatMemberStatusLeft(),
}


class Participant(LegacyParticipant, TelegramToXMPPMixin):
    contact: "Contact"
    session: "Session"
    muc: "MUC"

    @property
    def chat_id(self):
        return self.muc.legacy_id

    def __hash__(self):
        if self.is_user:
            return 0
        return self.contact.legacy_id


BACKFILLABLE = (
    tgapi.MessageAnimatedEmoji,
    tgapi.MessageAnimation,
    tgapi.MessageAudio,
    tgapi.MessagePhoto,
    tgapi.MessageSticker,
    tgapi.MessageText,
    tgapi.MessageVideo,
    tgapi.MessageVideoNote,
    tgapi.MessageVoiceNote,
)
