"""Config flow: Ring login (with 2FA), then discover the account's pan-tilt cameras."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
import uuid

from ring_doorbell import Auth, AuthenticationError, Requires2FAError, RingError
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import CONF_DEVICES, CONF_HARDWARE_ID, CONF_TOKEN, DOMAIN
from .ptz import USER_AGENT, async_discover

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
EMAIL = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))


class RingPtzConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with a separate Ring session (its own hardware id), so HA's Ring integration isn't affected."""

    VERSION = 1

    def __init__(self) -> None:
        self._username = ""
        self._password = ""
        self._hardware_id = str(uuid.uuid4())

    async def _login(self, code: str | None = None) -> tuple[dict | None, list | None]:
        """Fetch a token and discover cameras; raises Requires2FAError / AuthenticationError / RingError."""
        auth = Auth(USER_AGENT, None, None, self._hardware_id, http_client_session=async_get_clientsession(self.hass))
        token = await auth.async_fetch_token(self._username, self._password, code)
        return token, await async_discover(auth)

    async def _try(self, step_id: str, schema: vol.Schema, code: str | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        try:
            token, devices = await self._login(code)
        except Requires2FAError:
            return await self.async_step_2fa()
        except AuthenticationError:
            errors["base"] = "invalid_auth"
        except (RingError, TimeoutError, OSError):
            _LOGGER.exception("Ring login failed")
            errors["base"] = "cannot_connect"
        else:
            data = {CONF_USERNAME: self._username, CONF_TOKEN: token, CONF_HARDWARE_ID: self._hardware_id,
                    CONF_DEVICES: devices}
            if self.source == SOURCE_REAUTH:
                return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
            if not devices:
                return self.async_abort(reason="no_devices")
            return self.async_create_entry(title=self._username, data=data)
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema = vol.Schema({vol.Required(CONF_USERNAME): EMAIL, vol.Required(CONF_PASSWORD): PASSWORD})
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=schema)
        self._username, self._password = user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
        await self.async_set_unique_id(self._username.lower())
        self._abort_if_unique_id_configured()
        return await self._try("user", schema)

    async def async_step_2fa(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema = vol.Schema({vol.Required(CONF_CODE): str})
        if user_input is None:
            return self.async_show_form(step_id="2fa", data_schema=schema)
        return await self._try("2fa", schema, user_input[CONF_CODE].strip())

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._username = entry_data[CONF_USERNAME]
        self._hardware_id = entry_data.get(CONF_HARDWARE_ID, self._hardware_id)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema = vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD})
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema,
                                        description_placeholders={"username": self._username})
        self._password = user_input[CONF_PASSWORD]
        return await self._try("reauth_confirm", schema)
