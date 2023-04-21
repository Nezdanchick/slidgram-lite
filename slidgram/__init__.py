from slidge.util.util import get_version  # noqa: F401

from . import config, contact, gateway, group, session

__all__ = "config", "contact", "gateway", "group", "session"

__version__ = get_version()
