import logging
import tempfile
from io import BytesIO
from mimetypes import guess_extension, guess_type
from typing import BinaryIO, Never, cast
from urllib.parse import unquote

from PIL import Image
from pyrogram.enums import ChatAction
from pyrogram.errors import FileReferenceExpired
from slidge.util.types import ChatState, Sticker, XMPPMessage
from slixmpp.exceptions import XMPPError

from .errors import tg_to_xmpp_errors
from .session import Session
from .telegram import Client
from .text_entities import styling_to_entities

class RecipientMixin:
    session: Session
    legacy_id: str
    log: logging.Logger

    @property
    def tg_id(self) -> int:
        return int(self.legacy_id)

    @property
    def tg(self) -> Client:
        return self.session.tg

    async def on_invalid_key(self) -> Never:
        await self.session.on_invalid_key()

    @tg_to_xmpp_errors
    async def on_message(self, message: XMPPMessage) -> str | None:
        if message.attachments:
            return await self._on_files(message)

        if message.body:
            from .emojis import translate_to_unicode
            translated_body = translate_to_unicode(message.body).strip()
            if " " not in translated_body and translated_body.startswith(("http://", "https://")):
                ext = translated_body.split('?')[0].split('.')[-1].lower()
                if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'mp4', 'ogg', 'mp3', 'wav', 'pdf', 'zip'):
                    import aiohttp
                    try:
                        async with aiohttp.ClientSession() as http_session:
                            async with http_session.get(translated_body) as resp:
                                if resp.status == 200:
                                    content_type = resp.headers.get("Content-Type", "application/octet-stream")
                                    file_name = unquote(translated_body.split('?')[0].split('/')[-1])
                                    with NamedSpooledTemporaryFile(max_size=50 * 1024 * 1024) as fp:
                                        async for chunk in resp.content.iter_chunked(8192):
                                            fp.write(chunk)
                                        fp.seek(0)
                                        media = content_type.split("/")[0] if "/" in content_type else "document"
                                        args = self.tg_id, cast(BinaryIO, fp)
                                        fp.pseudo_name = file_name
                                        reply_to = None if message.reply is None else int(message.reply.msg_id)
                                        if media == "audio":
                                            tg_msg = await self.tg.send_audio(*args, file_name=file_name, reply_to_message_id=reply_to)
                                        elif media == "video":
                                            tg_msg = await self.tg.send_video(*args, file_name=file_name, reply_to_message_id=reply_to)
                                        elif media == "image":
                                            tg_msg = await self.tg.send_photo(*args, reply_to_message_id=reply_to)
                                        else:
                                            tg_msg = await self.tg.send_document(*args, file_name=file_name, reply_to_message_id=reply_to)
                                        return str(tg_msg.id)
                    except Exception as e:
                        self.log.warning("Failed to auto-upload URL %s: %s", translated_body, e)

            text, entities = await styling_to_entities(translated_body, message.mentions)

        if message.replace is not None:
            await self.tg.edit_message_text(
                self.tg_id,
                int(message.replace),
                text,
                entities=entities,
            )
            return None

        tg_msg = await self.tg.send_message(
            self.tg_id,
            text,
            reply_to_message_id=None
            if message.reply is None
            else int(message.reply.msg_id),  
            entities=entities,
        )
        return str(tg_msg.id)

    async def _on_files(self, msg_att: XMPPMessage) -> str:
        reply_to_msg_id = None if msg_att.reply is None else int(msg_att.reply.msg_id)

        att = list(msg_att.attachments)[0]
        file_name = unquote(att.url.split("/")[-1])
        content_type = att.content_type
        with NamedSpooledTemporaryFile(max_size=10 * 1024 * 1024) as fp:
            async with att.get() as http_response:
                async for chunk in http_response.content:
                    fp.write(chunk)
                content_type = content_type or http_response.content_type

            fp.seek(0)
            media, format = content_type.split("/")
            args = self.tg_id, cast(BinaryIO, fp)
            guessed_type, _encoding = guess_type(file_name)
            if guessed_type != content_type:
                guessed_ext = guess_extension(content_type)
                if guessed_ext is not None:
                    file_name += guessed_ext
            fp.pseudo_name = file_name
            if media == "audio":
                message = await self.tg.send_audio(
                    *args,
                    file_name=file_name,  
                    reply_to_message_id=reply_to_msg_id,  
                )
            elif media == "video":
                message = await self.tg.send_video(
                    *args,
                    file_name=file_name,
                    reply_to_message_id=reply_to_msg_id,  
                )
            elif media == "image":
                message = await self.tg.send_photo(
                    *args,
                    reply_to_message_id=reply_to_msg_id,  
                )
            else:
                message = await self.tg.send_document(
                    *args,
                    file_name=file_name,
                    reply_to_message_id=reply_to_msg_id,  
                )
        if message is None:
            raise XMPPError(
                "internal-server-error", "Telegram did not confirm this message"
            )
        return str(message.id)

    @tg_to_xmpp_errors
    async def on_sticker(self, sticker: Sticker) -> str:
        if sticker.content_type and sticker.content_type != "application/octet-stream":
            if sticker.content_type.endswith("mp4"):
                msg = await self.tg.send_animation(self.tg_id, str(sticker.path))
            elif sticker.content_type.startswith("video"):
                msg = await self.tg.send_video(self.tg_id, str(sticker.path))
            elif not sticker.content_type.startswith("image"):
                msg = await self.tg.send_document(self.tg_id, str(sticker.path))
            assert msg is not None
            return str(msg.id)
        reply_to_msg_id = None if sticker.reply is None else int(sticker.reply.msg_id)
        stickers = self.session.user.legacy_module_data.get("stickers", {})
        assert isinstance(stickers, dict)
        h = sticker.hashes["sha_512"]
        assert isinstance(h, str)
        if (file_id := stickers.get(h)) is None:
            self.log.debug("Uploading a new sticker")
            return await self.__new_sticker(sticker, reply_to_msg_id)
        self.log.debug("Reusing a previous sticker")
        assert isinstance(file_id, str)
        try:
            message = await self.tg.send_sticker(
                self.tg_id,
                file_id,
                reply_to_message_id=reply_to_msg_id,  
            )
        except FileReferenceExpired:
            self.log.warning("Sticker has expired, sending it again")
            return await self.__new_sticker(sticker, reply_to_msg_id)
        assert message is not None
        return str(message.id)

    async def __new_sticker(self, sticker: Sticker, reply_to_msg_id: int | None) -> str:
        stickers = self.session.user.legacy_module_data.get("stickers", {})
        if sticker.content_type != "image/webp" and (
            (img := Image.open(sticker.path)).format != "WEBP"
        ):
            with BytesIO() as fp:
                await self.session.xmpp.loop.run_in_executor(None, img.save, fp, "WEBP")
                fp.flush()
                fp.seek(0)
                fp.name = "xmpp-sticker.webp"
                message = await self.tg.send_sticker(
                    self.tg_id,
                    fp,
                    reply_to_message_id=reply_to_msg_id,  
                )
        else:
            message = await self.tg.send_sticker(
                self.tg_id,
                str(sticker.path),
                reply_to_message_id=reply_to_msg_id,  
            )
        assert message is not None
        if message.sticker is None:
            self.log.warning("%s was not sent as a sticker.", sticker.path)
            return str(message.id)
        stickers[sticker.hashes["sha_512"]] = message.sticker.file_id  
        self.session.legacy_module_data_update({"stickers": stickers})
        return str(message.id)

    @tg_to_xmpp_errors
    async def on_chat_state(
        self, chat_state: ChatState, thread: str | None = None
    ) -> None:
        match chat_state:
            case "composing":
                await self.tg.send_chat_action(self.tg_id, ChatAction.TYPING)
            case "paused":
                await self.tg.send_chat_action(self.tg_id, ChatAction.CANCEL)

    @tg_to_xmpp_errors
    async def on_displayed(self, legacy_msg_id: str, thread: str | None = None) -> None:
        await self.tg.read_chat_history(self.tg_id, int(legacy_msg_id))

    @tg_to_xmpp_errors
    async def on_react(
        self, legacy_msg_id: str, emojis: list[str], thread: str | None = None
    ) -> None:
        await self.tg.send_reaction(
            self.tg_id,
            int(legacy_msg_id),
            emoji=emojis,  
        )

    @tg_to_xmpp_errors
    async def on_retract(self, legacy_msg_id: str, thread: str | None = None) -> None:
        await self.tg.delete_messages(self.tg_id, [int(legacy_msg_id)], revoke=True)

class NamedSpooledTemporaryFile(tempfile.SpooledTemporaryFile[bytes]):
    pseudo_name = "file"

    @property
    def name(self) -> str:
        return super().name or self.pseudo_name
