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


def log_error_on_peer_id_invalid(func: WrappedMethod) -> WrappedMethod:
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
    async def wrapped(self, *a, **ka):
        try:
            return await func(self, *a, **ka)
        except XMPPError as e:
            self.log.error(
                "%r in %s called with %s and %s", e.text, func.__name__, a, ka
            )

    return wrapped


def ignore_event_on_peer_id_invalid(func: WrappedMethod) -> WrappedMethod:
    """
    Decorator to silently drop telegram events related to PeerIdInvalid errors.
    This seems to be related to deleted telegram accounts. In some situations, we do not
    even want to log an error when this happens, eg, for message deletion events or
    cached reactions from deleted accounts, since consequences are negligible.
    """

    @functools.wraps(func)
    async def wrapped(self, *a, **ka):
        try:
            return await func(self, *a, **ka)
        except XMPPError as e:
            if e.condition == "item-not-found":
                return
            else:
                self.log.error(
                    "%r in %s called with %s and %s", e.text, func.__name__, a, ka
                )

    return wrapped


def _raise(e: RPCError | InvalidUserException, func: WrappedMethod):
    condition = _ERROR_MAP.get(type(e), "internal-server-error")
    raise XMPPError(
        condition, getattr(e, "MESSAGE", str(e.args)) + f" in '{func.__name__}'"
    )
