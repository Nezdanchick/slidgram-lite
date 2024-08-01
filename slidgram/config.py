from typing import Optional

_api_txt = "\nIf you dont set it, users will have to enter their own on registration."

API_ID: Optional[int] = None
API_ID__DOC = "Telegram app api_id, obtained at https://my.telegram.org/apps" + _api_txt

API_HASH: Optional[str] = None
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

CONVERT_STICKERS = True
CONVERT_STICKERS__DOC = (
    "Convert incoming animated stickers to webm videos. "
    "Requires lottie_to_webm.sh in $PATH, cf <https://github.com/ed-asriyan/lottie-converter>, "
    "along with FFMPEG."
)

CONVERT_STICKERS_EXECUTABLE = "lottie_to_webm.sh"
CONVERT_STICKERS_EXECUTABLE__DOC = "Path to the TGS/webm converter executable."

CONVERT_STICKERS_SIZE = 128
CONVERT_STICKERS_SIZE__DOC = "Width and height video stickers."

CONVERT_STICKERS_FPS = 60
CONVERT_STICKERS_FPS__DOC = "Framerate of the video stickers"
