"""Raylogic MOD2U / MOD4U TCP protocol client - RE8-style architecture.

Ek hi class (RaylogicModDevice) dono models (MOD2U = 2 channel/1 pair,
MOD4U = 4 channel/2 pairs) ko handle karta hai - `model` param (const.py
DEVICE_MODELS) se channel-count aur BR40 code decide hote hain, baaki
poora protocol logic pair-count-agnostic hai.

Do modes mein kaam karta hai:

1. AUTO / BR40 mode (RE8 jaisa):
   Device se `?BR40=` query karke har channel ka Area (ch_index) aur
   (jab byte map fill ho jaye) uska configured Type padhta hai. Isse
   device ko *kisi bhi* Area (1-16) mein add karo, integration khud
   detect kar leta hai - manual config nahi chahiye.
   Ye tab hi chalega jab is model ka br40_code (const.py DEVICE_MODELS)
   discover ho chuka ho.

2. LEGACY / static mode (fallback, jab tak BR40 code na mile):
   DILEEPGO/MOD2U ke original behaviour jaisa - fixed channel count
   (model se: 2 ya 4), area config se ya LEGACY_DEFAULT_AREA (0x0C) se
   liya jata hai. Relay control fully working rehta hai is mode mein bhi
   (kyunki Relay ka command format already confirmed hai) - sirf
   auto-discovery nahi hoti.

Jaise hi kisi model ka br40_code aur CHANNEL_TYPE_BYTE_MAP const.py mein
bhar diye jayenge, integration khud us model ke liye AUTO mode mein
switch ho jayega - is file mein koi aur change nahi karna padega.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .const import (
    CONNECT_TIMEOUT,
    CMD_ADDR_HIGH,
    CMD_CHANNEL_DIRECT,
    RELAY_LEVEL_ON,
    RELAY_LEVEL_OFF,
    DIMMER_LEVEL_ON,
    DIMMER_LEVEL_OFF,
    FAN_LEVEL_OFF,
    FAN_SPEEDS,
    CURTAIN_PAIR_COMMANDS,
    CHANNELS_PER_PAIR,
    DEVICE_MODELS,
    DEFAULT_MODEL,
    CLIENT_SENDER_ID,
    RESYNC_INTERVAL,
    CHANNEL_TYPE_BYTE_MAP,
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
)

_LOGGER = logging.getLogger(__name__)

_STATE_DIR = Path(__file__).parent / "device_state"


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
    ):
        self.ip = ip
        self.port = port
        self.state_callback = state_callback

        # Konsa physical device hai (MOD2U ya MOD4U) - device_info (naam/
        # model text) ke liye, aur BR40 auto-detect code lookup ke liye.
        # Har model ka apna DEVICE_MODELS entry const.py mein hai.
        self._model = model
        _model_info = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])
        self._model_name: str = _model_info["name"]
        self._model_desc: str = _model_info["desc"]
        self._br40_code: Optional[int] = _model_info["br40_code"]

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
        self._state_file = _STATE_DIR / f"{self._state_key}_learned.json"
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
        self._resyncing = False  # soft-reconnect ke dauran duplicate na ho
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
        # BUG FIX (the big one - device power-cycle "stuck forever" bug):
        # jab tak _shutdown True na ho (matlab disconnect() /HA unload
        # explicitly ho), reconnect KABHI permanently give up nahi karega
        # - dekho _reconnect() neeche.
        self._shutdown = False

        # Device identity
        self.node_id: Optional[str] = None       # e.g. "101" - device ka apna ID
        self.mac: Optional[str] = None
        self.fw_version: Optional[str] = None
        self.br40_code: Optional[int] = None
        self.auto_mode: bool = False              # True jab BR40 se channels mil jayein
        # BUG FIX: pehle "BR40 auto-discovery nahi hui" WARNING _do_connect()
        # ke har call par log hoti thi - aur _do_connect() sirf real
        # power-cycle/drop ke baad hi nahi, balki HAR periodic soft-reconnect
        # (RESYNC_INTERVAL = 45s, dekho _resync_loop) ke baad bhi chalta hai.
        # Iska matlab ye warning har ~45s mein hamesha ke liye HA log ko
        # spam karti rehti thi (device chahe bilkul theek chal raha ho),
        # kyunki MOD2U/MOD4U/MOD2F kisi ka bhi br40_code set nahi hai aur
        # ye devices "?BR40=" query ka jawab "+BR40=" se kabhi nahi dete
        # (confirmed: sirf khud-ba-khud "+AR40=" heartbeat bhejte hain, jo
        # ek alag, abhi-tak-undecoded frame hai - "?BR40=" query ka koi
        # asar hi nahi hota in devices par). Ab is warning ko is connection
        # ke lifetime mein SIRF EK BAAR log karte hain; baad ke har (soft-)
        # reconnect par same jaankari sirf DEBUG level par jaati hai, taaki
        # log clean rahe lekin troubleshooting ke liye info fir bhi milti rahe.
        self._br40_warning_logged: bool = False

        # channel_states[ch_num] = {"area": int, "type": str, "on": bool, ...}
        self.channel_states: dict[int, dict] = {}

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
        """Public wrapper around _pair_bounds() - platform files (cover.py)
        ise use karte hain ye pata karne ke liye ki koi curtain channel
        kis pair mein hai (Model_Number_Mod4u.txt capture se ab Pair 1
        aur Pair 2 dono confirmed hain)."""
        lo, _hi = self._pair_bounds(ch_num)
        return (lo - self._channel_start) // CHANNELS_PER_PAIR

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
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, self.port),
                timeout=float(CONNECT_TIMEOUT),
            )
            self._connected = True
            self._reconnecting = False
            _LOGGER.info("Connected to Raylogic %s at %s", self._model_name, self.ip)

            await self._drain_initial_push()

            if self.br40_code is None:
                for _ in range(3):
                    line = await self._read_line(timeout=2.0)
                    if line and "*KA=" in line:
                        self._handle_ka_line(line)
                        break

            await self._try_auto_discovery()

            if not self.auto_mode:
                log_fn = (
                    _LOGGER.debug if self._br40_warning_logged else _LOGGER.warning
                )
                log_fn(
                    "Raylogic %s %s: BR40 auto-discovery nahi hui (is "
                    "model ka br40_code abhi const.py mein None hai ya "
                    "device ne jawab nahi diya). LEGACY mode mein chal "
                    "raha hai: area=0x%02X, channels=%d. Yeh sab Relay "
                    "control ke liye kaam karega. Auto-detect enable "
                    "karne ke liye upar wali *KA= log line share karo. "
                    "(Ye message ab is connection ke liye sirf ek baar "
                    "WARNING mein aayegi, aage har reconnect par sirf "
                    "DEBUG mein - taaki log spam na ho.)",
                    self._model_name, self.ip, self._legacy_area, self._legacy_channel_count,
                )
                self._br40_warning_logged = True
                self._setup_legacy_channels()

            self._listen_task = asyncio.create_task(self._listen_loop())
            self._ka_task = asyncio.create_task(self._keepalive_loop())
            self._resync_task = asyncio.create_task(self._resync_loop())

            if self.state_callback:
                self.state_callback(self.ip, None, {"available": True})

            return True

        except Exception as exc:
            _LOGGER.error("Failed to connect to Raylogic %s: %s", self.ip, exc)
            self._connected = False
            # BUG FIX: pehle yahan reader/writer close nahi hote the agar
            # connect ke baad (jaise auto-discovery step mein) exception aa
            # jaaye - har 30s retry par ek TCP socket leak hota, jo lambe
            # samay mein HA host par file-descriptor exhaustion se poore
            # system ko slow/stuck kar sakta tha.
            if self._writer:
                try:
                    self._writer.close()
                    await self._writer.wait_closed()
                except Exception:
                    pass
                self._writer = None
                self._reader = None
            return False

    async def disconnect(self):
        # Sabse pehle set karo - taaki agar ek reconnect-loop already chal
        # raha ho (device abhi bhi down hai), wo apni agli sleep/attempt ke
        # baad khud ruk jaaye, HA unload/reload hone ke baad bhi background
        # mein hamesha ke liye chalta na rahe.
        self._shutdown = True
        self._connected = False
        for task in (self._listen_task, self._ka_task, self._resync_task, self._reconnect_task):
            if task:
                task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

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
            if self.state_callback:
                self.state_callback(self.ip, None, {"available": False})
            while not self._connected and not self._shutdown:
                _LOGGER.warning(
                    "Raylogic %s %s: connection lost, retrying in 30s",
                    self._model_name, self.ip,
                )
                await asyncio.sleep(30)
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

    async def _resync_loop(self):
        """Har RESYNC_INTERVAL second mein connection ko khud band-khol
        karta hai - App reopen karne jaisa hi effect, taaki Raylogic App se
        kiya gaya koi bhi change (jo live-broadcast nahi hota) kuch second
        mein HA mein bhi reflect ho jaaye. HA se bheji gayi commands (jo
        instantly optimistically apply hoti hain) is se disturb nahi hoti."""
        while self._connected and not self._resyncing:
            await asyncio.sleep(RESYNC_INTERVAL)
            if not self._connected or self._resyncing:
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
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._connected = False
        self._resyncing = False
        ok = await self.connect()
        if not ok:
            _LOGGER.warning(
                "Raylogic %s: periodic resync fail hua, normal 30s "
                "reconnect cycle sambhal lega.", self.ip,
            )
            if self.state_callback:
                self.state_callback(self.ip, None, {"available": False})
            self._schedule_reconnect()

    # ------------------------------------------------------------------ #
    # I/O
    # ------------------------------------------------------------------ #
    def _next_msg(self) -> str:
        self._msg_counter = (self._msg_counter % 999) + 1
        return f"{self._msg_counter:03d}"

    async def _send_raw(self, cmd: str):
        if not self._connected or not self._writer:
            _LOGGER.warning(
                "Raylogic %s: command DROP hui kyunki connection abhi "
                "active nahi hai (connected=%s) - '%s' bheja nahi ja saka. "
                "Device se connection wapas ban raha hoga (30s reconnect "
                "cycle) - thodi der baad dobara try karo.",
                self.ip, self._connected, cmd,
            )
            return
        try:
            self._writer.write((cmd + "\r").encode())
            await self._writer.drain()
            _LOGGER.debug("TX %s: %s", self.ip, cmd)
        except Exception as exc:
            _LOGGER.error("Send error to Raylogic %s: %s", self.ip, exc)
            self._connected = False
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
        await self._send_raw(f"{CLIENT_SENDER_ID},{self._next_msg()},{cmd}")

    async def _read_line(self, timeout: float = 2.0) -> Optional[str]:
        try:
            data = await asyncio.wait_for(self._reader.readuntil(b'\r'), timeout=timeout)
            return data.decode(errors="replace").strip()
        except asyncio.TimeoutError:
            return None
        except asyncio.IncompleteReadError as exc:
            line = exc.partial.decode(errors="replace").strip()
            return line if line else None
        except Exception as exc:
            _LOGGER.error("Read error from Raylogic %s: %s", self.ip, exc)
            self._connected = False
            self._schedule_reconnect()
            return None

    async def _drain_initial_push(self):
        """Naye connection banate hi device jo bhi initial burst bhejta hai
        (App connect karte waqt bhi yahi hota hoga, isiliye reopen karne par
        App ko sahi status milta hai) - pehle hum sirf PEHLI line padh kar
        baaki discard kar dete the. Ab thodi der (2.5s) tak jitni bhi lines
        aayein, sabko _dispatch_line se process karte hain - agar isme
        per-channel *AR= state bhi ho, wo ab channel_states mein reflect
        hogi (state_callback bhi fire hoga, taaki HA entities turant update
        ho jayein)."""
        end_time = asyncio.get_event_loop().time() + 2.5
        first = True
        while asyncio.get_event_loop().time() < end_time:
            remaining = max(0.1, end_time - asyncio.get_event_loop().time())
            line = await self._read_line(timeout=remaining)
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

        # TODO CAPTURE: yahi wo jagah hai jaha H81/RE16/FN4/RE8 apna BR40
        # code nikalte hain (data_bytes[7]). Raw hex hamesha log karte hain
        # taaki jab MOD4U ka capture milega, hum sahi offset confirm kar sakein.
        try:
            if "*KA=" in line and "-" in line:
                ka_data = line.split("*KA=")[1]
                _, hex_data = ka_data.split("-")
                data_bytes = bytes.fromhex(hex_data.strip())
                _LOGGER.info(
                    "Raylogic %s %s raw *KA= bytes (share this for BR40 "
                    "auto-detect setup): %s", self._model_name, self.ip,
                    data_bytes.hex().upper(),
                )
                if self._br40_code is not None and len(data_bytes) > 7:
                    self.br40_code = data_bytes[7]
        except Exception as exc:
            _LOGGER.debug("%s KA parse note (not fatal, capture-only): %s", self._model_name, exc)

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    async def _try_auto_discovery(self):
        """Agar BR40 code known hai (const.py DEVICE_MODELS mein is model
        ke liye filled), device se channel list (Area + Type per channel)
        query karo. Warna silently skip."""
        if self._br40_code is None:
            return
        await asyncio.sleep(0.5)
        code_hex = f"01{self._br40_code:02X}"
        await self._send_addressed(f"?BR40={code_hex}")
        for _ in range(6):
            line = await self._read_line(timeout=3.0)
            if not line:
                continue
            if "+BR40=" in line:
                if self._parse_br40(line):
                    self.auto_mode = True
                    _LOGGER.info(
                        "Raylogic %s: auto-discovered %d channels",
                        self.ip, len(self.channel_states),
                    )
                return
            if "*KA=" in line:
                await self._send_raw("*KA=2")

    def _parse_br40(self, line: str) -> bool:
        """TODO CAPTURE: record layout (size, ch_index offset, type-byte
        offset) abhi H81/FN4 jaisa assume kiya hai (8-byte record, byte[0]
        = area/ch_index). Confirm hone tak in numbers ko capture se verify
        karo."""
        try:
            data_hex = line.split("+BR40=")[1].strip()
            data = bytes.fromhex(data_hex)
            if len(data) < 3:
                return False
            ch_count = data[2]
            records = data[3:]
            record_size = 8
            if len(records) < ch_count * record_size:
                _LOGGER.warning(
                    "Raylogic %s: BR40 response too short for assumed "
                    "record size - raw hex: %s", self.ip, data_hex,
                )
                return False

            for i in range(ch_count):
                r = records[i * record_size:(i + 1) * record_size]
                ch_num = i + 1
                area = r[0]
                type_byte = r[1] if len(r) > 1 else None
                ch_type = CHANNEL_TYPE_BYTE_MAP.get(type_byte, CH_TYPE_RELAY)
                self.channel_states[ch_num] = {
                    "area": area,
                    "type": ch_type,
                    "raw_type_byte": type_byte,
                    "on": False,
                }
                _LOGGER.info(
                    "Raylogic %s ch%d: area=%d type_byte=0x%02X -> "
                    "treated as '%s' (update CHANNEL_TYPE_BYTE_MAP once "
                    "confirmed in app)",
                    self.ip, ch_num, area, type_byte or 0, ch_type,
                )
            return True
        except Exception as exc:
            _LOGGER.warning("Raylogic BR40 parse error '%s': %s", line, exc)
            return False

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
        try:
            data = json.loads(self._state_file.read_text())
            return {int(k): int(v) for k, v in data.items()}
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return {}

    def _write_learned_file(self, snapshot: dict) -> None:
        try:
            _STATE_DIR.mkdir(exist_ok=True)
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
            await self._send_ctc_double(area, eff_brightness, eff_kelvin)
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

    async def _send_ctc_double(self, area: int, brightness: Optional[int], kelvin: int):
        """Double-driver frame - warm/cool cross-fade formula, derived to
        match both captured sweeps exactly (see const.py comment).

        Wire slots: <area>01<ch1_level>02<ch2_level><const><pct>.
        User-confirmed on real hardware: physical channel 1 (the slot
        after "01") is wired to the WHITE/cool driver, physical channel 2
        (the slot after "02") is wired to the YELLOW/warm driver - so
        ch1_level must carry the cool level and ch2_level the warm level
        (previously this was reversed: warm was written to the ch1 slot
        and cool to the ch2 slot, so channel 1 came out yellow and
        channel 2 came out white - backwards from the real wiring)."""
        kelvin = max(CTC_MIN_KELVIN, min(CTC_MAX_KELVIN, kelvin))
        warm_frac = 1 - (kelvin - CTC_MIN_KELVIN) / (CTC_MAX_KELVIN - CTC_MIN_KELVIN)
        brightness = max(0, min(255, brightness or 0))
        bright_frac = brightness / 255
        warm_on = warm_frac * bright_frac
        cool_on = (1 - warm_frac) * bright_frac
        warm_level = 0xFF if warm_on <= 0 else max(1, min(255, round(256 - warm_on * 255)))
        cool_level = 0xFF if cool_on <= 0 else max(1, min(255, round(256 - cool_on * 255)))
        pct = round(bright_frac * 100)
        # ch1 slot (after "01") = white/cool, ch2 slot (after "02") = yellow/warm
        cmd_hex = (
            f"{area:02X}01{cool_level:02X}02{warm_level:02X}"
            f"{CTC_DOUBLE_CONST_BYTE:02X}{pct:02X}"
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

    def _double_warm_level_to_kelvin(self, warm_level: int) -> int:
        warm_level = max(1, min(255, warm_level))
        warm_frac = (256 - warm_level) / 255
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
        if wire_channel is None or len(candidates) == 1:
            return candidates[0][0]
        # Multiple CTC candidates (dono pairs same Area mein) - wire_channel
        # ki pair se match karne wale ko hi pick karo.
        target_lo, target_hi = self._pair_bounds(wire_channel)
        for cn, _st in candidates:
            cn_lo, cn_hi = self._pair_bounds(cn)
            if (cn_lo, cn_hi) == (target_lo, target_hi):
                return cn
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
        <area><01><ch1_cool><02><ch2_warm><64><pct> (7 bytes, see
        const.py) - channel 1 slot = white/cool, channel 2 slot =
        yellow/warm (matches the corrected wiring in _send_ctc_double)."""
        try:
            idx = line.find("*AZ=")
            if idx == -1:
                return
            hex_part = line[idx + 4:idx + 18]
            if len(hex_part) < 14:
                return
            b = bytes.fromhex(hex_part)
            if len(b) < 7 or b[1] != 0x01 or b[3] != 0x02:
                return
            area = b[0]
            warm_level = b[4]
            pct = b[6]
            ch_num = self._find_ctc_channel(area, CTC_MODE_DOUBLE)
            if ch_num is None:
                return
            brightness = max(0, min(255, round(pct * 255 / 100)))
            st = self.channel_states.setdefault(ch_num, {})
            st.update({
                "on": brightness > 0,
                "brightness": brightness,
                "color_temp_kelvin": self._double_warm_level_to_kelvin(warm_level),
            })
            if self.state_callback:
                self.state_callback(self.ip, ch_num, st)
        except Exception as exc:
            _LOGGER.debug("Raylogic AZ parse error '%s': %s", line, exc)

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
    # Control - Curtain (CONFIRMED for BOTH pairs - Model_Number_Mod4u.txt
    # real MOD4U capture, Area 7. Literal commands - curtain uses a
    # different frame shape than relay/dimmer/fan, no channel-derivable
    # formula, and it's a PAIRED mode like CTC - see const.py
    # CURTAIN_PAIR_COMMANDS. MOD2U reuses the Pair 1 bytes since it only
    # ever has one pair.)
    # ------------------------------------------------------------------ #
    async def set_cover(self, ch_num: int, action: str):
        """action: 'open' | 'close' | 'stop'.

        Pair `ch_num` ki apni position se derive hoti hai (na ki literal
        "1" se), taaki agar channel_start 1 ke alawa kuch aur ho (kai
        installs mein hota hai), tab bhi sahi pair match ho aur sahi
        pair-specific bytes (0x02 vs 0x03 marker) bheje jaayein."""
        pair_lo, _pair_hi = self._pair_bounds(ch_num)
        pair_index = (pair_lo - self._channel_start) // CHANNELS_PER_PAIR
        cmd_map = CURTAIN_PAIR_COMMANDS.get(pair_index, {})
        cmd_hex = cmd_map.get(action)
        if not cmd_hex:
            _LOGGER.warning(
                "Raylogic %s %s: channel %d (Pair %d, channel %d-%d) "
                "curtain ke bytes abhi confirmed nahi hain. Raylogic GO "
                "app se ek baar open/close/stop karke us *AR=/*AZ= line "
                "ko log se share karo.", self._model_name, self.ip,
                ch_num, pair_index + 1, pair_lo, pair_lo + 1,
            )
            return
        _LOGGER.info(
            "Raylogic %s %s: curtain '%s' -> channel %d (Pair %d, "
            "connected=%s) - sending *AR=%s", self._model_name, self.ip,
            action, ch_num, pair_index + 1, self._connected, cmd_hex,
        )
        await self._send_addressed(f"*AR={cmd_hex}")
        if action != "stop":
            self.channel_states.setdefault(ch_num, {}).update(
                {"on": action == "open", "moving": True}
            )
            if self.state_callback:
                self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Background loops
    # ------------------------------------------------------------------ #
    async def _listen_loop(self):
        while self._connected:
            line = await self._read_line(timeout=30.0)
            if line:
                self._dispatch_line(line)

    async def _keepalive_loop(self):
        while self._connected:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if self._connected:
                await self._send_raw(KEEPALIVE_CMD)

    def _dispatch_line(self, line: str):
        _LOGGER.debug("RX %s: %s", self.ip, line)
        if "*KA=" in line:
            self._handle_ka_line(line)
        elif "+BR40=" in line:
            self._parse_br40(line)
        elif "+AR40=" in line:
            # NAYA MILA (typo/gap fix, is baar sirf VISIBILITY ke liye):
            # device MOD4U se ye ek ALAG, khud-ba-khud (~19-20s) periodic
            # frame hai jo pehle kabhi handle hi nahi hota tha (dispatch
            # sirf "+BR40=" dhundta tha, jo yahan kabhi milta hi nahi -
            # is naam ka koi confusion na ho, "+BR40=" ek humari QUERY ka
            # jawab hai jo hum abhi bhejte hi nahi, "+AR40=" device khud
            # apni marzi se bhejta hai). Iska byte-layout _parse_br40 ke
            # assumed 8-byte-per-channel record se MATCH NAHI karta - abhi
            # sirf RX log mein saaf dikhega taaki 3-4 samples collect karke
            # iska real structure decode kiya ja sake (agla concrete step).
            _LOGGER.info(
                "Raylogic %s: +AR40= status heartbeat mila (abhi tak "
                "decode nahi kiya gaya) - raw: %s", self.ip, line,
            )
        elif "*AZ=" in line:
            self._handle_az(line)
        elif "*AR=" in line:
            self._handle_ar(line)

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
        # Relay (default)
        return {"on": f"{level:02X}" == RELAY_LEVEL_ON}

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
            hex_part = line[idx + 4:idx + 14]
            if len(hex_part) < 10:
                return
            b = bytes.fromhex(hex_part)
            if len(b) < 5 or b[1] != 0x1A:
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
            self.channel_states[ch_num]["on"] = f"{level:02X}" == RELAY_LEVEL_ON

            if is_new and self.new_channel_callback:
                self.new_channel_callback(ch_num, self.channel_states[ch_num])

            if self.state_callback:
                self.state_callback(self.ip, ch_num, self.channel_states[ch_num])
        except Exception as exc:
            _LOGGER.debug("Raylogic AR parse error '%s': %s", line, exc)
