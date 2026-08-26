"""Bundle agent session transcripts into a credential-stripped, auditable set (the paper supplement).

``bundle`` redacts every transcript under an input directory into an output directory, writes a manifest
of what was removed, and RE-SCANS every output for residue. It fails loud if any file could not be
processed or if any secret survived -- the guard against a redactor that silently covers only part of its
input. The redacted sessions are the evidence behind Part 2's self-reported negative results.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from collections import Counter
from typing import Dict, List

from .redact import redact_transcript, scan_for_secrets, scan_for_substrate

_README = """# Sessions (credential-stripped, infrastructure-neutral)

These are the raw agent deploy/measurement sessions behind the paper's numbers and its falsifiability
claims, redacted so they can ship as auditable evidence: they show what the agent DID, the app it
deployed and the timing, not any secret or how the cloud is built underneath.

Removed: generated app secrets (passwords, API tokens, encryption keys), any cloud OAuth bearer/JWT,
private-key and SSH-key material, connection-string passwords; and the infrastructure setup -- provider
technology names, control-plane hostnames and internal IP addresses. Kept: the agent's tool calls, the
deployed app URLs, and the platform brand.

Redaction touched only secret- or substrate-bearing string values: timestamps, token counts, tool
structure and message shape are unchanged, so each session still reconstructs the exact owner-labelled
trace and token totals the reference core computes (see acspeed/transcript.py). Placeholders read
``[REDACTED:...]`` (secrets), ``[infra]`` / ``[infra-host]`` (setup) and ``[ip]``. See
REDACTION-MANIFEST.json for per-file counts; the bundle was verified to contain no residual secret- or
infrastructure-shaped strings.
"""


def bundle(in_dir: str, out_dir: str, pattern: str = "*.jsonl", substrate: bool = True,
           rules=None) -> Dict:
    """Redact every ``pattern`` file under ``in_dir`` into ``out_dir`` (flat basenames), write a
    manifest and a README, and verify no residue. ``substrate=True`` (default) also removes the
    infrastructure setup. Returns the report; raises RuntimeError on any unprocessed file or surviving
    secret/substrate so a caller can never ship a leak by accident.
    """
    inputs = sorted(glob.glob(os.path.join(in_dir, "**", pattern), recursive=True))
    if not inputs:
        raise RuntimeError("no transcripts matched %r under %s" % (pattern, in_dir))
    os.makedirs(out_dir, exist_ok=True)

    files: List[Dict] = []
    totals: Counter = Counter()
    residue: Dict[str, List] = {}
    seen_names: Dict[str, str] = {}

    for src in inputs:
        name = os.path.basename(src)
        if name in seen_names:  # flat output must not collide; disambiguate with a short path hash
            tag = hashlib.sha1(src.encode()).hexdigest()[:8]
            name = "%s.%s.jsonl" % (name[:-6], tag) if name.endswith(".jsonl") else name + "." + tag
        seen_names[name] = src

        with open(src, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        red, counts = redact_transcript(text, substrate=substrate, rules=rules)
        left = scan_for_secrets(red)
        if substrate:
            left = left + scan_for_substrate(red, rules)
        if left:
            residue[name] = left[:20]
        dst = os.path.join(out_dir, name)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(red)
        totals.update(counts)
        files.append({
            "name": name,
            "source": os.path.basename(src),
            "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
            "bytes_in": len(text.encode("utf-8")),
            "bytes_out": len(red.encode("utf-8")),
            "redactions": dict(counts),
            "residue": len(left),
        })

    report = {
        "inputs": len(inputs),
        "written": len(files),
        "total_redactions": dict(totals),
        "total_redactions_count": sum(totals.values()),
        "files_with_residue": {k: v for k, v in residue.items()},
        "files": files,
    }
    with open(os.path.join(out_dir, "REDACTION-MANIFEST.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(_README)

    if len(files) != len(inputs):
        raise RuntimeError("processed %d of %d inputs -- refusing to ship a partial bundle"
                           % (len(files), len(inputs)))
    if residue:
        raise RuntimeError("residual secrets survived redaction in %d file(s): %s"
                           % (len(residue), ", ".join(sorted(residue))))
    return report
