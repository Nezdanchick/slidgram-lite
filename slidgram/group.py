import asyncio
import mimetypes
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, Union

import aiotdlib.api as tgapi
from slidge import LegacyBookmarks, LegacyMUC, LegacyParticipant, MucType
from slixmpp.exceptions import XMPPError

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

    async def by_group_id(self, group_id: int):
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
        self.chat_id = self.legacy_id
        #                                     tuple[participant, emoji]
        self.reactions = defaultdict[int, set[tuple[Participant, str]]](set)
        self.__fetch_subject_task = self.session.xmpp.loop.create_task(
            self.update_subject_from_msg()
        )
        self.__avatar_fetch_task = None

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
        self.name = self.description = name
        await self.fill_participants(info)

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
                self.subject_setter = contact.name
        else:
            self.subject_setter = self.name

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
        for member in members:
            sender = member.member_id
            if not isinstance(sender, tgapi.MessageSenderUser):
                self.log.debug("Ignoring non-user sender")  # Does this happen?
                continue
            part = await self.participant_by_sender_id(sender)
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

        messages = [chat.last_message]
        try:
            async for m in tg.iter_chat_history(
                self.legacy_id,
                limit=n,
                from_message_id=before or 0,  # 0="None" for tdlib in this context
            ):
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
                    tgapi.InputChatPhotoStatic(photo=tgapi.InputFileLocal(path=f.name))
                    if data
                    else None,
                )
        else:
            response = await self.session.tg.api.set_chat_photo(self.legacy_id, None)
        self.log.debug("Set room avatar response: %s", response)
        return None


class Participant(LegacyParticipant, TelegramToXMPPMixin):
    contact: "Contact"
    session: "Session"
    muc: "MUC"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.chat_id = self.muc.legacy_id

    def __hash__(self):
        if self.is_user:
            return 0
        return self.contact.legacy_id
