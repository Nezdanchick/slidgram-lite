import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import aiotdlib.api as tgapi
from slidge.core.mixins.message import ContentMessageMixin
from slidge.util.types import LinkPreview, MessageReference
from slidge.util.util import remove_emoji_variation_selector_16
from slixmpp.exceptions import XMPPError

from . import config
from .text_entities import formatted_text_to_xep_0393

if TYPE_CHECKING:
    from .group import MUC
    from .session import Session


def get_best_file(content: tgapi.MessageContent) -> Optional[tgapi.File]:
    if isinstance(content, tgapi.MessagePhoto):
        photo = content.photo
        return max(photo.sizes, key=lambda x: x.width).photo
    elif isinstance(content, tgapi.MessageVideo):
        return content.video.video
    elif isinstance(content, tgapi.MessageVideoNote):
        return content.video_note.video
    elif isinstance(content, tgapi.MessageAnimation):
        return content.animation.animation
    elif isinstance(content, tgapi.MessageAudio):
        return content.audio.audio
    elif isinstance(content, tgapi.MessageVoiceNote):
        return content.voice_note.voice
    elif isinstance(content, tgapi.MessageDocument):
        return content.document.document
    return None


def get_file_name(content: tgapi.MessageContent) -> Optional[str]:
    if isinstance(content, tgapi.MessageVideo):
        return content.video.file_name
    elif isinstance(content, tgapi.MessageAnimation):
        return content.animation.file_name
    elif isinstance(content, tgapi.MessageAudio):
        return content.audio.file_name
    elif isinstance(content, tgapi.MessageDocument):
        return content.document.file_name
    return None


class AvailableEmojisMixin:
    session: "Session"
    chat_id: int
    log: logging.Logger
    REACTIONS_SINGLE_EMOJI = True

    async def available_emojis(self, legacy_msg_id=None):
        if legacy_msg_id is None:
            try:
                chat = await self.session.tg.get_chat(self.chat_id)
            except XMPPError as e:
                self.log.debug("Could not get the available emojis: %s", e)
                return
            available_reactions = chat.available_reactions
            if isinstance(available_reactions, tgapi.ChatAvailableReactionsSome):
                emojis = set(r.emoji for r in available_reactions.reactions)
                return emojis
            if chat.last_message is None:
                return await self.session.tg.active_emojis
            legacy_msg_id = chat.last_message.id
        elif str(legacy_msg_id).startswith(self.session.SPECIAL_MSG_ID_PREFIX):
            return EMOJIS_VOTE
        await self.session.wait_for_ready()
        try:
            available = await self.session.tg.api.get_message_available_reactions(
                chat_id=self.chat_id, message_id=legacy_msg_id, row_size=25
            )
        except XMPPError:
            self.session.log.warning(
                "Could not fetch chat-specific available emoji reactions, "
                "using the default 'active emojis' list.",
                stack_info=True,
            )
            return await self.session.tg.active_emojis
        return {
            a.type_.emoji
            for a in available.top_reactions
            + available.recent_reactions
            + available.popular_reactions
            if isinstance(a.type_, tgapi.ReactionTypeEmoji)
        }


class TelegramToXMPPMixin(ContentMessageMixin):
    session: "Session"
    chat_id: int
    is_group: bool
    muc: "MUC"

    async def _get_reply_to(self, msg: tgapi.Message):
        if not (reply := msg.reply_to):
            # if reply_to = 0, telegram really means "None"
            return
        reply_to = reply.message_id
        slidge_reference = MessageReference(legacy_id=reply_to)

        try:
            reply_to_msg = await self.session.tg.api.get_message(self.chat_id, reply_to)
        except XMPPError:
            slidge_reference.body = "[deleted message]"
            return slidge_reference

        reply_to_content = reply_to_msg.content
        reply_to_sender = reply_to_msg.sender_id

        if isinstance(reply_to_sender, tgapi.MessageSenderUser):
            sender_user_id = reply_to_sender.user_id
            if sender_user_id == self.session.contacts.user_legacy_id:
                slidge_reference.author = "user"
            elif self.is_group:
                slidge_reference.author = await self.muc.get_participant_by_legacy_id(
                    sender_user_id
                )
            else:
                slidge_reference.author = await self.session.contacts.by_legacy_id(
                    sender_user_id
                )
        elif isinstance(reply_to_sender, tgapi.MessageSenderChat) and self.is_group:
            slidge_reference.author = self.muc.get_system_participant()
        else:
            raise RuntimeError("This should not happen")

        if isinstance(reply_to_content, tgapi.MessageText):
            slidge_reference.body = await self.formatted_text_to_xep_0393(
                reply_to_content.text
            )
        elif isinstance(reply_to_content, tgapi.MessageAnimatedEmoji):
            slidge_reference.body = reply_to_content.animated_emoji.sticker.emoji
        elif isinstance(reply_to_content, tgapi.MessageSticker):
            slidge_reference.body = reply_to_content.sticker.emoji
        elif best_file := get_best_file(reply_to_content):
            slidge_reference.body = f"Attachment {best_file.id}"
        else:
            slidge_reference.body = "[unsupported by slidge]"

        return slidge_reference

    async def send_tg_message(self, msg: tgapi.Message, **kwargs):
        content = msg.content
        kwargs.update(
            dict(
                legacy_msg_id=msg.id,
                when=datetime.fromtimestamp(msg.date),
                reply_to=await self._get_reply_to(msg),
                carbon=msg.is_outgoing,
            )
        )

        self.session.log.debug("kwargs %s", kwargs)
        if isinstance(content, tgapi.MessageText):
            # TODO: parse formatted text to markdown
            formatted_text = content.text
            if web_page := content.web_page:
                if photo := web_page.photo:
                    await self.send_tg_file(
                        max(photo.sizes, key=lambda x: x.width).photo,
                        **kwargs | {"legacy_msg_id": f"preview-{msg.id}"},
                    )
                kwargs["link_previews"] = [
                    LinkPreview(
                        about=web_page.url,
                        url=web_page.display_url,
                        type=web_page.type_,
                        site_name=web_page.site_name,
                        title=web_page.title,
                        description=web_page.description.text,
                        image=None,
                    )
                ]
            self.send_text(
                body=await self.formatted_text_to_xep_0393(formatted_text),
                **kwargs,
            )
        elif isinstance(content, tgapi.MessageContactRegistered):
            self.send_text("/me has just registered a Telegram account", **kwargs)
        elif isinstance(content, tgapi.MessageAnimatedEmoji):
            emoji = content.animated_emoji.sticker.emoji
            self.send_text(body=emoji, **kwargs)
        elif isinstance(content, tgapi.MessageSticker):
            sticker = content.sticker
            if thumbnail := sticker.thumbnail:
                await self.send_tg_file(thumbnail.file, **kwargs)
            else:
                await self.send_tg_file(sticker.sticker, **kwargs)
        elif best_file := get_best_file(content):
            caption = getattr(content, "caption", None)
            await self.send_tg_file(
                best_file,
                await self.formatted_text_to_xep_0393(caption) if caption else None,
                get_file_name(content),
                **kwargs,
            )
        elif isinstance(content, tgapi.MessageBasicGroupChatCreate):
            # TODO: work out how to map this to group invitation
            pass
        elif isinstance(content, tgapi.MessageChatAddMembers):
            muc = self.muc
            for user_id in content.member_user_ids:
                await muc.get_participant_by_legacy_id(user_id)
        elif isinstance(content, tgapi.MessageChatDeleteMember):
            if not hasattr(self, "muc"):
                self.session.log.warning(
                    "Deleted member received for a 1:1 chat, wtf? %", msg
                )
                return
            self.muc.remove_participant(
                await self.muc.get_participant_by_legacy_id(content.user_id)
            )  # type:ignore
        elif isinstance(content, tgapi.MessagePinMessage):
            if await self.session.tg.is_private_chat(msg.chat_id):
                return
            muc = self.muc
            await muc.update_subject_from_msg()
        elif isinstance(content, tgapi.MessageCustomServiceAction):
            self.send_text(body=content.text, **kwargs)
        elif isinstance(content, tgapi.MessageChatChangeTitle):
            if not self.is_group:
                self.session.log.warning("Change title of a 1:1 chat?: %s", content)
                return
            self.muc.name = self.muc.description = content.title
        elif isinstance(content, tgapi.MessageSupergroupChatCreate):
            self.send_text(f"/me created a new super group chat: {content.title}")
        elif isinstance(content, tgapi.MessageChatChangePhoto):
            self.send_text("/me updated the chat photo")
            self.muc.update_tg_photo(content.photo)
        elif isinstance(content, tgapi.MessageChatDeletePhoto):
            self.send_text("/me deleted the chat photo")
            self.muc.update_tg_photo(None)
        elif isinstance(content, tgapi.MessagePoll):
            if not kwargs.get("correction"):
                self.send_text(
                    body=f"/me started a poll with the question '{content.poll.question}'",
                    **kwargs,
                )
            if user_has_voted(content.poll):
                choices = [
                    (
                        f"{e} {o.text} ({o.voter_count} vote{'s' if o.voter_count != 1 else ''})"
                        + ("*" if o.is_chosen else "")
                    )
                    for e, o in zip(EMOJIS_VOTE, content.poll.options)
                ]
            else:
                choices = [
                    f"{e} {o.text}" for e, o in zip(EMOJIS_VOTE, content.poll.options)
                ]
            self.send_text(
                "\n".join(choices) + f"\nTotal votes: {content.poll.total_voter_count}",
                **kwargs | {"legacy_msg_id": f"poll-{msg.id}"},
            )
        else:
            self.send_text(
                f"/me tried to send an unsupported content: {type(content)}.",
                **kwargs,
            )
            self.session.log.warning("Ignoring content: %s", type(content))

        if not isinstance(msg.reply_markup, tgapi.ReplyMarkupShowKeyboard):
            return

        commands = ["Suggested replies:"]
        for row_list in msg.reply_markup.rows:
            line = []
            for row in row_list:
                line.append(
                    f"{row.text} "
                    f"({row.type_.__class__.__name__.removeprefix('KeyboardButtonType')})"
                )
            commands.append(" --- ".join(line))
        self.send_text("\n".join(commands))

    async def send_tg_file(
        self,
        best_file: tgapi.File,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        **kwargs,
    ):
        query = tgapi.DownloadFile.construct(
            file_id=best_file.id, synchronous=True, priority=1
        )
        size = best_file.size
        if size > config.ATTACHMENT_MAX_SIZE:
            return self.send_text(
                (
                    "/me tried to send an attachment larger than"
                    f" {config.ATTACHMENT_MAX_SIZE}"
                ),
                **kwargs,
            )
        try:
            best_file_downloaded = await self.session.tg.request(query)  # type:ignore
        except XMPPError as e:
            return self.send_text(
                f"/me tried to send an attachment but something went wrong: {e.text}",
                **kwargs,
            )
        await self.send_file(
            best_file_downloaded.local.path,  # type:ignore
            caption=caption,
            file_name=file_name,
            legacy_file_id=str(best_file.remote.unique_id),
            **kwargs,
        )

    async def formatted_text_to_xep_0393(self, t: tgapi.FormattedText):
        if hasattr(self, "muc"):
            return formatted_text_to_xep_0393(
                t, await self.session.tg.get_my_id(), self.muc.user_nick
            )
        else:
            return formatted_text_to_xep_0393(t)


def user_has_voted(poll: tgapi.Poll):
    return any(
        # we can only get individual votes if we have voter ourselves
        o.is_chosen or o.is_being_chosen
        for o in poll.options
    )


EMOJIS_VOTE = [
    "1️⃣️",
    "2️⃣️",
    "3️⃣️",
    "4️⃣️",
    "5️⃣️",
    "6️⃣️",
    "7️⃣️",
    "8️⃣️",
    "9️⃣️",
    "🇦",
    "🇧",
    "🇨",
    "🇩",
    "🇪",
    "🇫",
    "🇬",
    "🇭",
    "🇮",
    "🇯",
    "🇰",
    "🇱",
    "🇲",
    "🇳",
    "🇴",
    "🇵",
    "🇶",
    "🇷",
    "🇸",
    "🇹",
    "🇺",
    "🇻",
    "🇼",
    "🇽",
    "🇾",
    "🇿",
]

EMOJIS_VOTE_NO_SELECTOR = [remove_emoji_variation_selector_16(x) for x in EMOJIS_VOTE]
