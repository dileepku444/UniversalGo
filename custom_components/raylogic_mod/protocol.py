"""Raylogic MOD2U / MOD4U TCP protocol client - RE8-style architecture.

Ek hi class (RaylogicModDevice) dono models (MOD2U = 2 channel/1 pair,
MOD4U = 4 channel/2 pairs) ko handle karta hai - `model` param (const.py
DEVICE_MODELS) se channel-count decide hoti hai, baaki poora protocol
logic pair-count-agnostic hai.

STATIC / config-based channel setup: fixed channel count (model se: 2
ya 4), area config se ya LEGACY_DEFAULT_AREA (0x0C) se liya jata hai, aur
har channel ka type (relay/dimmer/fan/curtain/ctc) config_flow se aata
hai. Relay/Dimmer/Fan/Curtain/CTC sab is mode mein fully working hain
(inke command formats already confirmed hain).

NOTE: pehle yahan ek "AUTO / BR40" mode bhi tha (RE8/H81 jaisa auto-
discovery, `?BR40=` query se) - MOD2U/MOD4U/MOD2F devices is query ka
jawab kabhi nahi dete the (confirmed via capture), isliye ye path in
models ke liye kabhi kaam hi nahi aata tha aur sirf har connect() par
extra latency (BR40 query wait) add karta tha. User ke kehne par is
poore BR40 code-path ko hata diya gaya hai - ab sirf static/legacy
channel setup use hota hai, jo pehle se hi actual working path tha.
"""
from __future__ import annotations


import asyncio
import json
import logging
import random
import re
import socket
import struct
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    # SIOCOUTQ (Linux) se pata chalta hai ki socket ke send-buffer me
    # kitne bytes abhi bhi UN-ACKED pade hain - yehi humara "command
    # device tak pahuncha ya nahi" ka asli proof hai (device khud koi
    # ACK nahi bhejta - dekho const.py ka COMMAND DELIVERY block).
    # HA OS / Supervised / Docker sab Linux hain, lekin agar kabhi kisi
    # aur platform par chale to ye import fail hone par verification
    # apne aap silently skip ho jaati hai, baaki sab waise hi chalta hai.
    import fcntl

    _SIOCOUTQ = 0x5411
except ImportError:  # pragma: no cover - non-Linux
    fcntl = None
    _SIOCOUTQ = None

from .const import (
    CONNECT_TIMEOUT,
    CLOSE_TIMEOUT,
    WRITE_TIMEOUT,
    CMD_ADDR_HIGH,
    CMD_CHANNEL_DIRECT,
    RELAY_LEVEL_ON,
    RELAY_LEVEL_OFF,
    DIMMER_LEVEL_ON,
    DIMMER_LEVEL_OFF,
    FAN_LEVEL_OFF,
    FAN_SPEEDS,
    CURTAIN_CMD_ADDR_HIGH,
    CURTAIN_CMD_MOVE,
    CURTAIN_CMD_STOP,
    CURTAIN_DIR_OPEN,
    CURTAIN_DIR_CLOSE,
    CURTAIN_RUN_BYTE_DEFAULT,
    CURTAIN_STOP_TAIL,
    CHANNELS_PER_CURTAIN_SLOT,
    CHANNELS_PER_PAIR,
    DEVICE_MODELS,
    DEFAULT_MODEL,
    CLIENT_SENDER_ID,
    RESYNC_INTERVAL,
    SCENE_FUNC,
    SCENE_AREAS,
    SCENE_MAX,
    CH_TYPE_RELAY,
    CH_TYPE_DIMMER,
    CH_TYPE_FAN,
    CH_TYPE_CURTAIN,
    CH_TYPE_CTC,
    CTC_MODE_SINGLE,
    CTC_MODE_DOUBLE,
    CTC_SINGLE_BRIGHTNESS_ON,
    CTC_SINGLE_BRIGHTNESS_OFF,
    CTC_SINGLE_CT_MIN_LEVEL,
    CTC_SINGLE_CT_MAX_LEVEL,
    CTC_DOUBLE_CONST_BYTE,
    CTC_MIN_KELVIN,
    CTC_MAX_KELVIN,
    CTC_DEFAULT_KELVIN,
    DEFAULT_CHANNEL_COUNT,
    LEGACY_DEFAULT_AREA,
    AREA_MIN,
    AREA_MAX,
    KEEPALIVE_CMD,
    KEEPALIVE_INTERVAL,
    KEEPALIVE_IDLE_THRESHOLD,
    KEEPALIVE_VARIANTS,
    KEEPALIVE_GOOD_SESSION,
    KA_REPLY_MIN_GAP,
    FAST_RECONNECT_MIN_SESSION,
    FAST_RECONNECT_DELAY,
    LISTEN_READ_TIMEOUT,
    INITIAL_DRAIN_QUIET,
    INITIAL_DRAIN_MAX,
    RX_SILENCE_TIMEOUT,
    RECONNECT_BACKOFF_STEPS,
    RECONNECT_SETTLE_MIN,
    RECONNECT_SETTLE_MAX,
    DELIVERY_VERIFY_TIMEOUT,
    DELIVERY_VERIFY_POLL,
    LINK_SUSPECT_SECONDS,
    PENDING_COMMAND_MAX_AGE,
    COMMAND_MAX_ATTEMPTS,
    UNAVAILABLE_GRACE_SECONDS,
    SOFT_RECONNECT_SETTLE_MIN,
    SOFT_RECONNECT_SETTLE_MAX,
)

_LOGGER = logging.getLogger(__name__)

# Purani (v1.6.3 tak) jagah - integration folder ke andar, jo HACS update
# par mit jaati hai. Ab sirf migration ke liye padhi jaati hai (P6).
_STATE_DIR = Path(__file__).parent / "device_state"

# P2 (v1.6.4): line terminator - confirmed firmware "\r" bhejta hai, lekin
# kisi firmware ka "\n" ya "\r\n" bhi chal jaaye. Limit wahi asyncio
# StreamReader default (64 KiB) - usse lamba "line" = garbage, reconnect.
_LINE_END_RE = re.compile(rb"[\r\n]")
_RX_LINE_LIMIT = 2 ** 16
# P3: "*AR=" / "+AR40=" jaisa frame-type token (unknown types log-once ke liye)
_FRAME_TYPE_RE = re.compile(r"[*+?][A-Z]{2}\d{0,2}=")
# v1.6.5: *AZ= status se kelvin tabhi update karo jab dono channel ka total
# output kam se kam itna ho (~10% brightness) - iske neeche byte quantization
# colour ko nasht kar deta hai (1% par ek channel round hokar off).
_AZ_MIN_OUTPUT_FOR_KELVIN = 0.10

# SCALE FIX: pehle koi limit nahi thi ki ek saath kitne devices apna TCP
# connect() try kar sakte hain - HA startup par (sab config entries
# lagbhag ek hi waqt setup hote hain), ya jab network/router mein koi
# chhota hiccup aaye aur bahut saare devices ka resync/reconnect ek
# saath trigger ho jaaye, 100 devices ek hi second mein 100 naye TCP
# connections + initial-burst reads try karte the. Chhote network
# (home router/switch) ya HA host (Raspberry Pi jaisa) par ye burst
# khud hi timeouts/"connection lost" ka cascade bana deta tha - jo
# dikhne mein har device ke apne connect() ke fail hone jaisa lagta,
# lekin asal wajah sirf itni thi ki sab kuch bilkul EK SAATH ho raha
# tha. Ab poore integration mein ek saath max
# `_MAX_CONCURRENT_CONNECTS` devices hi apna connect-handshake
# (TCP open + initial burst read + discovery) chala sakte hain -
# baaki apni baari ka wait karte hain (thodi der ke liye, chhota sa
# queue), taaki load hamesha smooth rahe chahe 5 devices ho ya 500.
_MAX_CONCURRENT_CONNECTS = 15
_connect_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CONNECTS)


class RaylogicModDevice:
    """Ek physical MOD2U ya MOD4U module = ek TCP connection."""

    def __init__(
        self,
        ip: str,
        port: int,
        model: str = DEFAULT_MODEL,
        legacy_area: int = LEGACY_DEFAULT_AREA,
        legacy_channel_count: int = DEFAULT_CHANNEL_COUNT,
        channel_start: int = 1,
        channel_types: Optional[dict[int, str]] = None,
        channel_ctc_modes: Optional[dict[int, str]] = None,
        state_callback: Optional[Callable] = None,
        state_dir: Optional[str] = None,
    ):
        self.ip = ip
        self.port = port
        self.state_callback = state_callback

        # Konsa physical device hai (MOD2U ya MOD4U) - device_info (naam/
        # model text) ke liye. Har model ka apna DEVICE_MODELS entry
        # const.py mein hai.
        self._model = model
        _model_info = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])
        self._model_name: str = _model_info["name"]
        self._model_desc: str = _model_info["desc"]

        # Legacy-mode fallback settings (config_flow se ya defaults se)
        self._legacy_area = legacy_area
        self._legacy_channel_count = legacy_channel_count
        # Kai installations mein is module ka pehla physical channel number
        # 1 nahi hota (Area ke andar globally assign hota hai) - jaise
        # confirm hua ek real capture mein: manually-created "ch1/ch2" kaam
        # nahi kar rahe the, kyunki us device ke asli channels 3,4 the.
        self._channel_start = max(1, channel_start)
        # Raylogic GO app mein har channel ka jo type set kiya gaya hai
        # (relay/dimmer/fan/curtain) - device khud ye batata nahi hai, isliye
        # config_flow se manually aata hai. {ch_num: type_str}
        self._channel_types: dict[int, str] = channel_types or {}
        # CTC channels ke liye: 'single' (CW/WW, *AR= frames) ya 'double'
        # (warm+cool, *AZ= frames) - config_flow se aata hai. Non-CTC
        # channels ke liye ignore hota hai.
        self._channel_ctc_modes: dict[int, str] = channel_ctc_modes or {}

        # LEARN mode: koi manual area/channel count na diya ho to device
        # khud *AR= echo (app/physical switch se) sunkar Area + Channel
        # seekhta hai - DIN devices ke BR40 auto-detect jaisa hi result,
        # bina kisi unknown byte guess kiye (sirf confirmed Relay format
        # use hota hai: 00 1A <area> <level> <channel>).
        self._state_key = f"{ip.replace('.', '_')}_{port}"
        # P6 (v1.6.4): learned-channels file ab HA ke /config/.storage/
        # raylogic_mod/ me (__init__.py deta hai) - integration folder HACS
        # update par replace hota hai aur file mit jaati thi. Purani file
        # pehli load par copy ho jaati hai (_load_learned).
        self._state_dir = Path(state_dir) if state_dir else _STATE_DIR
        self._state_file = self._state_dir / f"{self._state_key}_learned.json"
        self._legacy_state_file = _STATE_DIR / f"{self._state_key}_learned.json"
        # P2: apna RX buffer (\r ya \n dono par line todte hain)
        self._rx_buf = b""
        # P3/P4/P5: "sirf ek baar" log hone wale messages ki keys
        self._logged_once: set = set()
        # BUG FIX: pehle yahin __init__ (constructor) ke andar hi disk se
        # synchronously read hota tha - lekin __init__ HA ke event loop se
        # seedha call hota hai (async_setup_entry se), toh ye blocking
        # read/write call poore Home Assistant event loop ko (sirf is
        # integration ko nahi - saari entities/automations/UI) thodi der ke
        # liye freeze kar sakta tha, khaaskar slow disk (SD card / Pi) par -
        # exactly wahi "HA hang/stuck" symptom. Ab load asynchronously,
        # connect() ke andar (thread mein) hota hai - dekho _ensure_learned_loaded().
        self._learned: dict[int, int] = {}  # {ch_num: area}
        self._learned_loaded = False

        # switch.py registers this - called with (ch_num, initial_state)
        # jab bhi koi NAYA channel pehli baar seekha jaaye, taaki entity
        # turant HA mein dynamically add ho sake.
        self.new_channel_callback: Optional[Callable] = None

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._msg_counter = 0
        self._listen_task: Optional[asyncio.Task] = None
        self._ka_task: Optional[asyncio.Task] = None
        self._resync_task: Optional[asyncio.Task] = None
        # STABILITY FIX (v1.5.0) - "connection lost" ka asli root cause.
        # Har connect()/reconnect() naye _listen/_ka/_resync tasks banata
        # tha, lekin PURANE tasks kabhi cancel nahi hote the. Purana
        # resync task apni sleep me pada rehta tha; jab wo uthta, connection
        # dobara zinda mil jaati aur wo bhi ek soft-reconnect kar deta -
        # matlab ek hi device par 2, 3, 4... resync loops chalne lagte the.
        # Real logs me iska asar: RESYNC_INTERVAL 25s hone ke bawajood 94%
        # resync gaps 20s se KAM the (kuch to 4.8s), aur 10 minute me 202
        # TCP session khul gaye jinme se 174 humare apne resync ne banaye.
        #
        # Ab har safal connect ek naya "generation" number leta hai. Sab
        # background loops apna generation yaad rakhte hain aur jaise hi
        # generation badalta hai, khud turant exit ho jaate hain - purane
        # tasks ka koi asar nahi. Saath me _cancel_bg_tasks() unhe
        # explicitly cancel bhi karta hai.
        self._conn_generation = 0
        # Aakhri baar device se KUCH bhi (KA/AR/AZ) kab mila - passive
        # keepalive aur passive dead-connection detection dono isi par
        # chalte hain (dekho _keepalive_loop / _listen_loop).
        self._last_rx: float = 0.0
        self._last_tx: float = 0.0
        self._session_started: float = 0.0
        # Pichhli session kitni der chali - fast-reconnect ka faisla isi
        # se hota hai (expected ~12s close vs genuine failure).
        self._last_session_len: float = 0.0
        # KEEPALIVE AUTO-TUNING (v1.5.3): device har ~12s me connection
        # kaat deta hai kyunki uske *KA= ka humara jawab wo pehchaan nahi
        # pata (humara keepalive akela aisa frame tha jo bina "<id>,"
        # prefix ke jaata tha). Sahi format guess karne ke bajaye code
        # khud try karke seekhta hai - dekho const.py ka KEEPALIVE FORMAT
        # block.
        self._ka_variant = 0
        self._ka_variant_locked = False
        self._ka_best: dict[int, float] = {}
        self._last_ka_reply: float = 0.0
        self._resyncing = False  # soft-reconnect ke dauran duplicate na ho
        # BUG FIX (repeated-EOF-storm bug): pehle _resync_loop() aur
        # _keepalive_loop() dono apni "pehli baar random stagger" (100+
        # devices ka load spread karne ke liye) HAR BAAR fresh se draw
        # karte the - kyunki har connect()/reconnect() par ek NAYA task
        # banta hai, aur andar ka `random.uniform(...)` bhi har baar naya
        # tha. Matlab har reconnect ke baad resync-wait kabhi 25s hota,
        # kabhi sirf 1-2 second - jab bhi chhota wait lagta, connection
        # abhi-abhi bane 1-2 second baad hi phir se band-khol ho jaata,
        # jo device ke fragile WiFi/TCP stack ko confuse kar ke EOF de
        # deta (isi wajah se log mein baar-baar "peer ne connection band
        # kar diya (EOF)" dikhta tha) - device instance ki poori lifetime
        # mein sirf PEHLI baar (HA startup) hi random stagger hona chahiye
        # tha, har reconnect par nahi. Ye do flags (instance-level,
        # connect() calls ke across persist karte hain) ab isko track
        # karte hain - dekho _resync_loop() aur _keepalive_loop() neeche.
        self._resync_staggered = False
        self._keepalive_staggered = False
        # BUG FIX: pehle read-error, write-error, aur periodic-resync teeno
        # apna-apna independent reconnect/connect() chala sakte the - agar
        # ek hi time par 2 chal jaate (jaisa real disconnect + resync ka
        # coincide hona), device par EK SAATH 2 TCP connections khul jaate
        # the. Ye chhota embedded device isse confuse ho kar atak jaata tha
        # - HA integration reload karna padta tha. Ab connect() sirf is
        # lock ke andar hi chalta hai (ek time par ek hi attempt), aur
        # _reconnecting flag duplicate delayed-retry schedule hone se rokta
        # hai.
        self._connect_lock = asyncio.Lock()
        self._reconnecting = False
        self._reconnect_task: Optional[asyncio.Task] = None
        # STABILITY FIX: chhote/turant-recover-hone-wale disconnects (jaise
        # humara apna periodic resync, ya 5-10s ka network blip) ke liye UI
        # mein turant "Unavailable" nahi dikhana - dekho _mark_unavailable_soon().
        self._unavailable_task: Optional[asyncio.Task] = None
        # UX FIX: pehle agar command bhejte waqt connection down mila
        # (jaisa hamare apne 25s ke chhote resync-cycle mein bhi ho sakta
        # hai), command chup-chaap DROP ho jaata tha - sirf ek warning log
        # hoti thi, user ko UI mein turant pata bhi nahi chalta ki uska
        # on/off command asal mein device tak pahuncha hi nahi. Ab aisi
        # commands yahan chhoti si queue mein rakh li jaati hain, aur jaise
        # hi connection wapas aata hai (kuch second mein), _do_connect()
        # inhe khud-ba-khud replay/flush kar deta hai - user ko dobara
        # button dabana nahi padta.
        # {cmd: str, at: float} - `at` isliye taaki bahut purana command
        # replay na ho jaaye (dekho PENDING_COMMAND_MAX_AGE).
        self._pending_commands: list[dict] = []
        self._MAX_PENDING_COMMANDS = 5
        # COMMAND-DELIVERY FIX (v1.5.2): write aur close kabhi ek saath na
        # chalein. Pehle _soft_reconnect/_reconnect ka _close_writer_safe()
        # theek us waqt socket band kar sakta tha jab _send_raw ka data
        # abhi OS buffer me hi tha - command bina kisi error ke gayab ho
        # jaata tha. Ab dono ek hi lock ke andar hain.
        self._send_lock = asyncio.Lock()
        # Chal rahe delivery-verification tasks ke strong references.
        self._verify_tasks: set[asyncio.Task] = set()
        # ON-DEMAND RECONNECT: jab entity ko manually toggle karo ya koi
        # scene trigger ho jab device disconnect hai, background
        # _reconnect() loop ka backoff-wait (kabhi kabhi 30s tak) khatam
        # hone ka intezaar nahi karna - turant ek connect() attempt fire
        # karo taaki command jaldi se jaldi apply ho jaaye. Dekho
        # _trigger_on_demand_connect() neeche.
        self._on_demand_task: Optional[asyncio.Task] = None
        # BUG FIX (the big one - device power-cycle "stuck forever" bug):
        # jab tak _shutdown True na ho (matlab disconnect() /HA unload
        # explicitly ho), reconnect KABHI permanently give up nahi karega
        # - dekho _reconnect() neeche.
        self._shutdown = False

        # Device identity
        self.node_id: Optional[str] = None       # e.g. "101" - device ka apna ID
        # v1.6.3 (D1): entity unique_id / device identifier ka STABLE base.
        # Pehle "node_id or ip" tha - node_id sirf tab milta jab *KA= connect
        # ke 0.6s ke andar aa jaaye (idle module ~6s me bhejta hai), isliye
        # restart-to-restart unique_id badal jaata tha -> "_2" duplicate
        # entities + purane orphan. Ab config entry ka unique_id (host_port,
        # add karte waqt fix) use hota hai - __init__.py set karta hai; ye
        # default sirf fallback hai (same format).
        self.stable_id: str = f"{ip}_{port}"
        self.mac: Optional[str] = None
        self.fw_version: Optional[str] = None

        # Curtain frame ka aakhri "run" byte (travel parameter). Default
        # CURTAIN_RUN_BYTE_DEFAULT hai, lekin jaise hi device se koi real
        # curtain echo aata hai (app se ya physical switch se chalane par),
        # us slot ke liye asli value yahan seekh li jaati hai - taaki HA
        # bilkul wahi bheje jo Raylogic GO app bhejti hai.
        # {curtain_slot: run_byte}
        self._curtain_run_bytes: dict[int, int] = {}

        # Device ne *KA= frame mein khud jo Area/channel-range bataye
        # (self-report) - config verify karne ke liye. Dekho
        # _handle_ka_line() / _verify_against_device_report().
        self.detected_area: Optional[int] = None
        self.detected_channel_start: Optional[int] = None
        self.detected_channel_end: Optional[int] = None
        self._config_mismatch_logged = False

        # channel_states[ch_num] = {"area": int, "type": str, "on": bool, ...}
        self.channel_states: dict[int, dict] = {}

        # v1.6.0: har Area ka aakhri recall hua scene {area: scene} -
        # keypad/app echo (*AR=000F..) ya HA ke apne recall se update hota
        # hai. select.py isse initial value leta hai.
        self.active_scene: dict[int, int] = {}

        # BUG FIX (v1.5.4, journal-test se pakda gaya): manual-mode ke
        # channels ab YAHIN, constructor me hi ban jaate hain.
        #
        # Pehle ye sirf ek SAFAL connect() ke andar (_setup_legacy_channels)
        # bante the. Matlab agar device us waqt na mila (boot ho raha ho,
        # WiFi abhi aayi na ho, ya ek connect refuse ho jaaye), to
        # channel_states KHAALI reh jaata - aur set_relay/set_dimmer/
        # set_fan/set_cover sabse pehle `area` dhoondhte hain aur na milne
        # par CHUP-CHAAP return kar jaate the. Entity dikhti thi, click
        # bhi hota tha, lekin command kabhi bheja hi nahi jaata - na queue
        # me jaata, na retry hota. Test me ek aisa device 5 me se 0
        # command bhej paya.
        #
        # Area aur channel types config_flow se aate hain, device se nahi -
        # inhe connection ka intezaar karne ki koi zaroorat hi nahi thi.
        # Ab command hamesha kam se kam queue me chala jaata hai aur
        # connection banate hi apne aap device tak pahunch jaata hai.
        # (LEARN mode - area 0 - waise hi connect() par depend karta hai,
        # kyunki wahan channels device ke apne frames se seekhe jaate hain.)
        if self._legacy_area and self._legacy_area > 0:
            self._setup_legacy_channels()

    # ------------------------------------------------------------------ #
    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def ip_suffix(self) -> str:
        return self.ip.split(".")[-1]

    @property
    def model(self) -> str:
        return self._model

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_desc(self) -> str:
        return self._model_desc

    @property
    def channel_start(self) -> int:
        return self._channel_start

    def pair_index_for_channel(self, ch_num: int) -> int:
        """Ye channel is DEVICE ke andar konse pair mein hai (0 = pehla
        pair, 1 = doosra pair). Sirf display/logging ke liye - curtain ka
        wire byte iss se NAHI, curtain_slot_for_channel() se banta hai."""
        lo, _hi = self._pair_bounds(ch_num)
        return (lo - self._channel_start) // CHANNELS_PER_PAIR

    def curtain_slot_for_channel(self, ch_num: int) -> int:
        """GLOBAL curtain slot number - yehi curtain frame ka teesra byte
        hai (const.py ka Curtain block dekho).

        Raylogic installation mein channel numbers poore system mein
        globally, hamesha 2-2 ke block mein allot hote hain, isliye kisi
        bhi pair ka slot uske chhote channel number se seedha derive ho
        jaata hai:

            slot = (pair ka chhota channel + 1) // 2

        Examples (user ke asli devices se verified):
            ch 23-24 (Area 08, MOD2U)  -> slot 12 (0x0C)
            ch 13-14 (Area 12, MOD4U)  -> slot  7 (0x07)
            ch  3-4  (Area 07, MOD4U)  -> slot  2 (0x02)
            ch  5-6  (Area 07, MOD4U)  -> slot  3 (0x03)

        Isi wajah se ab koi bhi device, kisi bhi Area (1-16) mein aur
        kisi bhi channel number par add karo - curtain ka sahi frame
        khud ban jaata hai, kuch hardcode karne ki zaroorat nahi."""
        lo, _hi = self._pair_bounds(ch_num)
        return (lo + 1) // CHANNELS_PER_CURTAIN_SLOT

    def curtain_frame(self, ch_num: int, action: str) -> Optional[str]:
        """`action` ('open'/'close'/'stop') ke liye poora curtain hex
        payload banao (*AR= ke baad ka hissa). Ab ye 100% derive hota
        hai - koi per-pair hardcoded literal nahi."""
        slot = self.curtain_slot_for_channel(ch_num)
        if action == "stop":
            return (
                f"{CURTAIN_CMD_ADDR_HIGH}{CURTAIN_CMD_STOP:02X}"
                f"{slot:02X}{CURTAIN_STOP_TAIL}"
            )
        if action == "open":
            direction = CURTAIN_DIR_OPEN
        elif action == "close":
            direction = CURTAIN_DIR_CLOSE
        else:
            return None
        run = self._curtain_run_bytes.get(slot, CURTAIN_RUN_BYTE_DEFAULT)
        return (
            f"{CURTAIN_CMD_ADDR_HIGH}{CURTAIN_CMD_MOVE:02X}"
            f"{slot:02X}{direction:02X}{run:02X}"
        )

    def _find_curtain_channel(self, slot: int) -> Optional[int]:
        """Incoming curtain frame ke slot byte se apna configured curtain
        channel dhoondo (curtain frame mein Area byte hota hi nahi, isliye
        area se match nahi kiya ja sakta)."""
        for cn, st in self.channel_states.items():
            if st.get("type") != CH_TYPE_CURTAIN:
                continue
            if self.curtain_slot_for_channel(cn) == slot:
                return cn
        return None

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        if self._shutdown:
            return False
        async with self._connect_lock:
            if self._connected:
                _LOGGER.debug(
                    "Raylogic %s %s: already connected, skip duplicate "
                    "connect() call.", self._model_name, self.ip,
                )
                return True
            return await self._do_connect()

    async def _do_connect(self) -> bool:
        if not self._learned_loaded:
            # Disk read ab thread mein hoti hai (asyncio.to_thread) - event
            # loop kabhi block nahi hota, chahe disk kitni bhi slow ho.
            self._learned = await asyncio.to_thread(self._load_learned)
            self._learned_loaded = True
        # BUG FIX (v1.5.4, stress-test se pakda gaya): naya socket kholne
        # se PEHLE purana hamesha band karo.
        #
        # Pehle sirf _soft_reconnect() purana socket band karta tha. Baaki
        # saare raste - EOF (listen loop), delivery-verify fail, write
        # error, RX silence - sirf `_connected = False` set karte the aur
        # socket ko waise ka waisa chhod dete the. Peer ne FIN bhej diya
        # tha, lekin HAMARI taraf se connection kabhi close nahi hoti thi
        # (half-closed pada rehta tha), aur upar se naya connect() ek AUR
        # socket khol deta tha. Device ki nazar me ek hi client ke DO
        # connections - in chhote modules ke liye yehi wo halat hai jisme
        # wo confuse ho kar atak jaate hain / commands ignore karne lagte
        # hain. Stress test me 100 me se 44 device par ye reproduce hua.
        #
        # Ab ye ek hi jagah par central fix hai - _do_connect kabhi bhi
        # purana socket khula chhod kar naya nahi kholta.
        await self._close_writer_safe()
        self._reader = None

        async with _connect_semaphore:
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.ip, self.port),
                    timeout=float(CONNECT_TIMEOUT),
                )
                # RESEARCH-BACKED FIX: ye Raylogic modules ESP8266-class
                # sasta WiFi chip use karte hain - ye chip family ek jaani-
                # maani quirk ke liye mashhoor hai (ESP8266 Arduino core
                # issue #2552 jaisi kayi reports): agar TCP par Nagle's
                # algorithm ON rahe (chhote packets thodi der buffer/delay
                # hote hain taaki bade packet mein combine ho sakein), to
                # inka chhota/buggy TCP stack kabhi-kabhi connection ko
                # khud hi "band" samajh leta hai aur EOF de deta hai.
                # TCP_NODELAY set karke Nagle's algorithm OFF kar diya -
                # har chhota command (jaise humara *KA=01) turant bhej
                # diya jaata hai, buffer mein ruke bina - isse in modules
                # ke stack ko confuse hone ka mauka kam milta hai.
                try:
                    sock = self._writer.get_extra_info("socket")
                    if sock is not None:
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except (OSError, AttributeError) as exc:
                    _LOGGER.debug(
                        "Raylogic %s: TCP_NODELAY set nahi ho paya (not "
                        "fatal): %s", self.ip, exc,
                    )
                self._connected = True
                self._reconnecting = False
                self._rx_buf = b""          # P2: naya socket, naya buffer
                self._logged_once.discard("ar40")   # P5: per-session
                # Naya generation - purane background loops (agar koi abhi
                # bhi apni sleep me pade hon) is se turant invalid ho jaate
                # hain aur khud exit kar jaate hain.
                self._conn_generation += 1
                gen = self._conn_generation
                now = self._now()
                prev_session = (
                    now - self._session_started if self._session_started else 0.0
                )
                self._session_started = now
                self._last_rx = now
                await self._cancel_bg_tasks()
                if prev_session:
                    _LOGGER.info(
                        "Connected to Raylogic %s at %s (pichhli session %.0fs "
                        "chali thi)", self._model_name, self.ip, prev_session,
                    )
                else:
                    _LOGGER.info(
                        "Connected to Raylogic %s at %s", self._model_name, self.ip,
                    )

                # LATENCY FIX: pehle koi bhi pending/queued command (jaise
                # on-demand reconnect - user ne button dabaya jab device
                # disconnect tha) sirf _drain_initial_push() (2.5s fixed
                # wait) aur poora channel-setup + task-creation complete
                # hone ke BAAD bheji jaati thi. TCP par likhna (write) aur
                # padhna (read) do bilkul independent directions hain -
                # command bhejne ke liye device ka initial burst padhna
                # (drain) zaroori nahi hai. Isliye ab command TCP connect
                # hote hi TURANT bhej dete hain, drain/setup ka wait kiye
                # bina - on-demand reconnect ka real "button-press se
                # device tak" latency ~2.5-3.5s (pehle ka guaranteed
                # minimum) se ghat kar sirf TCP handshake time (LAN par
                # aksar kuch sau millisecond) tak reh jaata hai.
                if self._pending_commands:
                    pending, self._pending_commands = self._pending_commands, []
                    fresh = [
                        p for p in pending
                        if (now - p["at"]) <= PENDING_COMMAND_MAX_AGE
                    ]
                    stale = len(pending) - len(fresh)
                    if stale:
                        _LOGGER.debug(
                            "Raylogic %s: %d purane pending command(s) chhod "
                            "diye (%ds se zyada purane).",
                            self.ip, stale, PENDING_COMMAND_MAX_AGE,
                        )
                    if fresh:
                        _LOGGER.info(
                            "Raylogic %s: connection wapas aa gaya - %d pending "
                            "command(s) TURANT bhej rahe hain (drain/setup se "
                            "pehle hi, latency kam karne ke liye). User ko "
                            "dobara click karne ki zaroorat nahi.",
                            self.ip, len(fresh),
                        )
                    # BUG FIX (v1.5.4, stress-test se pakda gaya): pehle
                    # poori list queue se NIKAL kar ek local loop me bheji
                    # jaati thi. Agar us loop ke beech me kahin bhi koi
                    # exception aa jaata (jaise nayi connection ka turant
                    # mar jaana), to loop wahin ruk jaata aur BACHE HUE
                    # commands - jo queue se already nikal chuke the -
                    # hamesha ke liye gayab ho jaate. Mild chaos wale
                    # stress test me 500 me se 5 command isi tarah gum
                    # hue (queue khaali, phir bhi deliver nahi).
                    #
                    # Ab queue hi unhe "own" karti hai: command tabhi
                    # nikalta hai jab wo turant bheja ja raha ho, aur agar
                    # beech me kuch bhi galat ho to baaki sab queue me
                    # hi safe pade rehte hain (agla connect unhe bhej
                    # dega). Loop bounded hai taaki _send_raw ka apna
                    # re-queue karna infinite loop na bana de.
                    self._pending_commands = fresh + self._pending_commands
                    for _ in range(len(fresh)):
                        if not self._pending_commands:
                            break
                        p = self._pending_commands.pop(0)
                        # verify=True taaki replay bhi confirm ho, aur
                        # queue_on_disconnect=True taaki agar ye nayi
                        # connection bhi turant mar jaaye to command DROP
                        # na ho kar dobara queue me chala jaaye.
                        await self._send_raw(
                            p["cmd"], queue_on_disconnect=True, verify=True,
                            attempt=p.get("attempt", 0),
                        )

                await self._drain_initial_push()

                # BR40 auto-discovery poori tarah hata di gayi hai (user ki
                # request par) - MOD2U/MOD4U/MOD2F kabhi bhi `?BR40=` query
                # ka jawab nahi dete the, isliye ye path kabhi kaam hi nahi
                # aata tha, sirf har connect() par extra latency (BR40
                # probe wait) add karta tha. Ab seedha static/legacy
                # channel setup use hota hai - yehi hamesha se actual
                # working path tha.
                self._setup_legacy_channels()

                self._listen_task = asyncio.create_task(self._listen_loop(gen))
                self._ka_task = asyncio.create_task(self._keepalive_loop(gen))
                self._resync_task = asyncio.create_task(self._resync_loop(gen))

                self._cancel_unavailable_grace()
                if self.state_callback:
                    self.state_callback(self.ip, None, {"available": True})

                return True

            except Exception as exc:
                # exc ka str() kabhi khaali bhi ho sakta hai (jaise bare
                # ConnectionResetError) - type bhi log karo warna log me
                # sirf "Failed to connect ...:" dikhta hai, koi wajah nahi.
                _LOGGER.error(
                    "Failed to connect to Raylogic %s: %s%s",
                    self.ip, type(exc).__name__,
                    f" - {exc}" if str(exc) else "",
                )
                self._connected = False
                # BUG FIX: pehle yahan reader/writer close nahi hote the agar
                # connect ke baad (jaise auto-discovery step mein) exception aa
                # jaaye - har 30s retry par ek TCP socket leak hota, jo lambe
                # samay mein HA host par file-descriptor exhaustion se poore
                # system ko slow/stuck kar sakta tha.
                await self._close_writer_safe()
                self._reader = None
                return False

    @staticmethod
    def _now() -> float:
        return asyncio.get_event_loop().time()

    def _keepalive_frame(self) -> str:
        """Abhi jo keepalive variant try/lock kiya gaya hai uska poora
        frame. Dekho const.py ka KEEPALIVE FORMAT block."""
        template = KEEPALIVE_VARIANTS[self._ka_variant % len(KEEPALIVE_VARIANTS)]
        return template.format(
            id=CLIENT_SENDER_ID, seq=self._next_msg(), cmd=KEEPALIVE_CMD,
        )

    def _record_session_result(self, length: float) -> None:
        """Session khatam hone par: is keepalive variant ne kitni der
        connection zinda rakhi, ye yaad rakho aur agar ye 12-second wali
        deewar tod raha hai to isi ko lock kar do."""
        self._last_session_len = length
        if self._ka_variant_locked:
            return
        idx = self._ka_variant % len(KEEPALIVE_VARIANTS)
        self._ka_best[idx] = max(self._ka_best.get(idx, 0.0), length)
        if length >= KEEPALIVE_GOOD_SESSION:
            self._ka_variant_locked = True
            _LOGGER.info(
                "Raylogic %s %s: keepalive format '%s' ne connection %.0fs "
                "tak zinda rakhi (device ka ~12s wala auto-disconnect toot "
                "gaya) - ab hamesha yahi format use hoga.",
                self._model_name, self.ip, KEEPALIVE_VARIANTS[idx], length,
            )
            return
        # Is variant se baat nahi bani - agle connect par agla try karo.
        self._ka_variant += 1
        nxt = self._ka_variant % len(KEEPALIVE_VARIANTS)
        _LOGGER.debug(
            "Raylogic %s: keepalive format '%s' se session sirf %.0fs chali "
            "- agla format try kar rahe hain: '%s'",
            self.ip, KEEPALIVE_VARIANTS[idx], length, KEEPALIVE_VARIANTS[nxt],
        )

    async def _answer_device_keepalive(self) -> None:
        """Device har ~6s me apna *KA= bhejta hai aur uska JAWAB maangta
        hai - do jawab miss hone par (6x2 = wahi 12 second) wo connection
        kaat deta hai. Isliye ab hum har device-KA ka turant jawab dete
        hain (purana code fixed timer par, aur bina prefix ke, bhejta tha
        - dekho const.py). Rate-limit isliye ki agar device kabhi burst
        me KA bheje to hum spam na karein."""
        if not self._connected or self._shutdown:
            return
        if self._last_ka_reply and (
            self._now() - self._last_ka_reply
        ) < KA_REPLY_MIN_GAP:
            return
        self._last_ka_reply = self._now()
        await self._send_raw(self._keepalive_frame())

    async def _cancel_bg_tasks(self):
        """Purane listen/keepalive/resync tasks band karo.

        STABILITY FIX (v1.5.0): pehle ye kabhi hota hi nahi tha - har
        reconnect naye tasks bana deta tha aur purane apni sleep me zinda
        pade rehte the. Wo baad me uth kar apna kaam (khaaskar resync ka
        connection band-khol) dobara kar dete the, jis se ek hi device par
        multiple resync loops jam jaate the aur connection lagataar tootne
        lagti thi.

        Apne aap ko cancel nahi karte (soft-reconnect khud _resync_loop ke
        andar se hi chalta hai) - us case me _conn_generation guard purane
        task ko turant exit karwa deta hai."""
        current = asyncio.current_task()
        for attr in ("_listen_task", "_ka_task", "_resync_task"):
            task = getattr(self, attr, None)
            if task is not None and task is not current and not task.done():
                task.cancel()
            if task is not current:
                setattr(self, attr, None)

    async def _close_writer_safe(self):
        """BUG FIX: writer.close() + wait_closed() ko hamesha CLOSE_TIMEOUT
        ke andar wrap karo. Pehle koi upper-bound nahi tha, isliye agar
        device TCP connection cleanly close na kare (flaky network / sasta
        embedded device), to wait_closed() bina kisi limit ke atak sakta
        tha - "connect() phir se kuch der atak jaata hai" wale symptom ka
        yehi root cause tha. Ab timeout hone par bhi hum writer ko discard
        kar dete hain (best-effort close), taaki caller kabhi CLOSE_TIMEOUT
        se zyada wait na kare."""
        if not self._writer:
            return
        # COMMAND-DELIVERY FIX (v1.5.2): send-lock lekar close karo, taaki
        # koi abhi-abhi likha hua command beech me hi truncate na ho jaaye.
        # Lock par bhi timeout - agar kisi wajah se lock fansa reh jaaye to
        # close phir bhi hona chahiye (warna reconnect atak jaayega).
        try:
            await asyncio.wait_for(
                self._send_lock.acquire(), timeout=float(CLOSE_TIMEOUT)
            )
            locked = True
        except (asyncio.TimeoutError, RuntimeError):
            locked = False
        try:
            if not self._writer:
                return
            writer = self._writer
            self._writer = None
            try:
                writer.close()
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=float(CLOSE_TIMEOUT)
                )
            except Exception:
                pass
        finally:
            if locked:
                self._send_lock.release()

    async def disconnect(self):
        # Sabse pehle set karo - taaki agar ek reconnect-loop already chal
        # raha ho (device abhi bhi down hai), wo apni agli sleep/attempt ke
        # baad khud ruk jaaye, HA unload/reload hone ke baad bhi background
        # mein hamesha ke liye chalta na rahe.
        self._shutdown = True
        self._connected = False
        for task in (
            self._listen_task, self._ka_task, self._resync_task,
            self._reconnect_task, self._unavailable_task,
            self._on_demand_task, *tuple(self._verify_tasks),
        ):
            if task:
                task.cancel()
        await self._close_writer_safe()

    async def _reconnect(self):
        """BUG FIX (THE main "device power-cycle ke baad HA hamesha ke
        liye atka reh jaata hai" bug): pehle ye function sirf EK BAAR,
        30 second baad, dobara connect() try karta tha - agar wahi ek
        attempt (jaise device abhi boot ho hi raha ho, ya thoda aur der
        se network par aaye) fail ho jaata, to `finally` mein
        `_reconnecting = False` reset ho jaata aur function seedha khatam
        ho jaata - koi aage ka retry KABHI schedule nahi hota. Chunki
        `_send_raw()` (jab `_connected` pehle se hi False ho) command ko
        chup-chaap DROP kar deta hai bina dobara `_schedule_reconnect()`
        call kiye, integration hamesha ke liye "disconnected" state mein
        permanently atak jaata - device wapas ping/reachable ho jaane ke
        baad bhi, jab tak koi HA ko manually reload/restart na kare.
        Real-world mein device power-cycle (off phir on) 30 second se
        zyada le hi leta hai boot hone mein, isliye ye almost hamesha
        trigger ho jaata tha - exactly wahi symptom jo report hua tha:
        device wapas up + pingable, lekin HA entities hamesha ke liye
        unavailable/stuck.

        Fix: ab ye ek proper LOOP hai - jab tak connect() safal na ho
        jaaye YA integration explicitly disconnect/unload na ho jaaye
        (`_shutdown`), har 30 second par dobara try karta rehta hai,
        bilkul us "retrying in 30s" log message jaisa jo already tha
        (bas ab woh sach mein baar-baar retry bhi karta hai)."""
        try:
            # STABILITY FIX: turant "available: False" fire nahi karte -
            # ek chhota grace-timer shuru karo (dekho _mark_unavailable_soon).
            # Agar niche wala backoff-loop grace window ke andar hi
            # reconnect kar leta hai, UI mein kabhi flicker nahi dikhega.
            self._mark_unavailable_soon()
            attempt = 0
            # FAST RECONNECT (v1.5.3): agar pichhli session normal lambi
            # chali thi, to ye device ka expected ~12s auto-disconnect hai,
            # koi failure nahi - turant wapas jud jao. Measured impact:
            # har cycle ~2.3s downtime se ghat kar ~0.1s, uptime 84% -> 98%.
            fast = self._last_session_len >= FAST_RECONNECT_MIN_SESSION
            while not self._connected and not self._shutdown:
                if fast and attempt == 0:
                    delay = FAST_RECONNECT_DELAY
                    _LOGGER.debug(
                        "Raylogic %s %s: expected auto-disconnect - turant "
                        "reconnect (%.2fs).", self._model_name, self.ip, delay,
                    )
                else:
                    delay = RECONNECT_BACKOFF_STEPS[
                        min(attempt, len(RECONNECT_BACKOFF_STEPS) - 1)
                    ]
                    _LOGGER.warning(
                        "Raylogic %s %s: connection lost, retrying in %ds",
                        self._model_name, self.ip, delay,
                    )
                attempt += 1
                await asyncio.sleep(delay)
                if self._shutdown:
                    return
                if fast and attempt == 1:
                    # Expected close - settle-gap ki zaroorat nahi (device
                    # ne khud, cleanly, band ki thi).
                    try:
                        await self.connect()
                    except Exception as exc:
                        _LOGGER.error(
                            "Raylogic %s %s: fast-reconnect me error: %s",
                            self._model_name, self.ip, exc,
                        )
                    fast = False
                    continue
                # STABILITY FIX (v1.5.0): reconnect se pehle ek chhota
                # random settle-gap. Real logs me 5 baar "Failed to connect
                # ... peer ne connection band kar diya (EOF)" aaya tha -
                # matlab module ne TCP to accept kar liya lekin turant band
                # kar diya, kyunki uska purana socket abhi cleanup hua hi
                # nahi tha. Soft-reconnect me ye gap pehle se tha, lekin
                # is (asli failure wale) raste par nahi tha. Random hone se
                # 10 devices ek saath stampede bhi nahi karte.
                await asyncio.sleep(
                    random.uniform(RECONNECT_SETTLE_MIN, RECONNECT_SETTLE_MAX)
                )
                if self._shutdown:
                    return
                # connect() khud _connected set karta hai (safal hone par
                # True) - loop condition apne aap dobara check kar lega,
                # safal hote hi loop yahi ruk jaayega. Fail hua to koi
                # exception yahan tak nahi aani chahiye (_do_connect apne
                # andar hi sab exceptions handle karta hai aur False
                # return karta hai) - lekin ek extra safety net rakhte
                # hain taaki koi anexpected exception is poore retry-loop
                # ko crash na kar de (jo phir se wahi "permanently stuck"
                # bug wapas la deta).
                try:
                    await self.connect()
                except Exception as exc:
                    _LOGGER.error(
                        "Raylogic %s %s: unexpected error during "
                        "reconnect attempt (will retry in 30s): %s",
                        self._model_name, self.ip, exc,
                    )
        finally:
            self._reconnecting = False

    def _mark_unavailable_soon(self):
        """Disconnect hote hi turant entity ko 'Unavailable' mat dikhao.
        UNAVAILABLE_GRACE_SECONDS ka ek chhota grace-window shuru karo -
        agar usi window ke andar-andar connect() wapas safal ho jaata hai
        (jaisa zyadatar chhote blips/resync-hiccups mein hota hai), koi
        state_callback fire hi nahi hoga - matlab HA UI mein kabhi
        'Unavailable' flicker dikhega hi nahi. Sirf genuine, lambi outage
        ke liye hi entity Unavailable dikhegi."""
        if self._unavailable_task and not self._unavailable_task.done():
            return  # ek grace-timer already pending hai, dobara mat lagao
        self._unavailable_task = asyncio.create_task(self._unavailable_after_grace())

    async def _unavailable_after_grace(self):
        try:
            await asyncio.sleep(UNAVAILABLE_GRACE_SECONDS)
            if not self._connected and self.state_callback:
                self.state_callback(self.ip, None, {"available": False})
        except asyncio.CancelledError:
            pass

    def _cancel_unavailable_grace(self):
        """connect() safal hote hi call hota hai - agar grace-timer abhi
        pending tha (matlab UI mein 'Unavailable' abhi tak dikha hi nahi
        tha), use cancel kar do taaki wo late-fire ho kar galti se
        already-wapas-connected device ko unavailable na dikha de."""
        if self._unavailable_task and not self._unavailable_task.done():
            self._unavailable_task.cancel()
        self._unavailable_task = None

    def _schedule_reconnect(self):
        """Read-error, write-error, ya resync-fail - kahin se bhi reconnect
        chahiye ho, hamesha isi se guzro - taaki ek time par sirf EK
        reconnect-loop chale (device par 2 TCP connections ek saath khulne
        se device khud confuse ho kar atak jaata tha, HA reload karna
        padta tha).

        BUG FIX: pehle `_reconnecting = True` sirf `_reconnect()` coroutine
        ke ANDAR set hota tha - lekin ek naya asyncio task create hone ke
        baad turant nahi chalta (event loop ko turn milne tak wait karta
        hai). Agar isi synchronous stack ke andar `_schedule_reconnect()`
        dobara (jaldi jaldi, jaise ek connect-failure cascade mein) call ho
        jaaye, purana task abhi shuru hi nahi hua hota - flag abhi bhi
        False dikhta, aur DUPLICATE reconnect tasks ban jaate the. Ab flag
        yahin, task create hone se PEHLE, synchronously set hota hai."""
        if self._shutdown:
            return
        if not self._reconnecting:
            self._reconnecting = True
            self._reconnect_task = asyncio.create_task(self._reconnect())

    def _trigger_on_demand_connect(self):
        """User ne entity toggle ki, ya koi scene trigger hui, jab device
        disconnect state mein tha - iske liye background `_reconnect()`
        loop ka apna backoff-schedule (jo ek genuine outage ke case mein
        1s se badhte hue 15s/30s tak pahunch sakta hai) khatam hone ka
        wait nahi karna chahiye, warna command minute-scale tak "queued"
        hi padi reh sakti hai.

        Ye function turant, alag se, ek best-effort `connect()` attempt
        fire karta hai (fire-and-forget task) - safe hai kyunki:
          - `connect()` khud `_connect_lock` ke andar hai, isliye ye aur
            background `_reconnect()` loop kabhi ek saath do TCP
            connections nahi kholenge (jo bhi pehle lock le, dusra uske
            baad `_connected` already True dekh kar turant no-op ho
            jaayega).
          - `_do_connect()` module-level `_connect_semaphore` se bhi
            gated hai, isliye 100+ device installation mein bhi ek saath
            bahut saare on-demand attempts "thundering herd" nahi
            banate - wahi existing global concurrency-limit yahan bhi
            apply hoti hai.
        Agar ye attempt fail ho jaaye (device sach mein abhi down hai),
        koi problem nahi - background `_reconnect()` loop already chal
        raha hai (ise `_schedule_reconnect()` ne start kiya tha jab
        connection tooti thi) aur apna normal backoff-retry jaari
        rakhega.
        """
        if self._connected or self._shutdown:
            return
        if self._on_demand_task and not self._on_demand_task.done():
            return  # ek on-demand attempt already pending hai, dobara mat lagao
        self._on_demand_task = asyncio.create_task(self._on_demand_connect())

    async def _on_demand_connect(self):
        try:
            await self.connect()
        except Exception as exc:
            # connect()/_do_connect() apne andar hi saari exceptions handle
            # karte hain aur False return karte hain - lekin extra safety
            # net rakhte hain taaki ye fire-and-forget task kabhi bhi
            # "Task exception was never retrieved" jaisi unhandled warning
            # na de. Background _reconnect() loop retry jaari rakhega.
            _LOGGER.debug(
                "Raylogic %s: on-demand reconnect attempt fail hua (normal "
                "background retry loop chalta rahega): %s", self.ip, exc,
            )

    async def _resync_loop(self, gen: int):
        """Har RESYNC_INTERVAL second mein connection ko khud band-khol
        karta hai - App reopen karne jaisa hi effect, taaki Raylogic App se
        kiya gaya koi bhi change (jo live-broadcast nahi hota) kuch second
        mein HA mein bhi reflect ho jaaye. HA se bheji gayi commands (jo
        instantly optimistically apply hoti hain) is se disturb nahi hoti.

        SCALE FIX: pehle pehli sleep bhi seedha RESYNC_INTERVAL (fixed 45s)
        thi - iska matlab agar bahut saare devices (50-100+) HA startup ke
        thodi der ke andar-andar add/connect hote hain, to unke resync-cycle
        andar-andar EXACTLY sync ho jaate the, aur har ~45s mein SAARE
        devices EK SAATH apna TCP connection band-khol karte the
        ("thundering herd") - ye ek chhote device (Raspberry Pi jaisa) par
        CPU/network spike de sakta hai jo HA ko thodi der ke liye
        unresponsive kar de. Isliye pehli cycle ka wait RANDOM hai (0 se
        RESYNC_INTERVAL ke beech, per-device alag) taaki 100+ devices ka
        load poore 45-second window mein spread ho jaaye, na ki ek hi pal
        mein.

        BUG FIX (repeated-EOF-storm bug): pehle ye "random pehli baar"
        wait HAR baar dobara fresh se draw hota tha - kyunki ye function
        khud har naye connect()/soft-reconnect() ke baad ek NAYE task ke
        roop mein phir se shuru hota hai (dekho _do_connect), aur
        `random.uniform(...)` bhi wahin call ke andar tha. Matlab pehla
        cycle to theek tha, lekin USKE BAAD ka HAR resync bhi phir se
        random hi tha - kabhi 25s, lekin kabhi sirf 1-2 second bhi aa
        sakta tha. Jab bhi wo chhota wait aata, connection abhi-abhi
        bane 1-2 second baad hi dobara jaan-boojh kar band-khol ho jaata
        - jis se device ka fragile WiFi/TCP stack confuse ho kar EOF de
        deta tha (log mein baar-baar dikhne wala "peer ne connection band
        kar diya (EOF)" isi ki wajah se tha). Random stagger sirf is
        device-instance ki PEHLI resync-cycle mein hona chahiye tha (HA
        startup jitter), uske baad har cycle poore, fixed RESYNC_INTERVAL
        ka hona chahiye - `self._resync_staggered` (instance-level, sab
        reconnects ke across persist karta hai) ab yahi guarantee karta
        hai."""
        if not RESYNC_INTERVAL:
            # v1.5.4: resync band hai (const.py me wajah likhi hai) - device
            # apna state khud live push karta hai, aur connection ab stable
            # hai, isliye jaan-boojh kar connection todne ka koi fayda nahi,
            # sirf downtime hai.
            return
        if not self._resync_staggered:
            self._resync_staggered = True
            wait = random.uniform(0, RESYNC_INTERVAL)
        else:
            # Har cycle par thoda jitter, taaki 10+ devices apne resync ek
            # hi pal par sync karke stampede na bana lein.
            wait = RESYNC_INTERVAL * random.uniform(0.85, 1.15)
        while self._connected and not self._resyncing and gen == self._conn_generation:
            await asyncio.sleep(wait)
            # STABILITY FIX (v1.5.0): ye generation-check hi wo missing
            # guard hai jiski wajah se purane resync tasks (jo reconnect ke
            # baad bhi zinda reh jaate the) baar-baar connection tod rahe
            # the. Ab purani session ka task yahin chup-chaap khatam ho
            # jaata hai.
            if not self._connected or self._resyncing or gen != self._conn_generation:
                return
            _LOGGER.debug(
                "Raylogic %s: periodic resync (App-reopen jaisa "
                "soft-reconnect) taaki App se hue changes bhi sync ho jaayein.",
                self.ip,
            )
            await self._soft_reconnect()
            return  # naya connect() apna khud ka fresh resync-loop shuru kar dega

    async def _soft_reconnect(self):
        """Purana socket band karke turant naya connect() - is baar
        'available: False' event fire NAHI karte (bahut chhota gap hota
        hai, HA UI mein flicker nahi dikhna chahiye jab tak reconnect
        sach mein fail na ho jaaye)."""
        self._resyncing = True
        for task in (self._listen_task, self._ka_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        await self._close_writer_safe()
        self._connected = False
        self._resyncing = False
        # STABILITY FIX: purana socket band karke UPAR SE TURANT naya
        # connect() try karne se kai device apna purana socket saaf karne
        # se pehle hi nayi connection reject/EOF kar dete the. Ek chhota
        # "saans lene ka" gap (per-device random, taaki sab devices sync
        # na ho jaayein) deta hai device ko cleanup karne ka time.
        await asyncio.sleep(random.uniform(SOFT_RECONNECT_SETTLE_MIN, SOFT_RECONNECT_SETTLE_MAX))
        ok = await self.connect()
        if not ok:
            _LOGGER.warning(
                "Raylogic %s: periodic resync fail hua, normal reconnect "
                "cycle sambhal lega.", self.ip,
            )
            self._mark_unavailable_soon()
            self._schedule_reconnect()

    # ------------------------------------------------------------------ #
    # I/O
    # ------------------------------------------------------------------ #
    def _next_msg(self) -> str:
        self._msg_counter = (self._msg_counter % 999) + 1
        return f"{self._msg_counter:03d}"

    def _queue_command(self, cmd: str, attempt: int = 0) -> None:
        """Command ko replay-queue me daalo (bounded + timestamped).

        BUG FIX (v1.5.4, stress-test se pakda gaya): queue me command
        daalna tabhi kaam ka hai jab koi na koi use FLUSH bhi kare -
        flush sirf ek safal connect() ke andar hota hai. Pehle kuch raste
        (jaise `_send_raw` ka "connection down hai" wala branch) sirf
        `_trigger_on_demand_connect()` call karte the, jo ek hi best-effort
        koshish karta hai aur fail hone par chup ho jaata hai. Agar us
        waqt background `_reconnect()` loop bhi nahi chal raha ho (jaise
        wo abhi-abhi ek safal connect ke baad exit kar chuka ho), to
        command queue me pada-pada hamesha ke liye reh jaata tha - user
        ka click bekaar, koi error bhi nahi. Mild chaos wale stress test
        me 500 me se 5 command isi tarah gum hue.

        Ab har queue-add ke saath ye guarantee bhi jaati hai ki reconnect
        loop chal raha hai. `_schedule_reconnect()` idempotent hai (agar
        pehle se chal raha ho to turant return kar deta hai)."""
        self._pending_commands.append(
            {"cmd": cmd, "at": self._now(), "attempt": attempt}
        )
        if len(self._pending_commands) > self._MAX_PENDING_COMMANDS:
            self._pending_commands.pop(0)
        if not self._connected and not self._shutdown:
            self._schedule_reconnect()

    async def _resend_unconfirmed(self, cmd: str, attempt: int) -> None:
        """Command ka device tak pahunchna CONFIRM nahi hua - dobara bhejo.

        BUG FIX (v1.5.4, stress-test se pakda gaya): pehle agar verify ke
        beech me hi reconnect ho jaata (generation badal jaati), to
        _verify_delivery chup-chaap return kar deta tha - ye maan kar ki
        "reconnect wala rasta sambhal lega". Lekin us purane socket par
        likha hua command kahin queue me tha hi nahi, to wo HAMESHA KE
        LIYE gum ho jaata tha. Stress test me 500 me se 89 command isi
        tarah gayab hue (queue khaali, delivery 411 par ruki hui).

        Ab aisa command naye connection par dobara bheja jaata hai. Sab
        commands idempotent hain (absolute level, toggle nahi), isliye
        dobara bhejna safe hai. COMMAND_MAX_ATTEMPTS ka cap infinite
        retry se bachata hai."""
        if self._shutdown:
            return
        if attempt + 1 >= COMMAND_MAX_ATTEMPTS:
            _LOGGER.error(
                "Raylogic %s %s: command %d koshish ke baad bhi device tak "
                "nahi pahunch paya - chhod rahe hain: '%s'",
                self._model_name, self.ip, COMMAND_MAX_ATTEMPTS, cmd,
            )
            return
        _LOGGER.info(
            "Raylogic %s: command ka pahunchna confirm nahi hua (connection "
            "badal gayi) - naye connection par dobara bhej rahe hain "
            "(koshish %d/%d): '%s'",
            self.ip, attempt + 2, COMMAND_MAX_ATTEMPTS, cmd,
        )
        await self._send_raw(
            cmd, queue_on_disconnect=True, verify=True, attempt=attempt + 1,
        )

    @staticmethod
    def _unacked_bytes(sock) -> Optional[int]:
        """Socket ke send-queue me kitne bytes abhi tak device ke TCP
        stack se ACK nahi hue. 0 = sab kuch device tak pahunch gaya.
        None = is platform par ye check available nahi (Linux-only)."""
        if fcntl is None or sock is None:
            return None
        try:
            raw = fcntl.ioctl(sock.fileno(), _SIOCOUTQ, struct.pack("I", 0))
            return struct.unpack("I", raw)[0]
        except (OSError, ValueError, AttributeError):
            return None

    async def _verify_delivery(
        self, cmd: str, gen: int, sock, attempt: int = 0,
    ) -> None:
        """Background me confirm karo ki command sach me device tak
        pahuncha. Device khud koi ACK nahi bhejta, lekin uska TCP stack
        bhejta hai - SIOCOUTQ 0 hone ka matlab hai data receive ho gaya.

        Ye entity click ko block nahi karti (alag task me chalti hai),
        isliye UI ka response waisa hi turant rehta hai."""
        try:
            await self._verify_delivery_inner(cmd, gen, sock, attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # safety net - background task kabhi
            _LOGGER.debug(      # unhandled exception na de
                "Raylogic %s: delivery verification me error (ignore): %s",
                self.ip, exc,
            )

    async def _verify_delivery_inner(
        self, cmd: str, gen: int, sock, attempt: int = 0,
    ) -> None:
        deadline = self._now() + DELIVERY_VERIFY_TIMEOUT
        last_pending = None
        while self._now() < deadline:
            if gen != self._conn_generation:
                # Reconnect ho chuka aur is command ka pahunchna CONFIRM
                # nahi hua - purane socket par likha hua data gum ho gaya
                # hoga. Naye connection par dobara bhejo.
                await self._resend_unconfirmed(cmd, attempt)
                return
            pending = self._unacked_bytes(sock)
            if pending is None:
                return  # platform support nahi, verification skip
            if pending == 0:
                _LOGGER.debug(
                    "Raylogic %s: command device tak pahuncha (TCP ACK): %s",
                    self.ip, cmd,
                )
                return
            # FALSE-POSITIVE GUARD: agar queue ghat rahi hai to data JA
            # raha hai - bas link thoda slow/busy hai (100+ device wale
            # setup me ho sakta hai). Aise me deadline thoda aage badha
            # do; sirf tab "dead" maano jab bilkul koi progress hi na ho.
            if last_pending is not None and pending < last_pending:
                deadline = self._now() + DELIVERY_VERIFY_TIMEOUT
            last_pending = pending
            await asyncio.sleep(DELIVERY_VERIFY_POLL)

        if self._shutdown:
            return
        if gen != self._conn_generation:
            await self._resend_unconfirmed(cmd, attempt)
            return
        # Timeout - device ne TCP level par bhi data ACK nahi kiya, matlab
        # socket sach me dead hai. Ye wahi case hai jisme pehle command
        # chup-chaap kho jaata tha aur user ko dobara click karna padta tha.
        _LOGGER.warning(
            "Raylogic %s %s: command %.1fs me device tak nahi pahuncha "
            "(TCP ACK nahi mila) - socket dead maan kar reconnect kar rahe "
            "hain aur command KHUD DOBARA bhejenge, aapko phir se click "
            "karne ki zaroorat nahi: '%s'",
            self._model_name, self.ip, DELIVERY_VERIFY_TIMEOUT, cmd,
        )
        self._connected = False
        self._queue_command(cmd, attempt)
        self._mark_unavailable_soon()
        self._schedule_reconnect()
        self._trigger_on_demand_connect()

    async def _send_raw(
        self, cmd: str, queue_on_disconnect: bool = False,
        verify: bool = False, attempt: int = 0,
    ):
        # PRE-FLIGHT CHECK (v1.5.2): device normally har 6-12s me apna
        # frame bhejta hai. Agar LINK_SUSPECT_SECONDS se ekdum khamoshi
        # hai to socket bahut sambhavna se dead hai - us par likh kar
        # command gawane se behtar hai ki use queue karke pehle connection
        # refresh kar lein. (listen-loop apna RX_SILENCE_TIMEOUT check
        # bhi karta hai, lekin wo thoda der se trigger hota hai - user ka
        # click uska intezaar na kare.)
        if (
            verify
            and self._connected
            and self._last_rx
            and (self._now() - self._last_rx) > LINK_SUSPECT_SECONDS
        ):
            _LOGGER.warning(
                "Raylogic %s %s: %.0fs se device chup hai - command bhejne "
                "se pehle connection refresh kar rahe hain (command queue "
                "me safe hai, reconnect hote hi chala jaayega).",
                self._model_name, self.ip, self._now() - self._last_rx,
            )
            self._connected = False
            self._schedule_reconnect()

        if not self._connected or not self._writer:
            if queue_on_disconnect:
                # Bounded queue (max N) taaki agar user bahut jaldi-jaldi
                # toggle kare to memory mein bekaar buildup na ho.
                self._queue_command(cmd, attempt)
                _LOGGER.info(
                    "Raylogic %s: command abhi bheji nahi ja saki (connection "
                    "down) - queue mein rakh di, reconnect hote hi khud-ba-khud "
                    "bhej di jaayegi: '%s'", self.ip, cmd,
                )
                # ON-DEMAND FIX: sirf queue karke background backoff-cycle
                # ka wait mat karo - yeh ek REAL control command hai (user ne
                # toggle kiya ya scene trigger hui), isliye turant ek connect()
                # attempt bhi fire karo taaki command jaldi se jaldi apply ho.
                self._trigger_on_demand_connect()
            else:
                _LOGGER.warning(
                    "Raylogic %s: command DROP hui kyunki connection abhi "
                    "active nahi hai (connected=%s) - '%s' bheja nahi ja saka. "
                    "Device se connection wapas ban raha hoga (reconnect "
                    "cycle) - thodi der baad dobara try karo.",
                    self.ip, self._connected, cmd,
                )
            return
        try:
            # Write + drain ab _send_lock ke andar hai, taaki koi
            # _close_writer_safe() beech me socket band karke humara
            # abhi-abhi likha hua command truncate na kar de.
            async with self._send_lock:
                if not self._connected or not self._writer:
                    self._queue_command(cmd, attempt)
                    self._trigger_on_demand_connect()
                    return
                writer = self._writer
                gen = self._conn_generation
                sock = writer.get_extra_info("socket")
                writer.write((cmd + "\r").encode())
                await asyncio.wait_for(writer.drain(), timeout=float(WRITE_TIMEOUT))
                self._last_tx = self._now()
            _LOGGER.debug("TX %s: %s", self.ip, cmd)
            if verify:
                # Background me delivery confirm karo - UI ko rokte nahi.
                # Task ka reference rakhna zaroori hai: bina strong-ref ke
                # asyncio task ko garbage-collect kar sakta hai (Python ka
                # jaana-mana fire-and-forget pitfall) - tab verification
                # chup-chaap gayab ho jaati.
                task = asyncio.create_task(
                    self._verify_delivery(cmd, gen, sock, attempt)
                )
                self._verify_tasks.add(task)
                task.add_done_callback(self._verify_tasks.discard)
        except asyncio.TimeoutError:
            _LOGGER.error(
                "Raylogic %s: command send timed out after %ss (socket "
                "half-dead / device not ACKing) - marking disconnected "
                "and reconnecting.", self.ip, WRITE_TIMEOUT,
            )
            self._connected = False
            if verify:
                # v1.5.2: pehle yahan command bas gum ho jaata tha. Ab
                # queue me daal kar reconnect ke baad khud replay hota hai.
                self._queue_command(cmd, attempt)
                self._trigger_on_demand_connect()
            self._schedule_reconnect()
        except Exception as exc:
            _LOGGER.error("Send error to Raylogic %s: %s", self.ip, exc)
            self._connected = False
            if verify:
                self._queue_command(cmd, attempt)
                self._trigger_on_demand_connect()
            self._schedule_reconnect()

    async def _send_addressed(self, cmd: str):
        """CONFIRMED from real Docklight capture (device connected DIRECTLY,
        192.168.1.34:5550): wire traffic ALWAYS carries a "<id>,<seq>,"
        prefix before *AR=/+AR40= - the official PDF's bare "*AR=...\\r"
        examples are only the logical payload, not the real wire format.

        Pehle do galtiyan hui thi:
          1. Prefix bilkul hata diya tha (PDF examples dekh kar) - galat,
             real traffic mein prefix hota hai.
          2. Device ke apne broadcast id (jo *KA=/+AR40= lines mein "109"
             jaisa dikhta hai) ko apna sender-id samajh liya tha - galat,
             wo device/hub ki APNI identity hai, hamari nahi. Real working
             client commands (jaise "099,155,*AR=001A040203") ek ALAG id
             use karte hain - wahi CLIENT_SENDER_ID hai.
        """
        await self._send_raw(
            f"{CLIENT_SENDER_ID},{self._next_msg()},{cmd}",
            queue_on_disconnect=True,
            # v1.5.2: ye ek REAL user command hai (entity click / scene) -
            # iski delivery TCP-ACK se confirm karo, aur na pahunchne par
            # khud dobara bhejo. Keepalive is flag ke bina jaata hai.
            verify=True,
        )

    async def _read_line(self, timeout: float = 2.0) -> Optional[str]:
        try:
            line = await asyncio.wait_for(self._read_frame(), timeout=timeout)
            # Device se kuch bhi mila = connection pakka zinda hai. Passive
            # keepalive aur passive dead-detection dono isi timestamp par
            # chalte hain (dekho _keepalive_loop / _listen_loop).
            self._last_rx = self._now()
            return line
        except asyncio.TimeoutError:
            return None
        except asyncio.IncompleteReadError as exc:
            # ROOT CAUSE (webpage/HA UI hang jab dusra device add karo ya
            # koi bhi device apni taraf se TCP band kare): pehle isko
            # timeout jaisa hi "harmless" treat kiya jaata tha (bas None
            # return, self._connected ko chhua tak nahi jaata tha).
            # readuntil() EOF ke baad HAMESHA turant (zero delay, bina
            # kisi wait ke) IncompleteReadError deta hai - isliye
            # _listen_loop ka `while self._connected:` loop is None ko
            # dekh kar seedha agli read try karta, jo phir turant EOF
            # deti, aur yeh ek zero-delay tight loop ban jaata jo poore
            # HA event loop ko CPU-spin karke block kar deta (sirf is
            # device ka nahi - HA ka poora webpage/UI atak jaata, jab
            # tak restart na karo). Ab EOF ko real disconnect maante hain
            # (ConnectionError raise) taaki caller turant disconnected
            # state mein jaaye aur normal 30s reconnect-cycle trigger ho -
            # koi tight loop ab possible nahi.
            raise ConnectionError(
                f"Raylogic {self.ip}: peer ne connection band kar diya (EOF)"
            ) from exc
        except Exception as exc:
            _LOGGER.error("Read error from Raylogic %s: %s", self.ip, exc)
            self._connected = False
            self._schedule_reconnect()
            return None

    async def _read_frame(self) -> str:
        """P2 (v1.6.4): agli non-empty line - "\r", "\n" ya "\r\n", teeno
        chalte hain. Pehle sirf readuntil(b"\r") tha: "\n"-only firmware
        ka ek bhi frame parse nahi hota tha (simulator me confirm - har
        ~80s reconnect, device bekaar). Buffer instance par rehta hai, isliye
        _read_line ka timeout beech me cancel kare to bhi data nahi khota.
        EOF -> IncompleteReadError (purana EOF handling waisa hi), bina
        terminator ke 64 KiB -> ValueError (purana "Read error" -> reconnect)."""
        while True:
            m = _LINE_END_RE.search(self._rx_buf)
            if m:
                raw = self._rx_buf[:m.start()]
                self._rx_buf = self._rx_buf[m.end():]
                if len(raw) > _RX_LINE_LIMIT:
                    raise ValueError("Separator is found, but chunk is longer than limit")
                text = raw.decode(errors="replace").strip()
                if text:
                    return text
                continue    # "\r\n" ke beech ki khaali line
            if len(self._rx_buf) > _RX_LINE_LIMIT:
                self._rx_buf = b""
                raise ValueError("Line longer than limit without terminator")
            chunk = await self._reader.read(4096)
            if not chunk:
                raise asyncio.IncompleteReadError(self._rx_buf, None)
            self._rx_buf += chunk

    def _log_once(self, key, level: int, msg: str, *args) -> None:
        """P3/P4/P5: same cheez har frame par log na ho - ek baar."""
        if key in self._logged_once:
            return
        self._logged_once.add(key)
        _LOGGER.log(level, msg, *args)

    async def _drain_initial_push(self):
        """Naye connection banate hi device jo bhi initial burst bhejta hai
        (App connect karte waqt bhi yahi hota hoga, isiliye reopen karne par
        App ko sahi status milta hai) - pehle hum sirf PEHLI line padh kar
        baaki discard kar dete the. Ab thodi der (2.5s) tak jitni bhi lines
        aayein, sabko _dispatch_line se process karte hain - agar isme
        per-channel *AR= state bhi ho, wo ab channel_states mein reflect
        hogi (state_callback bhi fire hoga, taaki HA entities turant update
        ho jayein)."""
        # SCALE FIX (v1.5.2): pehle har read ka timeout poora bacha hua
        # window (2.5s tak) hota tha - matlab burst khatam hone ke baad
        # bhi har device apne connect par lagbhag 2.5 second baithta tha.
        # 100+ device wale setup me (jahan connects semaphore se batch me
        # hote hain) ye seedha HA startup me minute-scale ka delay ban
        # jaata hai. Ab burst khatam hone ka pata ek CHHOTE quiet-gap se
        # chalta hai (INITIAL_QUIET), overall cap wahi 2.5s hai - milne
        # wala data bilkul same rehta hai, bas bekaar ka intezaar nahi.
        end_time = asyncio.get_event_loop().time() + INITIAL_DRAIN_MAX
        first = True
        while asyncio.get_event_loop().time() < end_time:
            remaining = max(0.05, end_time - asyncio.get_event_loop().time())
            line = await self._read_line(
                timeout=min(INITIAL_DRAIN_QUIET, remaining)
            )
            if not line:
                break
            if first and "*KA=" in line:
                self._handle_ka_line(line)
            else:
                self._dispatch_line(line)
            first = False

    def _handle_ka_line(self, line: str):
        """*KA= line device/hub KHUD apni identity broadcast karne ke liye
        bhejta hai (e.g. "109,*KA=31-...") - ye HAMARA sender-id NAHI hai,
        sirf reference/logging ke liye store karte hain. Outgoing commands
        CLIENT_SENDER_ID (confirmed "099") use karte hain."""
        try:
            candidate = line.split(",")[0].strip()
            if candidate.isdigit():
                self.node_id = candidate
        except Exception:
            pass
        self._parse_ka_identity(line)
        # v1.5.3: device ke keepalive ka TURANT jawab do - warna wo 2 KA
        # baad (~12s) connection kaat deta hai. Fire-and-forget, kyunki
        # _handle_ka_line sync context se call hota hai.
        if self._connected and not self._shutdown:
            task = asyncio.create_task(self._answer_device_keepalive())
            self._verify_tasks.add(task)
            task.add_done_callback(self._verify_tasks.discard)

    def _parse_ka_identity(self, line: str) -> None:
        """*KA= payload mein device apna AREA aur apni CHANNEL RANGE khud
        batata hai. Decode (10 real devices ke logs par verify kiya gaya):

            *KA=<xx>-<ctr:3><n:1><AREA:2h><01><0><START:2h><END:2h>0000

        Examples:
            *KA=21-05421001001020000 -> area 0x10=16, ch 0x01-0x02 (1-2)
            *KA=11-04710C0100D100000 -> area 0x0C=12, ch 0x0D-0x10 (13-16)
            *KA=11-04820801017180000 -> area 0x08= 8, ch 0x17-0x18 (23-24)

        Ise hum config CHANGE karne ke liye use NAHI karte (jo devices
        abhi theek chal rahe hain unhe chhedna nahi hai) - sirf VERIFY
        karke ek saaf warning dete hain. Naya device add karte waqt agar
        Area ya First Channel Number galat daal diya, to ab log mein
        turant, exact sahi value ke saath dikh jaata hai - guess nahi
        karna padta."""
        try:
            idx = line.find("*KA=")
            if idx == -1:
                return
            payload = line[idx + 4:].strip()
            if "-" not in payload:
                return
            body = payload.split("-", 1)[1]
            if len(body) < 17 or body[6:8] != "01":
                return
            area = int(body[4:6], 16)
            start = int(body[9:11], 16)
            end = int(body[11:13], 16)
        except (ValueError, IndexError):
            return

        if not (AREA_MIN <= area <= AREA_MAX):
            return
        if not (1 <= start <= end <= 255):
            return

        self.detected_area = area
        self.detected_channel_start = start
        self.detected_channel_end = end

        if self._config_mismatch_logged:
            return
        if not self._legacy_area or self._legacy_area <= 0:
            return  # LEARN mode - yahan verify karne ko kuch nahi

        problems = []
        if area != self._legacy_area:
            problems.append(
                f"Area: config mein {self._legacy_area} hai, device khud "
                f"{area} bata raha hai"
            )
        if start != self._channel_start:
            problems.append(
                f"First Channel Number: config mein {self._channel_start} "
                f"hai, device khud {start} bata raha hai"
            )
        detected_count = end - start + 1
        if detected_count != self._legacy_channel_count:
            problems.append(
                f"Channel count: config (model {self._model_name}) "
                f"{self._legacy_channel_count} channel maanta hai, device "
                f"{detected_count} ({start}-{end}) bata raha hai - shayad "
                f"Device Model galat chuna gaya hai"
            )
        if problems:
            self._config_mismatch_logged = True
            _LOGGER.warning(
                "Raylogic %s %s: CONFIG MISMATCH - device apne baare mein "
                "kuch aur bata raha hai. %s. HA -> Settings -> Devices -> "
                "is device par 'Configure' kholkar sahi value daal do, "
                "warna commands galat address par jaayenge aur device "
                "unhe chup-chaap ignore kar dega.",
                self._model_name, self.ip, "; ".join(problems),
            )
        else:
            _LOGGER.debug(
                "Raylogic %s: config device ke self-report se match karta "
                "hai (area=%d, channels %d-%d).",
                self.ip, area, start, end,
            )

    def _setup_legacy_channels(self):
        """Agar user ne config_flow mein manually Area diya hai (0 ka matlab
        'auto/learn', sirf Relay ke liye), turant channels bana do - har
        channel ka type wahi jo config mein select kiya gaya hai (relay/
        dimmer/fan/curtain). Warna (LEARN mode) kuch bhi nahi banata jab tak
        real *AR= frame na aa jaye (app/switch se ek baar toggle karna hoga)
        - LEARN sirf Relay channels ke liye kaam karta hai.

        NOTE: ye function periodic resync (_soft_reconnect) ke baad bhi
        chalta hai - isliye agar channel PEHLE se maujood hai (purani
        session se), uski on/brightness/percentage state ko as-is rehne do,
        sirf area/type refresh karo. Warna har resync par HA mein light/
        switch galti se OFF flicker karti (jabki device asal mein badla
        nahi tha - naya connect() ke baad turant _drain_initial_push jo
        fresh *AR= bheje wahi asli update dega)."""
        if self._legacy_area and self._legacy_area > 0:
            area = max(AREA_MIN, min(AREA_MAX, self._legacy_area))
            start = self._channel_start
            for ch_num in range(start, start + self._legacy_channel_count):
                # NOTE: CTC aur Curtain dono PAIRED modes hain - jab pair
                # ka koi ek channel in mein se ek banta hai, doosre
                # physical channel ke liye is dict mein jaan-boojh kar
                # koi key nahi hoti (__init__.py ka _resolve_channel_types
                # dekho), taaki uske liye alag/duplicate entity na ban
                # jaaye.
                if ch_num not in self._channel_types:
                    continue
                ch_type = self._channel_types.get(ch_num, CH_TYPE_RELAY)
                existing = self.channel_states.get(ch_num, {})
                self.channel_states[ch_num] = {
                    "area": area,
                    "type": ch_type,
                    "on": existing.get("on", False),
                    "brightness": existing.get("brightness", 0),
                    "percentage": existing.get("percentage", 0),
                    "ctc_mode": self._channel_ctc_modes.get(ch_num, CTC_MODE_SINGLE),
                    "color_temp_kelvin": existing.get("color_temp_kelvin", CTC_DEFAULT_KELVIN),
                    "learned": False,
                }
            _LOGGER.info(
                "Raylogic %s: manual mode - area=%d, channels %d-%d "
                "ready (types: %s).", self.ip, area, start,
                start + self._legacy_channel_count - 1,
                {k: v.get("type") for k, v in self.channel_states.items()},
            )
        else:
            # LEARN mode: pichhle session mein seekhe hue Relay channels
            # turant restore kar do (disk se), naye channels *AR= frame se
            # aayenge. Sirf relay type yahan chalta hai.
            for ch_num, area in self._learned.items():
                existing = self.channel_states.get(ch_num, {})
                self.channel_states[ch_num] = {
                    "area": area,
                    "type": CH_TYPE_RELAY,
                    "on": existing.get("on", False),
                    "learned": True,
                }
            _LOGGER.info(
                "Raylogic %s: LEARN mode - %d channel(s) restored from "
                "previous session. Naye channel ke liye Raylogic GO app ya "
                "physical switch se ek baar us channel ko ON/OFF karo - HA "
                "khud detect karke entity bana dega.",
                self.ip, len(self._learned),
            )

    # ------------------------------------------------------------------ #
    # Learned-channel persistence
    # ------------------------------------------------------------------ #
    def _load_learned(self) -> dict[int, int]:
        # P6: nayi jagah par file nahi, lekin purani (integration folder)
        # me hai -> ek baar copy kar do. Purani file chhod dete hain (HACS
        # update use waise bhi hata dega), delete nahi karte.
        source = self._state_file
        if not source.exists() and self._legacy_state_file.exists():
            source = self._legacy_state_file
        try:
            data = json.loads(source.read_text())
            learned = {int(k): int(v) for k, v in data.items()}
        except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
            return {}
        if source is not self._state_file:
            _LOGGER.info(
                "Raylogic %s: learned channels purani jagah se %s par move "
                "kiye (HACS update se ab nahi mitenge).", self.ip, self._state_file,
            )
            self._write_learned_file({str(k): v for k, v in learned.items()})
        return learned

    def _write_learned_file(self, snapshot: dict) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(snapshot))
        except OSError as err:
            _LOGGER.warning("Raylogic %s: learned-state save fail: %s", self.ip, err)

    def _save_learned(self) -> None:
        # BUG FIX: pehle yahan seedha synchronous write_text() hota tha,
        # jo _handle_ar() (listen_loop task) se, matlab HA ke event loop
        # thread se hi call hota tha - slow disk par poora HA thodi der ke
        # liye freeze ho sakta tha. Ab background thread mein likha jaata
        # hai (fire-and-forget), event loop kabhi block nahi hota.
        snapshot = {str(k): v for k, v in self._learned.items()}
        asyncio.create_task(asyncio.to_thread(self._write_learned_file, snapshot))

    def _learn_channel(self, ch_num: int, area: int) -> bool:
        """Naya channel record karo. True return karta hai agar ye pehli
        baar dekha gaya channel tha (matlab entity create karni chahiye)."""
        is_new = ch_num not in self.channel_states
        if is_new or self.channel_states[ch_num].get("area") != area:
            self.channel_states[ch_num] = {
                "area": area, "type": CH_TYPE_RELAY, "on": False, "learned": True,
            }
            self._learned[ch_num] = area
            self._save_learned()
            _LOGGER.info(
                "Raylogic %s: LEARNED new channel %d in area %d "
                "(from a real *AR= frame).", self.ip, ch_num, area,
            )
        return is_new

    # ------------------------------------------------------------------ #
    # Control - Relay (CONFIRMED format, works in both auto & legacy mode)
    # ------------------------------------------------------------------ #
    async def set_relay(self, ch_num: int, on: bool):
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic %s: channel %d ka Area abhi maloom nahi hai "
                "(LEARN mode mein ho aur ye channel abhi tak seekha nahi gaya) "
                "- command bheja nahi ja sakta. Pehle Raylogic GO app ya "
                "physical switch se is channel ko ek baar ON/OFF karo.",
                self.ip, ch_num,
            )
            return
        level = RELAY_LEVEL_ON if on else RELAY_LEVEL_OFF
        cmd_hex = f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}{level}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(ch_num, {}).update({"on": on})
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control - Dimmer (CONFIRMED format, Model_Number_Mod2u.txt capture)
    #   Frame: 00 1A <area> <level> <channel>
    #   level: 0x01 = full brightness, 0xFF = off, in-between = dim curve.
    #   Linear approximation: brightness (0-255) -> level.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _snap_to_off(brightness: Optional[int]) -> int:
        """User-requested behaviour: jab HA slider ekdum neeche (1%) pe ho,
        to light ko fully OFF kar do, "on but barely lit" mat rehne do.
        HA ka brightness scale 0-255 hai, 1% ~ 2-3 ke barabar hai - is
        poore near-zero range ko 0 (= OFF) treat karte hain. 0 return
        karta hai agar effectively off honi chahiye, warna original
        brightness."""
        if not brightness:
            return 0
        if round(brightness * 100 / 255) <= 1:
            return 0
        return brightness

    async def set_dimmer(self, ch_num: int, brightness: Optional[int]):
        """brightness: 0-255 (HA scale), ya None/0 = off."""
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic %s: channel %d ka Area maloom nahi hai - "
                "dimmer command bheja nahi ja sakta.", self.ip, ch_num,
            )
            return
        brightness = self._snap_to_off(brightness)
        if not brightness:
            level = DIMMER_LEVEL_OFF
        else:
            brightness = max(1, min(255, brightness))
            # 255 (full) -> level 0x01, dim karte hue level badhta jaata hai.
            # 254 tak hi jaane do - 255 (0xFF) sirf OFF ke liye reserved hai,
            # warna sabse dim "on" brightness galti se OFF command ban jaata.
            level = max(DIMMER_LEVEL_ON, min(254, 256 - brightness))
        cmd_hex = f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}{level:02X}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(ch_num, {}).update(
            {"on": bool(brightness), "brightness": brightness or 0}
        )
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control - CTC (Colour Temperature Control / tunable white)
    # See const.py ke CTC comment block ke liye dono sub-modes (single/
    # double driver) ki poori wire-format explanation.
    # ------------------------------------------------------------------ #
    def _pair_bounds(self, ch_num: int) -> tuple[int, int]:
        """Ye channel (ch_num) kis PAIR mein aata hai (MOD4U ke 2 pairs:
        channel_start/+1, aur channel_start+2/+3) - us pair ke (lo, hi)
        physical channel numbers return karta hai.

        BUG FIX (MOD2U -> MOD4U generalization): pehle CTC hamesha
        (channel_start, channel_start+1) HARDCODED maanta tha - MOD2U mein
        theek tha kyunki wahan ek hi pair hota tha, lekin MOD4U mein 2nd
        pair (channel_start+2/+3) ka CTC isi wajah se TOOT jaata (1st
        pair ke channels use ho jaate, jabki asli hardware kuch aur hota).
        Ab pair configured channel ki apni position se derive hoti hai,
        kisi bhi pair ke liye sahi kaam karta hai."""
        offset = ch_num - self._channel_start
        pair_index = offset // CHANNELS_PER_PAIR
        lo = self._channel_start + pair_index * CHANNELS_PER_PAIR
        return lo, lo + 1

    def _ctc_single_wire_channels(self, ch_num: int) -> tuple[int, int]:
        """Single-driver CTC ek physical channel-PAIR use karta hai (jaise
        channel 3 + channel 4), fixed 0x01/0x02 sub-signal id nahi (jaisa
        pehle - sirf 1-channel capture dekh kar - galti se assume kiya gaya
        tha). User-confirmed rule: pair ke DO physical channels mein se
        chhote (lower) number wala colour-temperature hai, bade (higher)
        number wala brightness hai.

        `ch_num` yahan is CTC entity ka apna (primary/configured) physical
        channel number hai - iski PAIR (na ki hamesha device ka pehla
        pair) is se derive hoti hai, taaki MOD4U par CTC Pair 1
        (channel_start/+1) aur CTC Pair 2 (channel_start+2/+3) dono
        independently, sahi apne-apne physical channels ke saath kaam
        karein.

        Returns: (ct_channel, brightness_channel)."""
        lo, hi = self._pair_bounds(ch_num)
        return lo, hi

    async def set_ctc(
        self, ch_num: int,
        brightness: Optional[int] = None,
        color_temp_kelvin: Optional[int] = None,
    ):
        """brightness: 0-255 ya None (unchanged). color_temp_kelvin: None
        (unchanged) ya Kelvin value HA slider se. Jo bhi param None nahi
        hai wahi actually badla jaata hai - single-driver mode mein ye
        do independent *AR= frames hain (bilkul jaisa capture mein tha),
        double-driver mode mein dono hamesha ek hi combined *AZ= frame
        mein jaate hain (kyunki us format mein warm/cool byte dono ek
        saath encode hote hain - alag nahi bheje ja sakte)."""
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic %s: channel %d (CTC) ka Area maloom nahi "
                "hai - command bheja nahi ja sakta.", self.ip, ch_num,
            )
            return
        mode = state.get("ctc_mode", CTC_MODE_SINGLE)
        if brightness is not None:
            brightness = self._snap_to_off(brightness)

        if mode == CTC_MODE_DOUBLE:
            eff_brightness = (
                brightness if brightness is not None
                else state.get("brightness", 255)
            )
            eff_kelvin = (
                color_temp_kelvin if color_temp_kelvin is not None
                else state.get("color_temp_kelvin", CTC_DEFAULT_KELVIN)
            )
            await self._send_ctc_double(ch_num, area, eff_brightness, eff_kelvin)
        else:
            ct_channel, brightness_channel = self._ctc_single_wire_channels(ch_num)
            eff_brightness = state.get("brightness", 255)
            eff_kelvin = state.get("color_temp_kelvin", CTC_DEFAULT_KELVIN)
            if brightness is not None:
                eff_brightness = brightness
                level = (
                    CTC_SINGLE_BRIGHTNESS_OFF if not brightness
                    else max(CTC_SINGLE_BRIGHTNESS_ON, min(254, 256 - brightness))
                )
                cmd_hex = (
                    f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}"
                    f"{level:02X}{brightness_channel:02X}"
                )
                await self._send_addressed(f"*AR={cmd_hex}")
            if color_temp_kelvin is not None:
                eff_kelvin = color_temp_kelvin
                level = self._kelvin_to_single_ct_level(color_temp_kelvin)
                cmd_hex = (
                    f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}"
                    f"{level:02X}{ct_channel:02X}"
                )
                await self._send_addressed(f"*AR={cmd_hex}")

        self.channel_states.setdefault(ch_num, {}).update({
            "on": bool(eff_brightness),
            "brightness": eff_brightness or 0,
            "color_temp_kelvin": eff_kelvin,
        })
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    async def _send_ctc_double(
        self, ch_num: int, area: int, brightness: Optional[int], kelvin: int,
    ):
        """Double-driver frame - warm/cool cross-fade formula, derived to
        match both captured sweeps exactly (see const.py comment).

        Wire: <area><ch_lo><level_lo><ch_hi><level_hi><const><pct>

        BUG FIX (CCT/CTC kisi bhi doosre channel par kaam nahi karta tha):
        pehle yahan channel bytes HARDCODED "01" aur "02" the. Wo galti
        isliye pakdi nahi gayi kyunki jis MOD2U se capture liya gaya tha
        uske CTC pair ke physical channels hi 1 aur 2 the. Area 12 wale
        MOD4U par CTC pair channels 15-16 hai, to frame mein 0F/10 jaana
        chahiye - 01/02 bhejne par device frame drop kar deta tha, isliye
        CCT light bilkul respond nahi karti thi. Ab dono channel bytes is
        CTC channel ki apni pair se derive hote hain.

        Wiring (user ne real hardware par confirm kiya): pair ka CHHOTA
        physical channel = WHITE/cool driver, BADA channel = YELLOW/warm
        driver - isliye level_lo mein cool aur level_hi mein warm jaata
        hai."""
        cool_channel, warm_channel = self._pair_bounds(ch_num)
        kelvin = max(CTC_MIN_KELVIN, min(CTC_MAX_KELVIN, kelvin))
        warm_frac = 1 - (kelvin - CTC_MIN_KELVIN) / (CTC_MAX_KELVIN - CTC_MIN_KELVIN)
        brightness = max(0, min(255, brightness or 0))
        bright_frac = brightness / 255
        warm_on = warm_frac * bright_frac
        cool_on = (1 - warm_frac) * bright_frac
        warm_level = 0xFF if warm_on <= 0 else max(1, min(255, round(256 - warm_on * 255)))
        cool_level = 0xFF if cool_on <= 0 else max(1, min(255, round(256 - cool_on * 255)))
        pct = round(bright_frac * 100)
        # lower physical channel = white/cool, higher = yellow/warm
        cmd_hex = (
            f"{area:02X}{cool_channel:02X}{cool_level:02X}"
            f"{warm_channel:02X}{warm_level:02X}"
            f"{CTC_DOUBLE_CONST_BYTE:02X}{pct:02X}"
        )
        _LOGGER.debug(
            "Raylogic %s: CTC(double) ch%d -> cool ch%d=0x%02X, warm "
            "ch%d=0x%02X, pct=%d (area %d) - *AZ=%s",
            self.ip, ch_num, cool_channel, cool_level, warm_channel,
            warm_level, pct, area, cmd_hex,
        )
        await self._send_addressed(f"*AZ={cmd_hex}")

    def _kelvin_to_single_ct_level(self, kelvin: int) -> int:
        kelvin = max(CTC_MIN_KELVIN, min(CTC_MAX_KELVIN, kelvin))
        frac = (kelvin - CTC_MIN_KELVIN) / (CTC_MAX_KELVIN - CTC_MIN_KELVIN)
        level = round(CTC_SINGLE_CT_MIN_LEVEL + frac * (CTC_SINGLE_CT_MAX_LEVEL - CTC_SINGLE_CT_MIN_LEVEL))
        return max(CTC_SINGLE_CT_MIN_LEVEL, min(CTC_SINGLE_CT_MAX_LEVEL, level))

    def _single_ct_level_to_kelvin(self, level: int) -> int:
        level = max(CTC_SINGLE_CT_MIN_LEVEL, min(CTC_SINGLE_CT_MAX_LEVEL, level))
        frac = (level - CTC_SINGLE_CT_MIN_LEVEL) / (CTC_SINGLE_CT_MAX_LEVEL - CTC_SINGLE_CT_MIN_LEVEL)
        return round(CTC_MIN_KELVIN + frac * (CTC_MAX_KELVIN - CTC_MIN_KELVIN))

    @staticmethod
    def _double_levels_to_kelvin(cool_level: int, warm_level: int) -> Optional[int]:
        """*AZ= ke cool + warm level bytes se colour temperature (v1.6.5).

        Encoder (_send_ctc_double) har channel ko colour AUR brightness dono
        se scale karta hai: output = share x brightness, byte = 256 - output
        x 255, 0xFF = off. Pehle yahan sirf WARM byte se kelvin nikalta tha -
        brightness 100% se kam hote hi galat (2700K @50% -> 4593K), aur agla
        brightness-only command wahi galat kelvin bhej kar light ka rang
        badal deta tha (simulator se confirm). Ab dono channel ka output
        nikaal kar unka RATIO lete hain - brightness cancel ho jaati hai.
        0xFF = exactly 0 output (pehle full cool 6485K padhta tha, ab 6500K).
        Dono off, ya itna dim ki colour decode hi na ho -> None: caller
        pichhla kelvin rakhe."""
        def out(level: int) -> float:
            return 0.0 if level >= 0xFF else (256 - max(1, level)) / 255
        cool, warm = out(cool_level), out(warm_level)
        # Bahut kam brightness par byte me colour ki jaankari hi nahi bachti
        # (1% par ek channel round hokar "off" ho jaata hai) - wahan andaaza
        # store karne se agla command rang badal deta. 10% se kam total
        # output par None: caller pichhla (sahi) kelvin rakhta hai.
        if cool + warm < _AZ_MIN_OUTPUT_FOR_KELVIN:
            return None
        warm_frac = warm / (cool + warm)
        return round(CTC_MAX_KELVIN - warm_frac * (CTC_MAX_KELVIN - CTC_MIN_KELVIN))

    def _find_ctc_channel(
        self, area: int, mode: str, wire_channel: Optional[int] = None
    ) -> Optional[int]:
        """Configured CTC channel (agar koi ho) jo is Area aur is
        sub-mode (single/double) se match karta hai - CTC ke liye
        incoming frames ko normal ch_num-based lookup se PEHLE match
        karna padta hai, kyunki wire 'channel' byte yahan sub-signal
        (brightness/colour) hota hai, physical channel nahi.

        BUG FIX (MOD2U -> MOD4U generalization): MOD2U mein sirf ek hi
        CTC pair possible tha, isliye area+mode match hi kaafi tha. MOD4U
        mein 2 pairs ho sakte hain - agar dono SAME Area mein CTC (single
        mode) ho, purana code hamesha PEHLA match return karta, jisse
        dono pairs ka data mix ho jaata (Pair 2 ka incoming frame galti
        se Pair 1 ki entity update kar deta, ya vice versa). Ab agar
        `wire_channel` diya gaya ho (single mode ke liye hamesha milta
        hai, kyunki us frame mein real physical channel number hota hai),
        us wire_channel ki apni PAIR match karne wale channel ko hi
        return karte hain - dono pairs cleanly disambiguate ho jaate
        hain, chahe Area same ho."""
        candidates = [
            (cn, st) for cn, st in self.channel_states.items()
            if (
                st.get("type") == CH_TYPE_CTC
                and st.get("ctc_mode", CTC_MODE_SINGLE) == mode
                and st.get("area") == area
            )
        ]
        if not candidates:
            return None
        if wire_channel is None:
            return candidates[0][0]
        # Multiple CTC candidates (dono pairs same Area mein) - wire_channel
        # ki pair se match karne wale ko hi pick karo.
        target_lo, target_hi = self._pair_bounds(wire_channel)
        for cn, _st in candidates:
            cn_lo, cn_hi = self._pair_bounds(cn)
            if (cn_lo, cn_hi) == (target_lo, target_hi):
                return cn
        # Fallback: agar sirf EK hi candidate hai to usi ko maan lo, chahe
        # wire_channel uski pair se match na kare. Ye us purane firmware/
        # frame-variant ke liye safety net hai jo channel byte ki jagah
        # fixed 01/02 marker bhejta ho - us case mein bhi state-sync
        # kaam karta rahega.
        if len(candidates) == 1:
            return candidates[0][0]
        return None

    def _apply_ctc_single_update(self, ch_num: int, wire_channel: int, level: int):
        st = self.channel_states.setdefault(ch_num, {})
        _ct_channel, brightness_channel = self._ctc_single_wire_channels(ch_num)
        if wire_channel == brightness_channel:
            if level == CTC_SINGLE_BRIGHTNESS_OFF:
                st.update({"on": False, "brightness": 0})
            else:
                st.update({"on": True, "brightness": max(1, min(255, 256 - level))})
        else:  # ct_channel
            st["color_temp_kelvin"] = self._single_ct_level_to_kelvin(level)
        if self.state_callback:
            self.state_callback(self.ip, ch_num, st)

    def _handle_az(self, line: str):
        """*AZ= = double-driver CTC frame (cool+warm combined). Format:
        <area><ch_lo><cool_level><ch_hi><warm_level><64><pct> (7 bytes,
        see const.py) - pair ka chhota physical channel = white/cool,
        bada = yellow/warm (same wiring jo _send_ctc_double bhejta hai).

        BUG FIX: pehle ye parser `b[1] != 0x01 or b[3] != 0x02` par hi
        return kar deta tha, matlab channel 1-2 ke alawa kisi bhi CTC
        pair (jaise 15-16) ka incoming frame chup-chaap discard ho jaata
        tha - HA mein CCT light ki state kabhi sync hi nahi hoti thi. Ab
        koi bhi consecutive channel-pair accept hota hai."""
        try:
            idx = line.find("*AZ=")
            if idx == -1:
                return
            hex_part = "".join(line[idx + 4:].split())[:14]    # P3
            if len(hex_part) < 14:
                self._log_once(
                    ("short", "*AZ="), logging.INFO,
                    "Raylogic %s: chhota *AZ= frame ignore kiya: %s", self.ip, line[:120],
                )
                return
            b = bytes.fromhex(hex_part)
            if len(b) < 7:
                return
            # ch_lo/ch_hi hamesha ek consecutive physical pair hote hain.
            if b[3] != b[1] + 1:
                return
            area = b[0]
            cool_level, warm_level = b[2], b[4]
            pct = b[6]
            ch_num = self._find_ctc_channel(
                area, CTC_MODE_DOUBLE, wire_channel=b[1],
            )
            if ch_num is None:
                return
            brightness = max(0, min(255, round(pct * 255 / 100)))
            st = self.channel_states.setdefault(ch_num, {})
            st.update({"on": brightness > 0, "brightness": brightness})
            kelvin = self._double_levels_to_kelvin(cool_level, warm_level)
            if kelvin is not None:      # off frame: pichhla colour yaad rakho
                st["color_temp_kelvin"] = kelvin
            if self.state_callback:
                self.state_callback(self.ip, ch_num, st)
        except Exception as exc:
            _LOGGER.debug("Raylogic AZ parse error '%s': %s", line, exc)
            self._log_once(
                ("bad", "*AZ="), logging.INFO,
                "Raylogic %s: *AZ= frame samajh nahi aaya, ignore kiya: %s (%s)",
                self.ip, line[:120], exc,
            )

    # ------------------------------------------------------------------ #
    # Control - Fan (CONFIRMED format, Model_Number_Mod2u.txt capture)
    #   Frame: 00 1A <area> <level> <channel>
    #   level: 0x01=off, 0x02=speed1(25%), 0x03=speed2(50%),
    #          0x04=speed3(75%), 0x05=speed4/full(100%)
    # ------------------------------------------------------------------ #
    async def set_fan(self, ch_num: int, percentage: int):
        """percentage: 0, 25, 50, 75, 100 (HA fan speed steps)."""
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic %s: channel %d ka Area maloom nahi hai - "
                "fan command bheja nahi ja sakta.", self.ip, ch_num,
            )
            return
        # Nearest confirmed step le lo (0/25/50/75/100)
        step = min(FAN_SPEEDS.keys(), key=lambda k: abs(k - percentage))
        level = FAN_SPEEDS[step]
        cmd_hex = f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}{level:02X}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(ch_num, {}).update(
            {"on": step > 0, "percentage": step}
        )
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control - Curtain
    #
    # BUG FIX (curtain kisi bhi doosre Area/channel par kaam nahi karta
    # tha): pehle yahan 6 HARDCODED literal command strings the, jo ek hi
    # device (Area 7, channels 3-6) ke capture se aaye the aur har device
    # par jaise-ke-taise bhej diye jaate the. Curtain frame ka teesra byte
    # ek GLOBAL curtain slot hai - Area 08 ke channels 23-24 ke liye 0x0C
    # chahiye tha, lekin hamesha 0x02 hi ja raha tha, isliye device frame
    # ko chup-chaap ignore kar deta tha (koi error bhi nahi milta tha, bas
    # "kuch hota hi nahi" wala symptom). Ab poora frame channel se derive
    # hota hai - dekho curtain_slot_for_channel() / curtain_frame() aur
    # const.py ka Curtain block.
    # ------------------------------------------------------------------ #
    async def set_cover(self, ch_num: int, action: str):
        """action: 'open' | 'close' | 'stop'."""
        pair_lo, _pair_hi = self._pair_bounds(ch_num)
        pair_index = (pair_lo - self._channel_start) // CHANNELS_PER_PAIR
        slot = self.curtain_slot_for_channel(ch_num)
        cmd_hex = self.curtain_frame(ch_num, action)
        if not cmd_hex:
            _LOGGER.warning(
                "Raylogic %s %s: channel %d ke liye unknown curtain action "
                "'%s' - kuch bheja nahi gaya.",
                self._model_name, self.ip, ch_num, action,
            )
            return
        _LOGGER.info(
            "Raylogic %s %s: curtain '%s' -> channel %d (device Pair %d, "
            "physical ch %d-%d, global curtain slot %d/0x%02X, "
            "connected=%s) - sending *AR=%s",
            self._model_name, self.ip, action, ch_num, pair_index + 1,
            pair_lo, pair_lo + 1, slot, slot, self._connected, cmd_hex,
        )
        await self._send_addressed(f"*AR={cmd_hex}")
        if action != "stop":
            self.channel_states.setdefault(ch_num, {}).update(
                {"on": action == "open", "moving": True}
            )
            if self.state_callback:
                self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Area scenes (v1.6.0)
    # ------------------------------------------------------------------ #
    async def recall_scene(self, area: int, scene: int):
        """Raylogic GO app ka area-scene recall karo: *AR=000F<area><scene>00.

        Bus broadcast hai - us Area ke saare fixtures react karte hain, sirf
        is module ke channels nahi. _send_addressed() isse CLIENT_SENDER_ID
        (099, app/keypad node) ke under bhejta hai aur baaki commands ki
        tarah hi TCP-ACK verify + disconnect par queue/replay milta hai
        (recall idempotent hai, dobara bhejna safe)."""
        if not (1 <= area <= SCENE_AREAS and 1 <= scene <= SCENE_MAX):
            _LOGGER.warning(
                "Raylogic %s %s: invalid scene recall area=%s scene=%s - "
                "kuch bheja nahi gaya.", self._model_name, self.ip, area, scene,
            )
            return
        cmd_hex = f"00{SCENE_FUNC:02X}{area:02X}{scene:02X}00"
        _LOGGER.debug(
            "Raylogic %s: recall_scene area=%d scene=%d -> *AR=%s",
            self.ip, area, scene, cmd_hex,
        )
        await self._send_addressed(f"*AR={cmd_hex}")
        self.active_scene[area] = scene

    # ------------------------------------------------------------------ #
    # Background loops
    # ------------------------------------------------------------------ #
    async def _listen_loop(self, gen: int):
        while self._connected and gen == self._conn_generation:
            try:
                line = await self._read_line(timeout=float(LISTEN_READ_TIMEOUT))
            except ConnectionError as exc:
                # Peer ne connection band kar di (EOF) - real disconnect,
                # tight-loop nahi. Properly mark disconnected + normal
                # reconnect-cycle trigger karo, aur loop se nikal jao.
                if gen != self._conn_generation:
                    return  # ye ek purani session ka task hai, chup-chaap jao
                session = self._now() - self._session_started
                self._record_session_result(session)
                if session >= FAST_RECONNECT_MIN_SESSION:
                    # Device ka normal/expected auto-disconnect - ise error
                    # ki tarah shor machane ki zaroorat nahi, bas turant
                    # wapas jud jao (dekho const.py FAST RECONNECT).
                    _LOGGER.debug(
                        "Raylogic %s %s: device ne %.0fs baad connection band "
                        "ki (uska normal behaviour) - turant reconnect.",
                        self._model_name, self.ip, session,
                    )
                else:
                    _LOGGER.warning(
                        "Raylogic %s %s: %s (session sirf %.0fs chali) - "
                        "disconnected mark kiya, reconnect trigger ho raha hai.",
                        self._model_name, self.ip, exc, session,
                    )
                self._connected = False
                self._mark_unavailable_soon()
                self._schedule_reconnect()
                return
            if gen != self._conn_generation:
                return
            if line:
                self._dispatch_line(line)
                continue

            # Read timeout - is window me device se kuch nahi aaya.
            # STABILITY FIX (v1.5.0): pehle yahan kuch hota hi nahi tha,
            # loop bas dobara read par baith jaata tha. Ek "half-dead"
            # socket (jahan na EOF aata hai na data) ka pata tabhi chalta
            # tha jab agla WRITE fail hota - matlab dead connection
            # minute-scale tak "connected" dikhti reh sakti thi.
            # Ab detection PASSIVE hai: device khud har 6-12s me apna
            # *KA= bhejta hai, to itni der ki khamoshi = connection mar
            # chuki hai. Ye faster bhi hai aur module ko chhedta bhi nahi
            # (koi extra write nahi).
            idle = self._now() - self._last_rx
            if idle >= RX_SILENCE_TIMEOUT:
                _LOGGER.warning(
                    "Raylogic %s %s: %.0fs se device se koi frame nahi aaya "
                    "(normally har 6-12s me aata hai) - connection dead maan "
                    "kar reconnect kar rahe hain.",
                    self._model_name, self.ip, idle,
                )
                self._last_session_len = self._now() - self._session_started
                self._connected = False
                self._mark_unavailable_soon()
                self._schedule_reconnect()
                return

    async def _keepalive_loop(self, gen: int):
        # SCALE FIX: pehli keepalive bhi thoda random delay ke saath, taaki
        # bahut saare devices (100+) ka keepalive-write EXACTLY sync na ho
        # jaaye (chhota fix hai, KEEPALIVE_INTERVAL chhota hai isliye asar
        # bhi chhota hai, lekin resync jitter jaisa hi principle).
        #
        # BUG FIX: same as _resync_loop() - ye random stagger pehle HAR
        # reconnect ke naye task par fresh draw hota tha, na sirf HA
        # startup par. Ab `self._resync_staggered` jaisa hi ek dedicated,
        # instance-level flag (`self._keepalive_staggered`) use karke
        # sirf is device-instance ki PEHLI keepalive-cycle mein hi random
        # delay diya jaata hai - baad ke sab reconnects ke liye seedha
        # normal KEEPALIVE_INTERVAL wait hota hai.
        #
        # STABILITY FIX (v1.5.0) - keepalive ab PASSIVE hai. Real logs se:
        # device khud har 6-12 second me apna *KA= frame bhejta hai (10
        # minute me 293 RX frames), jabki hum uske upar se 125 apne
        # *KA=01 writes bhi thop rahe the. In sasta ESP8266-class modules
        # ke liye har extra write ek extra risk hai (unka TCP stack hi
        # kamzor hai) - aur wo write humein kuch nayi jaankari deta bhi
        # nahi tha, kyunki liveness ka proof device ke apne frames se
        # already mil raha tha.
        #
        # Ab hum tabhi likhte hain jab device se KUCH bhi na aaya ho
        # KEEPALIVE_INTERVAL tak. Normal halat me humara write count
        # practically 0 ho jaata hai.
        if not self._keepalive_staggered:
            self._keepalive_staggered = True
            await asyncio.sleep(random.uniform(0, KEEPALIVE_INTERVAL))
        while self._connected and gen == self._conn_generation:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not self._connected or gen != self._conn_generation:
                return

            # BUG FIX (test se pakda gaya): variant ko lock karne ka faisla
            # pehle SIRF disconnect hone par hota tha - lekin agar variant
            # sach me kaam kar jaaye to connection tootegi hi nahi, isliye
            # lock kabhi hota hi nahi aur agle reconnect par code kaam
            # karte hue format se hat jaata. Ab chalti hui session ko bhi
            # yahin check kar lete hain.
            if not self._ka_variant_locked:
                live = self._now() - self._session_started
                if live >= KEEPALIVE_GOOD_SESSION:
                    idx = self._ka_variant % len(KEEPALIVE_VARIANTS)
                    self._ka_variant_locked = True
                    _LOGGER.info(
                        "Raylogic %s %s: keepalive format '%s' se connection "
                        "%.0fs se lagataar zinda hai (device ka ~12s wala "
                        "auto-disconnect toot gaya) - ab hamesha yahi format "
                        "use hoga.",
                        self._model_name, self.ip, KEEPALIVE_VARIANTS[idx], live,
                    )

            # v1.5.3: asli keepalive ab device ke apne *KA= ka JAWAB hai
            # (_answer_device_keepalive) - wahi device ko chahiye. Ye loop
            # sirf ek fallback hai: agar kisi wajah se device ne KA na
            # bheja ho aur humne bhi kuch der se kuch na likha ho, tab ek
            # keepalive khud bhej do.
            since_tx = self._now() - self._last_tx if self._last_tx else 1e9
            idle = self._now() - self._last_rx
            if since_tx < KEEPALIVE_IDLE_THRESHOLD and idle < KEEPALIVE_IDLE_THRESHOLD:
                continue
            _LOGGER.debug(
                "Raylogic %s: %.0fs se koi traffic nahi - fallback keepalive "
                "bhej rahe hain.", self.ip, min(idle, since_tx),
            )
            self._last_tx = self._now()
            await self._send_raw(self._keepalive_frame())

    def _dispatch_line(self, line: str):
        _LOGGER.debug("RX %s: %s", self.ip, line)
        if "*KA=" in line:
            self._handle_ka_line(line)
        elif "+AR40=" in line:
            # NAYA MILA (typo/gap fix, is baar sirf VISIBILITY ke liye):
            # device MOD4U se ye ek ALAG, khud-ba-khud (~19-20s) periodic
            # frame hai. Iska byte-layout abhi decode nahi hua hai - sirf
            # RX log mein saaf dikhega taaki 3-4 samples collect karke
            # iska real structure decode kiya ja sake (agla concrete step,
            # agar kabhi zaroorat pade).
            # P5 (v1.6.4): ~20s me ek baar aata hai - har baar INFO log
            # bahut devices par shor tha. Ab har connection-session me ek
            # sample INFO par, baaki DEBUG par.
            self._log_once(
                "ar40", logging.INFO,
                "Raylogic %s: +AR40= status heartbeat mila (abhi tak "
                "decode nahi kiya gaya, is session me dobara log nahi hoga) "
                "- raw: %s", self.ip, line,
            )
            _LOGGER.debug("Raylogic %s: +AR40= raw: %s", self.ip, line)
        elif "*AZ=" in line:
            self._handle_az(line)
        elif "*AR=" in line:
            self._handle_ar(line)
        else:
            # P3: unknown frame type - ignore (pehle jaisa), lekin har type
            # ek baar INFO par dikhe taaki naye firmware ka pata chale.
            m = _FRAME_TYPE_RE.search(line)
            kind = m.group(0) if m else "?"
            self._log_once(
                ("unknown", kind), logging.INFO,
                "Raylogic %s: unknown frame type %s ignore kiya (is type ka "
                "sirf pehla sample log hota hai): %s", self.ip, kind, line[:120],
            )

    def _decode_level(self, ch_type: str, level: int) -> dict:
        """Incoming *AR= level byte ko channel-TYPE ke hisaab se decode karo.
        Pehle ye hamesha Relay ka check (level==0x02) use karta tha - isliye
        Dimmer/Fan channels ka status (aur Dimmer ki brightness) kabhi sahi
        update hi nahi hota tha, chahe device se sahi frame aa raha ho."""
        if ch_type == CH_TYPE_DIMMER:
            if level == DIMMER_LEVEL_OFF:
                return {"on": False, "brightness": 0}
            brightness = max(1, min(255, 256 - level))
            return {"on": True, "brightness": brightness}
        if ch_type == CH_TYPE_FAN:
            if level == FAN_LEVEL_OFF:
                return {"on": False, "percentage": 0}
            step = next(
                (pct for pct, lvl in FAN_SPEEDS.items() if lvl == level), None
            )
            if step is None:
                step = min(FAN_SPEEDS, key=lambda p: abs(FAN_SPEEDS[p] - level))
            return {"on": step > 0, "percentage": step}
        # Relay (default). P4 (v1.6.4): sirf confirmed levels - 01 = OFF,
        # 02 = ON. Pehle 02 ke alawa SAB kuch OFF maana jaata tha, to kisi
        # firmware ka alag ON-level relay ko galti se OFF dikhata. Ab
        # unknown level par state NAHI badalti (ek baar warning).
        if level == int(RELAY_LEVEL_ON, 16):
            return {"on": True}
        if level == int(RELAY_LEVEL_OFF, 16):
            return {"on": False}
        self._log_once(
            ("relay_level", level), logging.WARNING,
            "Raylogic %s: relay ke liye unknown level 0x%02X mila (sirf 01=OFF "
            "/ 02=ON confirmed hain) - state nahi badli. Ye naya firmware ho "
            "sakta hai; is level ka sample issue me bhejo.", self.ip, level,
        )
        return {}

    def _handle_curtain_frame(self, b: bytes):
        """Curtain echo (app ya physical switch se) - frame:
              00 27 <slot> <dir> <run>    (open/close)
              00 26 <slot> 00   00        (stop)

        Do kaam karta hai:
          1. HA ki cover entity ki state sync karta hai (pehle ye frames
             parse hi nahi hote the).
          2. <run> byte (curtain ka travel parameter) SEEKH leta hai -
             agar app kisi aur value se chalati hai to HA bhi aage se
             wahi bhejega, hardcoded default nahi."""
        slot = b[2]
        ch_num = self._find_curtain_channel(slot)
        if ch_num is None:
            _LOGGER.debug(
                "Raylogic %s: curtain frame slot %d (0x%02X) mila, lekin is "
                "device par us slot ka koi configured curtain channel nahi "
                "hai - ignore kiya.", self.ip, slot, slot,
            )
            return
        if b[1] == CURTAIN_CMD_STOP:
            _LOGGER.debug(
                "Raylogic %s: curtain STOP echo (slot %d, channel %d).",
                self.ip, slot, ch_num,
            )
            return
        direction = b[3]
        run = b[4]
        if run and self._curtain_run_bytes.get(slot) != run:
            self._curtain_run_bytes[slot] = run
            _LOGGER.info(
                "Raylogic %s: curtain slot %d ka 'run' byte device se seekh "
                "liya: 0x%02X (ab HA bhi yahi bhejega).",
                self.ip, slot, run,
            )
        if direction not in (CURTAIN_DIR_OPEN, CURTAIN_DIR_CLOSE):
            return
        st = self.channel_states.setdefault(ch_num, {})
        st.update({"on": direction == CURTAIN_DIR_OPEN, "moving": True})
        if self.state_callback:
            self.state_callback(self.ip, ch_num, st)

    def _handle_ar(self, line: str):
        """Mobile app ya kisi aur node se aaya *AR= echo - real-time sync ke
        liye. Format: <ID>,<Seq>,*AR=00 1A <area> <level> <channel>

        NOTE: is line ke wire par pehle "001,086," jaisa prefix bhi ho sakta
        hai (Docklight/kisi aur client ka apna format) - hum bas "*AR=" ke
        baad ka hex nikaalte hain, prefix se koi farak nahi padta.
        """
        try:
            idx = line.find("*AR=")
            if idx == -1:
                return
            # P3 (v1.6.4): "*AR= 001A.." / "00 1A 0C .." bhi chale - pehle
            # fixed 10-char slice space aate hi chup-chaap fail hota tha.
            hex_part = "".join(line[idx + 4:].split())[:10]
            if len(hex_part) < 10:
                self._log_once(
                    ("short", "*AR="), logging.INFO,
                    "Raylogic %s: chhota *AR= frame ignore kiya: %s", self.ip, line[:120],
                )
                return
            b = bytes.fromhex(hex_part)
            if len(b) < 5:
                return

            # v1.6.0: area-scene recall echo (keypad / app / koi aur node):
            #   00 0F <area> <scene> 00  -> select entity ka two-way feedback.
            # Pehle ye frame neeche `b[1] != 0x1A` par chup-chaap discard
            # hota tha. Channel-state par iska koi asar nahi (scene ke baad
            # channels apne khud ke *AR=001A.. frames se update hote hain).
            if b[0] == 0x00 and b[1] == SCENE_FUNC:
                area, scene = b[2], b[3]
                if 1 <= area <= SCENE_AREAS and 1 <= scene <= SCENE_MAX:
                    self.active_scene[area] = scene
                    if self.state_callback:
                        self.state_callback(self.ip, f"scene_{area}", {"scene": scene})
                return

            # NAYA: curtain frames (cmd 0x27 = open/close, 0x26 = stop)
            # pehle yahin `b[1] != 0x1A` check par discard ho jaate the -
            # matlab app/physical-switch se chalayi gayi curtain ka state
            # HA mein kabhi reflect hi nahi hota tha. Curtain frame mein
            # Area byte hota hi nahi, isliye match global curtain slot se
            # hota hai.
            if b[1] in (CURTAIN_CMD_MOVE, CURTAIN_CMD_STOP):
                self._handle_curtain_frame(b)
                return

            if b[1] != 0x1A:
                return
            area = b[2]
            level = b[3]
            ch_num = b[4]

            # CTC (single-driver) special case: iske liye "channel" byte
            # (ch_num, yahan) YE HAI real physical channel number (jaise
            # 3 ya 4) - fixed 0x01/0x02 sub-signal id NAHI (pehli galti,
            # sirf 1-channel capture dekh kar assume kiya gaya tha). Pair
            # ke andar chhota number = colour-temp, bada number =
            # brightness (const.py comment dekho). Isliye ise normal
            # ch_num-based lookup se PEHLE hi handle karna zaroori hai,
            # warna ye galti se kisi doosre normal channel (jiska asli
            # ch_num 1 ya 2 ho) ki state ko corrupt kar sakta tha.
            ctc_ch = self._find_ctc_channel(area, CTC_MODE_SINGLE, wire_channel=ch_num)
            if ctc_ch is not None:
                ct_channel, brightness_channel = self._ctc_single_wire_channels(ctc_ch)
                if ch_num in (ct_channel, brightness_channel):
                    self._apply_ctc_single_update(ctc_ch, ch_num, level)
                    return

            # Manual mode (Area configured, >0): sirf USI area ke frames
            # accept karo, aur sirf pehle-se-configured channels ki state
            # update karo - naye "phantom" channel apne aap mat bana do
            # (isi wajah se pehle ek 2-channel MOD2U par galti se ch3/ch4
            # bhi ban gaye the, kisi doosre device/area ke traffic se).
            if self._legacy_area and self._legacy_area > 0:
                if area != self._legacy_area:
                    return
                if ch_num not in self.channel_states:
                    _LOGGER.debug(
                        "Raylogic %s: area=%d ch=%d ka *AR= frame "
                        "aaya lekin ye channel manual config mein nahi hai "
                        "- ignore kiya (kisi doosre device ka ho sakta hai).",
                        self.ip, area, ch_num,
                    )
                    return
                ch_type = self.channel_states[ch_num].get("type", CH_TYPE_RELAY)
                self.channel_states[ch_num].update(self._decode_level(ch_type, level))
                if self.state_callback:
                    self.state_callback(self.ip, ch_num, self.channel_states[ch_num])
                return

            # LEARN mode (Area=0): naya channel discover hone par entity
            # dynamically bana do - ye purana intended behavior hai. LEARN
            # sirf Relay ke liye chalta hai (naya channel hamesha relay
            # maan kar banaya jaata hai), isliye yahan seedha Relay decode.
            is_new = self._learn_channel(ch_num, area)
            self.channel_states[ch_num].update(self._decode_level(CH_TYPE_RELAY, level))  # P4

            if is_new and self.new_channel_callback:
                self.new_channel_callback(ch_num, self.channel_states[ch_num])

            if self.state_callback:
                self.state_callback(self.ip, ch_num, self.channel_states[ch_num])
        except Exception as exc:
            _LOGGER.debug("Raylogic AR parse error '%s': %s", line, exc)
            self._log_once(
                ("bad", "*AR="), logging.INFO,
                "Raylogic %s: *AR= frame samajh nahi aaya, ignore kiya (is "
                "tarah ka sirf pehla log hota hai): %s (%s)", self.ip, line[:120], exc,
            )
