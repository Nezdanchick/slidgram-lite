import logging
from typing import TYPE_CHECKING

from pyrogram.types import ChatPhoto, Photo
from slidge.core.mixins import AvatarMixin

from . import config

if TYPE_CHECKING:
    from .session import Session


class SetAvatarMixin(AvatarMixin):
    session: "Session"
    log: logging.Logger

    async def update_chat_photo(self, photo: ChatPhoto | Photo | None) -> None:
        if photo is None:
            await self.set_avatar(None)
            return

        if isinstance(photo, ChatPhoto):
            if config.BIG_AVATARS:
                file_id = photo.big_file_id
                unique_id = photo.big_photo_unique_id
            else:
                file_id = photo.small_file_id
                unique_id = photo.small_photo_unique_id
        else:
            file_id = photo.file_id
            unique_id = photo.file_unique_id

        if self.avatar_id == unique_id:
            self.log.debug("Cached avatar is OK")
            return

        self.log.debug("Cached avatar is not OK: %r vs %r", self.avatar_id, unique_id)
        self.session.create_task(self.__download(file_id, unique_id))

    async def __download(self, file_id: str, unique_id: str) -> None:
        try:
            path = await self.session.tg.download_avatar(file_id)
        except Exception as e:
            self.log.error("Could not download avatar: %r", e)
            return

        if path is None:
            self.log.error("Empty avatar download path? %r", path)
            return

        await self.set_avatar(path, unique_id, delete=True)
