#!/usr/bin/env python3
"""
One-shot AU gov AI-procurement lead fetch.

Automates the manual copy-paste from public search pages. Run on demand.

AusTender uses *exact-phrase* keyword matching (no stemming), so we issue
many short queries and dedup. APS Jobs is a Salesforce Lightning page —
we drive its in-page search input.

Usage:
    .venv/bin/python scripts/lead_oneshot.py

Output:
    leads/queue.md            — append-only Markdown ledger
    leads/raw/<ts>.json       — raw payload for the run
    leads/seen.json           — GUID dedup across runs
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import re
import sys
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parents[1]
LEADS = ROOT / "leads"
QUEUE = LEADS / "queue.md"
RAW = LEADS / "raw"
SEEN = LEADS / "seen.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# AusTender does exact-phrase, no stemming — short queries hit broadest.
ATM_QUERIES = [
    "AI", "ML", "machine learning", "artificial intelligence",
    "large language", "generative AI", "automation",
    "predictive", "natural language", "data science",
]

# APS Jobs — broader, but still phrase-y.
JOB_QUERIES = [
    "artificial intelligence", "machine learning",
    "AI engineer", "data scientist",
]

# seek.com.au — pre-built category pages give cleaner results than keyword search.
SEEK_PATHS = [
    "/artificial-intelligence-jobs/in-All-Australia",
    "/machine-learning-jobs/in-All-Australia",
]
# Government-specific filter pages (the Simon angle):
SEEK_GOV_QUERIES = [
    "AI+government", "machine+learning+government",
    "data+scientist+government", "AI+cleared",
]

CLEARED_TERMS = [
    "negative vetting", "nv1", "nv2", "pspf", "protected",
    "baseline clearance", "security clearance", "security cleared",
    "ability to obtain clearance", "eligible for clearance",
    "australian citizenship required", "must be an australian citizen",
    "official: sensitive", "secret",
]

# Both AusTender and APS Jobs do substring matching — "AI" hits "air",
# "audit", "maintenance" — so we always post-filter for real AI relevance.
AI_RELEVANCE_TERMS = [
    "artificial intelligence", "machine learning",
    "data scien", "data engineer", "ml engineer", "ai engineer",
    "llm", "large language", "natural language",
    "generative ai", "automation engineer", "predictive model",
    "ai/ml", "ai & ml", "ai-powered", "neural network",
    "deep learning", "computer vision", "automated decision",
    "ai assurance", "responsible ai", "ai governance",
    "agentic", "ai agent", "ai system",
]
def is_ai_relevant(text: str) -> bool:
    t = " " + text.lower() + " "
    return any(term in t for term in AI_RELEVANCE_TERMS)


# Gov-agency markers. Two signals: company name is a gov agency, OR the
# job description mentions Federal/State government / Defence / clearance.
GOV_COMPANY_PATTERNS = [
    "department of ", "australian ", "commonwealth ", "ministry of ",
    "nsw government", "victorian government", "queensland government",
    "western australia government", "south australia government",
    "tasmania government", "northern territory government", "act government",
    "bureau of meteorology", "geoscience australia", "csiro",
    "ato", "austrac", "asic", "asd", "dta", "abs", "acma", "accc", "afp",
    "apra", "aec", "aihw", "asqa", "ndis", "ndia", "anao", "nla",
    "ip australia", "service nsw", "services australia",
    "australian digital health agency", "australian institute of",
    "australian taxation office", "australian securities",
    "department of defence", "department of finance", "department of health",
    "department of home affairs", "department of education",
    "department of infrastructure", "department of social services",
    "department of foreign affairs", "department of veterans",
    "australian electoral commission", "australian federal police",
    "reserve bank", "rba", "australia post",
]
GOV_DESCRIPTION_TERMS = [
    "federal government", "australian government", "commonwealth government",
    "state government", "department of defence", "defence force",
    "australian public service", "aps5", "aps6", "el1", "el2",
    "negative vetting", "nv1", "nv2", "pspf", "baseline clearance",
    "security clearance", "agency client", "government client",
    "government department", "defence client", "public sector",
]
def is_gov_role(company: str, snippet: str) -> bool:
    c = (company or "").lower()
    s = (snippet or "").lower()
    if any(p in c for p in GOV_COMPANY_PATTERNS):
        return True
    if any(p in s for p in GOV_DESCRIPTION_TERMS):
        return True
    return False


def is_cleared(text: str) -> bool:
    t = text.lower()
    return any(term in t for term in CLEARED_TERMS)


def hsh(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


# ---- AusTender ----------------------------------------------------------
def fetch_austender_query(page: Page, query: str) -> list[dict]:
    """Issue one keyword search and harvest the visible results page."""
    url = ("https://www.tenders.gov.au/Atm?Keyword="
           + urllib.parse.quote(query))
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2_000)

    # No-results page short-circuits via body text
    body_txt = page.locator("body").inner_text()
    if "no results that match your selection" in body_txt.lower():
        return []

    leads = []
    seen_in_run: set[str] = set()
    # Anchor on div.boxW.listInner (one per result card, n=15 per page).
    # Walk up to the surrounding div.row to capture the title that sits
    # alongside the field block.
    field_blocks = page.locator("div.boxW.listInner").all()
    for fb in field_blocks:
        try:
            row = fb.locator("xpath=ancestor::div[contains(@class,'row')][1]")
        except Exception:
            continue
        if not row.count():
            continue
        row = row.first
        try:
            row_txt = row.inner_text(timeout=1_500).strip()
        except Exception:
            continue
        if not row_txt:
            continue
        try:
            a = row.locator("a[href*='/Atm/Show']").first
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.tenders.gov.au" + href
        guid = hsh("austender", href)
        if guid in seen_in_run:
            continue
        seen_in_run.add(guid)

        lines = [ln.strip() for ln in row_txt.splitlines() if ln.strip()]
        # First line of the row is the descriptive title; field block follows
        title = lines[0] if lines else ""

        def field(label: str) -> str:
            for i, ln in enumerate(lines):
                if ln.rstrip(":").strip().lower() == label.lower():
                    if i + 1 < len(lines):
                        return lines[i + 1]
            return ""

        atm_id = field("ATM ID")
        agency = field("Agency")
        category = field("Category")
        close_date = field("Close Date & Time")
        description = field("Description")

        # AusTender's "AI" search is a substring match — drop noise.
        relevance_blob = f"{title} {category} {description}"
        if not is_ai_relevant(relevance_blob):
            continue

        snippet_parts = [agency, category, description]
        snippet = " — ".join(p for p in snippet_parts if p)
        snippet = re.sub(r"\s+", " ", snippet)[:600]
        leads.append({
            "source": "austender",
            "guid": guid,
            "query": query,
            "title": title,
            "atm_id": atm_id,
            "agency": agency,
            "category": category,
            "close_date": close_date,
            "link": href,
            "snippet": snippet or row_txt[:600],
            "cleared": is_cleared(row_txt),
        })
    return leads


def fetch_austender_cn_query(page: Page, query: str) -> list[dict]:
    """Contract Notices (already-awarded). Same DOM shape as ATM."""
    url = ("https://www.tenders.gov.au/Cn/Search?Keyword="
           + urllib.parse.quote(query) + "&KeywordTypeId=1")
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2_000)
    body_txt = page.locator("body").inner_text()
    if "no results that match" in body_txt.lower():
        return []
    leads = []
    seen_in_run: set[str] = set()
    field_blocks = page.locator("div.boxW.listInner").all()
    for fb in field_blocks:
        try:
            row = fb.locator("xpath=ancestor::div[contains(@class,'row')][1]")
        except Exception:
            continue
        if not row.count():
            continue
        row = row.first
        try:
            row_txt = row.inner_text(timeout=1_500).strip()
        except Exception:
            continue
        if not row_txt:
            continue
        try:
            a = row.locator("a[href*='/Cn/Show'], a[href*='/Cn/Display']").first
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.tenders.gov.au" + href
        guid = hsh("austender_cn", href)
        if guid in seen_in_run:
            continue
        seen_in_run.add(guid)

        lines = [ln.strip() for ln in row_txt.splitlines() if ln.strip()]
        title = lines[0] if lines else ""
        def field(label: str) -> str:
            for i, ln in enumerate(lines):
                if ln.rstrip(":").strip().lower() == label.lower():
                    if i + 1 < len(lines):
                        return lines[i + 1]
            return ""
        cn_id = field("CN ID")
        agency = field("Agency")
        category = field("Category")
        value = field("Value")
        published = field("Publish Date")
        description = field("Description")

        if not is_ai_relevant(f"{title} {category} {description}"):
            continue

        snippet = " — ".join(p for p in [agency, category, description] if p)
        leads.append({
            "source": "austender_cn",
            "guid": guid,
            "query": query,
            "title": title,
            "cn_id": cn_id,
            "agency": agency,
            "category": category,
            "value": value,
            "published": published,
            "link": href,
            "snippet": re.sub(r"\s+", " ", snippet)[:600] or row_txt[:600],
            "cleared": is_cleared(row_txt),
        })
    return leads


def fetch_austender(ctx: BrowserContext) -> list[dict]:
    page = ctx.new_page()
    out: list[dict] = []
    for q in ATM_QUERIES:
        try:
            got = fetch_austender_query(page, q)
            print(f"[austender ATM] {q!r}: {len(got)} relevant", file=sys.stderr)
            out.extend(got)
        except Exception as e:
            print(f"[austender ATM] {q!r}: ERROR {e}", file=sys.stderr)
        try:
            got = fetch_austender_cn_query(page, q)
            print(f"[austender CN ] {q!r}: {len(got)} relevant", file=sys.stderr)
            out.extend(got)
        except Exception as e:
            print(f"[austender CN ] {q!r}: ERROR {e}", file=sys.stderr)
    page.close()
    return out


# ---- APS Jobs (Salesforce Lightning) ------------------------------------
def fetch_apsjobs_query(page: Page, query: str) -> list[dict]:
    # Lightning page renders both a hidden mobile input and the visible
    # desktop one with the same id. Iterate visible candidates.
    candidates = page.locator(
        "input[name='searchString'], input[aria-label*='looking for' i], "
        "input[placeholder*='search' i], input[type='search']"
    ).all()
    target = None
    for c in candidates:
        try:
            if c.is_visible(timeout=500):
                target = c
                break
        except Exception:
            continue
    if target is None:
        print(f"[apsjobs] {query!r}: no visible search input", file=sys.stderr)
        return []
    try:
        target.fill(query, timeout=5_000)
        target.press("Enter")
    except Exception as e:
        print(f"[apsjobs] fill {query!r}: {e}", file=sys.stderr)
        return []
    page.wait_for_timeout(4_500)

    leads = []
    # Only articles whose anchor points at an actual job detail page.
    cards = page.locator("article:has(a[href*='job-details'])").all()
    for card in cards[:60]:
        try:
            txt = card.inner_text(timeout=2_000).strip()
        except Exception:
            continue
        if not txt:
            continue
        # Server-side keyword filter is unreliable — post-filter on text.
        if not is_ai_relevant(txt):
            continue
        try:
            link_el = card.locator("a[href*='job-details']").first
            title = link_el.inner_text(timeout=1_500).strip()
            href = link_el.get_attribute("href") or ""
        except Exception:
            title, href = txt.splitlines()[0][:120], ""
        if href.startswith("/") or href.startswith("./"):
            href = "https://www.apsjobs.gov.au/s/" + href.lstrip("./")
        guid = hsh("apsjobs", href, title)
        leads.append({
            "source": "apsjobs",
            "guid": guid,
            "query": query,
            "title": title,
            "link": href,
            "snippet": re.sub(r"\s+", " ", txt)[:600],
            "cleared": is_cleared(txt),
        })
    return leads


def fetch_seek_url(page: Page, url: str, query_label: str) -> list[dict]:
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3_500)
    leads = []
    seen_in_run: set[str] = set()
    cards = page.locator("article[data-card-type='JobCard']").all()
    for card in cards:
        try:
            txt = card.inner_text(timeout=2_000).strip()
        except Exception:
            continue
        if not txt:
            continue
        # Post-filter for genuine AI relevance (Seek's own taxonomy is wide).
        if not is_ai_relevant(txt):
            continue
        try:
            t_el = card.locator("[data-automation='jobTitle']").first
            title = t_el.inner_text(timeout=1_500).strip()
            href = t_el.get_attribute("href") or ""
        except Exception:
            try:
                a = card.locator("a[href*='/job/']").first
                title = a.inner_text(timeout=1_500).strip()
                href = a.get_attribute("href") or ""
            except Exception:
                continue
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.seek.com.au" + href.split("?")[0].split("#")[0]
        guid = hsh("seek", href)
        if guid in seen_in_run:
            continue
        seen_in_run.add(guid)

        # extract company, location, classification from the card text
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        company = location = classification = ""
        for i, ln in enumerate(lines):
            if ln == "at" and i + 1 < len(lines):
                company = lines[i + 1]
            if ln.startswith("classification:"):
                classification = ln.split(":", 1)[1].strip()
            if "NSW" in ln or "VIC" in ln or "QLD" in ln or "ACT" in ln \
               or "WA" in ln or "SA" in ln or "TAS" in ln or "NT" in ln:
                if not location and len(ln) < 80:
                    location = ln

        snippet = re.sub(r"\s+", " ", txt)[:600]
        leads.append({
            "source": "seek",
            "guid": guid,
            "query": query_label,
            "title": title,
            "company": company,
            "location": location,
            "classification": classification,
            "link": href,
            "snippet": snippet,
            "cleared": is_cleared(txt),
            "gov": is_gov_role(company, txt),
        })
    return leads


def fetch_seek(ctx: BrowserContext) -> list[dict]:
    page = ctx.new_page()
    out: list[dict] = []
    for path in SEEK_PATHS:
        try:
            got = fetch_seek_url(page, "https://www.seek.com.au" + path, path)
            print(f"[seek]      {path!r}: {len(got)} relevant", file=sys.stderr)
            out.extend(got)
        except Exception as e:
            print(f"[seek]      {path!r}: ERROR {e}", file=sys.stderr)
    for q in SEEK_GOV_QUERIES:
        url = f"https://www.seek.com.au/{q}-jobs/in-All-Australia"
        try:
            got = fetch_seek_url(page, url, q.replace("+", " "))
            print(f"[seek gov]  {q!r}: {len(got)} relevant", file=sys.stderr)
            out.extend(got)
        except Exception as e:
            print(f"[seek gov]  {q!r}: ERROR {e}", file=sys.stderr)
    page.close()
    return out


def fetch_apsjobs(ctx: BrowserContext) -> list[dict]:
    """Reload the search page once per query — Lightning rerenders the
    input after the first submit, so a fresh page is the simplest fix."""
    out: list[dict] = []
    for q in JOB_QUERIES:
        page = ctx.new_page()
        try:
            page.goto("https://www.apsjobs.gov.au/s/job-search",
                      wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            print(f"[apsjobs] initial nav {q!r}: {e}", file=sys.stderr)
            page.close()
            continue
        page.wait_for_timeout(6_000)
        got = fetch_apsjobs_query(page, q)
        print(f"[apsjobs]   {q!r}: {len(got)} rows", file=sys.stderr)
        out.extend(got)
        page.close()
    return out


# ---- main ---------------------------------------------------------------
def main() -> int:
    LEADS.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()

    all_leads: list[dict] = []
    with sync_playwright() as pw:
        _chrome = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
        import os as _os
        _exe = _chrome if _os.path.exists(_chrome) else None
        browser = pw.chromium.launch(headless=True, args=["--ignore-certificate-errors"],
                                     **({} if _exe is None else {"executable_path": _exe}))
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="en-AU",
        )
        all_leads.extend(fetch_austender(ctx))
        all_leads.extend(fetch_seek(ctx))
        all_leads.extend(fetch_apsjobs(ctx))
        ctx.close()
        browser.close()

    # Dedup within run
    by_guid: dict[str, dict] = {}
    for L in all_leads:
        # Keep the first sighting (which has its originating query) but
        # accumulate matched queries.
        if L["guid"] in by_guid:
            existing = by_guid[L["guid"]]
            if L["query"] not in existing.get("queries", [existing["query"]]):
                existing.setdefault("queries", [existing["query"]])
                existing["queries"].append(L["query"])
        else:
            by_guid[L["guid"]] = L

    fresh = [L for guid, L in by_guid.items() if guid not in seen]

    ts = now_iso()
    (RAW / f"{ts}.json").write_text(json.dumps(list(by_guid.values()), indent=2))

    if fresh:
        new_header = not QUEUE.exists()
        with QUEUE.open("a") as fh:
            if new_header:
                fh.write("# Simon — AU gov AI-procurement leads\n\n")
                fh.write("Captured by `scripts/lead_oneshot.py`. One-shot "
                         "snapshots, deduped via `seen.json`.\n\n")
            gov_leads = [L for L in fresh if L.get("gov")]
            other_leads = [L for L in fresh if not L.get("gov")]
            fh.write(f"## {ts}  ({len(fresh)} new — "
                     f"{len(gov_leads)} gov, {len(other_leads)} other)\n\n")

            def write_lead(L):
                badge = "🏛 gov" if L.get("gov") else "·"
                if L.get("cleared"):
                    badge += " · 🛡 cleared"
                qs = ", ".join(L.get("queries", [L["query"]]))
                fh.write(f"- **[{L['source']}]** {badge} "
                         f"[{L['title']}]({L['link']})\n")
                meta_bits = []
                if L.get("company"):        meta_bits.append(f"_{L['company']}_")
                if L.get("agency"):         meta_bits.append(f"_{L['agency']}_")
                if L.get("location"):       meta_bits.append(L["location"])
                if L.get("classification"): meta_bits.append(L["classification"])
                if L.get("category"):       meta_bits.append(L["category"])
                if L.get("close_date"):     meta_bits.append(f"closes {L['close_date']}")
                if L.get("published"):      meta_bits.append(f"awarded {L['published']}")
                if L.get("value"):          meta_bits.append(f"value {L['value']}")
                if L.get("atm_id"):         meta_bits.append(f"id `{L['atm_id']}`")
                if L.get("cn_id"):          meta_bits.append(f"id `{L['cn_id']}`")
                if meta_bits:
                    fh.write(f"  - {' · '.join(meta_bits)}\n")
                fh.write(f"  - q: `{qs}`\n")
                snip = L["snippet"][:280]
                if snip and snip.lower() != L["title"].lower():
                    fh.write(f"  - {snip}\n")

            if gov_leads:
                fh.write("### 🏛 Government roles / agencies\n\n")
                for L in sorted(gov_leads, key=lambda x: (
                        x.get("company") or "", x["title"])):
                    write_lead(L)
                fh.write("\n")
            if other_leads:
                fh.write("### Private sector\n\n")
                for L in sorted(other_leads, key=lambda x: (
                        not x.get("cleared"), x["source"], x["title"])):
                    write_lead(L)
                fh.write("\n")

    for L in by_guid.values():
        seen.add(L["guid"])
    SEEN.write_text(json.dumps(sorted(seen), indent=0))

    # ---- forward-building time series (one row per run) ----
    from collections import Counter
    by_source = Counter(L["source"] for L in by_guid.values())
    state_re = re.compile(r"\b(NSW|VIC|QLD|WA|SA|TAS|NT|ACT)\b")
    by_state: Counter = Counter()
    for L in by_guid.values():
        m = state_re.search(L.get("location") or "")
        if m: by_state[m.group(1)] += 1
    ts_csv = LEADS / "timeseries.csv"
    new_csv = not ts_csv.exists()
    with ts_csv.open("a") as fh:
        if new_csv:
            fh.write("run_ts,total_fetched,unique,new,gov,cleared,"
                     "austender,austender_cn,seek,apsjobs,"
                     "NSW,VIC,QLD,WA,SA,TAS,NT,ACT\n")
        fh.write(",".join(str(x) for x in [
            ts,
            len(all_leads),
            len(by_guid),
            len(fresh),
            sum(1 for L in by_guid.values() if L.get("gov")),
            sum(1 for L in by_guid.values() if L.get("cleared")),
            by_source.get("austender", 0),
            by_source.get("austender_cn", 0),
            by_source.get("seek", 0),
            by_source.get("apsjobs", 0),
            by_state.get("NSW", 0), by_state.get("VIC", 0),
            by_state.get("QLD", 0), by_state.get("WA", 0),
            by_state.get("SA", 0),  by_state.get("TAS", 0),
            by_state.get("NT", 0),  by_state.get("ACT", 0),
        ]) + "\n")

    print(f"\nrun {ts}")
    print(f"  total fetched: {len(all_leads)}")
    print(f"  unique within run: {len(by_guid)}")
    print(f"  new (not in seen.json): {len(fresh)}")
    print(f"  raw: leads/raw/{ts}.json")
    print(f"  queue: leads/queue.md")
    print(f"  timeseries: leads/timeseries.csv (+1 row)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
