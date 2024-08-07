from slidge import entrypoint
from slidge.util.util import get_version  # noqa: F401

from . import command, config, contact, gateway, group, session


def main():
    entrypoint("slidgram")


__all__ = "config", "command", "contact", "gateway", "group", "session", "main"

__version__ = get_version()
