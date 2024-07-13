from typing import TYPE_CHECKING, Sequence

import sqlalchemy as sa
from pyrogram.types import Message
from sqlalchemy import orm

if TYPE_CHECKING:
    from .session import Session


class Base(orm.DeclarativeBase):
    pass


class Reaction(Base):
    __tablename__ = "reaction"
    __table_args = sa.Index("client_name", "chat_id", "message_id")

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    client_name: orm.Mapped[str] = orm.mapped_column()

    chat_id: orm.Mapped[int] = orm.mapped_column()
    message_id: orm.Mapped[int] = orm.mapped_column()

    user_id: orm.Mapped[int] = orm.mapped_column()
    emoji: orm.Mapped[str] = orm.mapped_column()


class ReactionsMixin:
    session: "Session"

    legacy_id: int
    REACTIONS_SINGLE_EMOJI = True

    async def available_emojis(self, _msg_id=None) -> set[str] | None:
        return await self.session.tg.available_reactions()


class ReactionsStore:
    __slots__ = "_name"

    def __init__(self, name: str):
        self._name = name

    def get(self, message: Message) -> Sequence[tuple[int, str]]:
        with orm.Session(engine) as session:  # noqa: F821
            return session.execute(  # type:ignore
                sa.select(Reaction.user_id, Reaction.emoji)
                .where(Reaction.client_name == self._name)
                .where(Reaction.chat_id == message.chat.id)
                .where(Reaction.message_id == message.id)
            ).all()

    def set(self, message: Message, reactions: set[tuple[int, str]]) -> None:
        with orm.Session(engine) as session:  # noqa: F821
            session.execute(
                sa.delete(Reaction)
                .where(Reaction.client_name == self._name)
                .where(Reaction.chat_id == message.chat.id)
                .where(Reaction.message_id == message.id)
            )

            for user_id, emoji in reactions:
                session.add(
                    Reaction(
                        client_name=self._name,
                        chat_id=message.chat.id,
                        message_id=message.id,
                        user_id=user_id,
                        emoji=emoji,
                    )
                )

            session.commit()


engine: sa.Engine
