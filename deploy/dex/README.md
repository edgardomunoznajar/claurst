# Dex config for the Simon demo

This directory contains the Dex OIDC provider configuration used by the
Simon demo stack. It is mounted read-only into the upstream
`ghcr.io/dexidp/dex:v2.40.0` image at `/etc/dex/config.yaml`.

## Demo users

All three users share the password `Password!1` (bcrypt cost 10).

| Username | Email              | OIDC `sub`          | Clearance          |
|----------|--------------------|---------------------|--------------------|
| sarah    | sarah@simon.demo   | `sarah-unofficial`  | unofficial         |
| bill     | bill@simon.demo    | `bill-official`     | official           |
| alice    | alice@simon.demo   | `alice-protected`   | protected          |

(There is no demo user for `official-sensitive`. Add one by appending
another `staticPasswords` entry with `userID: "<name>-official-sensitive"`
if you need it.)

## How clearance is transmitted

Dex v2.40 has no first-class support for per-user static claims besides
the handful it knows about (`sub`, `email`, `name`, `groups`). The
`groups` field in `staticPasswords` does not exist as of v2.40, and the
workaround via connectors needs either Kubernetes or LDAP.

For the demo we piggy-back on `sub`: the `userID` field in
`staticPasswords` is emitted verbatim as the OIDC `sub` claim. We encode
clearance as the suffix after the last `-`. Simon's OIDC login code
splits `sub` on `-`, takes the last token, and maps it onto the
`Clearance` enum.

This is a hack and it is clearly marked as such in:

- the header comment of `config.yaml`
- Simon's OIDC client (parallel branch)
- the top-level `deploy/README.md`

A production Dex deployment would emit a proper `clearance` claim (or a
`groups` list containing `clearance:*`) from whatever upstream IdP backs
it. See `docs/simon/oidc-production.md` (parallel branch) for the
upgrade path.

## Password hash

The bcrypt hash in `config.yaml` corresponds to the literal string
`Password!1`. Regenerate with:

```bash
htpasswd -bnBC 10 "" 'Password!1' | tr -d ':\n'
```

Dex rejects any other hash format.

## Health check

Dex exposes `/healthz` on its HTTP listener. Docker compose uses it as
the `healthcheck` target for the `dex` service.
