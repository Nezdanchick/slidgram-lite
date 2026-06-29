import asyncio
import logging
from typing import TYPE_CHECKING, Literal

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
from .telegram import Client, handle_flood
from .text_entities import entities_to_xep_0393

if TYPE_CHECKING:
    from .contact import Contact, Roster
    from .group import MUC, Bookmarks, Participant
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
    session: "Session"
    log: logging.Logger
    muc: "MUC"

    def __init__(self, *a, **kw) -> None:  # type:ignore[no-untyped-def]  # noqa
        super().__init__(*a, **kw)
        self.send_file = handle_flood(self.send_file)  # type:ignore

    async def __get_thread(self, message: Message) -> str | None:
        if message.chat.type == ChatType.SUPERGROUP:
            return str(message.message_thread_id)
        if message.chat.type == ChatType.FORUM:
            if (topic := getattr(message, "topic", None)) is None:
                return None
            assert isinstance(topic, ForumTopic)
            await self.muc.send_thread_subject(topic)
            return str(topic.id)
        return None

    @property
    def tg(self) -> Client:
        return self.session.tg

    @property
    def contacts(self) -> "Roster":
        return self.session.contacts

    @property
    def bookmarks(self) -> "Bookmarks":
        return self.session.bookmarks

    async def send_tg_msg(
        self,
        message: Message,
        carbon: bool = False,
        correction: bool = False,
        archive_only: bool = False,
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
        carbon: bool = False,
        correction: bool = False,
        archive_only: bool = False,
        text: str | None = None,
    ) -> None:
        actual_text = self._to_message_styling(message) if text is None else text
        from .emojis import translate_to_jabber
        actual_text = translate_to_jabber(actual_text)
        
        if carbon:
            actual_text = f"[You]: {actual_text}"
            carbon = False

        self.send_text(
            actual_text,
            str(message.id),
            reply_to=await self._get_reply_to(message.reply_to_message),
            carbon=carbon,
            correction=correction,
            when=message.date,
            archive_only=archive_only,
            link_previews=_get_link_previews(message),
            thread=await self.__get_thread(message),
        )

    async def _send_media(
        self,
        message: Message,
        carbon: bool,
        correction: bool = False,
        archive_only: bool = False,
    ) -> None:
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

        # Пытаемся получить file_id, который нужен твоему прокси
        file_id = getattr(media, "file_id", None)
        if not file_id:
            self.log.warning("Media has no file_id: %s", media)
            return

        # Формируем имя файла (если его нет в метаданных — генерируем на лету)
        file_name = getattr(media, "file_name", None)
        if not file_name:
            # Для фото, стикеров и голосовых сообщений генерируем безопасное имя
            ext = "jpg" if isinstance(media, Photo) else "webp" if isinstance(media, Sticker) else "ogg" if isinstance(media, Voice) else "mp4"
            file_name = f"{message.media.name.lower()}_{media.file_unique_id}.{ext}"

        # Собираем ссылку через твой прокси
        srv_host = config.MEDIA_SERVER_HOST
        srv_port = config.MEDIA_SERVER_PORT
        
        import urllib.parse
        safe_name = urllib.parse.quote(file_name)
        local_target_url = f"http://{srv_host}:{srv_port}/{file_id}?name={safe_name}"

        # Префикс прокси WebOne (если задан)
        base_proxy = config.PROXY_MEDIA_URL.strip()
        if base_proxy:
            if not base_proxy.endswith("/"):
                base_proxy += "/"
            link = f"{base_proxy}{local_target_url}"
        else:
            link = local_target_url

        # Добавляем описание (caption), если оно есть, и сохраняем форматирование
        if message.caption:
            caption = self._to_message_styling_caption(message)
            formatted_text = f"{file_name}: {link}\n---\n{caption}"
        else:
            formatted_text = f"{file_name}: {link}"

        # Отправляем как обычный текст
        await self.__send_text(
            message,
            carbon,
            correction,
            archive_only,
            text=formatted_text,
        )

    async def __send_sticker(
        self,
        message: Message,
        carbon: bool,
        correction: bool = False,
        archive_only: bool = False,
    ) -> None:
        sticker = message.sticker
        sticker_id = sticker.file_unique_id
        tgs_path = lottie.sticker_path(sticker_id).with_suffix(".tgs")

        async with _sticker_download_lock:
            if not tgs_path.exists():
                downloader = self.tg.get_downloader(sticker.file_id)
                assert downloader is not None
                with tgs_path.open("wb") as fp:
                    async for chunk in downloader:
                        fp.write(chunk)
            self.log.debug("Converting sticker %s to video", sticker.file_id)
            attachment = await lottie.from_path(tgs_path, sticker_id)

        await self.send_file(
            attachment,
            legacy_msg_id=str(message.id),
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
                author: Literal["user"] | Participant | Contact | None = "user"
            else:
                if message.chat.type in (ChatType.PRIVATE, ChatType.BOT):
                    try:
                        author = await self.contacts.by_tg_id(message.from_user.id)
                    except XMPPError as e:
                        # deleted/banned user?
                        if e.condition == "item-not-found":
                            author = None
                        else:
                            raise
                else:
                    muc = await self.bookmarks.by_tg_id(message.chat.id)
                    try:
                        author = await muc.get_participant_by_tg_id(
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
            muc = await self.bookmarks.by_tg_id(message.chat.id)
            author = muc.get_system_participant()
        else:
            self.log.warning("Referenced message author not understood: %s", message)
            author = None

        return MessageReference(
            str(message.id),
            author,
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
            return media  # type:ignore[no-any-return]
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
