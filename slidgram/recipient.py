import logging
import tempfile
from io import BytesIO
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
            # TODO: handle several attachments (when slidge actually supports it)
            # for attachment in message.attachments:
            return await self._on_files(message)

        if message.body:
            text, entities = await styling_to_entities(message.body, message.mentions)

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
            else int(message.reply.msg_id),  # type:ignore
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
            fp.pseudo_name = file_name
            media, format = content_type.split("/")
            args = self.tg_id, cast(BinaryIO, fp)
            if media == "audio":
                message = await self.tg.send_audio(
                    *args,
                    file_name=file_name,  # pyrofork includes the full path without that
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
            elif media == "video":
                message = await self.tg.send_video(
                    *args,
                    file_name=file_name,
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
            elif media == "image":
                message = await self.tg.send_photo(
                    *args,
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
            else:
                message = await self.tg.send_document(
                    *args,
                    file_name=file_name,
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
        if message is None:
            raise XMPPError(
                "internal-server-error", "Telegram did not confirm this message"
            )
        return str(message.id)

    @tg_to_xmpp_errors
    async def on_sticker(self, sticker: Sticker) -> str:
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
                reply_to_message_id=reply_to_msg_id,  # type:ignore
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
                    reply_to_message_id=reply_to_msg_id,  # type:ignore
                )
        else:
            message = await self.tg.send_sticker(
                self.tg_id,
                str(sticker.path),
                reply_to_message_id=reply_to_msg_id,  # type:ignore
            )
        assert message is not None
        if message.sticker is None:
            self.log.warning("%s was not sent as a sticker.", sticker.path)
            return str(message.id)
        stickers[sticker.hashes["sha_512"]] = message.sticker.file_id  # type:ignore
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
            emoji=emojis,  # type:ignore[arg-type]
        )

    @tg_to_xmpp_errors
    async def on_retract(self, legacy_msg_id: str, thread: str | None = None) -> None:
        await self.tg.delete_messages(self.tg_id, [int(legacy_msg_id)], revoke=True)


class NamedSpooledTemporaryFile(tempfile.SpooledTemporaryFile[bytes]):
    # we need to guarantee the .name attribute for pyrogram to be happy
    pseudo_name = "file"

    @property
    def name(self) -> str:
        return super().name or self.pseudo_name
