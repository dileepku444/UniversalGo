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
    DEFAULT_PORT, DOMAIN, AREA_MAX, LEGACY_DEFAULT_AREA,
    DEVICE_MODELS, DEFAULT_MODEL, MODEL_MOD2U, MODEL_MOD4U, MODEL_MOD2F,
)

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

# MOD2U/MOD4U khud apna channel-type broadcast nahi karta (RE8 ke BR40
# jaisa readback confirm nahi hua) - type sirf Raylogic GO app se set
# hota hai. Isliye auto-detect ki jagah, jo type aapne app mein (Select
# Type screen) choose kiya hai wahi yahan bata do - relay/dimmer/fan/
# curtain/ctc sab ke confirmed *AR=/*AZ= formats implement ho chuke hain.
CHANNEL_TYPE_OPTIONS = ["relay", "dimmer", "fan", "curtain", "ctc"]

# CTC ka Single/Double Driver checkbox (Mod Settings screen mein) - konsa
# wire-format (*AR= sub-channel vs *AZ= combined) use hoga, wahi yahan bhi
# batana hoga. CTC ek logical entity DONO physical channels (apni pair ke)
# internally use karta hai, isliye jis bhi channel ko 'ctc' banaoge uska
# CTC-mode yahin daalna hai; us pair ke doosre channel ka Type field us
# waqt ignore ho jata hai (ek hi CTC entity banti hai). Curtain bhi
# PAIRED hai (same rule), lekin uska koi Driver-mode nahi hota.
CTC_MODE_OPTIONS = ["single", "double"]

DEVICE_MODEL_OPTIONS = [MODEL_MOD2U, MODEL_MOD4U, MODEL_MOD2F]


def _default_channel_type(model: str) -> str:
    """fixed_type models (MOD2F) ke liye 'Select Type' hota hi nahi -
    validation ke liye us model ka fixed type hi asli/default type hai
    (generic 'relay' default GALAT hoga: MOD2F ka Area kabhi 0/auto-learn
    nahi ho sakta, kyunki wo Fan hai, Relay nahi)."""
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
    """Model ke channel_count (2 ya 4) ke hisaab se sirf utne hi channel
    type/ctc-mode fields dikhao - MOD2U par sirf 2, MOD4U par sirf 4.
    fixed_type models (MOD2F) ke liye koi field hi nahi (channel type
    fixed hai, user ko choose karne ki zaroorat nahi)."""
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
    """NumberSelector float lauta sakta hai (e.g. 5550.0) - int mein cast
    karo taaki config entry aur protocol.py mein hamesha int hi ho."""
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
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return info


class RaylogicModConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._data: dict[str, Any] = {}

    @staticmethod
    def async_get_options_flow(config_entry):
        return RaylogicModOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1: host/port/device model - konsa hardware hai (MOD2U ya
        MOD4U) pehle hi maloom hona chahiye taaki step 2 mein sirf utne hi
        channel fields dikhein jitne us model par physically exist karte
        hain."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            try:
                info = await validate_connection(self.hass, host, port)
            except ConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error connecting to %s", host)
                errors["base"] = "unknown"
            else:
                # BUG FIX (root cause of "HA startup slow" / duplicate
                # connections): pehle yahan unique_id = mac (agar *KA= line
                # config-flow ke 5s validation window ke andar mil jaaye)
                # ORR host (agar na mile) hota tha. Ye NON-DETERMINISTIC
                # tha - same physical device ko DO ALAG baar add karne ki
                # koshish mein, agar dusri baar *KA= line thodi der se aayi
                # (ya bilkul na aayi - network jitter, device busy jawab
                # dene mein kyunki ek connection pehle se khula hai), to
                # unique_id DIFFERENT ban jaata (mac-based vs raw host
                # string) - is wajah se `_abort_if_unique_id_configured()`
                # is duplicate ko pakad hi nahi paata tha, aur DO config
                # entries usi ek physical IP par ban jaate the. Dono apna
                # apna TCP connection kholne ki koshish karte - device
                # (jo shayad ek time par sirf EK client accept karta hai)
                # in dono ke beech confuse hokar slow/flaky rehta, HAR
                # startup par is contention ki wajah se retries hote,
                # jo poore HA boot ko slow feel karata. Ab unique_id hamesha
                # host:port se hi deterministically banta hai - node/mac sirf
                # cosmetic reference ke liye store hota hai, uniqueness ke
                # liye kabhi use nahi hota, isliye same IP:port ka doosra
                # "Add" hamesha turant abort ho jayega (already_configured).
                unique_id = f"{host}_{port}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                self._data.update(user_input)
                self._data[CONF_PORT] = port
                return await self.async_step_channels()

        schema = vol.Schema({
            vol.Required(CONF_HOST): selector.TextSelector(),
            vol.Optional(CONF_PORT, default=DEFAULT_PORT): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_DEVICE_MODEL, default=DEFAULT_MODEL): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_model_options())
            ),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_channels(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 2: Area, channel numbering, aur har channel ka type - sirf
        model ke channel_count (2/4) jitne fields."""
        model = self._data.get(CONF_DEVICE_MODEL, DEFAULT_MODEL)
        errors: dict[str, str] = {}
        if user_input is not None:
            _coerce_numbers(user_input)
            channel_count = DEVICE_MODELS[model]["channel_count"]
            ch_types = tuple(
                user_input.get(_ALL_CH_TYPE_KEYS[i], _default_channel_type(model)) for i in range(channel_count)
            )
            # Dimmer/Fan/Curtain/CTC ka *AR= echo se "type" pata nahi chal
            # sakta, isliye unke liye Area manually dena zaroori hai.
            if user_input[CONF_LEGACY_AREA] == 0 and any(t != "relay" for t in ch_types):
                errors["base"] = "area_required_for_non_relay"
            else:
                self._data.update(user_input)
                return self.async_create_entry(
                    title=f"Raylogic {DEVICE_MODELS[model]['name']} {self._data[CONF_HOST]}",
                    data=self._data,
                )

        schema_fields = {
            vol.Optional(CONF_LEGACY_AREA, default=LEGACY_DEFAULT_AREA): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=AREA_MAX, mode=selector.NumberSelectorMode.BOX
                )
            ),
            # Kai Raylogic installations mein Area ke andar channel numbers
            # GLOBALLY assign hote hain (har module 1 se shuru nahi hota) -
            # agar Raylogic GO app mein aapke is module ke channels jaise
            # "5, 6" ya "5,6,7,8" dikhte hain (1,2.. nahi), to yahan 5 daal
            # do taaki commands sahi channel number par jaayen.
            vol.Optional(CONF_CHANNEL_START, default=1): selector.NumberSelector(
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
    """'Configure' button - taaki channel type (e.g. ch4 ko dimmer banana)
    ya Area/channel_start badalne ke liye device delete + dobara add na
    karna pade. Device Model yahan se badla nahi ja sakta (channel-count
    fundamentally badal jaata) - model change karna ho to device delete
    karke naya add karo."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        model = current.get(CONF_DEVICE_MODEL, DEFAULT_MODEL)
        channel_count = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])["channel_count"]

        if user_input is not None:
            _coerce_numbers(user_input)
            ch_types = tuple(
                user_input.get(_ALL_CH_TYPE_KEYS[i], _default_channel_type(model)) for i in range(channel_count)
            )
            if user_input[CONF_LEGACY_AREA] == 0 and any(t != "relay" for t in ch_types):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(model, current),
                    errors={"base": "area_required_for_non_relay"},
                )
            # NOTE: async_create_entry() sirf FlowResult banata hai -
            # entry.options tabhi update hote hain jab HA ka flow manager
            # is result ko process karta hai (ye function return hone ke
            # BAAD). __init__.py ka add_update_listener (_async_update_
            # listener) is ke baad khud reload trigger karta hai - naye
            # options ke saath. Manual reload yahan se dobara mat karo,
            # warna do overlapping reload chalte hain (ek stale data ke
            # saath), jo device connection ko atka sakta hai.
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
        return vol.Schema(fields)
