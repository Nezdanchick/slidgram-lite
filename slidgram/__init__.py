from slidge.util.util import get_version  # noqa: F401

from . import command, config, contact, gateway, group, session

__all__ = "command", "config", "contact", "gateway", "group", "session"

__version__ = get_version()
