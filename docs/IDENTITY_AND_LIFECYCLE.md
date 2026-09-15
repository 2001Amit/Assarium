# Identity, accounts, and connection lifecycle

Two questions drove this document:

1. **How does someone get an account?** Not every customer has a Microsoft directory. The
   current design reads the tenant from an Entra `tid` claim, which means Assarium only
   works for Microsoft shops. That is wrong for a product sold to anyone.
2. **A customer connects a source today. What guarantees it is still connected next month?**
   And when it genuinely does break, how does the system tell the difference between
   "the network blipped" and "an admin revoked our access"?

Both are answered from how the comparable platforms actually do it, cited at the end.

---

# Part A — Identity

## A1. The mistake in the current design

`Tenant.entra_tenant_id` is the join key. `get_tenant_context()` reads `tid` from a
Microsoft token and looks the tenant up by it.

That makes a Microsoft directory a **precondition for having an account**. A REIT running
on Google Workspace, or a firm with no IdP at all, cannot sign up. The security property is
right — tenant from the token, never from the request — but it is bolted to one vendor.

The fix keeps the property and drops the vendor.

## A2. The model

```
User             a person. Globally unique by verified email.
Identity         one way that person proves who they are.
                 A user may have several: password, Google, Microsoft, their org's SSO.
Organization     the customer. Owns the catalog, the storage container, the
                 connections, the dashboards, the bill. This is the tenant.
Membership       (user, organization, role). A user may belong to several organizations
                 with a different role in each.
Session          a refresh-token family. Revocable, and scoped to one organization.
```

The important separation: **a user is not a tenant, and an email domain is not a tenant.**
An organization is. A consultant working for three REITs is one user with three
memberships, and switching between them is an explicit act that re-issues the session.

### Where the tenant comes from now

The session token the API issues carries `org_id`, signed by us. The chain is:

```
credential  ->  User (verified)  ->  Membership (active)  ->  org_id in session
```

A request still cannot name its tenant. Switching organization is a dedicated endpoint that
re-checks membership and mints a new session; it is not a header. So the rule from
`ARCHITECTURE.md` §1.3 survives intact — it just no longer depends on Microsoft issuing the
token.

## A3. Three ways in, matched to who is buying

This is the shape Snowflake, Databricks and Datadog all converge on, and the reason is
commercial as much as technical: enterprises will not adopt a tool they cannot put behind
their own IdP, and small teams will not wait two weeks for a SAML setup call.

| Tier | Sign-in | Provisioning | For |
|---|---|---|---|
| **Self-serve** | Email + password, or Google / Microsoft social | Self-signup, invitations | Trials, small teams |
| **Team** | The above, with MFA enforced org-wide | Domain-verified auto-join | Growing customers |
| **Enterprise** | SAML 2.0 or OIDC to the customer's own IdP | JIT on first login, SCIM for lifecycle | The REIT clients |

Enterprise SSO is an **organization-level setting**, not a global one. One customer arrives
through Okta SAML, the next through Entra OIDC, a third through Google — all against the
same application.

### The tier boundary that matters

Once an organization connects an IdP and turns on **SSO enforcement**, password and social
login must be *refused* for that organization's members. Otherwise SSO is decorative: the
customer believes their conditional access policies apply, and a member quietly keeps
signing in with a password that bypasses all of it.

With one deliberate exception, below.

## A4. MFA

The research here is unambiguous and worth following exactly.

CISA and NIST SP 800-63-4 recognise only **FIDO2/WebAuthn and PIV/CAC** as phishing
resistant. SMS OTP, voice, email magic links and simple approve/deny push are explicitly
excluded — an adversary-in-the-middle proxy defeats all of them, and TOTP too.

So:

- **Passkeys (WebAuthn) are the primary factor.** Origin-bound, so a proxy on a lookalike
  domain cannot relay them.
- **TOTP is the fallback**, offered because passkey support is not universal yet, and
  labelled in the UI as the weaker option rather than presented as equal.
- **No SMS.** Not as an option, not as a fallback. Offering it means an attacker picks it.
- **Recovery codes**: ten, single-use, shown once, stored hashed. Regenerating invalidates
  the previous set.
- **Organization-level enforcement**, the way Snowflake's authentication policies work: an
  owner turns on "require MFA", existing members are given a grace period to enrol, and new
  members enrol before their first real action.

### The rule people get wrong

NIST 800-63-4 is explicit that **recovery must not undercut the assurance level.** If MFA is
enforced but "forgot my device" sends an email link that restores full access, then MFA is
optional and the attacker simply takes the email path.

So recovery requires either a recovery code, or an approval from another organization owner.
Never email alone.

### Break-glass

If an organization enforces SSO and their IdP goes down or their certificate expires,
**every one of their users is locked out, including the admins who could fix it.** This
happens often enough that it needs a designed answer rather than a support ticket.

One named owner account may be flagged break-glass: it keeps password + MFA login when SSO
is enforced. Its every use is logged and notified to all other owners. That trade is
deliberate — a slightly larger attack surface in exchange for an organization that can
always recover itself.

## A5. Domain verification, and the attack it prevents

Domain-based auto-join is what makes company-wide adoption work: a colleague signs up with
`@acme.com` and lands in the Acme workspace instead of creating a second, orphaned one.

It is also the exact mechanism behind the invite-spam and impersonation attacks that hit
Slack. Two rules make it safe:

1. **Ownership is proven by DNS, not by email.** A TXT record on the domain. "We sent a
   code to admin@acme.com" proves control of one mailbox, not of the domain.
2. **Public email providers can never be claimed.** gmail.com, outlook.com, yahoo.com and
   the rest are on a denylist. Without it, one person claims gmail.com and auto-joins every
   Gmail user on the platform into their organization.

An unverified domain gets **request-to-join** — the person is told the organization exists
and admins are notified — never silent auto-join.

## A6. The edge cases, and what each one does

These are the ones that turn into incidents if left undecided.

| Situation | Behaviour |
|---|---|
| Signs up with a personal address, no org | They create one and become its owner |
| Signs up with a domain a verified org owns | Auto-join if that org allows it, otherwise request-to-join with admins notified |
| Belongs to several organizations | Org switcher; the session is scoped to one at a time |
| **Last owner tries to leave or be deleted** | **Refused.** An organization with no owner cannot be recovered. Transfer ownership first |
| Invited, then signs in via SSO instead | Reconciled to the same user by verified email — not a duplicate account |
| Invitation | Single-use, 7-day expiry; re-inviting invalidates the previous token |
| Changes their email | Re-verification required. If the new domain belongs to another org with auto-join, they are **not** moved — moving someone between tenants on an email edit is a data-access change disguised as a profile edit |
| Removed from an organization | Sessions for that org revoked immediately, not at token expiry |
| Deactivated by SCIM | Access denied at once; their audit records and authored objects stay, attributed to them |
| Repeated failed passwords | Exponential backoff plus a challenge — **not** a hard account lock, which is a denial-of-service against any known email address |
| Signup or reset for an address that may not exist | Identical response either way, so the endpoint cannot be used to enumerate customers |
| Refresh token replayed after rotation | Treated as theft: the whole token family is revoked and the user must sign in again |

That last one is worth stating plainly, because rotation without it is close to useless.
Every refresh issues a new token and invalidates the old one. If the old one is ever
presented again, either it was stolen or it was cloned — both mean the family is
compromised, so all of it is revoked at once.

## A7. Sessions

- Access token: **10 minutes**, so removing someone takes effect in minutes without a
  database lookup on every request.
- Refresh token: 30 days, rotating on each use, with the reuse detection above.
- Every session row records device, IP and last-used, and is listed in the UI so a user can
  see and end their own sessions.
- Changing a password, enrolling or removing an MFA factor, or leaving an organization
  revokes the relevant sessions.

## A8. Build or buy

SAML is a swamp — signed assertions, certificate rotation, IdP-specific quirks, clock skew,
metadata refresh. SCIM is a second one.

The recommendation:

- **Build** the user / organization / membership / session / MFA core. It is core domain, it
  binds directly to the tenant catalog, and it is the part a vendor cannot model for us.
- **Put enterprise SAML/OIDC and SCIM behind an interface** so it can be served either by our
  own implementation or by a provider (WorkOS, Auth0, Clerk) without touching anything above
  the interface.

Everything in this document except the SAML/SCIM implementation itself is identical either
way, so this decision does not block the work — it decides one adapter later.

---

# Part B — Connection lifecycle

The requirement: *a source connected today is still connected next month, unless the user
disconnects it.*

The honest version of that promise is more useful, because it is one the system can actually
keep:

> **A connection is never removed except by the user. But it can stop being able to
> authenticate, and when that happens the platform must say so, say why, and make fixing it
> one click — without losing a single thing the user configured.**

## B1. There are exactly five reasons a connection stops working

Only the first is the user's own doing. The others must be told apart, because the fix
differs and the wrong response makes each of them worse.

### 1. The user disconnects it
Intentional. Confirm, stop the schedules, delete the Key Vault secret, keep the ingested
data for a retention window so an accidental click is recoverable.

### 2. The refresh token expires from disuse
Microsoft refresh tokens expire after **90 days without use**; each use rotates them and
restarts the clock.

So a connection whose schedule is paused for a quarter dies of neglect — nobody revoked
anything, it simply was not used. This is the failure mode that fits the user's question
most exactly, and it is invisible until someone tries to sync.

**The fix is a keep-alive.** A job refreshes every stored OAuth token on a cadence well
inside the shortest expiry window — every 14 days — whether or not a sync is due. The token
stays alive because it is being used.

### 3. Authorization is revoked at the source
An Entra admin removes the app; a Salesforce admin revokes the connected app; someone
withdraws consent.

Salesforce **will not tell us**. Requests simply start failing, and refreshes return
`invalid_grant`.

No amount of retrying fixes this — only a human re-authorizing. Retrying makes it worse by
attracting rate limits.

### 4. Salesforce's five-token limit
Salesforce keeps at most **five concurrent access/refresh token pairs per user per connected
app**. A sixth authorization silently revokes the oldest pair.

So a user authorizing the same app from another tool can kill our connection without
touching Assarium, and without any error at the moment it happens.

### 5. Something changed underneath
A database password rotated, an IP allowlist narrowed, a conditional access policy tightened,
a selected table renamed or dropped, a column's type changed, or a permission narrowed so
authentication still succeeds but one table is no longer readable.

## B2. So a connection needs more than connected/disconnected

```
healthy         last run succeeded
degraded        some selected tables failed, the rest are fine
needs_reauth    credentials rejected - only the user can fix this
paused          the user paused it
disconnected    the user removed it
```

`degraded` earns its place: one table losing permission is not a broken connection, and
showing it as one sends people to re-authorize something that was never the problem.

## B3. The rules that make the promise hold

**A failed sync never disconnects a connection.** Failure sets state. It does not delete
configuration. Table selections, schedules, watermarks, semantic model and dashboards all
survive untouched — this is the direct answer to the original question.

**Reconnecting resumes; it does not rebuild.** The user re-authorizes, the same connection id
keeps its selections and its watermarks, and the next run picks up exactly where the last
successful one stopped. They do not re-pick their tables and they do not re-ingest history.

**Errors are classified, not merely counted:**

| Class | Examples | Response |
|---|---|---|
| Transient | timeout, 429, 503, connection reset | Retry with backoff. Do not notify yet |
| Permanent auth | 401, `invalid_grant`, revoked consent | Stop retrying at once. `needs_reauth`. Notify immediately |
| Permission | 403 on one object | `degraded`, name the object |
| Schema | table gone, column type changed | `degraded`, propose the change |

Retrying a revoked credential is not merely useless, it is harmful — it burns rate limit and
can trip the source's own lockout.

**Notify on the second consecutive failure, not the first.** Fivetran does exactly this, and
the reason is sound: most first failures are transient, and a platform that pages someone
for every blip trains them to ignore it. The exception is `needs_reauth`, which is never
transient and is notified at once.

**Connections belong to the organization, not to the person who created them.** When an
employee leaves, the pipeline must not stop.

But OAuth tokens for SaaS sources *are* bound to a person, so we record whose authorization
backs each connection. When that person is deprovisioned, the connection goes
`needs_reauth` with an honest message — "authorized by Priya, who no longer has access; an
admin needs to re-authorize" — rather than a generic failure nobody can act on.

**Where the source supports app-level credentials, prefer them.** Microsoft Graph application
permissions and Salesforce's JWT bearer flow authenticate as the *application*, not as an
employee. A connection built that way survives anyone leaving. Offer it as the recommended
option for exactly that reason, and explain the trade — it needs an admin to grant it once.

## B4. Proactive checks, so problems are found before a sync does

| Check | Cadence | Catches |
|---|---|---|
| Token keep-alive refresh | every 14 days | reason 2, before it happens |
| Lightweight credential probe | daily | reasons 3, 4, 5 — while nobody is waiting |
| Selected-object check | before each run | renamed or dropped tables |
| Expiry forecast | daily | client secrets and certificates nearing expiry, warned 30 days out |

The point of the daily probe is that a connection should not first appear broken at 2am
inside a pipeline run. It should be found at a civilised hour, by a cheap call, with a
notification that has a reconnect button in it.

---

## What this changes in the codebase

| Area | Now | After |
|---|---|---|
| Tenant resolution | `Entra tid` claim | Organization membership carried in our own session |
| Sign-in | Entra only | Password, social, and per-organization enterprise SSO |
| MFA | none | Passkeys primary, TOTP fallback, recovery codes, org enforcement |
| Sessions | none | Rotating refresh tokens with reuse detection, revocable, listed |
| Connection state | implicit | Explicit five-state model with classified errors |
| Token upkeep | none | Keep-alive refresh, daily probe, expiry forecast |
| Failure handling | run fails | State change, second-failure notification, one-click reconnect |

The isolation rule does not change: **the tenant is never taken from the request.** It moves
from a Microsoft claim to our own signed session, and stays server-resolved.

---

## Sources

- [Best Authentication Solutions for Multi-Tenant B2B SaaS — Descope](https://www.descope.com/blog/post/auth-multi-tenant-b2b-saas)
- [The complete guide to user management for B2B SaaS — WorkOS](https://workos.com/blog/user-management-for-b2b-saas)
- [Multi-Tenant Identity Management for SaaS — SSOJet](https://ssojet.com/blog/multi-tenant-identity-management)
- [MFA enrollment enforced by default for new Snowflake accounts — Snowflake](https://docs.snowflake.com/en/release-notes/bcr-bundles/2024_08/bcr-1784)
- [Snowflake Admins Can Now Enforce Mandatory MFA — Snowflake](https://www.snowflake.com/en/blog/snowflake-admins-enforce-mandatory-mfa/)
- [Phishing-Resistant MFA Buyer's Guide to NIST SP 800-63-4 & OMB M-22-09 — WWPass](https://www.wwpass.com/blog/phishing-resistant-mfa-in-2025-buyer-s-guide-to-nist-sp-800-63-4-omb-m-22-09/)
- [Configurable Token Lifetimes — Microsoft Learn](https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes)
- [Salesforce OAuth refresh token invalid_grant — Nango](https://nango.dev/blog/salesforce-oauth-refresh-token-invalid-grant/)
- [Manage OAuth Access Policies for a Connected App — Salesforce](https://help.salesforce.com/s/articleView?language=en_US&id=sf.connected_app_manage_oauth.htm&type=5)
- [Why email verification is crucial for B2B applications — Stytch](https://stytch.com/blog/why-email-verification-is-crucial-for-b2b-apps/)
- [Refresh Token Rotation — Auth0 Docs](https://auth0.com/docs/secure/tokens/refresh-tokens/refresh-token-rotation)
- [OAuth2 Cheat Sheet — OWASP](https://cheatsheetseries.owasp.org/cheatsheets/OAuth2_Cheat_Sheet.html)
- [Fivetran Notifications](https://fivetran.com/docs/using-fivetran/fivetran-dashboard/user-management/notifications)
- [Connection status — Airbyte Docs](https://docs.airbyte.com/platform/cloud/managing-airbyte-cloud/review-connection-status)
- [Authenticate access to Azure Databricks using OAuth token federation — Microsoft Learn](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/auth/oauth-federation)
