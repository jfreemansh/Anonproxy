"""
Deterministic, format-preserving surrogate generation.

Design goals (these directly drive round-trip reliability):

1. **Format preserving.**  A surrogate is a *valid instance of the same type*:
   a hash surrogate is hex of the same length, an AWS key keeps its ``AKIA``
   prefix, an IP is a syntactically valid (but non-routable) address.  The model
   then treats it as the real thing and has no reason to "fix" or reformat it.

2. **Opaque body charset.**  Surrogate bodies use only ``[A-Za-z0-9]`` plus the
   structural separators the type naturally contains (``. _ - : @ /``).  No
   spaces inside single-token surrogates, no markdown-bait characters — so the
   restorer can match them back even after the model wraps them in ``**`` or
   backticks.

3. **Deterministic.**  Derived by HMAC keyed on the engagement id, so the same
   original always yields the same surrogate — even if the vault file is lost or
   the proxy restarts mid-engagement.  Consistency is structural, not just cached.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re

_HEX = "0123456789abcdef"
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"

# Small pools so PERSON / ORGANIZATION surrogates read like real names.
_FIRST = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Quinn", "Avery",
    "Parker", "Reese", "Skyler", "Rowan", "Hayden", "Emerson", "Finley", "Sage",
]
_LAST = [
    "Carter", "Bennett", "Harper", "Foster", "Reyes", "Brooks", "Sutton",
    "Nolan", "Pierce", "Dalton", "Mercer", "Sloane", "Vance", "Whitaker",
    "Lowell", "Ashford",
]
_ORG_A = ["Vertex", "Northwind", "Aether", "Cobalt", "Lumen", "Granite",
          "Solstice", "Meridian", "Helix", "Cardinal", "Onyx", "Pinnacle"]
_ORG_B = ["Systems", "Dynamics", "Holdings", "Labs", "Industries", "Group",
          "Technologies", "Networks", "Solutions", "Partners"]

# RFC 5737 documentation/TEST-NET ranges — guaranteed non-routable.
_TESTNETS = [
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
]
_DOC_V6 = ipaddress.ip_network("2001:db8::/32")


def _stream(key: str, *parts: str) -> bytes:
    """A deterministic, effectively unbounded byte stream from a keyed seed."""
    seed = hmac.new(key.encode(), "\x00".join(parts).encode(), hashlib.sha256).digest()
    out = bytearray(seed)
    counter = 0
    while True:
        # extend lazily; callers slice what they need
        if len(out) < 4096:
            counter += 1
            out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        return bytes(out)


def _pick(stream: bytes, alphabet: str, n: int, offset: int = 0) -> str:
    return "".join(alphabet[stream[offset + i] % len(alphabet)] for i in range(n))


def _int(stream: bytes, offset: int = 0, width: int = 4) -> int:
    return int.from_bytes(stream[offset:offset + width], "big")


def generate(entity_type: str, original: str, *, engagement: str = "default", salt: str = "") -> str:
    """Return a deterministic, format-preserving surrogate for ``original``.

    ``salt`` lets the vault break a rare surrogate collision by requesting an
    alternate deterministic value for the same original.
    """
    t = (entity_type or "OTHER").upper()
    s = _stream(engagement, t, original, salt)

    if t == "IP_ADDRESS":
        return _ip(original, s)
    if t == "CIDR":
        return _cidr(original, s)
    if t == "IPV6" or (t == "IP_ADDRESS" and ":" in original):
        return _ipv6(s)
    if t == "HASH":
        return _hash_like(original, s)
    if t == "MAC_ADDRESS":
        return _mac(s)
    if t == "TOKEN":
        return _token_like(original, s)
    if t == "EMAIL_ADDRESS":
        return f"{_pick(s, _LOWER, 6)}@{_pick(s, _LOWER, 7, 6)}.example"
    if t == "DOMAIN":
        return _domain(original, s)
    if t == "URL":
        return _url(original, s)
    if t == "HOSTNAME":
        return _hostname(original, s)
    if t == "USERNAME":
        return f"u{_pick(s, _LOWER + _DIGITS, 6)}"
    if t == "PERSON":
        return f"{_FIRST[_int(s) % len(_FIRST)]} {_LAST[_int(s, 4) % len(_LAST)]}"
    if t == "ORGANIZATION":
        return f"{_ORG_A[_int(s) % len(_ORG_A)]} {_ORG_B[_int(s, 4) % len(_ORG_B)]}"
    if t == "PAYMENT_CARD":
        return _payment_card(original, s)
    if t == "CREDENTIAL":
        # Looks like a password (so the model still treats it as a secret) but is
        # all-alnum + a couple of safe symbols, so it survives a round trip.
        body = _pick(s, _ALNUM, 10)
        return f"Pw{body}!7"
    if t == "PATH":
        return _path(original, s)
    if t == "IDENTIFIER":
        return _pick(s, _ALNUM, max(8, min(len(original), 24)))
    # OTHER / fallback
    return _pick(s, _ALNUM, max(6, min(len(original), 16)))


# ---------------------------------------------------------------------------
# Type-specific builders
# ---------------------------------------------------------------------------
def _ip(original: str, s: bytes) -> str:
    try:
        ip = ipaddress.ip_address(original)
    except ValueError:
        ip = None
    if ip is not None and ip.version == 6:
        return _ipv6(s)
    net = _TESTNETS[_int(s) % len(_TESTNETS)]
    host = 1 + (_int(s, 4) % 253)  # .1 .. .254
    return str(net.network_address + host)


def _ipv6(s: bytes) -> str:
    suffix = ":".join(_pick(s, _HEX, 4, off) for off in (0, 4, 8, 12))
    return f"2001:db8:{suffix}"


def _cidr(original: str, s: bytes) -> str:
    prefix = original.split("/")[-1] if "/" in original else "24"
    net = _TESTNETS[_int(s) % len(_TESTNETS)]
    return f"{net.network_address}/{prefix}"


def _hash_like(original: str, s: bytes) -> str:
    """Preserve length and case-style of the hash so its type stays recognizable.

    NTLM/LM hashes are often ``LM:NT`` — keep the colon structure too.
    """
    if ":" in original and re.fullmatch(r"[0-9a-fA-F:]+", original):
        return ":".join(_hash_like(part, _stream("x", part)) for part in original.split(":"))
    upper = original.isupper()
    n = len(original)
    out = _pick(s, _HEX, n)
    return out.upper() if upper else out


def _mac(s: bytes) -> str:
    # locally-administered, unicast (second-least-significant bit of first octet set)
    first = (s[0] & 0xFC) | 0x02
    octets = [first] + [s[i + 1] for i in range(5)]
    return ":".join(f"{o:02x}" for o in octets)


_TOKEN_PREFIXES = ["AKIA", "ASIA", "ghp_", "gho_", "sk_live_", "sk_test_",
                   "pk_live_", "xoxb-", "xoxp-", "glpat-", "AIza"]


def _token_like(original: str, s: bytes) -> str:
    """Keep a recognizable prefix (so the model knows it's an AWS/GitHub/Stripe
    key) and a same-length opaque body."""
    # JWT: three base64url segments separated by dots
    if original.count(".") == 2 and original.startswith("eyJ"):
        seg = lambda n, off: _pick(s, _ALNUM, n, off)  # noqa: E731
        return f"eyJ{seg(16, 0)}.{seg(20, 16)}.{seg(20, 36)}"
    for p in _TOKEN_PREFIXES:
        if original.startswith(p):
            body_len = max(8, len(original) - len(p))
            return p + _pick(s, _ALNUM, body_len)
    return _pick(s, _ALNUM, max(12, len(original)))


def _hostname(original: str, s: bytes) -> str:
    # preserve UPPER vs lower styling common to NetBIOS names
    body = _pick(s, _LOWER + _DIGITS, 7)
    host = f"host-{body}"
    return host.upper() if original.isupper() else host


def _domain(original: str, s: bytes) -> str:
    labels = original.split(".")
    if len(labels) <= 1:
        return f"{_pick(s, _LOWER, 8)}.example"
    # keep the same number of labels; replace all but a synthetic public suffix
    n_sub = len(labels) - 1
    subs = [_pick(s, _LOWER, 6, i * 3) for i in range(n_sub)]
    # if the original was an internal TLD (.local/.corp/.lan) keep that flavor
    internal = labels[-1].lower() in ("local", "corp", "lan", "internal", "intra")
    tld = "pentest.local" if internal else "example"
    if internal:
        return ".".join(subs) + ".pentest.local"
    return ".".join(subs) + "." + tld


def _url(original: str, s: bytes) -> str:
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/?#]+)(.*)$", original)
    if not m:
        return _domain(original, s)
    scheme, authority, rest = m.groups()
    # split optional userinfo / port
    host = authority
    port = ""
    userinfo = ""
    if "@" in host:
        userinfo, host = host.split("@", 1)
        userinfo = f"u{_pick(s, _LOWER, 4)}@"
    if ":" in host and not host.startswith("["):
        host, port = host.split(":", 1)
        port = ":" + port  # keep the real port — it's protocol info, not identity
    new_host = _domain(host, s)
    # neutralize identifying path/query but keep the shape minimal
    rest_clean = rest.split("?")[0].split("#")[0]
    return f"{scheme}{userinfo}{new_host}{port}{rest_clean}"


def _payment_card(original: str, s: bytes) -> str:
    """A Luhn-valid surrogate card: same digit count and separator layout, so the
    model still treats it as a card number (and a second pass re-detects it)."""
    digit_count = sum(c.isdigit() for c in original)
    arr = [int(c) for c in _pick(s, _DIGITS, digit_count)]
    parity = len(arr) % 2

    def _total(a: list[int]) -> int:
        t = 0
        for i, d in enumerate(a):
            if i % 2 == parity:
                d *= 2
                if d > 9:
                    d -= 9
            t += d
        return t

    for last in range(10):           # fix the final digit so Luhn passes
        arr[-1] = last
        if _total(arr) % 10 == 0:
            break
    digits = "".join(str(d) for d in arr)

    out, di = [], 0
    for c in original:               # reapply the original spacing/dashes
        if c.isdigit():
            out.append(digits[di])
            di += 1
        else:
            out.append(c)
    return "".join(out)


def _path(original: str, s: bytes) -> str:
    sep = "\\" if "\\" in original else "/"
    parts = original.split(sep)
    out = []
    for i, p in enumerate(parts):
        if not p or p in (".", "..", "~") or (len(p) == 2 and p.endswith(":")):
            out.append(p)  # keep drive letters / separators / relative markers
        else:
            out.append(_pick(s, _LOWER, min(max(len(p), 4), 10), i))
    return sep.join(out)
