"""Constants for the Raylogic MOD4U integration.

MOD4U bhi MOD2U jaisa hi "universal module" hai (same Raylogic GO app, same
Select Type screen) - farak sirf itna hai ki MOD4U mein 2 ki jagah 4
physical channels hote hain, jo 2 PAIRS mein group hote hain:
  Pair 1 = channel_start + 0, channel_start + 1   (jaise ch1, ch2)
  Pair 2 = channel_start + 2, channel_start + 3   (jaise ch3, ch4)
Har channel ko independently Relay / Dimmer / Fan banaya ja sakta hai, LEKIN
Curtain aur CTC dono "paired" modes hain - jab kisi pair ka ek channel
Curtain ya CTC banaya jaata hai, wahi pura pair (dono physical channels)
us EK logical entity ke andar internally consume ho jaata hai (bilkul jaisa
MOD2U mein CTC ke liye tha - yahan Curtain bhi wahi rule follow karta hai,
jaisa user ne confirm kiya).

Is file mein 2 tarah ke values hain:
  1. CONFIRMED  - MOD2U capture se already verify ho chuka hai (Relay/
     Dimmer/Fan/CTC-pair1 format MOD4U ke liye bhi identical hai, kyunki
     wire-protocol level pe MOD4U bhi wahi *AR=/*AZ= frame shape use karta
     hai, sirf zyada channels/pairs).
  2. PLACEHOLDER / TODO - Pair 2 (channel 3-4) ke Curtain literal bytes
     abhi tak capture nahi hue. Jaha "TODO CAPTURE" likha hai, wahan real
     value pata chalte hi yahin update karna hai. Tab tak fallback/warning
     logic (protocol.py mein) safe defaults use karta hai.
"""

DOMAIN = "raylogic_mod"

# Network -------------------------------------------------------------- #
DEFAULT_PORT = 5550
CONNECT_TIMEOUT = 5
RECONNECT_DELAY = 30

# STABILITY FIX (webpage/entities "Unavailable" flicker): pehle har
# disconnect (chahe 2-second ka chhota blip ho, jaise device ka apna
# resync ya ek chhota network hiccup) turant UI mein "Unavailable"
# dikhata tha, aur agli reconnect-try hamesha poore FIXED 30s baad hoti
# thi - chahe device turant wapas aa jaata. Real logs mein ye dikha ki
# bahut saare disconnect sirf 5-10 second ke andar khud theek ho jaate
# hain (device thoda busy tha, ya humara apna periodic resync tha) -
# lekin fixed 30s retry + turant "Unavailable" flag ki wajah se UI mein
# hamesha flicker/Unavailable dikhta rehta tha jabki device asal mein
# thodi hi der down tha.
#
# Fix (dono jagah): (1) reconnect ab FIXED 30s ka wait nahi karta - chhota,
# tez pehla retry karta hai aur dheere-dheere RECONNECT_DELAY tak badhta
# hai (neeche RECONNECT_BACKOFF_STEPS), taaki chhote blips 30s ka poora
# wait kiye bina hi turant recover ho jayein. (2) entity ko turant
# "Unavailable" mat dikhao - UNAVAILABLE_GRACE_SECONDS ka ek chhota grace
# window do; agar usi window ke andar reconnect safal ho jaaye (jaisa
# chhote blips mein hota hai), UI mein kabhi flicker hi nahi dikhega.
# Sirf agar itni der tak connect() wapas safal nahi hota, tab hi entity
# "Unavailable" dikhegi - matlab lagatar/genuine outage ke liye hi.
RECONNECT_BACKOFF_STEPS = (1, 2, 4, 8, 15, RECONNECT_DELAY)
UNAVAILABLE_GRACE_SECONDS = 45

# BUG FIX: writer.close() + wait_closed() ke liye pehle koi upper-bound
# timeout nahi tha. Agar koi Raylogic device network par flaky ho ya TCP
# FIN/ACK cleanly na kare (sasta embedded device kar sakta hai), to
# wait_closed() bina kisi limit ke atak sakta tha - kabhi kabhi OS ke apne
# TCP retry timeout tak (minutes). Ye "kuch dair atak kar phir wapas aata
# hai" wale pattern ka root cause tha, aur jitne zyada devices utna hi
# zyada trigger hone ka chance. Ab har jagah close-operation isi fixed
# CLOSE_TIMEOUT ke andar wrap hoti hai (protocol.py + config_flow.py) -
# timeout hone par bhi socket/writer ko discard/None kar diya jaata hai,
# taaki caller kabhi bhi is se zyada der na atke.
CLOSE_TIMEOUT = 5

# BUG FIX (2nd hang source, alag se pakda gaya): har command send ke baad
# `await writer.drain()` hota hai, taaki TCP write-buffer chhota rahe -
# lekin isko bhi pehle KOI timeout nahi tha. Agar device stop-ho-jaaye ya
# TCP ACK dena band kar de (socket "half-dead" ho jaaye lekin bina kisi
# error/FIN ke), drain() OS ke apne TCP retransmit-timeout tak (minutes)
# atak sakta tha - aur har switch/light/fan/cover ka turn_on/turn_off
# seedha isi drain() par await karta hai, isliye ye directly HA ke
# service-call ko (us entity ke liye) "hang" mehsoos karata. Ab isi tarah
# WRITE_TIMEOUT ke andar hard-capped hai - timeout hote hi connection ko
# turant "lost" maan kar reconnect-cycle trigger ho jaata hai (dekho
# protocol.py _send_raw), command chup-chaap kabhi bhi is se zyada der
# atki nahi rehti.
#
# TUNING NOTE (real-world regression pakdi gayi): pehla attempt 5s tha -
# bahut tight nikla. Ek naya device network par connect hote hi (jaise
# doosra Raylogic box add karna) chhote/sasta home router-WiFi par ek
# chhota transient hiccup (ARP resolution, thoda packet delay) bilkul
# NORMAL hai aur khud hi 1-2 second mein theek ho jaata hai. 5s ka tight
# limit isi normal hiccup ko galti se "device hang ho gaya" samajh kar
# turant connection ko force-kill kar deta tha - iska matlab naya device
# add karna already-working device ko bhi "connection lost" mein daal
# deta tha, jo asal bug se bhi zyada iss FIX ki wajah se ho raha tha. Ab
# 15s - genuine infinite/multi-minute hang tab bhi kabhi nahi hone
# dega, lekin normal network jitter ko galat-fahmi se "hang" nahi maanega.
WRITE_TIMEOUT = 15

# ------------------------------------------------------------------ #
# MODEL_* = config_flow mein "Device Model" dropdown ki values. Har model
# ka apna naam/description/default channel-count yahan ek hi jagah
# define hai - naya model add karna ho to bas yahan ek entry aur add
# karni hai, baaki poora code (protocol.py) generic hai aur kisi bhi
# channel-count/pair-count ke liye already kaam karta hai.
#
# NOTE: pehle yahan har model ke liye ek "br40_code" bhi tha (RE8/H81
# jaisa auto-discovery ke liye) - MOD2U/MOD4U/MOD2F kabhi bhi `?BR40=`
# query ka jawab nahi dete the (confirmed), isliye ye poora BR40 code-
# path (const.py + protocol.py dono se) hata diya gaya hai. Static/
# legacy channel setup (config_flow se Area + per-channel Type) hi
# hamesha se actual working path tha.
# ------------------------------------------------------------------ #
MODEL_MOD2U = "mod2u"
MODEL_MOD4U = "mod4u"
MODEL_MOD2F = "mod2f"

DEVICE_MODELS: dict[str, dict] = {
    MODEL_MOD2U: {
        "name": "MOD2U",
        "desc": "Universal 2-Channel Module (Dimmer/Fan/Curtain/Relay/CTC)",
        "channel_count": 2,
    },
    MODEL_MOD4U: {
        "name": "MOD4U",
        "desc": "Universal 4-Channel Module (Dimmer/Fan/Curtain/Relay/CTC)",
        "channel_count": 4,
    },
    MODEL_MOD2F: {
        "name": "MOD2F",
        "desc": "Single-Channel Fan Module (fixed Fan type)",
        "channel_count": 1,
        # MOD2U/MOD4U ke ulat, MOD2F ek "universal" module nahi hai - iska
        # ek hi physical channel hamesha Fan hi hota hai (Raylogic GO app
        # mein "Select Type" ka koi option nahi, seedha "Fan Mode"). Isliye
        # config_flow mein iske liye Channel-Type dropdown dikhaya hi nahi
        # jaata (_channels_schema_fields dekho) - type hamesha yahi fixed
        # value use hoti hai. (String literal "fan" = CH_TYPE_FAN, jo neeche
        # define hai - yahan upar forward-reference avoid karne ke liye
        # literal use kiya gaya hai.)
        # CONFIRMED (Model_Number_-_Mod2F.txt + app screenshot, device
        # 192.168.1.36, node 111, Area 04, Start Address/channel 0x09):
        #   Frame shape bilkul MOD2U/MOD4U jaisa hi: 00 1A <area> <level> <channel>
        #   Off: level=01   Speed1: level=02   Speed2: level=03
        #   Speed3: level=04   Speed4/full: level=05
        # (Same FAN_LEVEL_OFF/FAN_SPEEDS neeche reuse hote hain - MOD2F ke
        # liye alag se koi naya constant nahi chahiye.)
        "fixed_type": "fan",
    },
}
DEFAULT_MODEL = MODEL_MOD4U

# ------------------------------------------------------------------ #
# Area
# MOD2U capture se confirm hua: Area byte = seedha area number ka hex hai
# (Area 12 -> 0x0C). MOD4U bhi wahi Area scheme use karta hai (1-16).
# ------------------------------------------------------------------ #
AREA_MIN = 1
AREA_MAX = 16
LEGACY_DEFAULT_AREA = 0x0C  # 12 - default jab user ne config mein khud
                             # koi area na diya ho

# ------------------------------------------------------------------ #
# Command bytes - CONFIRMED (Docklight capture, MOD2U se, same wire
# protocol MOD4U ke liye bhi)
#   <ID>,<Seq>,*AR=<AddrHigh:00><Cmd:1A><Area><Level><Channel>
#   Relay: Level 01=OFF, 02=ON
# ------------------------------------------------------------------ #
CMD_ADDR_HIGH = "00"
CMD_CHANNEL_DIRECT = "1A"
RELAY_LEVEL_ON = "02"
RELAY_LEVEL_OFF = "01"

# ------------------------------------------------------------------ #
# Dimmer - CONFIRMED (Model_Number_Mod2u.txt, vendor capture, Area 12):
#   Ch1 On :  *AR=001A0C0101  -> level=01 (full/on)
#   Ch1 Off:  *AR=001A0CFF01  -> level=FF (off)
#   Ch1 dimming ramp goes FF -> ... -> 69 as brightness increases
#   Ch2 same pattern with channel byte = 02
# Same frame shape as Relay (00 1A <area> <level> <channel>), only the
# level byte meaning is different: 0x01 = full brightness, 0xFF = off,
# values in between are a proprietary dim curve. No exact 256-step table
# was captured, so brightness is mapped linearly between the two
# confirmed endpoints (good enough approximation; refine if a fuller
# capture turns up). Frame shape is per-channel (channel byte = the real
# physical channel number), so this scales to all 4 MOD4U channels
# unchanged - no MOD4U-specific capture needed.
# ------------------------------------------------------------------ #
DIMMER_LEVEL_ON = 0x01
DIMMER_LEVEL_OFF = 0xFF

# ------------------------------------------------------------------ #
# CTC (Colour Temperature Control / tunable white) - CONFIRMED
# (Model_Number_Mod2u.txt capture, Area 16, matches the user's own Mod
# Settings screenshot: Channel 1 = CTC, "Single Driver" checked).
#
# The Raylogic GO app supports TWO CTC sub-modes, each with its own wire
# format - which one a module uses is a config choice (the Double
# Driver / Single Driver checkboxes in the app), not something the
# device reports on its own, so it's selected in this integration's
# config too (see config_flow.py CTC_MODE_OPTIONS). MOD4U ke liye is
# sub-mode ko HAR PAIR ke liye ALAG se choose kiya ja sakta hai (Pair 1
# aur Pair 2 dono independently single ya double ho sakte hain).
#
# 1) SINGLE DRIVER (CW/WW, one physical channel-PAIR - confirmed by the
#    user: configuring a CTC light in the Raylogic GO app actually
#    consumes TWO real physical channels, e.g. channel 3 + channel 4,
#    exactly like any other 2-channel allocation on this device) -
#    identical frame shape to Relay/Dimmer/Fan (00 1A <area> <level>
#    <channel>), and the "channel" byte here IS the real physical
#    channel number of the pair, same as everywhere else in this
#    protocol - NOT a fixed 0x01/0x02 sub-signal id as first assumed.
#    Within the pair:
#      the LOWER physical channel number  -> colour temperature,
#        level 0x0B..0xFF across the 14 captured points (monotonic) -
#        linear approximation between the two confirmed endpoints,
#        same "good enough" approach already used for the Dimmer
#        brightness curve above. Which physical end (warm/cool) 0x0B
#        vs 0xFF corresponds to was NOT recorded in the capture (only
#        consecutive level values, no colour reference) - if warm/cool
#        comes out reversed in practice, just swap
#        CTC_SINGLE_CT_MIN_LEVEL/MAX_LEVEL below.
#      the HIGHER physical channel number -> brightness, level
#        0x01=full .. 0xFF=off (same curve/formula as Dimmer above,
#        reused as-is).
#    This "lower=CT, higher=brightness" rule applies to WHICHEVER pair
#    the CTC channel is in (Pair 1 = channel_start/channel_start+1, or
#    Pair 2 = channel_start+2/channel_start+3) - protocol.py derives the
#    pair from the configured CTC channel's own position, not a fixed
#    channel_start/channel_start+1 assumption (that was a MOD2U-only
#    shortcut since MOD2U only ever HAD one pair).
#
# 2) DOUBLE DRIVER (separate warm+cool physical channels, combined into
#    one *AZ= frame instead of *AR=):
#      Frame (7 bytes after *AZ=):
#          <area><ch_lo><level_lo><ch_hi><level_hi><64><pct>
#
#      BUG FIX (yehi CCT/CTC ka asli bug tha): pehle yahan <ch_lo> aur
#      <ch_hi> ko FIXED "01"/"02" markers samjha gaya tha. Wo galat-
#      fahmi sirf isliye hui kyunki jis device se capture liya gaya tha
#      (MOD2U, Area 16) uske CTC pair ke PHYSICAL channels hi 1 aur 2
#      the - to fixed marker aur real channel number bilkul ek jaise
#      dikh rahe the. Poore protocol mein har frame (relay/dimmer/fan/
#      CTC-single) hamesha REAL physical channel number carry karta
#      hai, aur curtain ka slot byte bhi channel se derive hota hai -
#      *AZ= bhi isi rule ko follow karta hai. Isliye Area 12 ke MOD4U
#      par jahan CTC pair channels 15-16 hain, frame mein 0F/10 jaana
#      chahiye tha, lekin 01/02 ja raha tha - device us frame ko drop
#      kar deta tha, isliye CCT light bilkul respond nahi karti thi.
#      Ab protocol.py in dono byte ko us CTC channel ki apni pair se
#      derive karta hai (chhota channel = cool slot, bada = warm slot).
#      warm+cool always summed to 0x100 (256) in every captured colour-
#      temperature-only sweep (fixed 100% brightness); a separate
#      brightness-only sweep (fixed colour) varied that same "cool"
#      byte position from 0xFF(off) down to 0x01(full) as brightness
#      rose 0->100%. Both are consistent with a normal dual-channel
#      cross-fade driver where each channel follows the same
#      0x01=full/0xFF=off convention used everywhere else in this
#      protocol, scaled by both colour-temp position AND overall
#      brightness together. `pct` (last byte) is brightness 0-100
#      (0x00-0x64) and moved in lockstep with the scaled channel bytes
#      in every sample. Only PURE colour-only and PURE brightness-only
#      sweeps were captured (not an arbitrary combined change) - the
#      formula in protocol.py is derived to satisfy both captured
#      sweeps exactly, but a combined brightness+colour capture would
#      help confirm it fully.
#      NOTE: purani "MOD4U limitation" (do double-driver CTC pairs ek
#      hi Area mein alag nahi kiye ja sakte) ab khatam ho gayi hai -
#      kyunki frame ab real channel numbers carry karta hai, dono pairs
#      apne apne channel bytes se cleanly disambiguate ho jaate hain.
# ------------------------------------------------------------------ #
CTC_MODE_SINGLE = "single"
CTC_MODE_DOUBLE = "double"

CTC_SINGLE_BRIGHTNESS_ON = DIMMER_LEVEL_ON      # 0x01, same curve as Dimmer
CTC_SINGLE_BRIGHTNESS_OFF = DIMMER_LEVEL_OFF    # 0xFF
CTC_SINGLE_CT_MIN_LEVEL = 0x0B   # confirmed lowest captured level
CTC_SINGLE_CT_MAX_LEVEL = 0xFF   # confirmed highest captured level

CTC_DOUBLE_CONST_BYTE = 0x64     # constant byte seen in every *AZ= sample

CTC_MIN_KELVIN = 2700   # warmest end of the HA colour-temp slider
CTC_MAX_KELVIN = 6500   # coolest end of the HA colour-temp slider
CTC_DEFAULT_KELVIN = 4000

# ------------------------------------------------------------------ #
# Fan - CONFIRMED (Model_Number_Mod2u.txt, Area 12):
#   Off:     *AR=001A0C0101 (ch1) / ...0102 (ch2) -> level=01
#   Speed 1: level=02   Speed 2: level=03
#   Speed 3: level=04   Speed 4: level=05
# Identical scheme to the Din-Re8 FN4 fan. Per-channel frame (channel
# byte = real physical channel), so this scales to all 4 MOD4U channels
# unchanged.
# ------------------------------------------------------------------ #
FAN_LEVEL_OFF = 0x01
FAN_SPEEDS = {0: 0x01, 25: 0x02, 50: 0x03, 75: 0x04, 100: 0x05}

# ------------------------------------------------------------------ #
# Curtain - ab FORMULA se derive hota hai (pehle 6 hardcoded literal
# strings the - YEHI curtain ka asli bug tha).
#
# Curtain Relay/Dimmer/Fan se ALAG frame shape use karta hai (cmd byte
# 0x27 = open/close, 0x26 = stop, 0x1A ki jagah), aur usme Area byte
# hota hi NAHI:
#
#     00 27 <curtain_slot> <direction> <run>      (open / close)
#     00 26 <curtain_slot> 00 00                  (stop)
#
# <curtain_slot> = GLOBAL curtain/pair index, area ya per-device pair
# number NAHI. Raylogic installation mein channel numbers poore system
# mein globally, hamesha 2-2 ke block mein allot hote hain (device 1 ->
# ch 1-2, device 2 -> ch 3-6, ek 1-channel MOD2F bhi apna poora block
# 7-8 leta hai, agla 9-10, ... aur aage 23-24). Isliye:
#
#     curtain_slot = (pair ka chhota channel number + 1) // 2
#
# CONFIRMED - real hardware echoes (user ke apne HA debug log se, jab
# curtain Raylogic GO app se chalayi gayi):
#   MOD2U node 115, Area 08, channels 23-24  -> *AR=00270C020A
#        (0x0C = 12 = (23+1)//2)   [close], *AR=00270C010A [open]
#   MOD4U node 111, Area 12, channels 13-16  -> *AR=002707020A
#        (0x07 =  7 = (13+1)//2)   [close], *AR=002707010A [open]
#   Purana Model_Number_Mod4u.txt capture (Area 7, channels 3-6) ->
#        0x02 = (3+1)//2 aur 0x03 = (5+1)//2 - matlab wahi formula,
#        bas wo ek hi device ke slot the. Unhe "Pair 1 / Pair 2" samajh
#        kar HAR device par bhej dena hi bug tha: Area 8 ke ch 23-24
#        wali curtain ko slot 12 chahiye tha, lekin slot 2 bheja ja
#        raha tha - device us frame ko chup-chaap ignore kar deta hai
#        (isliye "kuch hota hi nahi" wala symptom, koi error bhi nahi).
#
# <run> = aakhri byte. Real app traffic (dono devices) me 0x0A hai;
# purane Area-7 capture me 0x05 tha - matlab ye curtain ka configured
# travel/run parameter hai, fixed constant nahi. Default 0x0A rakha hai
# (user ke asli hardware jaisa), aur protocol.py device se aane wale
# curtain echo se is byte ko khud-ba-khud SEEKH bhi leta hai, taaki jo
# bhi value app use karti ho wahi HA bhi bheje.
# ------------------------------------------------------------------ #
CURTAIN_CMD_ADDR_HIGH = "00"
CURTAIN_CMD_MOVE = 0x27      # open/close
CURTAIN_CMD_STOP = 0x26      # stop
CURTAIN_DIR_OPEN = 0x01
CURTAIN_DIR_CLOSE = 0x02
CURTAIN_RUN_BYTE_DEFAULT = 0x0A
CURTAIN_STOP_TAIL = "0000"

# Har curtain slot 2 physical channels ka hota hai (ek pair).
CHANNELS_PER_CURTAIN_SLOT = 2

# ------------------------------------------------------------------ #
# +AR40= channel-type map - CONFIRMED (Model_Number_Mod2u.txt).
# This is a CONFIGURATION frame the Raylogic GO app sends to the module
# to SET each channel's mode (relay/dimmer/fan/curtain) - it is not a
# readback/query the module answers on its own, so it can't be used for
# live auto-detection the way RE8's +BR40= can. It's kept here for
# reference/documentation and for a future "set channel mode from HA"
# service, and to interpret the byte if it's ever seen echoed back.
# MOD4U ka apna +AR40= layout (4 channels ke liye ch_count/records kaise
# grow hote hain) capture nahi hua - neeche wala shape MOD2U (2-channel)
# ka hai, sirf reference ke liye.
#   Bytes (12 total, after +AR40=): 01 01 <ch_count> <ch1_type> <ch1_sub>
#   <ch2_type> <ch2_sub> 00 00 00 FF FF
#   ch_type: 00=relay 01=dimmer 02=fan 03=curtain
# ------------------------------------------------------------------ #
AR40_TYPE_RELAY = 0x00
AR40_TYPE_DIMMER = 0x01
AR40_TYPE_FAN = 0x02
AR40_TYPE_CURTAIN = 0x03

# ------------------------------------------------------------------ #
# Channel types (per-channel, as seen in "Select Type" screen)
# Ye types config_flow se (user ke manual selection se) aate hain - dekho
# _resolve_channel_types (__init__.py). Device khud apna channel-type
# broadcast nahi karta (BR40 auto-discovery hata di gayi hai - dekho
# protocol.py header note).
# ------------------------------------------------------------------ #
CH_TYPE_DIMMER = "dimmer"
CH_TYPE_FAN = "fan"
CH_TYPE_CURTAIN = "curtain"
CH_TYPE_RELAY = "relay"
CH_TYPE_CTC = "ctc"
CH_TYPE_EMPTY = "empty"

CHANNELS_PER_PAIR = 2
DEFAULT_CHANNEL_COUNT = 4  # fallback only, agar model info kisi wajah se na mile
# NOTE: pehle yahan ye hi fixed value poore integration ki channel-count
# default thi (jab integration sirf MOD4U ke liye tha). Ab actual channel
# count DEVICE_MODELS[model]["channel_count"] se aata hai (config_flow
# mein "Device Model" dropdown se), model ke hisaab se 2 (MOD2U) ya 4
# (MOD4U). PAIR_COUNT bhi ab per-model derive hota hai - config_flow.py
# ka _pair_count(model) dekho.

# "scene" + "select" (v1.6.0): Raylogic GO app ke AREA scenes. Ye poori
# installation ke liye global hain (kisi ek module ke nahi), isliye sirf EK
# entry ("scene host") inhe banata hai - dekho scene.py / select.py.
PLATFORMS = ["switch", "light", "fan", "cover", "scene", "select"]

KEEPALIVE_CMD = "*KA=01"

# ------------------------------------------------------------------ #
# KEEPALIVE FORMAT (v1.5.3) - "device har 12 second me khud connection
# tod deta hai" ka ilaaj
#
# v1.5.2 ke logs ne ek bahut saaf pattern dikhaya:
#   - 301 me se 301 session THEEK ~12.3 second chal kar device ki taraf
#     se band hui (12s/14s/15s, har ek device par bilkul same).
#   - Ye pehle bhi ho raha tha (purane v1.4.2 log me 1881 session, sab
#     12.0s par EOF) - bas tab humara apna buggy resync ~13s me connection
#     tod hi deta tha, isliye ye device-side timer kabhi saamne nahi aaya.
#   - Device khud har ~6 second me apna *KA= bhejta hai. 6 x 2 = 12.
#     Matlab: device har KA ka JAWAB maangta hai, aur do KA ka jawab na
#     mile to client ko dead maan kar connection kaat deta hai.
#
# Ab dekho humara purana keepalive kaisa jaata tha:
#     TX 192.168.120.101: *KA=01                  <-- BINA prefix
#     TX 192.168.120.101: 099,001,*AR=001A100101  <-- command, prefix ke saath
#     RX 192.168.120.101: 106,*KA=21-0542100...   <-- device, prefix ke saath
# Is wire par HAR frame "<id>," ya "<id>,<seq>," prefix carry karta hai.
# Sirf humara keepalive bina prefix ke jaata tha - bahut sambhavna hai ki
# device use malformed samajh kar phenk deta tha, isliye uske liye humne
# kabhi jawab diya hi nahi.
#
# Kyunki humare paas app ka capture nahi hai, exact format guess karne ke
# bajaye code ise KHUD SEEKHTA hai: har naye connection par ek variant
# try hota hai aur session kitni der chali ye naapa jaata hai. Jo variant
# 12-second wali deewar todta hai (session > KEEPALIVE_GOOD_SESSION), use
# permanently lock kar liya jaata hai. Device har 12s me reconnect karta
# hai, isliye ye tuning kuch hi minute me apne aap ho jaati hai.
#
# Agar koi bhi variant kaam na kare, to bhi koi problem nahi - FAST
# RECONNECT (neeche) downtime ko ~2.3s se ghata kar milliseconds me le
# aata hai, isliye user ko farak nahi padega.
# ------------------------------------------------------------------ #
# {seq} apne aap bhar jaata hai. Order = sabse zyada sambhavit pehle.
KEEPALIVE_VARIANTS = (
    "{id},{seq},*KA=01",   # humare command frames jaisa (id + seq)
    "{id},*KA=01",         # device ke apne KA frame jaisa (sirf id)
    "*KA=01",              # purana behaviour (bina prefix) - fallback
)
# Session itni der chal gayi = ye variant 12s ki deewar tod raha hai.
KEEPALIVE_GOOD_SESSION = 25

# Device ke *KA= ka jawab dene ke beech kam se kam itna gap - sirf
# anti-spam ke liye (agar device kabhi burst me KA bhej de). Device ka
# apna KA ~6s ka hai, isliye 1s se har KA ka jawab aaram se chala jaata
# hai. NOTE: ye jaan-boojh kar "kya humne haal me kuch aur bheja tha"
# par depend NAHI karta - device ko specifically KA ka jawab chahiye,
# koi bhi traffic kaafi nahi hai.
KA_REPLY_MIN_GAP = 1.0

# ------------------------------------------------------------------ #
# FAST RECONNECT (v1.5.3)
#
# Agar device ka ~12s par band karna uski design hi hai, to use "error"
# ki tarah treat karna galat hai. v1.5.2 me har EOF ke baad 1s backoff +
# 0.5-2.0s settle lagta tha = har cycle ~2.3s downtime, yaani 16% waqt
# connection down (measured). Usi 16% me pada hua click reconnect ka
# intezaar karta tha.
#
# Ab: agar session NORMAL lambi chali thi (FAST_RECONNECT_MIN_SESSION se
# zyada), to ye ek expected close hai - turant (FAST_RECONNECT_DELAY)
# dobara connect karo, koi backoff/settle nahi. Downtime ~2.3s se ghat
# kar ~0.1-0.3s reh jaata hai (uptime ~84% se ~98%).
#
# Genuine failure (session bahut chhoti, ya connect hi fail) par purana
# backoff + settle waisa hi rehta hai - warna ek sach me down device par
# HA hammer karne lagta.
# ------------------------------------------------------------------ #
FAST_RECONNECT_MIN_SESSION = 8
FAST_RECONNECT_DELAY = 0.05

# ------------------------------------------------------------------ #
# STABILITY (v1.5.0) - "connection lost" ka asli ilaaj
#
# Real logs (10 device, 9.8 minute) se jo saaf pakda gaya:
#   - 202 TCP connection khule, jinme se 174 (86%) HUMARE apne periodic
#     resync ne banaye. Sirf 26 asli device-side EOF the.
#   - 26 me se 26 EOF connect hone ke 30 SECOND KE ANDAR aaye, median
#     session sirf 12 second. Matlab device lambi session sambhal leta
#     hai - use mauka hi nahi mil raha tha.
#   - Har device khud, bina maange, har 6-12 second me apna *KA= frame
#     bhejta hai (293 RX vs humare 125 TX). Matlab "connection zinda hai
#     ya nahi" ka jawab device se MUFT me mil raha hai.
#
# Isliye ab strategy ulti hai: connection ko CHHEDNA HI BAND karo.
#   1. Keepalive ab PASSIVE hai - hum tabhi likhte hain jab device se
#      kuch aaya hi na ho (KEEPALIVE_INTERVAL tak). Normal halat me
#      humara write count 0 ho jaata hai.
#   2. Connection "mar gaya" ka pata ab READ se chalta hai, WRITE se
#      nahi - agar RX_SILENCE_TIMEOUT tak device ka apna heartbeat na
#      aaye tabhi use dead maanenge. Ye zyada tez bhi hai aur module ke
#      TCP stack ko chhedta bhi nahi.
#   3. Periodic resync ab sirf ek dur ka safety-net hai (neeche
#      RESYNC_INTERVAL), kyunki app/switch se hue changes device khud
#      live *AR= broadcast karta hai - state ke liye connection todne
#      ki zaroorat hi nahi hai.
#   4. Har naye connect par purane background tasks CANCEL hote hain
#      (dekho _cancel_bg_tasks + _conn_generation) - pehle har reconnect
#      ek NAYA resync task banata tha bina purana band kiye, wo jamte
#      jaate the aur 25s ka interval practically 5-13s ban jaata tha
#      (logs me 94% resync gaps 20s se kam the).
# ------------------------------------------------------------------ #

# Listen loop ek baar me itni der read par wait karta hai. Chhota rakhna
# theek hai - ye sirf "kitni jaldi silence check karna hai" decide karta
# hai, koi network traffic generate nahi karta.
LISTEN_READ_TIMEOUT = 10

# Naya connection bante hi device apna initial burst bhejta hai (usi se
# HA ko fresh state milti hai). Burst khatam hua ya nahi, ye ek chhote
# quiet-gap se pata chalta hai - overall cap INITIAL_DRAIN_MAX hai.
# (v1.5.2 se pehle har connect par poore 2.5s ka fixed wait hota tha, jo
# 100+ device wale setup me startup me minute-scale delay ban jaata tha.)
INITIAL_DRAIN_QUIET = 0.6
INITIAL_DRAIN_MAX = 2.5

# Humara apna *KA=01 write TABHI jaayega jab device se itni der tak KUCH
# bhi na aaya ho. Device ka apna heartbeat 6-12s ka hai, isliye 30s
# (2.5x margin) rakha hai - normal halat me humara write count 0 rehta
# hai, aur ek-aadh late heartbeat par bhi bekaar ka traffic nahi banta.
# Ye KEEPALIVE_INTERVAL se alag hai: interval = kitni der me CHECK karna
# hai, threshold = kitni khamoshi ke baad sach me LIKHNA hai.
KEEPALIVE_IDLE_THRESHOLD = 30

# Device khud har 6-12s me apna *KA= bhejta hai. Itni der tak agar KUCH
# bhi na aaye, tab connection ko dead maano (~6 missed heartbeats - itna
# margin isliye ki busy device ya thoda WiFi jitter galti se "dead" na
# ban jaaye).
RX_SILENCE_TIMEOUT = 75

# Har reconnect attempt se pehle ek chhota random gap - do fayde:
#   (a) module ko apna purana socket cleanup karne ka time milta hai
#       (iske bina naya connect turant EOF kha jaata tha - logs me 5
#        "Failed to connect ... EOF" isi wajah se the),
#   (b) 10 device ek saath stampede nahi karte.
RECONNECT_SETTLE_MIN = 0.5
RECONNECT_SETTLE_MAX = 2.0

# ------------------------------------------------------------------ #
# COMMAND DELIVERY (v1.5.2) - "ek click me device chale" wala fix
#
# Problem (real logs se): Raylogic device command ka koi ACK nahi bhejta
# (log1 me 454 TX commands gaye, sirf 11 RX aaye - aur wo 11 bhi app ke
# apne broadcast the, humare command ka jawab nahi). Aur `writer.write()
# + drain()` TCP par ek half-dead socket par bhi CHUP-CHAAP "safal" ho
# jaata hai - drain() sirf itna batata hai ki data OS ke buffer se nikal
# gaya, ye nahi ki device tak pahuncha. Isliye HA ko kabhi pata hi nahi
# chalta tha ki command kho gaya - user ko dobara/teesri baar click
# karna padta tha.
#
# Log1 ke 460 commands me se 337 (73%) aise moment par pade jab
# connection ya to band ho rahi thi ya abhi-abhi bani thi, aur 159 (35%)
# to sirf 1.5 second ke andar - yani drop hone ka poora mauka.
#
# Fix: device se ACK nahi milta, lekin TCP se milta hai. Linux par
# SIOCOUTQ ioctl se pata chal jaata hai ki socket ke send-queue me
# kitne bytes abhi tak UN-ACKED pade hain. Command bhejne ke baad hum
# background me (UI ko bina rok ke) ye queue watch karte hain:
#   - queue 0 ho gayi  -> device ke TCP stack ne data receive kar liya,
#                          command pakka pahuncha.
#   - timeout tak 0 na  -> socket sach me dead hai. Tab HA khud
#     ho                  connection ko dead mark karta hai, command ko
#                          queue me daal kar turant reconnect karta hai,
#                          aur reconnect hote hi command dobara bhej
#                          deta hai. User ko dobara click karne ki
#                          zaroorat NAHI.
# Ye verification background task me chalti hai, isliye entity click ka
# response turant hi rehta hai (koi UI lag nahi).
#
# NOTE: SIOCOUTQ Linux-specific hai (HA OS/Docker/Supervised sab Linux
# hain). Kisi aur platform par ye check apne aap skip ho jaata hai -
# baaki sab fixes wahan bhi kaam karte hain.
# ------------------------------------------------------------------ #
DELIVERY_VERIFY_TIMEOUT = 1.2
DELIVERY_VERIFY_POLL = 0.05

# Command bhejne se PEHLE ka sanity check: device normally har 6-12s me
# apna frame bhejta hai. Agar itni der se ekdum khamoshi hai to socket
# ko "shakki" maano - us par likhne ki koshish karke command gawane se
# behtar hai ki use queue karke pehle connection refresh kar lo.
LINK_SUSPECT_SECONDS = 30

# Queue me pada command itna purana ho jaaye to use replay mat karo -
# tab tak user ne shayad kuch aur hi kar diya hoga, purana command
# replay karna ulta confusing hoga.
PENDING_COMMAND_MAX_AGE = 30

# Ek command ko zyada se zyada itni baar bheja jaayega (pehli koshish +
# retries). Saare commands IDEMPOTENT hain (absolute level bhejte hain,
# toggle nahi) - relay ON, dimmer 60%, curtain open - inhe dobara bhejna
# bilkul safe hai. Cap isliye taaki ek sach me down device par HA
# hamesha ke liye retry karta na rahe.
COMMAND_MAX_ATTEMPTS = 4

# ------------------------------------------------------------------ #
# Client sender-ID - CONFIRMED from real Docklight capture (device
# 192.168.1.34:5550, connected DIRECTLY, no TCP-HUB machine in between).
# Real traffic shows TWO different identities on the wire:
#   "109,...,*KA=..." / "109,...,+AR40=..." -> the MODULE/HUB's OWN
#     identity broadcasting its status. NOT to be reused as our sender id
#     (device ignores/loops commands that claim to be from itself).
#   "099,155,*AR=001A040203" (and 099,158 / 099,159 / 099,160...) -> a
#     CLIENT session's real, working *AR= commands (mobile app's own
#     session sending real accepted commands). This is genuinely honored
#     by the device, so we mirror it for our own outgoing commands.
# Official PDF's bare "*AR=...\r" (no prefix) examples are the LOGICAL
# payload only - real wire traffic always carries this <id>,<seq>, prefix.
# ------------------------------------------------------------------ #
CLIENT_SENDER_ID = "099"
# F1 (v1.6.1) - "raylogic" (main/DIN) integration ke saath co-existence:
# main integration normal commands "003" se bhejta hai aur sirf scene
# recall "099" se; ye integration sab kuch "099" se. Ye CLASH NAHI hai:
#   - Sender ID reply-address nahi hai - har integration ka har module ke
#     saath apna TCP connection hai, device frames usi connection par
#     lautata hai.
#   - main integration ka ack-matching sirf node "003" ke echo dekhta hai,
#     hum "003" kabhi nahi bhejte; hum echo-ack use hi nahi karte (TCP
#     SIOCOUTQ verify).
#   - Ek doosre ke "099" *AR= frames dono ke liye keypad/app activity
#     hain - yahi intended two-way sync hai.
# "098" jaisi value par MAT badlo jab tak MOD hardware par capture se
# confirm na ho: firmware node IDs ko alag treat karta hai (query sirf
# 003 se answer, *BS= sirf 099 recall par) - unknown ID par MOD saare
# commands chup-chaap ignore kar sakta hai.
# DIAGNOSTIC NOTE: official Raylogic App device se sirf thodi der ke liye
# connect hota hai (App khulne par), 24/7 nahi - isliye App mein disconnect
# problem kabhi dikhta hi nahi. HA iske ulat, SAARE devices se HAMESHA
# connected rehta hai aur har KEEPALIVE_INTERVAL second mein ping karta
# hai - in chhote/sasta WiFi modules ke limited TCP stack ke liye ye
# "hamesha zinda connection + baar-baar ping" wala load unke apne firmware
# design se zyada hai. Interval 5s se 15s kar diya - taaki fleet-wide
# protocol chatter kam ho (10 devices x har 5s se ghata kar har 15s), bina
# responsiveness khoye (HA ka apna optimistic-update + real-time *AR=
# listener already turant UI update kar deta hai, ye keepalive sirf
# "connection zinda hai ya nahi" check karne ke liye hai).
KEEPALIVE_INTERVAL = 15

# ------------------------------------------------------------------ #
# Periodic resync (soft-reconnect)
#
# Confirmed via real-world test: device apna CORRECT, up-to-date state
# sirf ek NAYE connection ke initial burst par deta hai (isi wajah se
# Raylogic App band-khol karne par sahi status dikhata hai, chahe HA se
# change kiya ho). Device kisi bhi channel (Relay ho ya Dimmer) ka state
# change doosre already-connected sessions ko live broadcast NAHI karta.
#
# Isliye HA yahan periodically apna connection khud band-khol karta hai
# (background mein, entities/commands mein koi rukawat nahi) - bilkul
# App reopen karne jaisa hi effect - taaki dono taraf (App se kiya gaya
# change HA mein, aur HA se kiya gaya change App mein) kuch hi second mein
# sync ho jaaye, bina live-push ke bharose rahe.
# ------------------------------------------------------------------ #
# STABILITY FIX: real-world logs se pata chala ki ye periodic resync
# khud hi disconnect-cycle ka sabse bada source tha - har 45s mein
# EVERY device apna TCP band-khol karta tha, aur kai chhote/sasta
# embedded modules is baar-baar band-khol ko turant handle nahi kar
# paate (nayi connection khulte hi turant band kar dete - "peer ne
# connection band kar diya (EOF)"), jisse device baar-baar
# "Unavailable" dikhta rehta. Interval ko 45s se badha kar 180s (3
# minute) kar diya - App se hua koi change ab bhi kuch hi minute mein
# HA mein sync ho jayega, lekin device par band-khol ka load ~4x kam
# ho gaya hai, jo real-world mein bahut zyada stable dikha.
# DIAGNOSTIC NOTE (real logs se pata chala): device apni taraf se/network
# ki taraf se har ~30-40s mein khud disconnect ho rahe hain - ye humare
# resync interval (180s) se bhi zyada frequent hai, matlab asli disconnects
# ka MAIN source hamara resync nahi hai. Isliye interval ko 600s (10 min)
# tak badha diya - taaki resync khud kisi bhi extra churn ka reason na bane,
# aur agar disconnects fir bhi same rate se aate rahein, to confirm ho
# jaayega ki ye purely network/device-side hai (WiFi/router/power), code
# fix se nahi rukega.
# SOFTWARE-HUB STRATEGY (bina koi hardware kharide): official Raylogic
# App device se sirf THODI DER ke liye connect hota hai - kabhi 24/7
# nahi - aur usmein koi disconnect problem nahi dikhti (user ne khud
# confirm kiya). HA ulta karta tha: 24/7 ek hi connection zinda rakhta
# tha, jise in modules ka chhota/sasta WiFi chip handle nahi kar paata
# aur khud hi, UNCONTROLLED tarike se tod deta tha (random EOF).
#
# Fix: HUB-1 jaisa external hardware kharidne ki jagah, HA ab khud
# App jaisa hi short-session pattern follow karta hai - HAR
# RESYNC_INTERVAL second mein connection ko khud, CONTROLLED tarike se
# band-khol karta hai (dekho _resync_loop/_soft_reconnect) - device ko
# kabhi itni der connection zinda dikhti hi nahi ki uska stack "confuse"
# ho aur khud EOF de. Interval ko 600s (rare touch-up) se ghata kar 25s
# kar diya - taaki HA hamesha device ki tolerance-limit (~30-40s, jo
# real logs mein dekha gaya) se PEHLE khud gracefully cycle kar de.
# Grace-period (UNAVAILABLE_GRACE_SECONDS) is baar bhi UI ko flicker se
# bachata hai - ye cycling background mein invisible rehni chahiye.
#
# FINAL FIX (v1.5.0): upar wali "short-session" theory real logs me GALAT
# nikli. Data ye kehta hai:
#   - Device 30s+ ki session bilkul theek sambhalta hai. 26 me se 26 EOF
#     connect ke 30 SECOND ke ANDAR aaye (median session 12s) - matlab
#     device lambi session se nahi TOOT raha tha, use lambi session MILI
#     hi nahi thi, kyunki hum har 25s (aur duplicate loops ki wajah se
#     asal me har 5-13s) me khud hi connection tod rahe the.
#   - Device apna state khud live broadcast KARTA hai (app se curtain
#     chalane par *AR= frame HA ki chalti hui connection par aaya) -
#     matlab resync ka original justification hi galat tha.
# Isliye ab resync sirf ek DUR ka safety-net hai (15 minute), normal
# state-sync live push se hota hai. Iske saath _conn_generation guard bhi
# aa gaya hai taaki purane/duplicate resync loops kabhi na chalein.
#
# FINAL (v1.5.4): ab periodic resync BAND hai (0 = off).
#
# v1.5.3 ke real log ne ye sab confirm kar diya:
#   - device-side EOF: 301 se ghat kar 0. Sessions 545 second tak chali
#     (pehle sab 12s par mar jaati thi) - keepalive fix ne 12-second wali
#     deewar sach me tod di.
#   - Us log me connection ke tootne ka EKMATRA baaki source humara apna
#     resync tha: 9 resync, har ek me device ~1.8s (max 2.6s) DOWN.
#     Usi window me pada click queue me jaakar wait karta hai - yehi
#     ekmatra bacha hua "lag" tha.
#   - Resync ki zaroorat hi nahi rahi: device app/switch se hue changes
#     khud live *AR= broadcast karta hai, aur connection ab TCP-level par
#     stable hai (TCP guarantee karta hai ki connected rehte hue koi
#     frame gum nahi hoga). Agar kabhi connection sach me tootti hai, to
#     reconnect ke baad device ka initial burst waise bhi fresh state de
#     deta hai.
# Isliye 0 = poori tarah band. Koi bhi non-zero value dobara enable kar
# degi (code us value ko waise hi respect karta hai).
RESYNC_INTERVAL = 0

# STABILITY FIX: pehle resync ke dauran purana socket band karke UPAR
# SE TURANT (0 second wait) naya connect() try hota tha - kai devices
# itni jaldi wapas connection accept nahi karte (unhe apna purana
# socket poori tarah saaf karne ke liye thoda time chahiye hota hai),
# isliye naya connect() bhi turant EOF de kar fail ho jaata. Ab
# close karne ke baad ek chhota (~1-2s, per-device random taaki sab
# devices sync na ho jaayein) "saans lene ka" gap diya jaata hai,
# taaki device ko apna purana socket cleanup karne ka mauka mile.
SOFT_RECONNECT_SETTLE_MIN = 1.0
SOFT_RECONNECT_SETTLE_MAX = 2.5


# ------------------------------------------------------------------ #
# Area scenes (v1.6.0) - Raylogic GO app ke area-scenes
# Wire format (reference "raylogic" integration ke live captures se
# confirmed): *AR=000F <area> <scene> 00 - ye ek BUS BROADCAST hai, us
# Area ke saare fixtures react karte hain. Keypad/app se recall hone par
# yahi frame dusre nodes par echo hota hai - usse select entity ko
# two-way feedback milta hai. Recall CLIENT_SENDER_ID (099) ke under hi
# jaata hai, jo app/keypad wala node hai (reference me bhi scene recall
# isi node se hota hai taaki app me scene "selected" dikhe).
# ------------------------------------------------------------------ #
SCENE_FUNC = 0x0F
SCENE_AREAS = 16          # areas 1..16 (AREA_MIN..AREA_MAX jaisa)
SCENE_MAX = 64            # scene number 1..64
CONF_SCENE_COUNTS = "scene_counts"

# Scene/select entities kis entry par bane hain, aur kis merged map se -
# hass.data[SCENE_DATA_KEY] = {"host": entry_id, "map": {...}}. Alag key
# rakhi hai taaki hass.data[DOMAIN] me sirf RaylogicModDevice objects hi
# rahein (purana code unhe entry_id se hi padhta hai).
SCENE_DATA_KEY = f"{DOMAIN}_scenes"

# Global (entry-independent) dispatcher signal. Scene feedback kisi bhi
# module se aa sakta hai (jo bhi us bus frame ko sune), isliye ye per-entry
# nahi hai - lekin inhe sirf scene/select entities sunte hain (areas x
# scenes jitne), channel entities nahi, isliye scale-fix (per-entry
# signals) par koi asar nahi.
SIGNAL_AREA_SCENE = f"{DOMAIN}_area_scene"


def parse_scene_map(text) -> dict:
    """"area:scene,scene; area:scene" -> {area: [scenes]}.

    Example: "12:1,2,3; 5:1,4" = Area 12 me scene 1,2,3 aur Area 5 me 1,4.
    Khaali text = koi scene nahi. Galat tokens chup-chaap skip hote hain,
    ranges clamp hoti hain, kabhi raise nahi karta - ek typo setup nahi
    tod sakta."""
    result: dict[int, list] = {}
    for block in str(text or "").replace("\n", ";").split(";"):
        area_str, sep, scenes_str = block.strip().partition(":")
        if not sep:
            continue
        try:
            area = int(area_str.strip())
        except ValueError:
            continue
        if not 1 <= area <= SCENE_AREAS:
            continue
        scenes = set()
        for tok in scenes_str.split(","):
            try:
                num = int(tok.strip())
            except ValueError:
                continue
            if 1 <= num <= SCENE_MAX:
                scenes.add(num)
        if scenes:
            result.setdefault(area, [])
            result[area] = sorted(set(result[area]) | scenes)
    return result


def merge_scene_maps(option_values) -> dict:
    """Kai devices ke scene maps ka union - area scenes global hain, isliye
    scene config KISI BHI ek device ke Configure me daala ja sakta hai."""
    merged: dict[int, set] = {}
    for text in option_values:
        for area, scenes in parse_scene_map(text).items():
            merged.setdefault(area, set()).update(scenes)
    return {area: sorted(scenes) for area, scenes in sorted(merged.items())}
