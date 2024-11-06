import logging
import typing

import sqlalchemy as sa
from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered, SessionPasswordNeeded
from pyrogram.types import User as TGUser
from slidge import BaseGateway, global_config
from slidge.command.register import (
    FormField,
    GatewayUser,
    RegistrationType,
    TwoFactorNotRequired,
)
from slidge.util.util import is_valid_phone_number
from slixmpp import JID
from slixmpp.exceptions import XMPPError

from . import config, reactions

if typing.TYPE_CHECKING:
    from .session import Session

REGISTRATION_INSTRUCTIONS = (
    "You need to create a telegram account in an official telegram client.\n\nThen you"
    " can enter your phone number here, and you will receive a confirmation code in the"
    " official telegram client. You can uninstall the telegram client after this if you"
    " want."
)


class Gateway(BaseGateway):
    REGISTRATION_INSTRUCTIONS = REGISTRATION_INSTRUCTIONS
    REGISTRATION_FIELDS = [
        FormField(var="phone", label="Phone number", required=True),
        FormField(
            var="password",
            label="Password (only required if you set up one in Telegram)",
            required=False,
            private=True,
        ),
    ]
    REGISTRATION_TYPE = RegistrationType.TWO_FACTOR_CODE
    ROSTER_GROUP = "Telegram"
    COMPONENT_NAME = "Telegram (slidge)"
    COMPONENT_TYPE = "telegram"
    COMPONENT_AVATAR = "https://web.telegram.org/img/logo_share.png"

    SEARCH_FIELDS = [
        FormField(var="phone", label="Phone number", required=True),
        FormField(
            var="first",
            label="A first name for this contact (you can use whatever you want).",
            required=True,
        ),
        FormField(
            var="last",
            label="A last name for this contact (you can use whatever you want).",
        ),
    ]

    GROUPS = True

    LEGACY_MSG_ID_TYPE = LEGACY_CONTACT_ID_TYPE = LEGACY_ROOM_ID_TYPE = int

    def __init__(self):
        super().__init__()
        if not config.API_ID:
            self.REGISTRATION_FIELDS.extend(
                [
                    FormField(
                        var="info",
                        type="fixed",
                        label="Get API id and hash on https://my.telegram.org/apps",
                    ),
                    FormField(var="api_id", label="API ID", required=True),
                    FormField(var="api_hash", label="API Hash", required=True),
                ]
            )

        if not logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.getLogger("pyrogram.connection.connection").setLevel(
                logging.WARNING
            )
            logging.getLogger("pyrogram.session.session").setLevel(logging.WARNING)
            logging.getLogger("pyrogram.session.auth").setLevel(logging.WARNING)

        reactions_db_path = global_config.HOME_DIR / "reacters.sqlite"
        reactions.engine = sa.create_engine(f"sqlite:///{reactions_db_path}")
        if not reactions_db_path.exists():
            reactions.Base.metadata.create_all(reactions.engine)

        if config.CONVERT_STICKERS:
            self.stickers_dir = global_config.HOME_DIR / "stickers"
            self.stickers_dir.mkdir(exist_ok=True)

    async def validate(self, user_jid: JID, registration_form: dict[str, str | None]):
        phone = registration_form["phone"]
        assert isinstance(phone, str)
        if not is_valid_phone_number(phone):
            raise ValueError("Not a valid phone number")
        for u in self.store.users.get_all():
            if u.legacy_module_data.get("phone") == phone:
                raise XMPPError(
                    "not-allowed",
                    text="Someone is already using this phone number on this server.",
                )
        tg_client = Client(
            str(user_jid.bare),
            phone_number=phone,
            api_id=registration_form.get("api_id") or config.API_ID,
            api_hash=registration_form.get("api_hash") or config.API_HASH,
            workdir=global_config.HOME_DIR,
        )
        if await tg_client.connect():
            await tg_client.disconnect()
            raise TwoFactorNotRequired

        sent_code = await tg_client.send_code(phone)
        log.debug("The confirmation code for has been sent via %s", sent_code)

        _clients[str(user_jid.bare)] = tg_client

        return registration_form | {
            "sent_code_hash": sent_code.phone_code_hash,
            "api_id": registration_form.get("api_id") or config.API_ID,
            "api_hash": registration_form.get("api_hash") or config.API_HASH,
        }

    async def validate_two_factor_code(self, user: GatewayUser, code: str):
        phone = user.legacy_module_data["phone"]
        code_hash = user.legacy_module_data["sent_code_hash"]

        assert isinstance(phone, str)
        assert isinstance(code_hash, str)

        tg_client = _clients[str(user.jid)]
        tg_client.phone_code = code

        try:
            tg_user = await tg_client.sign_in(phone, code_hash, code)
        except SessionPasswordNeeded as e:
            log.debug("Password needed:", exc_info=e)
            password = user.legacy_module_data["password"]
            assert isinstance(password, str)
            tg_user = await tg_client.check_password(password)
            tg_client.password = password

        await tg_client.disconnect()
        del _clients[str(user.jid)]

        if not isinstance(tg_user, TGUser):
            log.error("Not a TG User: %s", tg_user)
            raise XMPPError(
                "not-authorized",
                text=(
                    "Something went wrong when trying to authenticate you on the "
                    "telegram network. Please retry and/or contact your slidge admin."
                ),
            )

    async def unregister(self, user: GatewayUser):
        session: "Session" = self.get_session_from_user(user)  # type: ignore
        try:
            await session.tg.log_out()
        except (AuthKeyUnregistered, ConnectionError):
            # can happen when the session is killed from another tg client
            await session.tg.storage.delete()


_clients: dict[str, Client] = {}


log = logging.getLogger(__name__)
