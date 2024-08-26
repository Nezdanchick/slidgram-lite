import asyncio
import functools
import logging
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, OrderedDict, ParamSpec, TypeVar

from pyrogram import Client as TelegramClient
from pyrogram.client import Cache
from pyrogram.errors import FloodWait, PeerIdInvalid
from pyrogram.file_id import FileId
from pyrogram.raw.functions.messages import EditChatAdmin, GetAvailableReactions
from pyrogram.raw.types import (
    InputPeerChat,
    InputPeerUser,
    PeerUser,
    ReactionCustomEmoji,
    ReactionEmoji,
)
from pyrogram.raw.types import User as RawUser
from pyrogram.raw.types import UserEmpty
from pyrogram.raw.types.messages import AvailableReactions
from pyrogram.types import Chat, Message, Update, User
from slidge import global_config
from slixmpp.exceptions import XMPPError

from .reactions import ReactionsStore

P = ParamSpec("P")
R = TypeVar("R")
WrappedMethod = Callable[P, R]

AVATAR_DOWNLOAD_SLEEP = 15  # seconds between avatar downloads
MAX_FLOOD_ATTEMPTS = 10


class InvalidUser:
    pass


class InvalidUserException(Exception):
    pass


class Client(TelegramClient):
    message_cache: "MessageCache"

    def __init__(self, name: str) -> None:
        super().__init__(name, workdir=str(global_config.HOME_DIR))

        self._available_reactions: set[str] | None = None
        self.log = logging.getLogger(f"Telegram:{name}")

        self.get_chat = handle_flood(self.get_chat)  # type: ignore
        self.get_contacts = handle_flood(self.get_contacts)  # type: ignore
        self.get_users = handle_flood(self.get_users)  # type: ignore
        self.download_media = handle_flood(self.download_media)  # type: ignore
        self.get_chat_member = handle_flood(self.get_chat_member)  # type: ignore
        self._download_avatar_lock = asyncio.Lock()
        self._get_user_lock = asyncio.Lock()
        self._users_to_get = list[int]()

        self.on_raw_update(group=0)(self._on_raw)  # type:ignore
        self.on_edited_message(group=1)(self._on_edited_message)  # type: ignore

        self._reactions = ReactionsStore(name)
        # We cache raw users because they seem to have a measurably
        # lower memory footprint than pyrogram's "rich" Users.
        self._user_cache: OrderedDict[int, RawUser | InvalidUser] = LimitedSizeDict(
            1_000
        )
        self.message_cache = MessageCache(10_000)

        self._reaction_handler: (
            Callable[[Message, int, str | None], Awaitable[None]] | None
        ) = None

    def is_me(self, user: User | int) -> bool:
        # we need this because is_self is not always set
        if isinstance(user, User):
            if user.is_self is not None:
                return user.is_self
            user_id = user.id
        else:
            user_id = user
        assert self.me is not None
        return self.me.id == user_id

    @property
    def download_path(self):
        # the trailing slash is needed by pyrogram
        return str(global_config.HOME_DIR.absolute() / "telegram_downloads") + "/"

    async def download_avatar(self, file_id: str) -> Path | None:
        async with self._download_avatar_lock:
            await asyncio.sleep(AVATAR_DOWNLOAD_SLEEP)
            path = await self.download_media(file_id, self.download_path)
            if path is None:
                return None
            assert isinstance(path, str)
            return Path(path)

    def get_downloader(self, file_id_str: str) -> AsyncIterator[bytes] | None:
        file_id_obj = FileId.decode(file_id_str)
        return self.get_file(file_id_obj)

    async def get_available_reactions(self) -> AvailableReactions:
        rpc = GetAvailableReactions(hash=0)
        return await self.invoke(rpc)

    async def available_reactions(self) -> set[str]:
        if self._available_reactions is None:
            available_reactions = await self.get_available_reactions()
            self._available_reactions = {
                r.reaction for r in available_reactions.reactions
            }
        return self._available_reactions

    def on_reaction(self, callback):
        self._reaction_handler = callback

    async def _on_edited_message(self, _tg: TelegramClient, message: Message):
        if (
            message.raw.reactions is not None
            and message.raw.reactions.recent_reactions is not None
        ):
            await self._on_reaction(message)

    async def _on_reaction(self, message: Message):
        if self._reaction_handler is None:
            return

        assert message.raw.reactions.recent_reactions is not None
        assert self.me is not None

        new_reacters = set[tuple[int, str]]()
        for reaction in message.raw.reactions.recent_reactions:
            if reaction.my:
                user_id = self.me.id
            elif isinstance(reaction.peer_id, PeerUser):
                user_id = reaction.peer_id.user_id
            else:
                continue

            if isinstance(reaction.reaction, ReactionEmoji):
                new_reacters.add((user_id, reaction.reaction.emoticon))
            elif isinstance(reaction.reaction, ReactionCustomEmoji):
                new_reacters.add((user_id, "❓"))
            else:
                self.log.warning("Unknown reaction: %s", reaction.reaction)

        old_reacters = set(self._reactions.get(message))
        self._reactions.set(message, new_reacters)
        self.log.debug("Old reacters: %s", old_reacters)
        self.log.debug("New reacters: %s", new_reacters)

        old_all_reacters = {x[0] for x in old_reacters}
        new_all_reacters = {x[0] for x in new_reacters}
        for unreacter_id in old_all_reacters - new_all_reacters:
            await self._reaction_handler(message, unreacter_id, None)
        for reacter_id, emoji in new_reacters - old_reacters:
            await self._reaction_handler(message, reacter_id, emoji)

    async def _on_raw(
        self, _tg, update: Update, _users: dict[int, User], _chats: dict[int, Chat]
    ):
        async with self._get_user_lock:
            self.__update_user_cache(update)

    async def get_user(self, user_id: int) -> User:
        """
        Get a telegram user from local cache or through a RPC if needed.

        :param user_id:
        :return:
        """
        async with self._get_user_lock:
            cached_raw = self._user_cache.get(user_id)
            if cached_raw is invalid_user:
                raise InvalidUserException(user_id)
            if cached_raw is not None:
                self.log.debug("user was cached! YAY!")
                # noinspection PyProtectedMember
                cached_user = User._parse(self, cached_raw)  # type:ignore
                assert cached_user is not None
                return cached_user
        self.log.debug("user %s was not cached! damn!", user_id)
        try:
            user = await self.get_users(user_id)
        except PeerIdInvalid:
            self._user_cache[user_id] = invalid_user
            self.log.debug("user %s could not be resolved", user_id)
            raise InvalidUserException(user_id)
        assert isinstance(user, User)
        return user

    async def invoke(self, *a, **k):
        r = await super().invoke(*a, **k)
        self.__update_user_cache(r)
        return r

    def __update_user_cache(self, raw_obj):
        raw_users: list[RawUser] = getattr(raw_obj, "users", [])
        for raw_user in raw_users:
            if isinstance(raw_user, UserEmpty):
                continue
            self._user_cache[raw_user.id] = raw_user

    async def edit_chat_admin(
        self, chat_id: int | str, user_id: int | str, is_admin: bool
    ) -> bool:
        chat = await self.resolve_peer(chat_id)
        if not isinstance(chat, InputPeerChat):
            self.log.warning("Tried to set admin of a wrong peer type: %s", type(chat))
            return False
        user = await self.resolve_peer(user_id)
        if not isinstance(user, InputPeerUser):
            self.log.warning("Tried to make admin but wrong peer type: %s", type(user))
            return False
        return await self.invoke(
            EditChatAdmin(
                chat_id=chat.chat_id,
                user_id=user,  # type:ignore
                is_admin=is_admin,
            )
        )


class LimitedSizeDict(OrderedDict):
    def __init__(self, size: int, *args, **kwargs):
        self._size = size
        super().__init__(*args, **kwargs)
        self._check_size_limit()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._check_size_limit()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._check_size_limit()

    def _check_size_limit(self):
        while len(self) > self._size:
            self.popitem(last=False)


class MessageCache(Cache):
    # we need to subclass the default message cache because _on_tg_deleted_msg
    # comes with message IDs only, and we need to know which chat they actually
    # belong too

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._chat_by_message_ids = LimitedSizeDict(10_000)

    def __setitem__(self, key: tuple[int, int], value: Message) -> None:
        # tuple = [chat_id, message_id]
        super().__setitem__(key, value)
        self._chat_by_message_ids[value.id] = value

    def get_by_message_id(self, message_id: int) -> Message:
        return self._chat_by_message_ids.get(message_id)  # type:ignore

    def remove_chat(self, chat_id: int) -> None:
        # a bit hacky, but works. maybe perf will be an issue eventually
        new = {
            message_id: message
            for message_id, message in self._chat_by_message_ids.items()
            if message.chat.id != chat_id
        }
        self._chat_by_message_ids = LimitedSizeDict(10_000, new)


invalid_user = InvalidUser()


def handle_flood(func: WrappedMethod) -> WrappedMethod:
    @functools.wraps(func)
    async def wrapped(*a, **kw):
        start = datetime.now()
        for i in range(MAX_FLOOD_ATTEMPTS):
            try:
                return await func(*a, **kw)
            except FloodWait as e:
                log.warning(
                    "Flood in %s(%s %s) (%s), sleep for %s seconds",
                    func.__name__,
                    a,
                    kw,
                    start,
                    e.value,
                )
                await asyncio.sleep(e.value + i)
        raise XMPPError("internal-server-error", "Telegram flood")

    return wrapped


log = logging.getLogger(__name__)
