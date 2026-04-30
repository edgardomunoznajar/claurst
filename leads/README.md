# Simon — leads

Pull AU gov AI-procurement signals from public feeds. Output of
`scripts/lead_scanner.py`.

## Files

- `queue.md` — append-only Markdown ledger of new matches per run.
- `seen.json` — GUID dedup set; safe to delete to force a full re-scan.
- `scan.log` — one summary line per run.

## Sources

| source     | feed                                                              | auth | ToS posture |
|------------|-------------------------------------------------------------------|------|-------------|
| austender  | https://www.tenders.gov.au/Atm/Search/Atom?keyword=...            | none | public Atom, official |
| apsjobs    | https://www.apsjobs.gov.au/Search/RSS?Keywords=...                | none | public RSS, official |

Out of scope for v1 (require auth or sit in a ToS grey area):
- seek.com.au (private API; saved-search RSS exists but ToS-flagged for bots)
- LinkedIn Jobs (rate-limited; needs authenticated session)
- BuyICT / DTA Digital Marketplace (account required for opportunities feed)

## Match rules

A listing is captured when an **AI term** hits in `title + summary`. A
listing is also flagged **🛡 cleared** when a **clearance term** hits.

- AI terms: `artificial intelligence`, `machine learning`, `generative ai`,
  `llm`, `large language`, `automated decision`, `agentic`, `ai assurance`,
  `responsible ai`, `ai governance`, `ai safety`, …
- Clearance terms: `negative vetting`, `nv1`, `nv2`, `pspf`, `protected`,
  `baseline clearance`, `security cleared`, `official: sensitive`, `secret`.

Edit `scripts/lead_scanner.py` to extend either list.

## Run

```
# from simon/ root
python3 scripts/lead_scanner.py
python3 scripts/lead_scanner.py --source austender
python3 scripts/lead_scanner.py --quiet
```

## Cron

Nightly at 06:00 local. Add to `crontab -e`:

```
0 6 * * * cd /home/edgardo/projectsd/simon && /usr/bin/python3 scripts/lead_scanner.py --quiet >> leads/scan.log 2>&1
```

## Why this exists

Companion infrastructure for the Simon architecture post: *"the LLM cannot
reach the ACL check; deny is final; clearance is OIDC sub claim; here's the
demo stack against a real Hansard corpus."* The post drives readers; this
scanner surfaces the actual procurement opportunities the post is pitching
into. A live `queue.md` linked from the post is the funnel.
