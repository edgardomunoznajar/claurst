# prep_hansard.py

Builds the small demo corpus that Simon's demo container mounts at
`/workspace`. It pulls a single sitting day of the Australian Parliamentary
Debates from Zenodo, stamps each speech with a synthetic PSPF classification
label, and writes markdown + ACL sidecar files into four clearance tiers.

## Dataset citation

> Katz, Lindsay, & Alexander, Rohan (2023).
> *A new, comprehensive database of all proceedings of the Australian
> Parliamentary Debates (1998-2022)*. Zenodo.
> <https://doi.org/10.5281/zenodo.8121950>

Zenodo record `8121950` ships several files. We only need
`hansard-daily-csv.zip` (329 MB), and from it only a single daily CSV (a few
hundred KB). The script avoids downloading the full zip by reading the ZIP
central directory with HTTP range requests and pulling just the compressed
bytes for the target day.

## Running

From the repository root:

```bash
python3 scripts/prep_hansard.py
```

This generates `demo/corpus/` populated with four subdirectories:

```
demo/corpus/
  unofficial/
  official/
  official-sensitive/
  protected/
```

Each document has two files:

- `YYYY-MM-DD-speaker-slug-topic-slug.md` -- frontmatter + speech body.
- `YYYY-MM-DD-speaker-slug-topic-slug.md.acl.json` -- classification,
  required clearance, handling note, source tag, and speaker metadata.

Arguments (all optional):

| Flag                    | Default        | Purpose                                   |
| ----------------------- | -------------- | ----------------------------------------- |
| `--zenodo-record`       | `8121950`      | Zenodo record ID                          |
| `--sample-day`          | `2022-09-08`   | Sitting day to sample                     |
| `--output`              | `demo/corpus`  | Output directory                          |
| `--target-count`        | `60`           | Number of speeches to include             |
| `--fallback-synthetic`  | off            | Skip Zenodo, use hand-written speeches    |

The script is deterministic: same input, same output (random seed is fixed
at `20220908`). Re-running wipes the four tier subdirectories and regenerates
them with identical content.

Only Python 3.11+ standard library is required. No `requests`, no `pandas`,
no `nltk`.

## Synthetic PSPF heuristic

Real PSPF tagging requires policy judgement we cannot automate. We instead
apply a fully deterministic keyword cascade per speech body, first match
wins (highest tier first):

1. **PROTECTED** -- body contains any of: *classified, confidential,
   national security, intelligence, ASIO, ASIS, defence capability,
   operational, cabinet*.
2. **OFFICIAL:Sensitive** -- body contains any of: *ministerial, personnel
   matter, legal advice, commercial in confidence, procurement*.
3. **OFFICIAL** -- body contains any of: *committee, department, policy,
   regulation, agency*.
4. **UNOFFICIAL** -- everything else (constituency business, condolences,
   general debate, thanks).

After the natural pass, a rebalancing step forcibly promotes a seeded random
sample of lower-tier items to higher tiers so every clearance level carries
at least 10 documents. Demo visibility beats tagging fidelity -- the
reviewer needs to *see* the difference between clearances.

Any document that was reassigned by the balancer has
`"promoted_for_demo_balance": true` in its ACL sidecar. This is not a real
policy system.

## Current distribution

Run output with defaults (`--sample-day 2022-09-08`, `--target-count 60`):

```
unofficial           27
official             13
official-sensitive   10
protected            10
```

Total size on disk: ~588 KB (under 20 MB -- committed to git).

## Fallback mode

If Zenodo is unreachable or `--fallback-synthetic` is supplied, the script
generates 60 hand-templated Australian-sounding speeches from a fixed list
of 20 fictional MPs. Source tag in the ACL sidecar switches from
`hansard-YYYY-MM-DD` to `synthetic-fallback` so the demo operator can tell
which path ran.

## Australian English

All comments and documentation use Australian English spelling (authorise,
defence, organisation) to match the repository conventions.
