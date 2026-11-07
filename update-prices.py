#!/usr/bin/env python3
"""
Updates prices/prices.json from one of two sources:

  AHledger (default) — fetches live AH data via the AHledger public API.
  Auctionator        — parses a local prices/Auctionator.lua SavedVariables file.

Usage:
    python update-prices.py [market_id]           # AHledger (default market)
    python update-prices.py --markets             # list AHledger markets

    python update-prices.py auctionator           # parse Auctionator.lua (auto-select realm)
    python update-prices.py auctionator --realms  # list realms in Auctionator.lua
    python update-prices.py auctionator --realm "RealmName-Faction"
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

API_BASE   = "https://api.ahledger.com/v1"
DATA_DIR   = Path(__file__).parent / "data"
PRICES_DIR = Path(__file__).parent / "prices"
PRICES_OUT = PRICES_DIR / "prices.json"
AUCTIONATOR_LUA = PRICES_DIR / "Auctionator.lua"

DEFAULT_MARKET = "forever.normal.horde.us"


def fetch(path: str) -> bytes:
    req = Request(
        f"{API_BASE}{path}",
        headers={"User-Agent": "WowProfessionAssistant/1.0"},
    )
    with urlopen(req, timeout=30) as resp:
        return resp.read()


def list_markets() -> None:
    data = json.loads(fetch("/markets"))
    forever = [m for m in data["markets"] if m["game"] == "forever"]
    other   = [m for m in data["markets"] if m["game"] != "forever"]
    print("WoW: Forever markets:")
    for m in forever:
        print(f"  {m['id']:<40}  {m['label']}")
    if other:
        print("\nOther markets:")
        for m in other:
            print(f"  {m['id']:<40}  {m['label']}")


def build_name_lookup() -> dict[str, str]:
    """
    Load all profession JSON files from data/ and merge their item name
    mappings into a single {item_id_str: name} dict.
    """
    names: dict[str, str] = {}
    for path in DATA_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        for item_id, info in data.get("items", {}).items():
            if info.get("name") and item_id not in names:
                names[item_id] = info["name"]
    return names


def fetch_prices(market: str) -> tuple[dict[str, int], int]:
    """
    Fetch the bulk pricetable for `market` and return
    ({item_id_str: min_buyout_copper}, scan_timestamp).

    Pricetable format (plain text):
      Line 1 header : AHL1|{market}|{unix_ts}|{item_count}
      Data rows     : {item_id}:{minBuyout}:{median}:{quantity}:{auctions}:{median7d}:{median30d}:{median90d}
    """
    raw = fetch(f"/pricetable/{market}").decode()
    lines = raw.strip().splitlines()

    if not lines or not lines[0].startswith("AHL1|"):
        raise ValueError(f"Unexpected pricetable format: {lines[0][:80]!r}")

    header_parts = lines[0].split("|")
    scan_ts    = int(header_parts[2]) if len(header_parts) >= 3 else 0
    item_count = int(header_parts[3]) if len(header_parts) >= 4 else "?"
    print(f"Market : {market}")
    print(f"Items  : {item_count}")

    prices: dict[str, int] = {}
    for line in lines[1:]:
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 2:
            continue
        item_id    = parts[0]
        min_buyout = int(parts[1])
        if min_buyout > 0:
            prices[item_id] = min_buyout

    return prices, scan_ts


def enrich_with_names(prices: dict[str, int], names: dict[str, str]) -> dict[str, dict]:
    """Combine price and name into a single entry per item."""
    return {
        item_id: {"name": names.get(item_id, ""), "price": price}
        for item_id, price in prices.items()
    }


# ---------------------------------------------------------------------------
# Auctionator source
# ---------------------------------------------------------------------------

_LUA_ESCAPES = {
    "n": 10, "r": 13, "t": 9, "a": 7, "b": 8, "f": 12, "v": 11,
    "\\": 92, '"': 34, "'": 39, "\n": 10,
}


def _read_lua_string(data: bytes, start: int) -> tuple[bytes, int]:
    """Read a double-quoted Lua string literal beginning at data[start] == '"'.
    Returns (decoded_bytes, index_after_closing_quote)."""
    assert data[start] == 0x22
    out = bytearray()
    i = start + 1
    while i < len(data):
        c = data[i]
        if c == 0x22:  # closing quote
            return bytes(out), i + 1
        if c == 0x5C:  # backslash
            i += 1
            nxt = chr(data[i])
            if nxt in _LUA_ESCAPES:
                out.append(_LUA_ESCAPES[nxt])
                i += 1
            elif nxt.isdigit():  # \ddd decimal escape (1-3 digits)
                j = i
                while j < len(data) and j < i + 3 and chr(data[j]).isdigit():
                    j += 1
                out.append(int(data[i:j]))
                i = j
            else:
                out.append(data[i])
                i += 1
            continue
        out.append(c)
        i += 1
    raise ValueError("Unterminated Lua string literal")


def _cbor_decode(buf: bytes, pos: int = 0):
    """Minimal CBOR decoder (RFC 8949). Returns (value, new_pos)."""
    import struct

    ib = buf[pos]
    major, info = ib >> 5, ib & 0x1F
    pos += 1

    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        n = 1 << (info - 24)
        arg = int.from_bytes(buf[pos:pos + n], "big")
        pos += n
    elif info == 31:
        arg = None  # indefinite length
    else:
        raise ValueError(f"Unsupported CBOR additional info {info}")

    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if arg is None:
            chunks = b""
            while buf[pos] != 0xFF:
                chunk, pos = _cbor_decode(buf, pos)
                chunks += chunk if isinstance(chunk, bytes) else chunk.encode()
            pos += 1
            raw = chunks
        else:
            raw = buf[pos:pos + arg]
            pos += arg
        return (raw if major == 2 else raw.decode("utf-8", errors="replace")), pos
    if major == 4:
        items = []
        if arg is None:
            while buf[pos] != 0xFF:
                v, pos = _cbor_decode(buf, pos)
                items.append(v)
            pos += 1
        else:
            for _ in range(arg):
                v, pos = _cbor_decode(buf, pos)
                items.append(v)
        return items, pos
    if major == 5:
        result = {}
        count = 0
        while (arg is None and buf[pos] != 0xFF) or (arg is not None and count < arg):
            k, pos = _cbor_decode(buf, pos)
            v, pos = _cbor_decode(buf, pos)
            result[k] = v
            count += 1
        if arg is None:
            pos += 1
        return result, pos
    if major == 6:  # tag: ignore, decode the tagged value
        return _cbor_decode(buf, pos)
    # major 7: simple values and floats
    if info == 20:
        return False, pos
    if info == 21:
        return True, pos
    if info in (22, 23):
        return None, pos
    if info == 25:
        return struct.unpack(">e", buf[pos - 2:pos])[0], pos
    if info == 26:
        return struct.unpack(">f", buf[pos - 4:pos])[0], pos
    if info == 27:
        return struct.unpack(">d", buf[pos - 8:pos])[0], pos
    raise ValueError(f"Unsupported CBOR simple value {info}")


def parse_auctionator_lua(lua_path: Path) -> tuple[dict, list[str]]:
    """
    Parse AUCTIONATOR_PRICE_DATABASE from a SavedVariables Lua file.

    Auctionator (db version 8) stores each realm as a CBOR-serialized binary
    string: {db_key: {m=last_min_price, h={...}, l={...}, a={...}}}, where
    db_key is an item ID ("2589"), a pet ("p:123") or gear ("g:<id>:<ilvl>").

    Returns (realm_data, realm_names) where realm_data maps
    realm → {item_id_str: min_price_copper} (plain item IDs only).
    """
    import re

    data = lua_path.read_bytes()

    m = re.search(rb"^AUCTIONATOR_PRICE_DATABASE\s*=\s*\{", data, re.M)
    if not m:
        raise ValueError(f"AUCTIONATOR_PRICE_DATABASE not found in {lua_path}")
    end = re.compile(rb"^AUCTIONATOR_\w+\s*=", re.M).search(data, m.end())
    block_end = end.start() if end else len(data)

    realm_data: dict[str, dict[str, int]] = {}
    entry = re.compile(rb'\["([^"]+)"\]\s*=\s*"')
    pos = m.end()
    while True:
        em = entry.search(data, pos, block_end)
        if not em:
            break
        realm = em.group(1).decode("utf-8", errors="replace")
        blob, pos = _read_lua_string(data, em.end() - 1)
        decoded, _ = _cbor_decode(blob)
        if not isinstance(decoded, dict):
            continue
        # Lua strings are serialized as CBOR byte strings, so keys arrive as bytes.
        prices: dict[str, int] = {}
        for key, v in decoded.items():
            key = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
            if not key.isdigit() or not isinstance(v, dict):
                continue  # skip pets ("p:..."), gear ("g:..."), __dbversion
            price = v.get(b"m", v.get("m"))
            if price:
                prices[key] = int(price)
        realm_data[realm] = prices

    if not realm_data:
        raise ValueError("No realm data found in AUCTIONATOR_PRICE_DATABASE")
    return realm_data, list(realm_data.keys())


def auctionator_prices(realm_data: dict[str, dict[str, int]], realm: str) -> dict[str, int]:
    """Return {item_id_str: min_buyout_copper} for a realm (keys are already item IDs)."""
    return dict(realm_data[realm])


def main() -> None:
    args = sys.argv[1:]

    if "--markets" in args:
        list_markets()
        return

    source = args[0].lower().lstrip("-") if args else ""

    # --- Auctionator mode ---
    if source == "auctionator":
        if not AUCTIONATOR_LUA.exists():
            print(f"Error: {AUCTIONATOR_LUA} not found.")
            sys.exit(1)

        try:
            realm_data, realm_names = parse_auctionator_lua(AUCTIONATOR_LUA)
        except Exception as e:
            print(f"Parse error: {e}")
            sys.exit(1)

        if "--realms" in args:
            print("Realms found in Auctionator.lua:")
            for r in realm_names:
                print(f"  {r}")
            return

        realm_arg = None
        if "--realm" in args:
            idx = args.index("--realm")
            if idx + 1 < len(args):
                realm_arg = args[idx + 1]
            else:
                print("Error: --realm requires a value.")
                sys.exit(1)

        if realm_arg:
            if realm_arg not in realm_data:
                print(f"Error: realm {realm_arg!r} not found. Available: {realm_names}")
                sys.exit(1)
            realm = realm_arg
        elif len(realm_names) == 1:
            realm = realm_names[0]
        else:
            print(f"Multiple realms found: {realm_names}")
            print("Specify one with: python update-prices.py auctionator --realm \"<realm>\"")
            sys.exit(1)

        prices = auctionator_prices(realm_data, realm)
        names  = build_name_lookup()
        named  = sum(1 for iid in prices if names.get(iid))
        entries = enrich_with_names(prices, names)

        print(f"Realm  : {realm}")
        print(f"Items  : {len(entries)} priced items in Lua")

        output = {
            "source":     "auctionator",
            "market":     realm,
            "url":        str(AUCTIONATOR_LUA),
            "scan_at":    None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "prices":     entries,
        }

        PRICES_OUT.write_text(json.dumps(output, indent=2))
        print(f"Saved {len(entries)} prices ({named} with names) → {PRICES_OUT}")
        return

    # --- AHledger mode ---
    market = args[0] if args else DEFAULT_MARKET

    try:
        prices, scan_ts = fetch_prices(market)
    except URLError as e:
        print(f"Error fetching prices: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"Parse error: {e}")
        sys.exit(1)

    names   = build_name_lookup()
    named   = sum(1 for iid in prices if names.get(iid))
    entries = enrich_with_names(prices, names)

    scan_at = datetime.fromtimestamp(scan_ts, tz=timezone.utc).isoformat() if scan_ts else None

    output = {
        "source":     "ahledger",
        "market":     market,
        "url":        f"{API_BASE}/pricetable/{market}",
        "scan_at":    scan_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "prices":     entries,
    }

    PRICES_OUT.write_text(json.dumps(output, indent=2))
    print(f"Saved {len(entries)} prices ({named} with names) → {PRICES_OUT}")


if __name__ == "__main__":
    main()
