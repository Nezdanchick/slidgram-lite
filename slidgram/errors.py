import functools
from typing import TYPE_CHECKING, Any, Callable, ParamSpec, TypeVar

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

if TYPE_CHECKING:
    from .session import Session

P = ParamSpec("P")
R = TypeVar("R")
WrappedMethod = Callable[P, R]


_ERROR_MAP: dict[Any, ErrorConditions] = {
    ReactionInvalid: "not-acceptable",
    Forbidden: "forbidden",
    BadRequest: "bad-request",
    Unauthorized: "not-authorized",
    InvalidUserException: "item-not-found",
}


def tg_to_xmpp_errors(func: WrappedMethod) -> WrappedMethod:
    @functools.wraps(func)
    async def wrapped(*a, **ka):
        try:
            return await func(*a, **ka)
        except AuthKeyUnregistered:
            self: "Session" = a[0]
            await self.on_invalid_key()
        except (RPCError, InvalidUserException) as e:
            _raise(e, func)

    return wrapped


def tg_to_xmpp_errors_it(func: WrappedMethod) -> WrappedMethod:
    @functools.wraps(func)
    async def wrapped(*a, **ka):
        try:
            async for x in func(*a, **ka):
                yield x
        except AuthKeyUnregistered:
            self: "Session" = a[0]
            await self.on_invalid_key()
        except (RPCError, InvalidUserException) as e:
            _raise(e, func)

    return wrapped


def catch_peer_id_invalid(func: WrappedMethod) -> WrappedMethod:
    @functools.wraps(func)
    async def wrapped(self, *a, **ka):
        try:
            return await func(self, *a, **ka)
        except XMPPError as e:
            self.log.error(
                "%r in %s called with %s and %s", e.text, func.__name__, a, ka
            )
        except InvalidUserException as e:
            self.log.error("Couldn't find user", e.args[0], func.__name__, a, ka)

    return wrapped


def _raise(e: RPCError | InvalidUserException, func: WrappedMethod):
    condition = _ERROR_MAP.get(type(e), "internal-server-error")
    raise XMPPError(
        condition, getattr(e, "MESSAGE", str(e.args)) + f" in '{func.__name__}'"
    )
