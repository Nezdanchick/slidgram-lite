import asyncio
import functools
import logging
import re
import tempfile
from pathlib import Path
from typing import Union

import aiotdlib.api as tgapi
from aiotdlib.api.errors import BadRequest
from slidge import BaseSession, FormField, SearchResult
from slixmpp.exceptions import XMPPError

from . import config
from .client import TelegramClient
from .contact import Contact
from .gateway import Gateway
from .group import MUC
from .text_entities import to_formatted_text


def catch_chat_not_found(coroutine):
    @functools.wraps(coroutine)
    async def wrapped(self: "Session", *a, **k):
        try:
            return await coroutine(self, *a, **k)
        except XMPPError as e:
            if e.condition == "bad-request":
                if a:
                    chat = a[0]
                else:
                    chat = k.get("chat", k.get("c"))
                if chat is None:
                    raise RuntimeError(a, k)
                await self.tg.api.create_private_chat(chat.legacy_id, False)
            return await coroutine(self, *a, **k)

    return wrapped


Recipient = Union[Contact, MUC]


class Session(BaseSession[int, Recipient]):
    xmpp: Gateway

    def __init__(self, user):
        super().__init__(user)
        self.sent_read_marks = set[int]()
        self.ack_futures = dict[int, asyncio.Future]()
        self.user_correction_futures = dict[int, asyncio.Future]()
        self.delete_futures = dict[int, asyncio.Future]()

        self.tg = TelegramClient(self)

    @staticmethod
    def xmpp_to_legacy_msg_id(i: str) -> int:
        return int(i)

    async def login(self):
        await self.tg.start()
        self.tg.ready.set()
        my_id = await self.tg.get_my_id()
        self.contacts.user_legacy_id = my_id
        me = await self.tg.get_user(my_id)
        my_name = (me.first_name + " " + me.last_name).strip()
        self.bookmarks.user_nick = my_name
        return f"Connected as {my_name}"

    async def logout(self):
        await self.tg.stop()
        self.tg.ready.clear()

    async def wait_for_tdlib_success(self, result_id: int):
        fut = self.xmpp.loop.create_future()
        self.ack_futures[result_id] = fut
        return await fut

    @catch_chat_not_found
    async def send_text(
        self,
        chat: Recipient,
        text: str,
        *,
        reply_to_msg_id=None,
        reply_to_fallback_text=None,
        reply_to=None,
        **kwargs,
    ) -> int:
        result = await self.tg.send_formatted_text(
            text=to_formatted_text(text),
            chat_id=chat.legacy_id,
            reply_to_message_id=reply_to_msg_id,
        )
        new_message_id = await self.wait_for_tdlib_success(result.id)
        self.log.debug("Result: %s / %s", result, new_message_id)
        return new_message_id

    @catch_chat_not_found
    async def send_file(
        self, chat: Recipient, url: str, http_response, reply_to_msg_id=None, **_
    ) -> int:
        type_, _subtype = http_response.content_type.split("/")
        kwargs = dict(chat_id=chat.legacy_id, reply_to_message_id=reply_to_msg_id)
        stickers_pattern = config.OUTGOING_STICKERS_REGEXP
        file_name = url.split("/")[-1]
        with tempfile.TemporaryDirectory() as d:
            tmp_file = Path(d) / file_name
            tmp_file.write_bytes(await http_response.read())
            tmp_file_str = str(tmp_file)
            if stickers_pattern and re.match(stickers_pattern, file_name):
                result = await self.tg.send_sticker(sticker=tmp_file_str, **kwargs)
            elif type_ == "image" and tmp_file.stat().st_size < 10_000_000:
                result = await self.tg.send_photo(photo=tmp_file_str, **kwargs)
            elif type_ == "video":
                result = await self.tg.send_video(video=tmp_file_str, **kwargs)
            elif type_ == "audio":
                result = await self.tg.send_audio(audio=tmp_file_str, **kwargs)
            else:
                result = await self.tg.send_document(document=tmp_file_str, **kwargs)

            new_message_id = await self.wait_for_tdlib_success(result.id)

        return new_message_id

    @catch_chat_not_found
    async def active(self, c: Recipient, thread=None):
        res = await self.tg.api.open_chat(chat_id=c.legacy_id)
        self.log.debug("Open chat res: %s", res)

    @catch_chat_not_found
    async def inactive(self, c: Recipient, thread=None):
        res = await self.tg.api.close_chat(chat_id=c.legacy_id)
        self.log.debug("Close chat res: %s", res)

    @catch_chat_not_found
    async def composing(self, c: Recipient, thread=None):
        res = await self.tg.api.send_chat_action(
            chat_id=c.legacy_id,
            action=tgapi.ChatActionTyping(),  # type:ignore
            message_thread_id=0,  # TODO: check what telegram's threads really are
        )
        self.log.debug("Send composing res: %s", res)

    @catch_chat_not_found
    async def paused(self, c: Recipient, thread=None):
        res = await self.tg.api.send_chat_action(
            chat_id=c.legacy_id,
            action=tgapi.ChatActionCancel(),  # type:ignore
            message_thread_id=0,
        )
        self.log.debug("Send composing res: %s", res)

    @catch_chat_not_found
    async def displayed(self, c: Recipient, tg_id: int, thread=None):
        res = await self.tg.api.view_messages(
            chat_id=c.legacy_id,
            message_ids=[tg_id],
            force_read=True,
        )
        self.log.debug("Send chat action res: %s", res)

    @catch_chat_not_found
    async def correct(self, c: Recipient, text: str, legacy_msg_id: int, thread=None):
        f = self.user_correction_futures[legacy_msg_id] = self.xmpp.loop.create_future()
        await self.tg.api.edit_message_text(
            chat_id=c.legacy_id,
            message_id=legacy_msg_id,
            reply_markup=None,  # type:ignore
            input_message_content=tgapi.InputMessageText.construct(
                text=to_formatted_text(text)
            ),
            skip_validation=True,
        )
        await f

    async def search(self, form_values: dict[str, str]):
        phone = form_values["phone"]
        first = form_values.get("first", phone)
        last = form_values.get("last", "")
        response = await self.tg.api.import_contacts(
            contacts=[
                tgapi.Contact(  # type:ignore
                    phone_number=phone,
                    user_id=0,
                    first_name=first,
                    vcard="",
                    last_name=last,
                )
            ]
        )
        user_id = response.user_ids[0]
        if user_id == 0:
            return

        contact = await self.contacts.by_legacy_id(user_id)
        await contact.add_to_roster()

        return SearchResult(
            fields=[FormField("phone"), FormField("jid", type="jid-single")],
            items=[{"phone": form_values["phone"], "jid": contact.jid.bare}],
        )

    async def remove_reactions(self, c: "Recipient", legacy_msg_id):
        added_reactions = await self.tg.api.get_message_added_reactions(
            chat_id=c.legacy_id, message_id=legacy_msg_id, offset="", limit=100
        )
        my_id = await self.tg.get_my_id()
        for r in added_reactions.reactions:
            if not isinstance(r.type_, tgapi.ReactionTypeEmoji):
                continue
            if not isinstance(r.sender_id, tgapi.MessageSenderUser):
                continue
            if r.sender_id.user_id == my_id:
                emoji = r.type_.emoji
                break
        else:
            self.log.debug("Cannot find which reaction to remove")
            return
        try:
            r = await self.tg.api.remove_message_reaction(
                chat_id=c.legacy_id,
                message_id=legacy_msg_id,
                reaction_type=tgapi.ReactionTypeEmoji(emoji=emoji),
            )
        except BadRequest as e:
            self.log.debug("Remove reaction error: %s", e)
        else:
            self.log.debug("Remove reaction response: %s", r)

    @catch_chat_not_found
    async def react(
        self, c: Recipient, legacy_msg_id: int, emojis: list[str], thread=None
    ):
        if len(emojis) == 0:
            await self.remove_reactions(c, legacy_msg_id)
            return

        # we never have more than 1 emoji, slidge core makes sure of that
        try:
            r = await self.tg.api.add_message_reaction(
                chat_id=c.legacy_id,
                message_id=legacy_msg_id,
                reaction_type=tgapi.ReactionTypeEmoji(emoji=emojis[0]),
                is_big=False,
            )
        except BadRequest as e:
            raise XMPPError("bad-request", text=e.message)
        else:
            self.log.debug("Message reaction response: %s", r)

    @catch_chat_not_found
    async def retract(self, c: Recipient, legacy_msg_id, thread=None):
        f = self.delete_futures[legacy_msg_id] = self.xmpp.loop.create_future()
        r = await self.tg.api.delete_messages(c.legacy_id, [legacy_msg_id], revoke=True)
        self.log.debug("Delete message response: %s", r)
        confirmation = await f
        self.log.debug("Message delete confirmation: %s", confirmation)


log = logging.getLogger(__name__)
