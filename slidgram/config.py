import os
import secrets

MEDIA_TOKEN = os.environ.get("SLIDGRAM_MEDIA_TOKEN", "")
MEDIA_TOKEN__DOC = "Token for media server"

PUBLIC_MEDIA_URL = os.environ.get("SLIDGRAM_PUBLIC_MEDIA_URL", "")
PUBLIC_MEDIA_URL__DOC = "Public URL for media server"
PROXY_MEDIA_URL = os.environ.get("SLIDGRAM_PROXY_MEDIA_URL", "")
PROXY_MEDIA_URL__DOC = "URL of proxy for media files"
MEDIA_SERVER_HOST = os.environ.get("SLIDGRAM_MEDIA_SERVER_HOST", "localhost")
MEDIA_SERVER_HOST__DOC = "Host for local media server"
MEDIA_SERVER_PORT = int(os.environ.get("SLIDGRAM_MEDIA_SERVER_PORT", "5050"))
MEDIA_SERVER_PORT__DOC = "Port for local media server"

_api_txt = "\nIf you dont set it, users will have to enter their own on registration."

API_ID: int | None = None
API_ID__DOC = "Telegram app api_id, obtained at https://my.telegram.org/apps" + _api_txt

API_HASH: str | None = None
API_HASH__DOC = (
    "Telegram app api_hash, obtained at https://my.telegram.org/apps" + _api_txt
)

REGISTRATION_AUTH_CODE_TIMEOUT: int = 60
REGISTRATION_AUTH_CODE_TIMEOUT__DOC = (
    "On registration, users will be prompted for a 2FA code they receive "
    "on other telegram clients."
)

GROUP_HISTORY_MAXIMUM_MESSAGES = 50
GROUP_HISTORY_MAXIMUM_MESSAGES__DOC = (
    "The number of messages to fetch from a group history. "
    "These messages and their attachments will be fetched on slidge startup."
)

ATTACHMENT_MAX_SIZE: int = 10 * 1024**2
ATTACHMENT_MAX_SIZE__DOC = (
    "Maximum file size (in bytes) to download from telegram automatically/"
)

BIG_AVATARS = False
BIG_AVATARS__DOC = (
    "Fetch contact avatars in high-resolution (640x640) instead of the "
    "default 160x160. NB: slidge core main config AVATAR_SIZE still applies."
)
