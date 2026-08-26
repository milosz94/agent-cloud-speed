"""Strip credentials from agent session transcripts so the sessions can ship as auditable evidence.

Why this exists. Part 2's falsifiability rests on a self-reported negative ("none of these observations
appeared in the development sessions"). Shipping the sessions themselves makes that claim checkable -- but
a deploy session is full of generated app credentials (DB passwords, admin secrets, API tokens) and the
cloud OAuth bearer. This module removes those while preserving everything the reference core reads.

The preservation contract. Redaction touches only string VALUES that carry a secret. It never changes a
JSON key, a number, a timestamp, or the message shape, so a redacted transcript reconstructs the SAME
owner-labelled trace and the SAME token totals as the original (transcript.py reads structure and numbers,
never the secret strings). test_redact.py asserts this on a real-shaped transcript.

The failure mode is deliberately OVER-redaction. A stripper that lets one secret through is a leak, so an
ambiguous key-name match is redacted rather than kept. After writing, ``scan_for_secrets`` re-checks the
OUTPUT and the bundler fails loud on any residue -- the guard against a redactor that silently covered only
part of its input.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any, Dict, List, Tuple

_MARK = "REDACTED"  # every placeholder contains this token; scan_for_secrets treats it as clean


def _ph(cat: str) -> str:
    return "[%s:%s]" % (_MARK, cat)


# -- Pattern layer: secrets recognizable by their own shape, anywhere in a string ------------------
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)
_SSH = re.compile(r"\b(ssh-(?:rsa|ed25519|dss))\s+[A-Za-z0-9+/]{40,}={0,3}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}")
_VENDOR = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{16,}|sk_live_[A-Za-z0-9]{16,}"
    r"|rk_live_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
_CONN = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/@\s]+:)([^@/\s]{1,})(@)")

# env / inline assignment: KEY=value or KEY: value, quoted or not. Key must END in a secret word so
# POSTGRES_PASSWORD / MINIO_ROOT_PASSWORD / JWT_SECRET match but OUTPUT_TOKENS (count) does not.
_SECRET_WORD = (r"(?:PASSWORD|PASSWD|SECRET|SECRET_KEY|TOKEN|APIKEY|API_KEY|ACCESS_KEY|PRIVATE_KEY|"
                r"ENCRYPTION_KEY|CLIENT_SECRET|REFRESH_TOKEN|ACCESS_TOKEN|AUTH_TOKEN|APP_KEY|"
                r"DB_PASS(?:WORD)?|PASSPHRASE|CREDENTIAL|SALT)")
# allow underscore-delimited prefix (POSTGRES_) and suffix (_BASE, _ID) segments so POSTGRES_PASSWORD,
# SECRET_KEY_BASE and ACCESS_KEY_ID all match, while the trailing "S" of OUTPUT_TOKENS still breaks it.
_KEYNAME = r"((?:[A-Za-z0-9]+_)*" + _SECRET_WORD + r"(?:_[A-Za-z0-9]+)*)"
_ASSIGN = re.compile(r"(?i)" + _KEYNAME + r"(\s*[:=]\s*)('|\")?([^\s'\"`,;&\\)}\]]{2,})")

# -- Key layer: a JSON key whose NAME is a secret; its string value is redacted whole ---------------
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|_)(?:password|passwd|secret|token|apikey|api_?key|access_?key|private_?key|"
    r"encryption_?key|client_?secret|refresh_?token|access_?token|auth_?token|app_?key|"
    r"passphrase|credential|salt|authorization)(?:$|_)"
)
# structural token-count keys are never secrets; string-only redaction already protects their int
# values, but skip them by name too for defense in depth.
_SKIP_KEYS = {"input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens", "total_tokens"}


def _sensitive_key(name: str) -> bool:
    if name in _SKIP_KEYS:
        return False
    return bool(_SENSITIVE_KEY.search(name))


def _assign_sub(m: "re.Match") -> str:
    value = m.group(4)
    if _MARK in value:  # already redacted: leave it (keeps redaction idempotent)
        return m.group(0)
    return "%s%s%s%s" % (m.group(1), m.group(2), m.group(3) or "", _ph("value"))


def redact_text(s: str) -> Tuple[str, Counter]:
    """Redact secrets recognizable inside a free string (shell output, an env line, a bearer header).

    Specific shapes first, generic KEY=value last. Returns the redacted string and a category counter.
    """
    counts: Counter = Counter()
    for cat, rx, repl in (
        ("private-key", _PEM, _ph("private-key")),
        ("ssh-key", _SSH, r"\1 " + _ph("ssh-key")),
        ("jwt", _JWT, _ph("jwt")),
        ("bearer", _BEARER, r"\1" + _ph("bearer")),
        ("vendor-token", _VENDOR, _ph("vendor-token")),
        ("conn-password", _CONN, r"\1" + _ph("conn-password") + r"\3"),
    ):
        s, n = rx.subn(repl, s)
        if n:
            counts[cat] += n
    s, n = _ASSIGN.subn(_assign_sub, s)
    if n:
        counts["value"] += n
    return s, counts


# -- Substrate layer: hide HOW the cloud is built, keep WHAT the agent did --------------------------
# The public bundle shows the agent's actions, the app it deployed and the timing, not the provider's
# implementation. It removes infrastructure technology names, control-plane hostnames and internal IPs,
# keeping the deployed app URLs, the agent's tool calls and the platform brand. Applied only to string
# values, like credentials, so timing and structure are preserved.
#
# The actual list of infrastructure names/hosts is SITE-SPECIFIC and is itself a disclosure of the very
# setup we are hiding, so it is NOT in this file. It is loaded from a private ``_substrate_rules.py``
# (gitignored) or from the path in ``$ACSPEED_SUBSTRATE_RULES``. Without a rules file only generic IP
# scrubbing runs; the shipped sessions were already scrubbed with the private list at bundle time. See
# ``_substrate_rules.example.py`` for the format. IP scrubbing below is generic and reveals nothing.
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_IP_KEEP = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "8.8.8.8", "1.1.1.1"}  # localhost/bind/broadcast/public DNS

# A compiled ruleset: (infra-words regex or None, [infra-pattern regexes], [host regexes]).
Rules = Tuple[Any, List[Any], List[Any]]


def compile_rules(words: List[str], patterns: List[str], hosts: List[str]) -> Rules:
    """Compile a substrate denylist. ``words`` are literal infra names (matched whole-word,
    case-insensitively); ``patterns`` and ``hosts`` are raw regex strings."""
    infra = re.compile(r"(?i)\b(?:%s)\b" % "|".join(re.escape(w) for w in words)) if words else None
    pats = [re.compile("(?i)(?:%s)" % p) for p in patterns]
    hres = [re.compile("(?i)(?:%s)" % h) for h in hosts]
    return infra, pats, hres


def _load_rules() -> Rules:
    mod = None
    try:
        from . import _substrate_rules as mod  # type: ignore
    except Exception:
        path = os.environ.get("ACSPEED_SUBSTRATE_RULES")
        if path and os.path.exists(path):
            import importlib.util
            spec = importlib.util.spec_from_file_location("_acspeed_substrate_rules", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
    if mod is None:
        return compile_rules([], [], [])
    return compile_rules(list(getattr(mod, "INFRA_WORDS", []) or []),
                         list(getattr(mod, "INFRA_PATTERNS", []) or []),
                         list(getattr(mod, "HOST_PATTERNS", []) or []))


_RULES = _load_rules()


def scrub_substrate(s: str, rules: "Rules | None" = None) -> Tuple[str, Counter]:
    """Remove substrate (per ``rules``, default the loaded private denylist) and internal IPs from a
    string. Keeps localhost/bind IPs; a non-substrate app URL is kept because it is not in ``rules``."""
    infra, pats, hres = rules or _RULES
    counts: Counter = Counter()
    for h in hres:
        s, n = h.subn("[infra-host]", s)
        if n:
            counts["infra-host"] += n
    for p in pats:
        s, n = p.subn("[infra]", s)
        if n:
            counts["infra"] += n
    if infra is not None:
        s, n = infra.subn("[infra]", s)
        if n:
            counts["infra"] += n
    kept = [0]

    def _ip(m: "re.Match") -> str:
        if m.group(0) in _IP_KEEP:
            return m.group(0)
        kept[0] += 1
        return "[ip]"

    s = _IPV4.sub(_ip, s)
    if kept[0]:
        counts["ip"] += kept[0]
    return s, counts


def scan_for_substrate(text: str, rules: "Rules | None" = None) -> List[Tuple[str, str]]:
    """Re-scan (scrubbed) text for surviving substrate or non-allowlisted IPs. []=clean."""
    infra, pats, hres = rules or _RULES
    hits: List[Tuple[str, str]] = []
    for h in hres:
        for m in h.finditer(text):
            hits.append(("infra-host", m.group(0)[:24]))
    for p in pats:
        for m in p.finditer(text):
            hits.append(("infra", m.group(0)[:24]))
    if infra is not None:
        for m in infra.finditer(text):
            hits.append(("infra", m.group(0)[:24]))
    for m in _IPV4.finditer(text):
        if m.group(0) not in _IP_KEEP:
            hits.append(("ip", m.group(0)))
    return hits


def redact_obj(o: Any, counts: Counter) -> Any:
    """Recursively redact CREDENTIALS in a parsed JSON value. A dict entry whose KEY is a secret has its
    string value replaced whole; every other string is passed through the credential pattern layer.
    Numbers, bools, None, timestamps and keys are never altered. Substrate scrubbing is a separate pass
    over the serialized line (see redact_line), so it can reach infra names that live in KEYS too, such
    as provider image-metadata property names."""
    if isinstance(o, dict):
        out: Dict[str, Any] = {}
        for k, v in o.items():
            if isinstance(v, str) and _sensitive_key(k):
                out[k] = v if _MARK in v else _ph("keyed")
                if _MARK not in v:
                    counts["keyed"] += 1
            else:
                out[k] = redact_obj(v, counts)
        return out
    if isinstance(o, list):
        return [redact_obj(x, counts) for x in o]
    if isinstance(o, str):
        s2, c = redact_text(o)
        counts.update(c)
        return s2
    return o


def redact_line(line: str, substrate: bool = False, rules: "Rules | None" = None) -> Tuple[str, Counter]:
    """Redact one JSONL line: credentials structurally (confined to string values, output valid JSON),
    then -- if ``substrate`` -- substrate names, control-plane hosts and internal IPs on the serialized
    line, which reaches infra strings in KEYS as well as values."""
    counts: Counter = Counter()
    stripped = line.strip()
    if not stripped:
        return line, counts
    try:
        obj = json.loads(stripped)
    except ValueError:
        out, counts = redact_text(line)
    else:
        out = json.dumps(redact_obj(obj, counts), ensure_ascii=False, separators=(",", ":"))
    if substrate:
        out, c2 = scrub_substrate(out, rules)
        counts.update(c2)
    return out, counts


def redact_transcript(text: str, substrate: bool = False, rules: "Rules | None" = None) -> Tuple[str, Counter]:
    """Redact a whole JSONL transcript string. With ``substrate=True`` also hides the infrastructure
    setup (per ``rules``, default the loaded private denylist). Returns the redacted text and counts."""
    counts: Counter = Counter()
    out_lines: List[str] = []
    for line in text.splitlines():
        red, c = redact_line(line, substrate, rules)
        out_lines.append(red)
        counts.update(c)
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(out_lines) + trailing, counts


# -- Verification: prove the OUTPUT holds no residue ------------------------------------------------
def scan_for_secrets(text: str) -> List[Tuple[str, str]]:
    """Re-scan (redacted) text for anything still secret-shaped. A clean redaction returns []. Matches
    whose captured value is already a placeholder are ignored, so placeholders never self-flag."""
    hits: List[Tuple[str, str]] = []
    for cat, rx in (("private-key", _PEM), ("ssh-key", _SSH), ("jwt", _JWT), ("bearer", _BEARER),
                    ("vendor-token", _VENDOR)):
        for m in rx.finditer(text):
            if _MARK not in m.group(0):
                hits.append((cat, m.group(0)[:24]))
    for m in _CONN.finditer(text):
        if _MARK not in m.group(2):
            hits.append(("conn-password", m.group(0)[:24]))
    for m in _ASSIGN.finditer(text):
        if _MARK not in m.group(4):
            hits.append(("value", m.group(0)[:32]))
    return hits
