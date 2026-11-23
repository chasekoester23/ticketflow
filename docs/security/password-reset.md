# Password reset by email: design

Status: **designed, not built.** It needs an email provider and a new table (owned by
the database workstream). Until both exist, the only ways back into an account are the
ones that exist today, and they stay:

- an **owner issues a temporary password** from the Team page (about 120 bits, shown
  once, and the person must choose their own before the API does anything else); and
- for a company's last owner, **`scripts/reset_password.py`** on the server, which is
  audited.

Nothing below changes either of those.

## What a person sees

1. "Forgot your password?" on the sign-in page asks for a username or email address.
2. Whatever is typed, the page says the same thing: *"If that matches an account, we've
   sent a link to its email address. It works once, for 30 minutes."*
3. The email holds one link: `https://<APP_ORIGIN>/reset#token=<token>`. The token is in
   the fragment, so it never reaches a server log, a proxy, or a `Referer` header.
4. The page posts the token and a new password. On success every session for that
   account ends, including the one that asked, and the person signs in again.

## The token

| Property | Choice | Why |
|---|---|---|
| Value | 32 random bytes (`secrets.token_urlsafe(32)`), about 256 bits | Unguessable; no rate limit is needed to protect it |
| Stored as | SHA-256 digest only, like session tokens | A database leak gives no usable link |
| Lifetime | 30 minutes | Long enough for slow email, short enough to limit a leaked inbox |
| Use | Single use: consumed in the same transaction that sets the password | A replayed link fails |
| Scope | One account; a newer request invalidates older unused tokens for it | Only the latest email works |
| Bound to | The account's current password hash (a digest of it is stored with the token) | Changing the password any other way voids outstanding links |

## Table for the database workstream (B)

```
password_reset_tokens
  token_digest      char(64)     primary key      -- sha256 hex of the token
  user_id           integer      not null references users(id) on delete cascade
  organization_id   integer      not null references organizations(id)  -- for row-level security
  password_marker   char(64)     not null         -- sha256 of users.password_hash when issued
  created_at        timestamptz  not null default now()
  expires_at        timestamptz  not null
  used_at           timestamptz  null
  requested_ip      varchar(64)  null             -- the real client address, for abuse review
index (user_id, created_at)
```

Rows older than a day are deleted by the same housekeeping that purges expired sessions.
Users also need an `email` column (unique, nullable, verified-at), which doesn't exist yet.

## Endpoints

- `POST /api/auth/password-reset` `{identifier}` → always `202 {"ok": true}`, after the
  same work whether or not the account exists (look up, then either create a token and
  queue the email, or do a dummy digest), so neither the answer nor its timing reveals
  an account. Public, behind the CSRF header gate like every mutating route.
- `POST /api/auth/password-reset/confirm` `{token, new_password}` → `200` and the account
  profile, or one generic `400 "That link has expired or was already used."` for every
  failure (unknown, expired, used, superseded or password changed since).

Confirming runs, in one transaction: look up the digest `FOR UPDATE`; check not used, not
expired, and the password marker still matches; `validate_new_password`; set the hash;
clear `must_change_password`; mark the token used; delete every other unused token for
the user; `revoke_sessions(user_id)` with nothing kept; write an `account_events` row
(`password_reset_by_email`, actor = the user). The response does **not** sign the person
in: they sign in with the new password, which also proves it was typed correctly.

## Limits

Per real client address (through `TRUSTED_PROXY_IPS`, see `internet-facing.md`) and per
account, kept in the database so they hold across processes:

- requests: 5 per address per 15 minutes, 3 per account per hour (excess answered with
  the same `202`, so the limit itself reveals nothing; the email just isn't sent);
- confirmations: 10 failures per address per 15 minutes, then `429`.

An edge rule on `/api/auth/password-reset*` (see `internet-facing.md`) is still wanted.

## Email

- From a no-reply address on the product's domain, with SPF, DKIM and DMARC set.
- Plain text plus simple HTML, no tracking pixels or click tracking (tracking rewrites
  the link and would put the token through a third party).
- Says which company and username it's for, when it expires, and "if this wasn't you,
  ignore this email: nothing has changed".
- A separate notice email after a successful reset: "your password was changed".

## Not doing

- Security questions, SMS codes, or reset by an owner without the person (the existing
  temporary-password flow covers owners helping their people, in person).
- Telling the requester whether the account exists, or showing a masked email address.
- Signing the person in from the link.

## Before building it

An email provider with a data processing agreement, the `email` column with
verification, the table above, and tests for: enumeration (same body and comparable
timing for known and unknown accounts), single use, expiry, superseding, password
changed since issue, sessions revoked, limits across two API processes, and the token
never appearing in any log line.
