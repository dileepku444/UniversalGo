"""Config flow for Raylogic MOD2U / MOD4U integration (unified)."""
from __future__ import annotations
import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DEFAULT_PORT, DOMAIN, AREA_MAX, LEGACY_DEFAULT_AREA, CLOSE_TIMEOUT,
    DEVICE_MODELS, DEFAULT_MODEL, MODEL_MOD2U, MODEL_MOD4U, MODEL_MOD2F,
    CONF_SCENE_COUNTS, parse_scene_map, CONF_AUTO_DISCOVERY,
)
from . import discovery

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE_MODEL = "device_model"
CONF_LEGACY_AREA = "legacy_area"
CONF_CHANNEL_START = "channel_start"
CONF_CH1_TYPE = "channel_1_type"
CONF_CH2_TYPE = "channel_2_type"
CONF_CH3_TYPE = "channel_3_type"
CONF_CH4_TYPE = "channel_4_type"
CONF_CH1_CTC_MODE = "channel_1_ctc_mode"
CONF_CH2_CTC_MODE = "channel_2_ctc_mode"
CONF_CH3_CTC_MODE = "channel_3_ctc_mode"
CONF_CH4_CTC_MODE = "channel_4_ctc_mode"

_ALL_CH_TYPE_KEYS = (CONF_CH1_TYPE, CONF_CH2_TYPE, CONF_CH3_TYPE, CONF_CH4_TYPE)
_ALL_CH_CTC_MODE_KEYS = (CONF_CH1_CTC_MODE, CONF_CH2_CTC_MODE, CONF_CH3_CTC_MODE, CONF_CH4_CTC_MODE)

# MOD2U/MOD4U do not broadcast their own channel type (no readback like
# the RE8's BR40 has been confirmed) - the type is only set from the
# Raylogic GO app. So instead of auto-detection, enter here the type chosen
# in the app (Select Type screen). The confirmed *AR=/*AZ= formats for
# relay/dimmer/fan/curtain/ctc are all implemented.
CHANNEL_TYPE_OPTIONS = ["relay", "dimmer", "fan", "curtain", "ctc"]

# The CTC Single/Double Driver checkbox (Mod Settings screen) decides which
# wire format is used (*AR= sub-channel vs *AZ= combined), so it must be
# entered here too. One logical CTC entity internally uses BOTH physical
# channels of its pair, so enter the CTC mode on whichever channel you set
# to 'ctc'; the Type field of the other channel of that pair is then
# ignored (only one CTC entity is created). Curtain is PAIRED as well (same
# rule) but has no driver mode.
CTC_MODE_OPTIONS = ["single", "double"]

DEVICE_MODEL_OPTIONS = [MODEL_MOD2U, MODEL_MOD4U, MODEL_MOD2F]


def _default_channel_type(model: str) -> str:
    """fixed_type models (MOD2F) have no 'Select Type' at all - for
    validation, the model's fixed type is the real/default type (a generic
    'relay' default would be WRONG: a MOD2F can never use Area 0/auto-learn,
    because it is a fan, not a relay)."""
    fixed = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL]).get("fixed_type")
    return fixed or "relay"


def _model_options() -> list[selector.SelectOptionDict]:
    return [
        selector.SelectOptionDict(
            value=key, label=f"{info['name']} - {info['desc']}"
        )
        for key, info in DEVICE_MODELS.items()
    ]


def _pair_count(model: str) -> int:
    info = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])
    return max(1, info["channel_count"] // 2)


def _channels_schema_fields(model: str, current: dict | None = None) -> dict:
    """Show only as many channel type/CTC-mode fields as the model's
    channel_count (2 or 4) - 2 on a MOD2U, 4 on a MOD4U. fixed_type models
    (MOD2F) get no fields at all (the channel type is fixed, the user does
    not need to choose it)."""
    model_info = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])
    if model_info.get("fixed_type"):
        return {}
    current = current or {}
    channel_count = model_info["channel_count"]
    fields: dict = {}
    for i in range(channel_count):
        type_key = _ALL_CH_TYPE_KEYS[i]
        ctc_key = _ALL_CH_CTC_MODE_KEYS[i]
        pair_no = (i // 2) + 1
        fields[
            vol.Optional(type_key, default=current.get(type_key, "relay"))
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=CHANNEL_TYPE_OPTIONS)
        )
        fields[
            vol.Optional(ctc_key, default=current.get(ctc_key, "single"))
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=CTC_MODE_OPTIONS)
        )
    return fields


def _coerce_numbers(user_input: dict[str, Any]) -> None:
    """NumberSelector may return a float (e.g. 5550.0) - cast to int so the
    config entry and protocol.py always receive integers."""
    if CONF_PORT in user_input:
        user_input[CONF_PORT] = int(user_input[CONF_PORT])
    if CONF_LEGACY_AREA in user_input:
        user_input[CONF_LEGACY_AREA] = int(user_input[CONF_LEGACY_AREA])
    if CONF_CHANNEL_START in user_input:
        user_input[CONF_CHANNEL_START] = int(user_input[CONF_CHANNEL_START])


async def validate_connection(hass, host: str, port: int) -> dict:
    info: dict[str, Any] = {"mac": None, "node": None}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5.0,
        )
    except Exception as exc:
        raise ConnectionError(f"Cannot connect to {host}:{port}") from exc

    try:
        data = await asyncio.wait_for(reader.readuntil(b'\r'), timeout=5.0)
        line = data.decode(errors="replace").strip()
        if "*KA=" in line:
            info["node"] = line.split(",")[0].strip()
            info["mac"] = f"{host.replace('.', '_')}_{info['node']}"
    except Exception:
        pass
    finally:
        # BUG FIX: wait_closed() used to be unbounded here - the "Add device"
        # wizard could hang on a close without timeout if the device did not
        # close the TCP connection cleanly. It is now hard-capped by
        # CLOSE_TIMEOUT.
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=float(CLOSE_TIMEOUT))
        except Exception:
            pass

    return info


class RaylogicModConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._data: dict[str, Any] = {}
        # v1.6.0: network scan hits, and form defaults taken from the chosen
        # hit (host/port/model/area/channel_start) - empty for a manual add.
        self._hits: list[dict] = []
        self._suggest: dict[str, Any] = {}

    @staticmethod
    def async_get_options_flow(config_entry):
        return RaylogicModOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """v1.6.0: ask first - network scan or manual IP entry."""
        return self.async_show_menu(step_id="user", menu_options=["scan", "manual"])

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Probe every host of the local subnet(s) on TCP 5550 (discovery.py).
        Hosts that are already configured (raylogic_mod + raylogic) are skipped."""
        errors: dict[str, str] = {}
        subnets_default = ", ".join(await discovery.async_local_subnets(self.hass))
        if user_input is not None:
            typed = str(user_input.get("subnets", "") or "").strip()
            subnets = [t.strip() for t in typed.replace(";", ",").split(",") if t.strip()]
            if not discovery.hosts_in(subnets):
                errors["base"] = "bad_subnet"
            else:
                self._hits = await discovery.async_scan(
                    subnets, skip=discovery.configured_hosts(self.hass),
                )
                if self._hits:
                    return await self.async_step_pick()
                errors["base"] = "no_devices_found"
        return self.async_show_form(
            step_id="scan",
            data_schema=vol.Schema({
                vol.Required("subnets", default=subnets_default): selector.TextSelector(),
            }),
            errors=errors,
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Pick one of the modules found by the scan -> the manual form opens
        pre-filled with its detected values (model/area/first channel), which
        the user can confirm or change. Connection validation is unchanged."""
        labels = {discovery.describe(h): h for h in self._hits}
        if user_input is not None:
            hit = labels.get(user_input.get("device"))
            if hit is not None:
                self._suggest = {
                    CONF_HOST: hit["host"],
                    CONF_PORT: hit.get("port", DEFAULT_PORT),
                }
                if hit.get("model"):
                    self._suggest[CONF_DEVICE_MODEL] = hit["model"]
                if hit.get("area") is not None:
                    self._suggest[CONF_LEGACY_AREA] = hit["area"]
                    self._suggest[CONF_CHANNEL_START] = hit["channel_start"]
                return await self.async_step_manual()
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({
                vol.Required("device", default=next(iter(labels))): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(labels))
                ),
            }),
            description_placeholders={"count": str(len(self._hits))},
        )

    # ------------------------------------------------ integration_discovery
    async def async_step_integration_discovery(self, discovery_info: dict) -> FlowResult:
        """v1.6.6: the background auto-scan (__init__.async_auto_discovery_scan)
        found a new module -> "Discovered" card in Home Assistant. The
        unique_id is the same host_port a manual add creates, so the card
        disappears once the module is added and does not return after
        "Ignore"."""
        host = str(discovery_info.get("host", "")).strip()
        port = int(discovery_info.get("port", DEFAULT_PORT))
        if not host or host in discovery.configured_hosts(self.hass):
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(f"{host}_{port}")
        self._abort_if_unique_id_configured()
        self._hits = [dict(discovery_info)]
        self.context["title_placeholders"] = {"name": discovery.describe(discovery_info)}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """"Add" on the card -> confirm here. If the model is known, the
        host/model form is skipped and the pre-filled channels form opens
        directly (with validation) - a MOD device does not report its channel
        types, so the user chooses them."""
        hit = self._hits[0]
        if user_input is not None:
            self._suggest = {CONF_HOST: hit["host"], CONF_PORT: hit.get("port", DEFAULT_PORT)}
            if hit.get("area") is not None:
                self._suggest[CONF_LEGACY_AREA] = hit["area"]
                self._suggest[CONF_CHANNEL_START] = hit["channel_start"]
            if hit.get("model"):
                self._suggest[CONF_DEVICE_MODEL] = hit["model"]
                return await self.async_step_manual({
                    CONF_HOST: hit["host"],
                    CONF_PORT: hit.get("port", DEFAULT_PORT),
                    CONF_DEVICE_MODEL: hit["model"],
                })
            return await self.async_step_manual()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"name": discovery.describe(hit)},
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1: host/port/device model - the hardware (MOD2U or MOD4U)
        must be known first so that step 2 shows only the channel fields
        that physically exist on that model."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            user_input[CONF_HOST] = host
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            # F2 (v1.6.1): if this module is already in the 'raylogic'
            # (main/DIN) integration, do not allow adding it again here -
            # otherwise two TCP connections (from two integrations) would be
            # opened to one device. Checked BEFORE connecting, so that live
            # device is never touched.
            if host in discovery.other_integration_hosts(self.hass):
                errors["base"] = "host_in_other_integration"
            else:
                try:
                    info = await validate_connection(self.hass, host, port)
                except ConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error connecting to %s", host)
                    errors["base"] = "unknown"
                else:
                    # BUG FIX (root cause of "HA startup slow" / duplicate
                    # connections): the unique_id used to be the mac (if the
                    # *KA= line arrived within the config flow's 5 s validation
                    # window) OR the host (if it did not). That was
                    # NON-DETERMINISTIC: when the same physical device was added
                    # TWICE and the *KA= line arrived late the second time (or
                    # not at all - network jitter, or the device busy because a
                    # connection was already open), the unique_id came out
                    # DIFFERENT (mac-based vs raw host string). As a result
                    # `_abort_if_unique_id_configured()` could not catch the
                    # duplicate and TWO config entries were created for the same
                    # physical IP. Both tried to open their own TCP connection;
                    # the device (which may accept only ONE client at a time)
                    # became slow/flaky switching between them, and this
                    # contention caused retries on EVERY startup, making the
                    # whole HA boot feel slow. The unique_id is now always built
                    # deterministically from host:port - node/mac is stored only
                    # as a cosmetic reference and never used for uniqueness, so
                    # a second "Add" for the same IP:port always aborts
                    # immediately (already_configured).
                    unique_id = f"{host}_{port}"
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()
                    self._data.update(user_input)
                    self._data[CONF_PORT] = port
                    return await self.async_step_channels()

        sug = self._suggest
        host_key = (
            vol.Required(CONF_HOST, default=sug[CONF_HOST])
            if CONF_HOST in sug else vol.Required(CONF_HOST)
        )
        schema = vol.Schema({
            host_key: selector.TextSelector(),
            vol.Optional(CONF_PORT, default=sug.get(CONF_PORT, DEFAULT_PORT)): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_DEVICE_MODEL, default=sug.get(CONF_DEVICE_MODEL, DEFAULT_MODEL)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_model_options())
            ),
        })
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    async def async_step_channels(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 2: Area, channel numbering and the type of each channel - only
        as many fields as the model's channel_count (2/4)."""
        model = self._data.get(CONF_DEVICE_MODEL, DEFAULT_MODEL)
        errors: dict[str, str] = {}
        if user_input is not None:
            _coerce_numbers(user_input)
            channel_count = DEVICE_MODELS[model]["channel_count"]
            ch_types = tuple(
                user_input.get(_ALL_CH_TYPE_KEYS[i], _default_channel_type(model)) for i in range(channel_count)
            )
            # The "type" of a Dimmer/Fan/Curtain/CTC channel cannot be derived
            # from an *AR= echo, so the Area must be entered manually for them.
            if user_input[CONF_LEGACY_AREA] == 0 and any(t != "relay" for t in ch_types):
                errors["base"] = "area_required_for_non_relay"
            else:
                self._data.update(user_input)
                return self.async_create_entry(
                    title=f"Raylogic {DEVICE_MODELS[model]['name']} {self._data[CONF_HOST]}",
                    data=self._data,
                )

        schema_fields = {
            vol.Optional(
                CONF_LEGACY_AREA, default=self._suggest.get(CONF_LEGACY_AREA, LEGACY_DEFAULT_AREA)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=AREA_MAX, mode=selector.NumberSelectorMode.BOX
                )
            ),
            # In many Raylogic installations channel numbers are assigned
            # GLOBALLY within an Area (not every module starts at 1) - if the
            # Raylogic GO app shows this module's channels as e.g. "5, 6" or
            # "5,6,7,8" (not 1,2..), enter 5 here so commands are sent to the
            # correct channel numbers.
            vol.Optional(
                CONF_CHANNEL_START, default=self._suggest.get(CONF_CHANNEL_START, 1)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=255, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
        schema_fields.update(_channels_schema_fields(model))
        return self.async_show_form(
            step_id="channels", data_schema=vol.Schema(schema_fields), errors=errors,
        )


class RaylogicModOptionsFlow(config_entries.OptionsFlow):
    """'Configure' button - so that changing a channel type (e.g. making ch4
    a dimmer) or the Area/channel_start does not require deleting and
    re-adding the device. The device model cannot be changed here (it would
    fundamentally change the channel count) - to change the model, delete
    the device and add it again."""

    # BUG FIX (v1.6.0, found on a live HA 2026.9): __init__(config_entry)
    # used to set `self.config_entry = config_entry`. In current Home
    # Assistant `config_entry` is a read-only property populated by HA
    # itself - that assignment raised AttributeError and the "Configure"
    # button returned a 500 error. The self.config_entry provided by HA is
    # now used.

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        model = current.get(CONF_DEVICE_MODEL, DEFAULT_MODEL)
        channel_count = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])["channel_count"]

        if user_input is not None:
            _coerce_numbers(user_input)
            if CONF_HOST in user_input:
                user_input[CONF_HOST] = str(user_input[CONF_HOST]).strip()
            # v1.6.2: same block as F2 - changing the host in Configure to
            # point at a module owned by 'raylogic' (main) cannot be saved.
            if user_input.get(CONF_HOST) in discovery.other_integration_hosts(self.hass):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(model, current),
                    errors={"base": "host_in_other_integration"},
                )
            ch_types = tuple(
                user_input.get(_ALL_CH_TYPE_KEYS[i], _default_channel_type(model)) for i in range(channel_count)
            )
            if user_input[CONF_LEGACY_AREA] == 0 and any(t != "relay" for t in ch_types):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(model, current),
                    errors={"base": "area_required_for_non_relay"},
                )
            # v1.6.0: area scenes - if something was entered but not a single
            # valid "area:scene" could be parsed, it is a typo; do not save it
            # silently.
            scene_text = str(user_input.get(CONF_SCENE_COUNTS, "") or "").strip()
            user_input[CONF_SCENE_COUNTS] = scene_text
            if scene_text and not parse_scene_map(scene_text):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(model, current),
                    errors={"base": "bad_scene_map"},
                )
            # NOTE: async_create_entry() only builds a FlowResult -
            # entry.options are updated when HA's flow manager processes
            # that result (AFTER this function returns). The
            # add_update_listener in __init__.py (_async_update_listener)
            # then triggers a reload with the new options by itself. Do not
            # reload manually here as well, otherwise two overlapping reloads
            # run (one with stale data), which can stall the device
            # connection.
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(step_id="init", data_schema=self._schema(model, current))

    def _schema(self, model: str, current: dict) -> vol.Schema:
        fields = {
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): selector.TextSelector(),
            vol.Optional(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_LEGACY_AREA, default=current.get(CONF_LEGACY_AREA, LEGACY_DEFAULT_AREA)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=AREA_MAX, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_CHANNEL_START, default=current.get(CONF_CHANNEL_START, 1)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=255, mode=selector.NumberSelectorMode.BOX)
            ),
        }
        fields.update(_channels_schema_fields(model, current))
        # v1.6.6: background auto-discovery (integration-wide - turning it
        # off on any device stops the scan).
        fields[
            vol.Optional(CONF_AUTO_DISCOVERY, default=current.get(CONF_AUTO_DISCOVERY, True))
        ] = selector.BooleanSelector()
        # v1.6.0: Raylogic GO app area scenes, e.g. "12:1,2,3; 5:1,4".
        # Enter them on any ONE device - the union of all devices is used.
        fields[
            vol.Optional(CONF_SCENE_COUNTS, default=current.get(CONF_SCENE_COUNTS, ""))
        ] = selector.TextSelector()
        return vol.Schema(fields)
