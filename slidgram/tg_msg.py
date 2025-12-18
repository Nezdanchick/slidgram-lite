import asyncio
import logging
from typing import TYPE_CHECKING

from pyrogram.enums import ChatType
from pyrogram.types import (
    Animation,
    Audio,
    Document,
    ForumTopic,
    Message,
    Photo,
    Sticker,
    Thumbnail,
    Video,
    VideoNote,
    Voice,
    WebPageEmpty,
)
from slidge.core.mixins.message import ContentMessageMixin
from slidge.util import lottie
from slidge.util.types import LegacyAttachment, LinkPreview, MessageReference
from slixmpp.exceptions import XMPPError

from . import config
from .telegram import handle_flood
from .text_entities import entities_to_xep_0393

if TYPE_CHECKING:
    from .gateway import Gateway
    from .group import MUC
    from .session import Session

TgMediaTypes = (
    Audio
    | Document
    | Photo
    | Sticker
    | Animation
    | Voice
    | Video
    | VideoNote
    | Thumbnail
)

MSG_POLL = "/me sent a poll but this is not supported by slidgram yet"


class TelegramMessageSenderMixin(ContentMessageMixin):
    xmpp: "Gateway"
    session: "Session"
    log: logging.Logger
    muc: "MUC"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.send_file = handle_flood(self.send_file)  # type:ignore

    async def __get_thread(self, message: Message) -> int | None:
        if message.chat.type == ChatType.SUPERGROUP:
            return message.message_thread_id
        if message.chat.type == ChatType.FORUM:
            if (topic := getattr(message, "topic", None)) is None:
                return None
            assert isinstance(topic, ForumTopic)
            await self.muc.send_thread_subject(topic)
            return topic.id
        return None

    @property
    def tg(self):
        return self.session.tg

    @property
    def contacts(self):
        return self.session.contacts

    @property
    def bookmarks(self):
        return self.session.bookmarks

    async def send_tg_msg(
        self, message: Message, carbon=False, correction=False, archive_only=False
    ) -> None:
        if message.poll is not None:
            await self.__send_text(message, carbon, correction, archive_only, MSG_POLL)
        elif message.media is not None and message.web_page_preview is None:
            await self._send_media(message, carbon, correction, archive_only)
        else:
            await self.__send_text(message, carbon, correction, archive_only)

    async def __send_text(
        self,
        message: Message,
        carbon=False,
        correction=False,
        archive_only=False,
        text: str | None = None,
    ):
        self.send_text(
            self._to_message_styling(message) if text is None else text,
            message.id,
            reply_to=await self._get_reply_to(message.reply_to_message),
            carbon=carbon,
            correction=correction,
            when=message.date,
            archive_only=archive_only,
            link_previews=_get_link_previews(message),
            thread=await self.__get_thread(message),
        )

    async def _send_media(
        self, message: Message, carbon: bool, correction=False, archive_only=False
    ) -> None:
        if message.sticker is not None and message.sticker.is_animated:
            await self.__send_sticker(message, carbon, correction, archive_only)

        media = _get_media(message)
        if media is None:
            self.log.warning("Could not determine media in %s", message)
            await self.__send_text(
                message,
                carbon,
                correction,
                archive_only,
                f"Unsupported media type: {message.media}",
            )
            return
        if message.caption is None:
            caption = None
        else:
            caption = self._to_message_styling_caption(message)
        if media.file_size > config.ATTACHMENT_MAX_SIZE:
            text = f"{media} (larger than {config.ATTACHMENT_MAX_SIZE})"
            if message.text:
                text += f"\n{self._to_message_styling(message)}"
            if caption:
                text += f"\n{caption}"
            await self.__send_text(
                message,
                carbon,
                correction,
                archive_only,
                text,
            )
            return
        self.log.debug("Downloading %s", media.file_id)
        downloader = self.tg.get_downloader(media.file_id)
        if downloader is None:
            self.log.warning("Could not download %s", media)
            return
        await self.send_file(
            LegacyAttachment(
                aio_stream=downloader,
                name=getattr(media, "file_name", None),
                legacy_file_id=media.file_unique_id,
                caption=caption,
                content_type=(
                    "image/jpeg"
                    if isinstance(
                        media, Photo
                    )  # no mime_type attribute for Photos, but always JPEG
                    else getattr(media, "mime_type", None)
                ),
                disposition="inline"
                if isinstance(media, (Sticker, Animation, Thumbnail))
                else None,
            ),
            message.id,
            reply_to=await self._get_reply_to(message.reply_to_message),
            carbon=carbon,
            correction=correction,
            when=message.date,
            archive_only=archive_only,
            link_previews=_get_link_previews(message),
            thread=await self.__get_thread(message),
        )

    async def __send_sticker(
        self, message: Message, carbon: bool, correction=False, archive_only=False
    ):
        sticker = message.sticker
        sticker_id = sticker.file_unique_id
        tgs_path = lottie.sticker_path(sticker_id).with_suffix(".tgs")

        async with _sticker_download_lock:
            if not tgs_path.exists():
                downloader = self.tg.get_downloader(sticker.file_id)
                with tgs_path.open("wb") as fp:
                    async for chunk in downloader:
                        fp.write(chunk)
            self.log.debug("Converting sticker %s to video", sticker.file_id)
            attachment = await lottie.from_path(tgs_path, sticker_id)

        await self.send_file(
            attachment,
            legacy_msg_id=message.id,
            reply_to=await self._get_reply_to(message.reply_to_message),
            carbon=carbon,
            correction=correction,
            when=message.date,
            archive_only=archive_only,
        )

    async def _get_reply_to(self, message: Message | None) -> MessageReference | None:
        if message is None:
            return None

        if message.from_user is not None:
            if self.tg.is_me(message.from_user):
                author = "user"
            else:
                if message.chat.type in (ChatType.PRIVATE, ChatType.BOT):
                    try:
                        author = await self.contacts.by_legacy_id(message.from_user.id)
                    except XMPPError as e:
                        # deleted/banned user?
                        if e.condition == "item-not-found":
                            author = None
                        else:
                            raise
                else:
                    muc = await self.bookmarks.by_legacy_id(message.chat.id)
                    try:
                        author = await muc.get_participant_by_legacy_id(
                            message.from_user.id
                        )
                    except XMPPError as e:
                        # deleted/banned user?
                        if e.condition == "item-not-found":
                            author = None
                        else:
                            raise
        elif message.sender_chat is not None or (
            message.chat is not None and message.chat.type == ChatType.CHANNEL
        ):
            muc = await self.bookmarks.by_legacy_id(message.chat.id)
            author = muc.get_system_participant()
        else:
            self.log.warning("Referenced message author not understood: %s", message)
            author = None

        return MessageReference(
            message.id,
            author,  # type:ignore
            self._to_message_styling(message),
        )

    def _to_message_styling(self, message: Message) -> str:
        assert self.tg.me is not None
        return entities_to_xep_0393(
            message.text, message.entities, self.tg.me.id, self.bookmarks.user_nick
        )

    def _to_message_styling_caption(self, message: Message) -> str:
        assert self.tg.me is not None
        return entities_to_xep_0393(
            message.caption,
            message.caption_entities,
            self.tg.me.id,
            self.bookmarks.user_nick,
        )


def _get_link_previews(message: Message) -> list[LinkPreview] | None:
    if message.web_page_preview is None:
        return None

    page = message.web_page_preview.webpage

    if isinstance(page, WebPageEmpty):
        return None

    return [
        LinkPreview(
            about=page.description,
            title=page.title,
            description=page.description,
            url=page.url,
            image=None,
            type=page.type,
            site_name=page.site_name,
        )
    ]


def _get_media(message: Message) -> TgMediaTypes | None:
    if message.sticker is not None:
        if message.sticker.is_animated and message.sticker.thumbs:
            return message.sticker.thumbs[0]
        if message.sticker.is_video:
            return message.sticker
    for name in _MEDIAS:
        media = getattr(message, name, None)
        if media is not None:
            return media
    return None


_MEDIAS = (
    "audio",
    "document",
    "photo",
    "sticker",
    "animation",
    "video",
    "voice",
    "video_note",
    "new_chat_photo",
)


_sticker_download_lock = asyncio.Lock()
