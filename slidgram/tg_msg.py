import asyncio
import logging
import os
from typing import TYPE_CHECKING

from pyrogram.enums import ChatType
from pyrogram.types import (
    Animation,
    Audio,
    Document,
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
from slidge.util.types import LinkPreview, MessageReference

from . import config
from .telegram import handle_flood
from .text_entities import entities_to_xep_0393

if TYPE_CHECKING:
    from .gateway import Gateway
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

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.send_file = handle_flood(self.send_file)  # type:ignore
        self._convert_args = [
            "--height",
            str(config.CONVERT_STICKERS_SIZE),
            "--width",
            str(config.CONVERT_STICKERS_SIZE),
            "--fps",
            str(config.CONVERT_STICKERS_FPS),
        ]

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
        )

    async def _send_media(
        self, message: Message, carbon: bool, correction=False, archive_only=False
    ) -> None:
        if (
            message.sticker is not None
            and message.sticker.is_animated
            and config.CONVERT_STICKERS
        ):
            try:
                await self.__send_sticker(message, carbon, correction, archive_only)
            except Exception as e:
                self.log.error("Could not convert stickers.", exc_info=e)
            else:
                return

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
            None,
            message.id,
            async_data_stream=downloader,
            file_name=getattr(media, "file_name", None),
            legacy_file_id=media.file_unique_id,
            caption=caption,
            reply_to=await self._get_reply_to(message.reply_to_message),
            carbon=carbon,
            correction=correction,
            when=message.date,
            archive_only=archive_only,
            link_previews=_get_link_previews(message),
            content_type=(
                "image/jpeg"
                if isinstance(
                    media, Photo
                )  # no mime_type attribute for Photos, but always JPEG
                else getattr(media, "mime_type", None)
            ),
        )

    async def __send_sticker(
        self, message: Message, carbon: bool, correction=False, archive_only=False
    ):
        sticker = message.sticker
        sticker_id = sticker.file_unique_id
        webm_path = (self.xmpp.stickers_dir / sticker_id).with_suffix(".tgs.webm")

        if not webm_path.exists():
            tgs_filename = (self.xmpp.stickers_dir / sticker_id).with_suffix(".tgs")
            downloader = self.tg.get_downloader(sticker.file_id)
            with tgs_filename.open("wb") as fp:
                async for chunk in downloader:
                    fp.write(chunk)
            self.log.debug("Converting sticker %s to video", sticker.file_id)
            async with _conversion_lock:
                proc = await asyncio.create_subprocess_exec(
                    config.CONVERT_STICKERS_EXECUTABLE,
                    str(tgs_filename),
                    *self._convert_args,
                )
                await proc.communicate()
            self.log.debug("Conversion finished with return code: %s", proc.returncode)

        await self.send_file(
            legacy_msg_id=message.id,
            file_path=webm_path,
            legacy_file_id="sticker-" + sticker_id,
            reply_to=await self._get_reply_to(message.reply_to_message),
            carbon=carbon,
            correction=correction,
            when=message.date,
            archive_only=archive_only,
            content_type="video/webm",
        )

    async def _get_reply_to(self, message: Message | None) -> MessageReference | None:
        if message is None:
            return None

        if message.from_user is not None:
            if self.tg.is_me(message.from_user):
                author = "user"
            else:
                if message.chat.type in (ChatType.PRIVATE, ChatType.BOT):
                    author = await self.contacts.by_legacy_id(message.from_user.id)
                else:
                    muc = await self.bookmarks.by_legacy_id(message.chat.id)
                    author = await muc.get_participant_by_legacy_id(
                        message.from_user.id
                    )
        elif message.sender_chat is not None:
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

cpu_count = os.cpu_count()
if cpu_count is None or cpu_count <= 2:
    _conversion_lock: asyncio.Lock | asyncio.Semaphore = asyncio.Lock()
else:
    _conversion_lock = asyncio.Semaphore(cpu_count - 1)
