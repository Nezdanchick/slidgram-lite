import asyncio
import logging
import shutil
import typing

from slidge import BaseGateway, FormField, GatewayUser, global_config
from slidge.command.register import RegistrationType
from slidge.util.util import is_valid_phone_number
from slixmpp import JID
from slixmpp.exceptions import XMPPError

from . import config
from .client import CredentialsValidation

if typing.TYPE_CHECKING:
    pass

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
    ]

    GROUPS = True

    LEGACY_MSG_ID_TYPE = LEGACY_CONTACT_ID_TYPE = LEGACY_ROOM_ID_TYPE = int

    def __init__(self):
        super().__init__()
        if not getattr(config, "TDLIB_PATH", None):
            config.TDLIB_PATH = global_config.HOME_DIR / "tdlib"
        self._pending_registrations = dict[
            str, tuple[asyncio.Task[CredentialsValidation], CredentialsValidation]
        ]()
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
        log.debug("CONFIG %s", vars(config))
        self.download_semaphore: asyncio.Semaphore = asyncio.Semaphore(
            config.MAX_PARALLEL_DOWNLOADS
        )

    async def validate(
        self, user_jid: JID, registration_form: dict[str, typing.Optional[str]]
    ):
        phone = registration_form.get("phone")
        if not is_valid_phone_number(phone):
            raise ValueError("Not a valid phone number")
        for u in self.store.users.get_all():
            if u.legacy_module_data.get("phone") == phone:
                raise XMPPError(
                    "not-allowed",
                    text="Someone is already using this phone number on this server.",
                )
        tg_client = CredentialsValidation(registration_form)  # type: ignore
        auth_task = self.loop.create_task(tg_client.start())
        self._pending_registrations[user_jid.bare] = auth_task, tg_client  # type:ignore

    async def validate_two_factor_code(self, user: GatewayUser, code: str):
        auth_task, tg_client = self._pending_registrations.pop(user.jid.bare)
        tg_client.code_future.set_result(code)
        try:
            await asyncio.wait_for(auth_task, config.REGISTRATION_AUTH_CODE_TIMEOUT)
        except asyncio.TimeoutError:
            raise XMPPError(
                "not-authorized",
                text=(
                    "Something went wrong when trying to authenticate you on the "
                    "telegram network. Please retry and/or contact your slidge admin."
                ),
            )
        await tg_client.stop()

    async def unregister(self, user: GatewayUser):
        session = self.session_cls.from_user(user)
        session.logged = False
        workdir = session.tg.settings.files_directory.absolute()
        await session.tg.api.log_out()
        shutil.rmtree(workdir)


log = logging.getLogger(__name__)
