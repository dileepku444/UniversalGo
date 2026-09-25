"""LAN discovery for Raylogic MOD2U / MOD4U / MOD2F modules (v1.6.0).

Raylogic modules koi standard discovery (mDNS/SSDP/broadcast) nahi karte -
reference "raylogic" integration ke live captures me confirm hua. Lekin har
module TCP 5550 par sunta hai aur connect hote hi apna "*KA=" identity frame
khud bhejta hai. Isliye subnet ke har host par ek chhota TCP connect karke
jo Raylogic frame bheje wahi "hit" hai.

MOD ka *KA= payload apna Area aur channel range bhi batata hai (wahi decode
jo protocol._parse_ka_identity use karta hai), isliye scan hit se model
(2 ch = MOD2U, 4 ch = MOD4U, 1 ch = MOD2F), Area aur First Channel Number
bhi suggest ho jaate hain.

Safety: jo hosts pehle se HA me configured hain (raylogic_mod YA reference
"raylogic" integration me) unhe scan chhoota hi nahi - taaki live
connection se koi takraav na ho. Scan sirf user ke kehne par (Add device ->
Scan network) chalta hai, background me kabhi nahi.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket

from .const import (
    AREA_MAX, AREA_MIN, CLOSE_TIMEOUT, DEFAULT_PORT, DEVICE_MODELS,
)

_LOGGER = logging.getLogger(__name__)

# Sirf asli Raylogic frame hit maana jaata hai: "<node>,*KA=..." (keepalive,
# bina message number) ya "<node>,<msg>,*AR=..." jaisa. 5550 par koi aur
# appliance ho to ignore.
_FRAME_RE = re.compile(r"^(\d{1,3}),(?:(\d+),)?([*+][A-Z]{2}\d{0,2}=)(.*)$")

CONNECT_TIMEOUT = 1.5     # LAN host SYN ka jawab isse kaafi pehle deta hai
# Idle module har ~6s me *KA= bhejta hai - usse thoda zyada ruko; KA aate hi
# turant ruk jaate hain.
BANNER_TIMEOUT = 7.5
MAX_CONCURRENCY = 64      # 254 hosts ~8s me
MAX_HOSTS = 1024          # galti se bhi /22 se bada scan nahi

_COUNT_TO_MODEL = {
    info["channel_count"]: key for key, info in DEVICE_MODELS.items()
}


def decode_mod_ka(payload: str) -> dict | None:
    """MOD *KA= payload -> {"area", "start", "end"} (ya None).

        *KA=<xx>-<ctr:3><n:1><AREA:2h><01><0><START:2h><END:2h>0000
        e.g. *KA=21-05421001001020000 -> area 16, channels 1-2 (MOD2U)

    protocol._parse_ka_identity jaisa hi layout/validation."""
    if "-" not in payload:
        return None
    body = payload.split("-", 1)[1].strip()
    try:
        if len(body) < 17 or body[6:8] != "01":
            return None
        area = int(body[4:6], 16)
        start = int(body[9:11], 16)
        end = int(body[11:13], 16)
    except (ValueError, IndexError):
        return None
    if not (AREA_MIN <= area <= AREA_MAX and 1 <= start <= end <= 255):
        return None
    return {"area": area, "start": start, "end": end}


def parse_banner(lines: list[str]) -> dict | None:
    """Connect ke baad module ke pehle frames -> hit dict, ya None agar
    koi bhi Raylogic frame nahi."""
    info: dict = {"node": None, "area": None, "channel_start": None,
                  "channel_count": None, "model": None}
    hit = False
    for raw in lines:
        m = _FRAME_RE.match(raw.strip())
        if not m:
            continue
        hit = True
        node, _msg, cmd, payload = m.groups()
        info["node"] = info["node"] or node
        if cmd == "*KA=" and info["area"] is None:
            ka = decode_mod_ka(payload)
            if ka:
                info["area"] = ka["area"]
                info["channel_start"] = ka["start"]
                info["channel_count"] = ka["end"] - ka["start"] + 1
                info["model"] = _COUNT_TO_MODEL.get(info["channel_count"])
    return info if hit else None


async def async_probe_host(host: str, port: int = DEFAULT_PORT,
                           connect_timeout: float = CONNECT_TIMEOUT,
                           banner_timeout: float = BANNER_TIMEOUT) -> dict | None:
    """Ek baar connect, module jo bheje wo padho, band karo. None = Raylogic nahi."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), connect_timeout)
    except Exception:
        return None
    lines: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + banner_timeout
    buf = b""
    try:
        while len(lines) < 12 and len(buf) < 4096:
            # "\r", "\n" ya "\r\n" - protocol._read_frame jaisa (v1.6.4)
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()               # adhoori line
            new = [p.decode(errors="replace").strip() for p in parts]
            lines.extend(x for x in new if x)
            if any("*KA=" in x for x in new):
                break   # identity frame mil gaya
            left = deadline - loop.time()
            if left <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(1024), left)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
    finally:
        # Close bhi hard-capped (config_flow.validate_connection jaisa) -
        # device FIN na bheje to scan atakna nahi chahiye.
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), float(CLOSE_TIMEOUT))
        except Exception:
            pass
    info = parse_banner(lines)
    if info is None:
        return None
    info.update({"host": host, "port": port})
    return info


def hosts_in(subnets: list[str]) -> list[str]:
    """CIDR strings -> host addresses (dedup, capped, sirf IPv4)."""
    out: list[str] = []
    seen: set[str] = set()
    for cidr in subnets:
        try:
            net = ipaddress.ip_network(str(cidr).strip(), strict=False)
        except ValueError:
            continue
        if net.version != 4 or net.num_addresses > MAX_HOSTS + 2:
            continue
        hosts = list(net.hosts()) or [net.network_address]
        for ip in hosts:
            s = str(ip)
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _fallback_subnet() -> list[str]:
    """Default-route wale interface ka /24 (UDP 'connect' koi packet nahi
    bhejta, sirf source address chunta hai)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 53))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return [str(ipaddress.ip_network(f"{ip}/24", strict=False))]
    except Exception:
        return []


async def async_local_subnets(hass) -> list[str]:
    """HA ke har enabled IPv4 adapter ka CIDR (bahut bada LAN ho to /24)."""
    subnets: list[str] = []
    try:
        from homeassistant.components.network import async_get_adapters
        for adapter in await async_get_adapters(hass):
            if not adapter.get("enabled"):
                continue
            for ipv4 in adapter.get("ipv4", []):
                addr = ipv4.get("address", "")
                prefix = int(ipv4.get("network_prefix", 24) or 24)
                if not addr or addr.startswith("127."):
                    continue
                if prefix < 22:
                    prefix = 24
                net = str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
                if net not in subnets:
                    subnets.append(net)
    except Exception:
        pass
    if not subnets:
        subnets = await hass.async_add_executor_job(_fallback_subnet)
    return subnets


OTHER_DOMAIN = "raylogic"   # main/DIN integration - sirf padhte hain


def _entry_hosts(hass, domain: str) -> set[str]:
    hosts: set[str] = set()
    for e in hass.config_entries.async_entries(domain):
        h = {**e.data, **e.options}.get("host")
        if h:
            hosts.add(str(h).strip())
    return hosts


def other_integration_hosts(hass) -> set[str]:
    """'raylogic' (main) integration me configured IPs. Same module dono
    integrations me ho to do TCP connections ek hi device par khulte hain."""
    return _entry_hosts(hass, OTHER_DOMAIN)


def configured_hosts(hass) -> set[str]:
    """Jo IPs pehle se HA me hain - raylogic_mod AUR 'raylogic' dono ke.
    Scan inse kabhi connect nahi karta."""
    return _entry_hosts(hass, "raylogic_mod") | other_integration_hosts(hass)


def ignored_hosts(hass) -> set[str]:
    """HA ke "Discovered" card par user ne "Ignore" dabaya ho to entry
    source=ignore ke saath bachti hai (bina data ke) - host uske unique_id
    ("<host>_<port>") se nikalte hain, taaki auto-scan use dobara chhue bhi
    nahi."""
    hosts: set[str] = set()
    for e in hass.config_entries.async_entries("raylogic_mod", include_ignore=True):
        if e.source == "ignore" and e.unique_id and "_" in e.unique_id:
            hosts.add(e.unique_id.rsplit("_", 1)[0])
    return hosts


async def async_auto_subnets(hass, max_subnets: int = 4) -> list[str]:
    """Background scan ke subnets - user ko kuch type nahi karna padta:
      1. HA ke apne enabled IPv4 adapters (async_local_subnets jaisa)
      2. har configured module (raylogic_mod + raylogic) ka /24 - modules
         kisi doosre VLAN par hon jahan HA route karta hai, to bhi cover
    Duplicates hataye, max `max_subnets`, total hosts <= MAX_HOSTS."""
    subnets = list(await async_local_subnets(hass))
    for h in sorted(configured_hosts(hass)):
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            continue    # hostname - skip
        if ip.version != 4 or ip.is_loopback:
            continue
        net = str(ipaddress.ip_network(f"{h}/24", strict=False))
        if net not in subnets:
            subnets.append(net)
    out: list[str] = []
    total = 0
    for net in subnets:
        n = len(hosts_in([net]))
        if not n or len(out) >= max_subnets or total + n > MAX_HOSTS:
            continue
        out.append(net)
        total += n
    return out


async def async_scan(subnets: list[str], *, port: int = DEFAULT_PORT,
                     skip: set[str] | None = None,
                     concurrency: int = MAX_CONCURRENCY) -> list[dict]:
    """Subnets ke har host ko probe karo; Raylogic hits IP order me."""
    skip = skip or set()
    targets = [h for h in hosts_in(subnets) if h not in skip]
    sem = asyncio.Semaphore(concurrency)

    async def one(host: str) -> dict | None:
        async with sem:
            return await async_probe_host(host, port)

    results = await asyncio.gather(*(one(h) for h in targets),
                                   return_exceptions=True)
    hits = [r for r in results if isinstance(r, dict)]
    hits.sort(key=lambda r: ipaddress.ip_address(r["host"]))
    _LOGGER.debug("Raylogic MOD scan of %s: %d hosts, %d hits",
                  subnets, len(targets), len(hits))
    return hits


def describe(hit: dict) -> str:
    """Pick-list ke liye ek line: IP, node, model, area, channels."""
    parts = [hit["host"]]
    if hit.get("node"):
        parts.append(f"node {hit['node']}")
    model = hit.get("model")
    parts.append(DEVICE_MODELS[model]["name"] if model else "unknown model")
    if hit.get("area") is not None:
        start = hit["channel_start"]
        end = start + hit["channel_count"] - 1
        parts.append(f"area {hit['area']}, ch {start}-{end}")
    return " - ".join(parts)

