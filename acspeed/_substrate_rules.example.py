"""Template for the PRIVATE substrate denylist.

Copy this to ``_substrate_rules.py`` (gitignored) and fill in your own infrastructure so redact.py
strips it from published sessions; the substrate scrub is part of ``acspeed sessions``. Without a rules
file, only generic IP scrubbing runs (which reveals nothing). You can also point
``$ACSPEED_SUBSTRATE_RULES`` at a file elsewhere. This template lists no real infrastructure. See
``acspeed/redact.py``.
"""
# Literal names, matched whole-word and case-insensitively, replaced with [infra]:
INFRA_WORDS = ["your-hypervisor", "your-object-store", "your-network-service"]
# Raw regexes replaced with [infra] (e.g. service-daemon names):
INFRA_PATTERNS = [r"your-service-(?:api|worker|agent)"]
# Raw regexes for control-plane hostnames, replaced with [infra-host]:
HOST_PATTERNS = [r"(?:api|console)\.control-plane\.example", r"controller-?\d+"]
