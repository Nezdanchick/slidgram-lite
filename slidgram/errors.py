import functools
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import (
    Any,
    Concatenate,
    Never,
    ParamSpec,
    Protocol,
    TypeVar,
)

from pyrogram.errors import (
    AuthKeyUnregistered,
    BadRequest,
    Forbidden,
    ReactionInvalid,
    RPCError,
    Unauthorized,
)
from slixmpp.exceptions import XMPPError
from slixmpp.types import ErrorConditions

from .telegram import InvalidUserException

P = ParamSpec("P")
R = TypeVar("R")


class HasInvalidKeyMethodAndLoggerAttribute(Protocol):
    log: logging.Logger

    async def on_invalid_key(self) -> Never: ...


T = TypeVar("T", bound=HasInvalidKeyMethodAndLoggerAttribute)

WrappedMethod = Callable[Concatenate[T, P], Coroutine[Any, Any, R]]
WrappedIterator = Callable[Concatenate[T, P], AsyncIterator[R]]


_ERROR_MAP: dict[Any, ErrorConditions] = {
    ReactionInvalid: "not-acceptable",
    Forbidden: "forbidden",
    BadRequest: "bad-request",
    Unauthorized: "not-authorized",
    InvalidUserException: "item-not-found",
}


def tg_to_xmpp_errors(func: WrappedMethod[T, P, R]) -> WrappedMethod[T, P, R]:
    @functools.wraps(func)
    async def wrapped(self: T, /, *a: P.args, **ka: P.kwargs) -> R:
        try:
            return await func(self, *a, **ka)
        except AuthKeyUnregistered:
            await self.on_invalid_key()
        except (RPCError, InvalidUserException) as e:
            _raise(e, func)

    return wrapped


def tg_to_xmpp_errors_it(func: WrappedIterator[T, P, R]) -> WrappedIterator[T, P, R]:
    @functools.wraps(func)
    async def wrapped(self: T, /, *a: P.args, **ka: P.kwargs) -> AsyncIterator[R]:
        try:
            async for x in func(self, *a, **ka):
                yield x
        except AuthKeyUnregistered:
            await self.on_invalid_key()
        except (RPCError, InvalidUserException) as e:
            _raise(e, func)

    return wrapped


def log_error_on_peer_id_invalid(
    func: WrappedMethod[T, P, R],
) -> WrappedMethod[T, P, R | None]:
    """
    Decorator to log an error when a telegram event is ignored because of a
    PeerIdInvalid error. Unfortunately, because of slidge's design, if a telegram
    profile cannot be fetched, we need to raise an XMPPError to prevent filling the
    LegacyRoster with invalid user IDs.
    Ideally, we would need to let events propagate to XMPP even if the profile cannot
    be fetched, but this would require some serious refactoring in slidge core. It is
    not even clear whether this is actually achievable, how would we discriminate
    between "bogus user IDs" and "deleted accounts" since telegram does not explicitly
    make the difference?
    Part of the issue is related to MUCs, where we *need* a nickname and not just a user
    ID to translate a telegram event.
    """

    @functools.wraps(func)
    async def wrapped(self: T, /, *a: P.args, **ka: P.kwargs) -> R | None:
        try:
            return await func(self, *a, **ka)
        except XMPPError as e:
            self.log.error(
                "%r in %s called with %s and %s", e.text, func.__name__, a, ka
            )
        return None

    return wrapped


def ignore_event_on_peer_id_invalid(
    func: WrappedMethod[T, P, R],
) -> WrappedMethod[T, P, R | None]:
    """
    Decorator to silently drop telegram events related to PeerIdInvalid errors.
    This seems to be related to deleted telegram accounts. In some situations, we do not
    even want to log an error when this happens, eg, for message deletion events or
    cached reactions from deleted accounts, since consequences are negligible.
    """

    @functools.wraps(func)
    async def wrapped(self: T, /, *a: P.args, **ka: P.kwargs) -> R | None:
        try:
            return await func(self, *a, **ka)
        except XMPPError as e:
            if e.condition != "item-not-found":
                self.log.error(
                    "%r in %s called with %s and %s", e.text, func.__name__, a, ka
                )
            return None

    return wrapped


def _raise(e: RPCError | InvalidUserException, func: Callable[[Any], Any]) -> Never:
    condition = _ERROR_MAP.get(type(e), "internal-server-error")
    raise XMPPError(
        condition, getattr(e, "MESSAGE", str(e.args)) + f" in '{func.__name__}'"
    )
