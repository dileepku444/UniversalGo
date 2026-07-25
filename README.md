# Raylogic MOD2U / MOD4U / MOD2F - Home Assistant Integration

Ek hi integration, teeno devices ke liye:

- **MOD2U** - 2 physical channels, 1 pair, universal type (Relay/Dimmer/Fan/Curtain/CTC)
- **MOD4U** - 4 physical channels, 2 pairs, universal type (Relay/Dimmer/Fan/Curtain/CTC)
- **MOD2F** - 1 physical channel, **fixed Fan type** (no Select Type option -
  device hamesha Fan hi hota hai, config sirf Area + channel number maangta hai)

RE8-style config-flow architecture (Relay / Dimmer / Fan / Curtain / CTC
per channel, MOD2F ke liye sirf Fan). Device add karte waqt "Device Model"
dropdown se MOD2U, MOD4U, ya MOD2F chuno - baaki config screen usi model ke
hisaab se sirf utne hi channel fields dikhata hai (MOD2U = 2, MOD4U = 4,
MOD2F = 1 aur koi Type dropdown nahi).

Channels **PAIRS** mein hote hain:

- **Pair 1** = Channel 1 (+ Channel 2 on MOD4U)
- **Pair 2** = Channel 3 + Channel 4 (MOD4U only)

Relay, Dimmer, aur Fan har channel par **independently** set ho sakte hain.
Curtain aur CTC (colour-temperature) dono **paired** modes hain - jis pair
ka koi ek channel Curtain ya CTC banaya jaata hai, wahi pura pair (dono
physical channels) ek hi logical entity ke andar internally consume ho
jaata hai (2 alag entity nahi bantin).

## Setup

1. Copy `custom_components/raylogic_mod` into your HA `config/custom_components/`.
2. Restart Home Assistant.
3. Settings -> Devices & Services -> Add Integration -> "Raylogic MOD2U / MOD4U / MOD2F".
4. Step 1: IP Address, Port (default 5550), aur **Device Model** (MOD2U/MOD4U/MOD2F).
5. Step 2 (MOD2U/MOD4U):
   - **Area** - device ke Mod Settings screen par jo Area dikhta hai (e.g.
     12, ya 16 for a CTC pair). `0` sirf tab use karo jab SAARE channels
     Relay ho - HA khud Area seekh lega pehli baar app/switch se toggle
     karne par. Dimmer/Fan/Curtain/CTC ke liye Area manually dena zaroori
     hai.
   - **Channel 1-N Type** - jo aapne Raylogic GO app ke "Select Type"
     screen mein set kiya hai: `relay`, `dimmer`, `fan`, `curtain`, ya
     `ctc`. Device khud apna type report nahi karta, isliye ye ek baar
     yahan batana padta hai.
   - **CTC / Curtain (paired types)**: Channel 1 (ya 3) Type ko `ctc`/
     `curtain` set karne se Channel 2 (ya 4) Type field us pair ke liye
     automatically ignore ho jaata hai - ek hi entity banti hai, do
     conflicting nahi. CTC ke liye Driver Mode bhi batao (Single/Double,
     jaisa app ke Mod Settings screen mein hai) - har pair independently.
5. Step 2 (MOD2F): sirf 2 field - **Area** (device app mein jo dikhta hai,
   e.g. 4) aur **First Channel Number** (device app ke "Start Address" jitna
   hi, e.g. `9`). Koi Type dropdown nahi - Fan hamesha fixed hai. Area `0`
   yahan nahi chalega (Fan Learn-mode support nahi karta, sirf Relay karta
   hai) - integration khud isko block kar dega agar Area 0 chhoda.

## Bug fixes is version mein (dono files se merge kiye gaye)

1. **THE MAIN FIX - device power-cycle ke baad HA permanently "stuck"**:
   pehle connection-loss retry sirf **EK BAAR** (30 second baad) hota tha
   - agar wahi ek attempt fail ho jaata (jaise device abhi reboot ho hi
   raha ho - real-world power-cycle mein bilkul normal hai), integration
   **hamesha ke liye** disconnected state mein reh jaata, chahe device
   wapas up ho jaaye aur ping karta rahe. Ab `_reconnect()` ek proper
   loop hai - device dobara reachable hote hi (agle 30s retry cycle par)
   khud-ba-khud reconnect ho jaata hai, HA restart/reload ki zaroorat
   nahi padti.
2. **CTC pair-derivation bug** (MOD4U): CTC channel ki pair hamesha
   `(channel_start, channel_start+1)` maani jaati thi - MOD2U mein theek
   tha (sirf ek hi pair), lekin MOD4U ke 2nd pair (Channel 3-4) ka CTC
   isi wajah se galat physical channels use karta. Ab har CTC entity
   apni khud ki pair derive karti hai.
3. **CTC same-Area disambiguation** (MOD4U): agar 2 Single-Driver CTC
   pairs same Area mein configured hon, ab incoming frames sahi pair se
   match hote hain (wire ke real channel number se), pehle jo bhi milta
   wahi return nahi hota.
4. **Curtain - dono pairs confirmed** (MOD4U): real MOD4U capture
   (Area 7) se ab Pair 1 **aur** Pair 2 dono ke curtain open/close/stop
   bytes confirmed hain (pehle sirf Pair 1 tha, aur wo bhi ek MOD2U
   capture se guess kiya gaya tha jo galat nikla - real MOD4U byte
   layout alag hai: pair-marker byte 0x02/0x03).
5. Event-loop-blocking disk I/O (learned-channel save/load) background
   thread mein move kiya gaya - slow SD-card/Pi storage par bhi poora HA
   freeze nahi hota.
6. Duplicate/overlapping TCP connections (jab read-error, write-error,
   aur periodic-resync ek saath trigger ho jaayein) ek connect-lock se
   roke gaye - device khud confuse ho kar atakta tha, isse bachne ke
   liye.
7. **BR40 log spam fix**: "BR40 auto-discovery nahi hui" WARNING pehle
   HAR periodic resync (~45s) par bhi dobara-dobara aati thi, hamesha ke
   liye (chahe device bilkul theek chal raha ho) - kyunki MOD2U/MOD4U/
   MOD2F kisi ka bhi `br40_code` set nahi hai aur ye devices `?BR40=`
   query ka jawab `+BR40=` se kabhi nahi dete (sirf khud-ba-khud
   `+AR40=` heartbeat bhejte hain). Ab ye message connection ke lifetime
   mein sirf ek baar WARNING mein aati hai, uske baad DEBUG mein.
8. **MOD2F support added**: naya "single-channel, fixed Fan type" model.
   Wire-protocol MOD2U/MOD4U jaisa hi hai (`00 1A <area> <level> <channel>`,
   level 01=off/02-05=speed 1-4), isliye `protocol.py` mein koi naya
   command-code chahiye hi nahi tha - sirf `const.py` mein ek naya model
   entry aur `config_flow.py`/`__init__.py` mein "iske liye Type dropdown
   mat dikhao, type hamesha Fan hai" wala chhota sa special-case.
9. **THE STARTUP-SLOW ROOT CAUSE - duplicate config entries same device
   par**: config-flow ka `unique_id` pehle non-deterministic tha - agar
   device add karte waqt uska `*KA=` line 5-second validation window ke
   andar mil jaata to `unique_id` = mac-jaisi string banti, warna sirf
   raw host string. Isi wajah se same physical device (same IP:port) ko
   agar kabhi dobara add karne ki koshish hoti (ya purani MOD2U/MOD4U
   standalone integration se migrate karte waqt), to dono attempt ka
   `unique_id` ALAG ban sakta tha - matlab duplicate-detection
   (`_abort_if_unique_id_configured`) is duplicate ko pakadta hi nahi
   tha, aur DO config entries usi ek IP par ban jaate the. Device agar
   ek time par sirf ek hi TCP client accept karta hai (jaisa lag raha
   hai), to dono entries ek-doosre se connection ke liye "fight" karte -
   yahi HA startup slow/flaky hone ki asli wajah thi. Ab `unique_id`
   hamesha sirf `host:port` se deterministically banta hai - same
   IP:port ka doosra "Add" hamesha turant "already configured" bolke
   abort ho jayega.

   **IMPORTANT**: ye fix sirf AAGE se duplicate banne se rokta hai -
   agar already 2 entries same IP ke liye ban chuki hain, unhe khud
   Settings -> Devices & Services mein dhundh kar (jaise do baar
   "Raylogic MOD2U 192.168.1.34" dikhega) extra wali delete karni
   padegi.

## MOD2F

Ek dedicated single-channel Fan module - Raylogic GO app mein iske liye
"Select Type" screen hi nahi hota, module hamesha Fan hi hota hai. Isliye
HA config flow mein bhi is model ke liye koi Channel Type dropdown nahi
dikhta, bas:

- **Area** - jo device app mein "Area" field mein dikhta hai (e.g. `4`).
- **First Channel Number** - device app ke "Start Address" jitna hi
  (e.g. `9`, wahi jo *AR= frame mein channel byte ke liye use hota hai).

Area `0` (auto-learn) MOD2F par kaam nahi karta (sirf Relay devices ke
liye hai) - config flow khud is combination ko block kar deta hai.

## Learn mode (Relay only, Area = 0)

Agar Area `0` par chhod do aur channel Relay hai, HA passively `*AR=`
frame sunta hai (jab aap app/physical switch se toggle karte ho), Area
seekh leta hai, aur entity turant bana deta hai (restart nahi chahiye).
Dimmer/Fan/Curtain/CTC ke liye ye kaam NAHI karta - unke liye Area
manually dena zaroori hai.

## Repairs

Agar kisi pair ke curtain bytes confirm nahi hain (future device variant
mein), entity nahi banti aur Settings > Repairs mein ek issue dikhta hai
jisme exact steps hain ki kaise capture karke share karo.
