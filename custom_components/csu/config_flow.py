"""Config flow for Colorado Springs Utilities integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry  # type: ignore
from homeassistant.config_entries import ConfigFlow  # type: ignore
from homeassistant.config_entries import ConfigFlowResult  # type: ignore
from homeassistant.config_entries import CONN_CLASS_CLOUD_POLL  # type: ignore
from homeassistant.config_entries import OptionsFlow  # type: ignore
from homeassistant.const import CONF_PASSWORD  # type: ignore
from homeassistant.const import CONF_USERNAME  # type: ignore
from homeassistant.core import HomeAssistant  # type: ignore
from homeassistant.helpers.aiohttp_client import async_create_clientsession  # type: ignore

from .const import DOMAIN
from .csu import CannotConnect
from .csu import CSU
from .csu import InvalidAuth

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_login(
    hass: HomeAssistant, login_data: dict[str, str]
) -> dict[str, str]:
    """Validate login data and return any errors."""

    api = CSU(
        async_create_clientsession(hass),
        login_data[CONF_USERNAME],
        login_data[CONF_PASSWORD],
    )
    errors: dict[str, str] = {}
    try:
        await api.async_login()
    except InvalidAuth:
        errors["base"] = "invalid_auth"
    except CannotConnect:
        errors["base"] = "cannot_connect"
    return errors


class CSUConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config Flow for setting up CSU."""

    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        """Initialize a new CSUConfigFlow."""
        self.reauth_entry: ConfigEntry | None = None

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry):
        """Get the options flow for this handler."""
        return CSUOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""

        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match(
                {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                }
            )
            errors = await _validate_login(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"'CSU - {user_input[CONF_USERNAME]}",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle configuration by re-auth."""

        self.reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""

        assert self.reauth_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self.reauth_entry.data, **user_input}
            errors = await _validate_login(self.hass, data)
            if not errors:
                self.hass.config_entries.async_update_entry(
                    self.reauth_entry, data=data
                )
                await self.hass.config_entries.async_reload(self.reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): self.reauth_entry.data[CONF_USERNAME],
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class CSUOptionsFlow(OptionsFlow):
    """Options flow for the integration."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""

        self.config_entry = config_entry
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input=None):
        """Manage the options."""

        return await self.async_step_options()

    async def async_step_options(self, user_input=None):
        """Handle options step flow initiated by user."""

        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "electric_rate_per_kwh",
                        default=self.config_entry.options.get(
                            "electric_rate_per_kwh", 0.0
                        ),
                    ): float,
                    vol.Optional(
                        "electric_rate_per_day",
                        default=self.config_entry.options.get(
                            "electric_rate_per_day", 0.0
                        ),
                    ): float,
                    vol.Optional(
                        "gas_rate_per_ccf",
                        default=self.config_entry.options.get("gas_rate_per_ccf", 0.0),
                    ): float,
                    vol.Optional(
                        "gas_rate_per_day",
                        default=self.config_entry.options.get("gas_rate_per_day", 0.0),
                    ): float,
                    vol.Optional(
                        "water_rate_per_cf",
                        default=self.config_entry.options.get("water_rate_per_cf", 0.0),
                    ): float,
                    vol.Optional(
                        "water_rate_per_day",
                        default=self.config_entry.options.get(
                            "water_rate_per_day", 0.0
                        ),
                    ): float,
                }
            ),
            last_step=True,
        )

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(title="", data=self.options)
