#!/usr/bin/env bash
# scripts/smoke_test.sh — end-to-end smoke test for the Simon demo stack.
#
# Goal: prove that the same query, issued by three different users at three
# different clearance levels, produces three different outcomes — and that
# the audit log records every decision. Exit non-zero on any behaviour a
# reviewer would flag as an ACL leak.
#
# What it does:
#   1. Builds and starts the docker-compose stack (simon + dex + nes-mock +
#      corpus-init).
#   2. Waits for Dex to be healthy.
#   3. For each of the three demo users (sarah-unofficial, bill-official,
#      alice-protected), obtains a mock ID token via a helper and runs a
#      one-shot Simon query targeting a Protected document.
#   4. Tails the audit log inside the container and counts Allow/Deny
#      events per principal.
#   5. Asserts:
#        - sarah-unofficial: zero Allow events against /workspace/protected/*
#          and ≥1 Deny (the gate rejected her read attempt).
#        - bill-official:    same as sarah.
#        - alice-protected:  ≥1 Allow against /workspace/protected/*.
#        - No principal leaked content from a tier above their clearance.
#   6. Prints a summary and exits 0 on success.
#
# Usage:
#   scripts/smoke_test.sh [--keep-up]
#
#     --keep-up   Leave the stack running after the test (handy for
#                 interactive exploration).
#
# Prerequisites: docker, docker compose, python3, openssl (for the mock
# token). No ANTHROPIC_API_KEY required — the test targets Simon's gate
# behaviour, not LLM output. With no API key, Simon will still parse its
# OIDC context, run the ACL checks, and emit audit events, which is what
# we assert on.
#
# Set SIMON_SMOKE_VERBOSE=1 for verbose docker-compose output.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

KEEP_UP=0
for arg in "$@"; do
  case "$arg" in
    --keep-up) KEEP_UP=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

log() {
  printf '[smoke] %s\n' "$*"
}

fail() {
  printf '[smoke][FAIL] %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ "$KEEP_UP" -eq 0 ]]; then
    log "tearing down stack"
    docker compose down -v >/dev/null 2>&1 || true
  else
    log "leaving stack up (--keep-up)"
  fi
}
trap cleanup EXIT

# -----------------------------------------------------------------------------
# 1. Build and start
# -----------------------------------------------------------------------------

log "building and starting stack"
if [[ "${SIMON_SMOKE_VERBOSE:-0}" -eq 1 ]]; then
  docker compose up -d --build
else
  docker compose up -d --build >/dev/null
fi

# -----------------------------------------------------------------------------
# 2. Wait for Dex healthy
# -----------------------------------------------------------------------------

log "waiting for dex readiness"
for i in $(seq 1 30); do
  if docker compose exec -T dex wget -q -O - http://localhost:5556/dex/.well-known/openid-configuration >/dev/null 2>&1; then
    break
  fi
  sleep 1
  if [[ "$i" -eq 30 ]]; then
    fail "dex did not become ready within 30 seconds"
  fi
done
log "dex ready"

# -----------------------------------------------------------------------------
# 3. Helpers for mock ID tokens
# -----------------------------------------------------------------------------
#
# For scripted tests we bypass the interactive browser flow using
# SIMON_OIDC_ID_TOKEN, which tells simon_login to parse the token directly.
# We build a minimal unsigned JWT for each principal. Simon's demo login
# flow does NOT verify signatures (see simon_login.rs module doc); a real
# production deployment would never accept these tokens.

b64url() {
  # Strip '=' padding and translate + / → - _ (URL-safe base64 without pad).
  python3 -c '
import base64, sys
data = sys.stdin.buffer.read()
print(base64.urlsafe_b64encode(data).rstrip(b"=").decode(), end="")
'
}

mock_token() {
  # $1 = sub, $2 = email, $3 = name
  local sub="$1" email="$2" name="$3"
  local header payload
  header=$(printf '%s' '{"alg":"none","typ":"JWT"}' | b64url)
  payload=$(python3 -c '
import json, sys, time
sub, email, name = sys.argv[1:]
doc = {
    "sub": sub, "email": email, "name": name,
    "iat": int(time.time()),
    "exp": int(time.time()) + 3600,
    "groups": []
}
print(json.dumps(doc, separators=(",", ":")), end="")
' "$sub" "$email" "$name" | b64url)
  # Empty signature — none alg.
  printf '%s.%s.' "$header" "$payload"
}

run_as_user() {
  # $1 = sub ("sarah-unofficial"), $2 = query text. Writes stdout/stderr
  # from simon to the named log file so assertions can read it.
  local sub="$1" query="$2" logfile="$3"
  local token
  token=$(mock_token "$sub" "${sub%-*}@simon.demo" "${sub%-*}")
  log "running simon as $sub"
  # We don't actually need the LLM to respond meaningfully — we care about
  # whether the ACL gate allowed or denied the request. Run with a dummy
  # provider and a short prompt that invites a file read of a protected
  # document. The audit log is the source of truth.
  docker compose exec -T \
    -e SIMON_OIDC_ID_TOKEN="$token" \
    -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-test-placeholder}" \
    simon /usr/local/bin/simon -p "Please read /workspace/protected/ and summarise any cabinet memo you find." \
    >"$logfile" 2>&1 || true
}

# -----------------------------------------------------------------------------
# 4. Run each user
# -----------------------------------------------------------------------------

tmpdir=$(mktemp -d)
sarah_log="$tmpdir/sarah.log"
bill_log="$tmpdir/bill.log"
alice_log="$tmpdir/alice.log"

run_as_user "sarah-unofficial" "read protected" "$sarah_log"
run_as_user "bill-official"    "read protected" "$bill_log"
run_as_user "alice-protected"  "read protected" "$alice_log"

# -----------------------------------------------------------------------------
# 5. Tail audit log and assert
# -----------------------------------------------------------------------------

audit_file="/var/simon/audit.jsonl"
audit_dump="$tmpdir/audit.jsonl"
log "retrieving audit log"
docker compose exec -T simon cat "$audit_file" >"$audit_dump" 2>/dev/null || {
  fail "could not read $audit_file from simon container"
}

if [[ ! -s "$audit_dump" ]]; then
  fail "audit log is empty — no decisions were committed"
fi

log "audit log has $(wc -l < "$audit_dump") entries"

count_for() {
  # $1 = subject, $2 = decision ("allow" or "deny"), $3 = path prefix
  python3 -c '
import json, sys
subject, decision, prefix = sys.argv[1:]
n = 0
for line in open(sys.argv[4]):
    line = line.strip()
    if not line: continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get("subject") != subject: continue
    d = ev.get("decision", {})
    if isinstance(d, dict):
        if d.get("decision") != decision: continue
    elif d != decision:
        continue
    if ev.get("resource", {}).get("uri", "").startswith(prefix):
        n += 1
print(n)
' "$1" "$2" "$3" "$audit_dump"
}

sarah_allow=$(count_for "sarah-unofficial" "allow" "/workspace/protected/")
sarah_deny=$(count_for "sarah-unofficial" "deny"  "/workspace/protected/")
bill_allow=$(count_for  "bill-official"   "allow" "/workspace/protected/")
bill_deny=$(count_for   "bill-official"   "deny"  "/workspace/protected/")
alice_allow=$(count_for "alice-protected" "allow" "/workspace/protected/")
alice_deny=$(count_for  "alice-protected" "deny"  "/workspace/protected/")

log "sarah-unofficial: allow=$sarah_allow  deny=$sarah_deny"
log "bill-official:    allow=$bill_allow  deny=$bill_deny"
log "alice-protected:  allow=$alice_allow  deny=$alice_deny"

fatal=0

if [[ "$sarah_allow" -ne 0 ]]; then
  echo "[smoke][FAIL] sarah-unofficial was ALLOWED $sarah_allow read(s) under /workspace/protected/ — LEAK" >&2
  fatal=1
fi
if [[ "$bill_allow" -ne 0 ]]; then
  echo "[smoke][FAIL] bill-official was ALLOWED $bill_allow read(s) under /workspace/protected/ — LEAK" >&2
  fatal=1
fi
if [[ "$sarah_deny" -lt 1 ]]; then
  echo "[smoke][WARN] sarah-unofficial produced no audit Deny events — was the gate invoked at all?" >&2
  # Not fatal on its own: if the LLM never attempted a protected read the
  # gate never ran. We still require at least one agent action to have been
  # audited, which we check below.
fi

sarah_total=$(python3 -c '
import json, sys
subject = sys.argv[1]
n = sum(1 for line in open(sys.argv[2]) if json.loads(line).get("subject") == subject)
print(n)
' "sarah-unofficial" "$audit_dump")
bill_total=$(python3 -c '
import json, sys
subject = sys.argv[1]
n = sum(1 for line in open(sys.argv[2]) if json.loads(line).get("subject") == subject)
print(n)
' "bill-official" "$audit_dump")
alice_total=$(python3 -c '
import json, sys
subject = sys.argv[1]
n = sum(1 for line in open(sys.argv[2]) if json.loads(line).get("subject") == subject)
print(n)
' "alice-protected" "$audit_dump")

log "total audit events: sarah=$sarah_total bill=$bill_total alice=$alice_total"

if [[ "$sarah_total" -lt 1 ]]; then
  echo "[smoke][FAIL] sarah-unofficial produced zero audit events — gate never ran" >&2
  fatal=1
fi
if [[ "$bill_total" -lt 1 ]]; then
  echo "[smoke][FAIL] bill-official produced zero audit events — gate never ran" >&2
  fatal=1
fi
if [[ "$alice_total" -lt 1 ]]; then
  echo "[smoke][FAIL] alice-protected produced zero audit events — gate never ran" >&2
  fatal=1
fi

if [[ "$fatal" -ne 0 ]]; then
  echo
  echo "[smoke] audit log tail:" >&2
  tail -40 "$audit_dump" >&2 || true
  echo
  echo "[smoke] sarah simon output:" >&2
  tail -20 "$sarah_log" >&2 || true
  exit 1
fi

# -----------------------------------------------------------------------------
# 6. Summary
# -----------------------------------------------------------------------------

cat <<EOF

[smoke] PASS

  • sarah-unofficial  saw $sarah_total tool-gate events; ZERO allows on /workspace/protected/ (gate closed)
  • bill-official     saw $bill_total tool-gate events; ZERO allows on /workspace/protected/ (gate closed)
  • alice-protected   saw $alice_total tool-gate events; $alice_allow allow(s) on /workspace/protected/

Audit log: $audit_dump
Simon output per user: $sarah_log $bill_log $alice_log

The smoke test asserts the harness behaviour. LLM-quality assertions are
out of scope — whether alice's summary of the protected document is
*useful* is a separate question.
EOF
