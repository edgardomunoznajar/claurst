#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prep_hansard.py -- build a small demo corpus for Simon.

Fetches a single sitting-day of the Australian Parliamentary Debates from the
Zenodo-hosted Hansard daily CSV archive, samples a deterministic subset of
speeches, stamps each with a synthetic PSPF classification label, and writes
one markdown file + one ``.acl.json`` sidecar per document into four
classification subdirectories under the output path.

Dataset citation:
    Katz, Lindsay, & Alexander, Rohan (2023).
    A new, comprehensive database of all proceedings of the Australian
    Parliamentary Debates (1998-2022). Zenodo.
    https://doi.org/10.5281/zenodo.8121950

This script uses only the Python standard library (Python 3.11+). It does
not require ``requests``, pandas, nltk, or any other third-party package.

The download path uses HTTP range requests to pull just the ZIP central
directory and the single compressed CSV entry we need (a few hundred KB),
rather than fetching the full 329 MB daily archive.

Australian English is used throughout -- authorise, defence, organisation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import random
import re
import struct
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Iterable

ZENODO_RECORD_DEFAULT = "8121950"
SAMPLE_DAY_DEFAULT = "2022-09-08"
TARGET_COUNT_DEFAULT = 60
RANDOM_SEED = 20220908  # deterministic -- do not change without regenerating
MIN_PER_TIER = 10

TIERS = ["unofficial", "official", "official-sensitive", "protected"]

# Synthetic PSPF heuristic keyword buckets. Matched case-insensitively against
# the speech body. Order matters: first match wins, highest tier first.
PROTECTED_KEYWORDS = (
    "classified", "confidential", "national security", "intelligence",
    "asio", "asis", "defence capability", "operational", "cabinet",
)
OFFICIAL_SENSITIVE_KEYWORDS = (
    "ministerial", "personnel matter", "legal advice",
    "commercial in confidence", "procurement",
)
OFFICIAL_KEYWORDS = (
    "committee", "department", "policy", "regulation", "agency",
)

HANDLING_NOTES = {
    "unofficial": "Public record. No access restriction.",
    "official": "Routine official business. Internal Australian Government use.",
    "official-sensitive": "Handle with care. Limit distribution to cleared staff.",
    "protected": "PROTECTED. Clearance required. Do not redistribute.",
}

# Fallback MP names (only used if Zenodo is unreachable). Fictional but
# Australian-sounding. Deterministic ordering.
FALLBACK_MPS = [
    ("Alicia Kowalski", "Bennelong", "ALP"),
    ("Bradley O'Sullivan", "Herbert", "LNP"),
    ("Chloe Nguyen", "Chisholm", "ALP"),
    ("Darius Papadopoulos", "Dunkley", "ALP"),
    ("Eleanor Whitfield", "Wentworth", "LP"),
    ("Finn McPherson", "Mayo", "CA"),
    ("Grace Tanaka", "Griffith", "AG"),
    ("Harold Bennett", "Forrest", "LP"),
    ("Isla Beauchamp", "Lingiari", "ALP"),
    ("Jeremy Callaghan", "Hume", "LP"),
    ("Kirra Anderson", "Durack", "LP"),
    ("Lachlan Hargreaves", "New England", "NAT"),
    ("Maya Fitzgerald", "Cooper", "ALP"),
    ("Nigel Ashworth", "Fisher", "LNP"),
    ("Ophelia Ramirez", "Sydney", "ALP"),
    ("Piper Donoghue", "Clark", "IND"),
    ("Quentin Marsh", "Flinders", "LP"),
    ("Ruby Sinclair", "Blair", "ALP"),
    ("Samuel Okonkwo", "Hotham", "ALP"),
    ("Tilly Brennan", "Canberra", "ALP"),
]

FALLBACK_TOPICS = [
    ("housing affordability", "official"),
    ("defence capability acquisition", "protected"),
    ("asio annual report", "protected"),
    ("cabinet submission process", "protected"),
    ("intelligence services oversight", "protected"),
    ("national security legislation", "protected"),
    ("operational readiness briefing", "protected"),
    ("classified procurement", "protected"),
    ("confidential contract review", "protected"),
    ("ministerial statement on trade", "official-sensitive"),
    ("personnel matter in the public service", "official-sensitive"),
    ("legal advice regarding indemnities", "official-sensitive"),
    ("commercial in confidence tender", "official-sensitive"),
    ("procurement policy update", "official-sensitive"),
    ("productivity commission inquiry", "official"),
    ("committee report on aged care", "official"),
    ("department of health policy", "official"),
    ("regulation impact statement", "official"),
    ("agency corporate plan", "official"),
    ("rural infrastructure investment", "official"),
    ("local bushfire recovery", "unofficial"),
    ("condolence motion", "unofficial"),
    ("constituency thanks", "unofficial"),
    ("school visit acknowledgement", "unofficial"),
    ("community sport grant", "unofficial"),
    ("volunteer recognition", "unofficial"),
    ("grievance on local transport", "unofficial"),
    ("general debate adjournment", "unofficial"),
]


# ---------------------------------------------------------------------------
# Zenodo ranged-read helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: dict | None = None, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_range(url: str, start: int, end_inclusive: int) -> bytes:
    """Fetch bytes [start, end_inclusive] via HTTP Range."""
    return _http_get(url, headers={"Range": f"bytes={start}-{end_inclusive}"})


def _get_total_size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as resp:
        cl = resp.headers.get("Content-Length")
        if cl is None:
            raise RuntimeError(f"No Content-Length for {url}")
        return int(cl)


def _find_eocd(tail: bytes) -> tuple[int, int, int]:
    """Return (entry_count, cdir_size, cdir_offset) from EOCD in tail bytes."""
    sig = b"PK\x05\x06"
    eocd_pos = tail.rfind(sig)
    if eocd_pos < 0:
        raise RuntimeError("EOCD signature not found in zip tail")
    rec = tail[eocd_pos:eocd_pos + 22]
    (_sig, _disk, _disk_start, _entries_disk, entries_total,
     cdir_size, cdir_offset, _comment_len) = struct.unpack("<IHHHHIIH", rec)
    if cdir_offset == 0xFFFFFFFF or cdir_size == 0xFFFFFFFF:
        raise RuntimeError("ZIP64 EOCD locator encountered -- unsupported")
    return entries_total, cdir_size, cdir_offset


def _parse_central_directory(blob: bytes) -> list[tuple[str, int, int, int, int]]:
    """Return list of (filename, method, csize, usize, local_offset)."""
    entries: list[tuple[str, int, int, int, int]] = []
    pos = 0
    sig = b"PK\x01\x02"
    while pos + 46 <= len(blob):
        if blob[pos:pos + 4] != sig:
            break
        hdr = blob[pos:pos + 46]
        (_s, _vmade, _vneed, _flags, method, _mt, _md, _crc, csize, usize,
         fname_len, extra_len, comment_len,
         _disk, _iattr, _eattr, loff) = struct.unpack(
            "<IHHHHHHIIIHHHHHII", hdr)
        name = blob[pos + 46:pos + 46 + fname_len].decode("utf-8", "replace")
        entries.append((name, method, csize, usize, loff))
        pos += 46 + fname_len + extra_len + comment_len
    return entries


def _fetch_single_zip_entry(url: str, filename_fragment: str) -> bytes:
    """Fetch and decompress a single file from a remote zip by name fragment.

    Uses HTTP range requests so we avoid downloading the full archive.
    """
    total = _get_total_size(url)
    tail_size = min(262144, total)
    tail = _fetch_range(url, total - tail_size, total - 1)
    entry_count, cdir_size, cdir_offset = _find_eocd(tail)
    cdir_blob = _fetch_range(url, cdir_offset, cdir_offset + cdir_size - 1)
    entries = _parse_central_directory(cdir_blob)
    if not entries:
        raise RuntimeError("Central directory parsed zero entries")

    match = None
    for name, method, csize, usize, loff in entries:
        if filename_fragment in name and not name.startswith("__MACOSX"):
            match = (name, method, csize, usize, loff)
            break
    if match is None:
        raise RuntimeError(
            f"No entry containing '{filename_fragment}' found in {url}")
    name, method, csize, usize, loff = match

    # Local file header: signature(4) + 26 bytes of fields = 30
    lfh = _fetch_range(url, loff, loff + 29)
    (_sig, _vneed, _flags, l_method, _mt, _md, _crc, _lc, _lu,
     fname_len, extra_len) = struct.unpack("<IHHHHHIIIHH", lfh)
    if l_method != method:
        method = l_method
    data_start = loff + 30 + fname_len + extra_len
    comp = _fetch_range(url, data_start, data_start + csize - 1)
    if method == 0:
        return comp
    if method == 8:
        return zlib.decompress(comp, -15)
    raise RuntimeError(f"Unsupported compression method {method} for {name}")


def _zenodo_record_url(record: str) -> str:
    return f"https://zenodo.org/api/records/{record}"


def _resolve_daily_csv_url(record: str) -> tuple[str, dict]:
    meta_url = _zenodo_record_url(record)
    blob = _http_get(meta_url, timeout=60)
    doc = json.loads(blob.decode("utf-8"))
    target = None
    for f in doc.get("files", []):
        if f.get("key") == "hansard-daily-csv.zip":
            target = f.get("links", {}).get("self")
            break
    if not target:
        raise RuntimeError("hansard-daily-csv.zip not found in Zenodo record")
    return target, doc


# ---------------------------------------------------------------------------
# Corpus generation
# ---------------------------------------------------------------------------

_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 48) -> str:
    text = text.lower().strip()
    text = _SLUG_CLEAN.sub("-", text)
    text = text.strip("-")
    if not text:
        text = "entry"
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text


def classify(body: str) -> str:
    haystack = body.lower()
    if any(k in haystack for k in PROTECTED_KEYWORDS):
        return "protected"
    if any(k in haystack for k in OFFICIAL_SENSITIVE_KEYWORDS):
        return "official-sensitive"
    if any(k in haystack for k in OFFICIAL_KEYWORDS):
        return "official"
    return "unofficial"


def _topic_from_body(body: str) -> str:
    # Use the first short-ish sentence as a topic cue.
    first = re.split(r"[.!?]", body, maxsplit=1)[0].strip()
    if len(first) > 80:
        first = first[:80]
    return first or "parliamentary speech"


def _clean_name(raw: str) -> str:
    # Hansard column: "Surname, Given Names MP" or similar.
    n = raw.replace(" MP", "").replace(" Mr", "").strip()
    if "," in n:
        surname, rest = n.split(",", 1)
        return f"{rest.strip()} {surname.strip()}"
    return n


def load_speeches_from_csv(csv_bytes: bytes) -> list[dict]:
    text = csv_bytes.decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    col = {name: i for i, name in enumerate(header)}
    needed = ["name", "body", "electorate", "party"]
    for n in needed:
        if n not in col:
            raise RuntimeError(f"Column '{n}' missing from Hansard CSV")

    out: list[dict] = []
    for row in reader:
        name = row[col["name"]]
        body = row[col["body"]]
        if not body or len(body) < 200:
            continue
        if name.strip().lower() in ("", "stage direction", "na", "the speaker"):
            continue
        out.append({
            "name": _clean_name(name),
            "body": body.strip(),
            "electorate": row[col["electorate"]].strip(),
            "party": row[col["party"]].strip(),
        })
    return out


def build_synthetic_speeches() -> list[dict]:
    """Fallback corpus generator -- only used if Zenodo is unreachable."""
    rng = random.Random(RANDOM_SEED)
    out: list[dict] = []
    for i in range(60):
        mp = FALLBACK_MPS[i % len(FALLBACK_MPS)]
        topic, _hint = FALLBACK_TOPICS[i % len(FALLBACK_TOPICS)]
        paragraphs = _synth_paragraphs(topic, mp[0], rng)
        out.append({
            "name": mp[0],
            "electorate": mp[1],
            "party": mp[2],
            "body": paragraphs,
        })
    return out


def _synth_paragraphs(topic: str, speaker: str, rng: random.Random) -> str:
    openings = [
        f"Mr Speaker, I rise today to address the matter of {topic}.",
        f"Madam Speaker, the house should give due attention to {topic}.",
        f"I thank the member opposite for raising {topic} in this chamber.",
    ]
    bodies = [
        ("The Australian Government remains committed to robust oversight "
         "and to ensuring that our public institutions serve the Australian "
         "people with integrity and transparency."),
        ("This parliament must recognise the genuine concerns held by "
         "constituents across every electorate, from the suburbs of Sydney "
         "to the remote communities of the Northern Territory."),
        ("Defence capability, national security, and the work of the "
         "intelligence community are matters this parliament takes with "
         "the utmost seriousness."),
        ("The relevant department has briefed the committee, and the agency "
         "responsible has issued a regulation consistent with policy settings."),
        ("I draw the attention of the house to the ministerial statement "
         "circulated this morning, which contains legal advice on the matter."),
    ]
    closings = [
        "I commend this motion to the house.",
        "Thank you, Mr Speaker.",
        "I seek leave to continue my remarks later.",
    ]
    parts = [
        rng.choice(openings),
        rng.choice(bodies),
        rng.choice(bodies),
        f"As the member for their electorate, {speaker} has long advocated "
        f"for action on {topic}.",
        rng.choice(closings),
    ]
    return " ".join(parts)


def balance_classifications(
    speeches: list[dict], target_count: int, rng: random.Random
) -> list[dict]:
    """Assign tiers to speeches, rebalance so every tier has >= MIN_PER_TIER.

    Mutates by adding ``_tier`` key to each selected speech dict. Returns the
    list of selected speeches (exactly ``target_count`` items when possible).
    """
    # Deterministic shuffle so we pick a reproducible subset.
    indexed = list(enumerate(speeches))
    rng.shuffle(indexed)
    selected = [s for _, s in indexed[:target_count]]

    for s in selected:
        s["_tier"] = classify(s["body"])

    counts = {t: 0 for t in TIERS}
    for s in selected:
        counts[s["_tier"]] += 1

    # Promotion pool: reclassify items from over-represented tiers to those
    # that are short of the minimum. Order is arbitrary but deterministic.
    promotion_rng = random.Random(RANDOM_SEED + 1)
    need_order = ["protected", "official-sensitive", "official", "unofficial"]
    for tier in need_order:
        while counts[tier] < MIN_PER_TIER:
            # Only take donors from tiers that are currently above the
            # minimum (so we never starve another tier below it).
            donors = [s for s in selected
                      if s["_tier"] != tier
                      and counts[s["_tier"]] > MIN_PER_TIER]
            if not donors:
                # Last-resort: take from whichever other tier has the most.
                donors = [s for s in selected if s["_tier"] != tier]
                if not donors:
                    break
                over_tier = max(
                    (t for t in TIERS if t != tier),
                    key=lambda t: counts[t],
                )
                donors = [s for s in selected if s["_tier"] == over_tier]
                if not donors:
                    break
            donor = promotion_rng.choice(donors)
            counts[donor["_tier"]] -= 1
            donor["_tier"] = tier
            donor["_promoted"] = True
            counts[tier] += 1
    return selected


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_corpus(
    selected: list[dict],
    out_dir: Path,
    sample_day: str,
    source_tag: str,
) -> dict[str, int]:
    # Ensure clean state for determinism: wipe the tier subdirs then recreate.
    for t in TIERS:
        d = out_dir / t
        if d.exists():
            for p in sorted(d.iterdir()):
                p.unlink()
        d.mkdir(parents=True, exist_ok=True)
    (out_dir / ".gitkeep").touch()

    counts = {t: 0 for t in TIERS}
    seen_slugs: set[str] = set()
    for s in selected:
        tier = s["_tier"]
        speaker_slug = slugify(s["name"]) or "unknown-speaker"
        topic = _topic_from_body(s["body"])
        topic_slug = slugify(topic) or "debate"
        base = f"{sample_day}-{speaker_slug}-{topic_slug}"
        base = base[:100].rstrip("-")
        suffix_i = 0
        fname = base
        while fname in seen_slugs:
            suffix_i += 1
            fname = f"{base}-{suffix_i}"
        seen_slugs.add(fname)

        md_path = out_dir / tier / f"{fname}.md"
        acl_path = out_dir / tier / f"{fname}.md.acl.json"

        frontmatter = [
            "---",
            f"speaker: {s['name']}",
            f"electorate: {s.get('electorate') or 'unknown'}",
            f"party: {s.get('party') or 'unknown'}",
            f"date: {sample_day}",
            f"topic: {topic}",
            f"length_chars: {len(s['body'])}",
            f"classification: {tier}",
            "source: hansard-daily-csv (zenodo 8121950)",
            "---",
            "",
        ]
        md_body = "\n".join(frontmatter) + s["body"] + "\n"
        md_path.write_text(md_body, encoding="utf-8")

        acl = {
            "classification": tier,
            "required_clearance": tier,
            "handling": HANDLING_NOTES[tier],
            "source": source_tag,
            "speaker": s["name"],
            "electorate": s.get("electorate") or "",
            "party": s.get("party") or "",
            "date": sample_day,
            "promoted_for_demo_balance": bool(s.get("_promoted", False)),
        }
        acl_path.write_text(
            json.dumps(acl, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        counts[tier] += 1
    return counts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zenodo-record", default=ZENODO_RECORD_DEFAULT)
    parser.add_argument("--sample-day", default=SAMPLE_DAY_DEFAULT,
                        help="YYYY-MM-DD of the sitting day to sample.")
    parser.add_argument("--output", default="demo/corpus",
                        help="Output directory (will be created).")
    parser.add_argument("--target-count", type=int, default=TARGET_COUNT_DEFAULT,
                        help="How many speeches to include in the demo corpus.")
    parser.add_argument("--fallback-synthetic", action="store_true",
                        help="Skip Zenodo and generate synthetic speeches.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    used_fallback = args.fallback_synthetic
    speeches: list[dict] = []
    source_tag = f"hansard-{args.sample_day}"

    if not used_fallback:
        try:
            print(f"[prep_hansard] resolving zenodo record {args.zenodo_record}",
                  file=sys.stderr)
            zip_url, _meta = _resolve_daily_csv_url(args.zenodo_record)
            print(f"[prep_hansard] ranged-fetching {args.sample_day}.csv from "
                  f"hansard-daily-csv.zip", file=sys.stderr)
            csv_bytes = _fetch_single_zip_entry(
                zip_url, f"{args.sample_day}.csv")
            speeches = load_speeches_from_csv(csv_bytes)
            print(f"[prep_hansard] loaded {len(speeches)} substantive speeches",
                  file=sys.stderr)
        except (urllib.error.URLError, RuntimeError, OSError) as exc:
            print(f"[prep_hansard] Zenodo fetch failed ({exc}); "
                  f"falling back to synthetic corpus", file=sys.stderr)
            used_fallback = True

    if used_fallback:
        speeches = build_synthetic_speeches()
        source_tag = "synthetic-fallback"

    if not speeches:
        print("[prep_hansard] no speeches available", file=sys.stderr)
        return 1

    rng = random.Random(RANDOM_SEED)
    target = min(args.target_count, len(speeches))
    if target < MIN_PER_TIER * len(TIERS):
        target = min(MIN_PER_TIER * len(TIERS), len(speeches))
    selected = balance_classifications(speeches, target, rng)

    counts = write_corpus(selected, out_dir, args.sample_day, source_tag)
    total = sum(counts.values())
    print("")
    print("Hansard demo corpus generated.")
    print(f"  output:   {out_dir}")
    print(f"  source:   {source_tag}")
    print(f"  total:    {total} documents")
    for tier in TIERS:
        print(f"    {tier:<20} {counts[tier]:>4}")
    if used_fallback:
        print("  NOTE: synthetic fallback was used (Zenodo unreachable "
              "or --fallback-synthetic set).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
