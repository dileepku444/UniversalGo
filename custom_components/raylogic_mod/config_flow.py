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
        # BUG FIX: pehle yahan wait_closed() unbounded tha - "Add device"
        # wizard bhi is se bina-timeout wale close ka shikaar ho sakta tha
        # agar device TCP connection cleanly close na kare. Ab CLOSE_TIMEOUT
        # ke andar hard-capped hai.
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
        # v1.6.0: network scan ke hits, aur chune hue hit se form defaults
        # (host/port/model/area/channel_start) - manual add me khaali.
        self._hits: list[dict] = []
        self._suggest: dict[str, Any] = {}

    @staticmethod
    def async_get_options_flow(config_entry):
        return RaylogicModOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """v1.6.0: pehle poochho - network scan ya IP manually."""
        return self.async_show_menu(step_id="user", menu_options=["scan", "manual"])

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Local subnet(s) ke har host ko TCP 5550 par probe karo (discovery.py).
        Pehle se configured hosts (raylogic_mod + raylogic) skip hote hain."""
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
        """Scan me mile modules me se ek chuno -> manual form uske detected
        values (model/area/first channel) se pre-filled khulta hai; user
        confirm/badal sakta hai. Connection validation wahi purana hai."""
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
        """v1.6.6: background auto-scan (__init__.async_auto_discovery_scan)
        ne naya module paaya -> HA me "Discovered" card. unique_id wahi
        host_port jo manual add banata hai, isliye add hone ke baad card
        khud hat jaata hai, aur "Ignore" dabane par dobara nahi aata."""
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
        """Card par "Add" -> yahan confirm. Model pata ho to host/model form
        skip karke seedha (validation ke saath) pre-filled channels form -
        MOD channel types device khud nahi batata, wo user chunta hai."""
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
        """Step 1: host/port/device model - konsa hardware hai (MOD2U ya
        MOD4U) pehle hi maloom hona chahiye taaki step 2 mein sirf utne hi
        channel fields dikhein jitne us model par physically exist karte
        hain."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            user_input[CONF_HOST] = host
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            # F2 (v1.6.1): ye module pehle se 'raylogic' (main/DIN)
            # integration me hai to yahan dobara add mat hone do - warna
            # ek hi device par do TCP connections (do integrations se) khulte.
            # Connect karne se PEHLE check, taaki us live device ko chhuein
            # bhi nahi.
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
            vol.Optional(
                CONF_LEGACY_AREA, default=self._suggest.get(CONF_LEGACY_AREA, LEGACY_DEFAULT_AREA)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=AREA_MAX, mode=selector.NumberSelectorMode.BOX
                )
            ),
            # Kai Raylogic installations mein Area ke andar channel numbers
            # GLOBALLY assign hote hain (har module 1 se shuru nahi hota) -
            # agar Raylogic GO app mein aapke is module ke channels jaise
            # "5, 6" ya "5,6,7,8" dikhte hain (1,2.. nahi), to yahan 5 daal
            # do taaki commands sahi channel number par jaayen.
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
    """'Configure' button - taaki channel type (e.g. ch4 ko dimmer banana)
    ya Area/channel_start badalne ke liye device delete + dobara add na
    karna pade. Device Model yahan se badla nahi ja sakta (channel-count
    fundamentally badal jaata) - model change karna ho to device delete
    karke naya add karo."""

    # BUG FIX (v1.6.0, live HA 2026.9 par pakda gaya): pehle yahan
    # __init__(config_entry) me `self.config_entry = config_entry` set hota
    # tha. Naye HA me `config_entry` ek read-only property hai jo HA khud
    # bharta hai - us assignment se AttributeError aata tha aur "Configure"
    # button 500 error de deta tha. Ab HA ka diya hua self.config_entry hi
    # use hota hai.

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        model = current.get(CONF_DEVICE_MODEL, DEFAULT_MODEL)
        channel_count = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])["channel_count"]

        if user_input is not None:
            _coerce_numbers(user_input)
            if CONF_HOST in user_input:
                user_input[CONF_HOST] = str(user_input[CONF_HOST]).strip()
            # v1.6.2: F2 jaisa hi block - Configure se host badal kar kisi
            # 'raylogic' (main) wale module par point karna save hi na ho.
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
            # v1.6.0: area scenes - kuch likha hai lekin ek bhi valid
            # "area:scene" nahi nikla to typo hai, chup-chaap save mat karo.
            scene_text = str(user_input.get(CONF_SCENE_COUNTS, "") or "").strip()
            user_input[CONF_SCENE_COUNTS] = scene_text
            if scene_text and not parse_scene_map(scene_text):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(model, current),
                    errors={"base": "bad_scene_map"},
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
        # v1.6.6: background auto-discovery (poori integration ke liye -
        # kisi bhi device par band karo to scan band).
        fields[
            vol.Optional(CONF_AUTO_DISCOVERY, default=current.get(CONF_AUTO_DISCOVERY, True))
        ] = selector.BooleanSelector()
        # v1.6.0: Raylogic GO app ke area scenes, e.g. "12:1,2,3; 5:1,4".
        # Kisi bhi EK device par daalo - saare devices ka union banta hai.
        fields[
            vol.Optional(CONF_SCENE_COUNTS, default=current.get(CONF_SCENE_COUNTS, ""))
        ] = selector.TextSelector()
        return vol.Schema(fields)
