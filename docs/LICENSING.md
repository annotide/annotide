# Licensing and editions

Covers LIC-1…LIC-9, the editions and what a key unlocks (LIC-32…LIC-36), what
the heartbeat collects and how it is protected, and what an install enforces
and how keys are delivered and renewed without any vendor service in the
customer's path (LIC-23…LIC-31).

## Editions (LIC-32)

Annotide is licensed under the **Elastic License 2.0** (`LICENSE.md`): anyone
may use, modify and run it, commercially too, but may not circumvent the
licence key or remove what it protects, and may not offer it to others as a
hosted or managed service. The edition is whatever the install's key allows.

| Edition | Key `tier` | Price | Users | Business features |
| ------- | ---------- | ----- | ----- | ----------------- |
| **Community** | no key | free, commercial use included | 3 active | no |
| **Team** | `team` | 12 €/user/month, or 29 €/month for 5 + 12 € per extra user; self-serve to 25 | the key's seats | no |
| **Business** | `business` | 20 €/user/month; self-serve to 25 | the key's seats | yes |
| **Enterprise** | `enterprise` | 10 €/user/month, annual, from 3 000 €/year | the key's seats | yes, plus offline keys, invoicing, contract terms |
| **Trial** | `trial` | free, 30 days, once per install (LIC-34) | 25 | yes |

**The rule** (replaces "every feature in every edition"): everything an
annotator or ML engineer works with is in every edition: every media type
and tool, pre-labelling with your own model, interactive segmentation, OCR,
active learning, import and export in every format, snapshots, splits and
lineage, every storage connector except SharePoint, the task queue and
review, API keys, the SDK, webhooks, the MCP server, MFA, GDPR export and
erasure, and all operations tooling. Signing in to customer services with a
cloud identity (Entra managed identity or service principal, AWS IAM role,
GCP workload identity) is part of the security baseline and in every
edition, as are keys, tokens and SAS. Cross-cloud identity federation is
planned as a Business feature. What an organisation's IT, security and
compliance need is Business:

| Feature id | What it unlocks | Requirement |
| ---------- | --------------- | ----------- |
| `sso` | OIDC sign-in (Entra ID or any provider), including group and role sync from the ID token | AUTH-1, AUTH-3 |
| `scim` | SCIM 2.0 provisioning and its token | AUTH-3 |
| `path_permissions` | Setting folder-level access (`membership.path_prefixes`) | item/folder permissions |
| `audit_history` | Audit events older than 30 days | audit log |
| `seat_report` | The seat report export | LIC-30 |
| `sharepoint` | New SharePoint / OneDrive connectors | connectors |
| `ml_platforms` | Azure ML, Databricks and MLflow integration | API-6 |
| `quality` | Consensus, agreement metrics, disagreement resolution, gold tasks and annotator accuracy | QA-1…QA-4 |

Never behind a key: data ownership (export, import, formats, reading),
the security baseline (MFA and its enforcement, GDPR erasure, audit
*recording*), and anything that would make a lapsed licence weaken security
(LIC-33).

## Organisation fingerprint

Extends the LIC-6 heartbeat. Lets the licence server cluster installs belonging
to one organisation **without learning any personal data** (LIC-9).

Every signal is an organisation-level identifier — a company, not a person — and
is sent as a **salted hash**, never in the clear. The server matches hashes
against hashes; it never learns the domain, the tenant or the account number.

| Signal | Example source | Why |
| ------ | -------------- | --- |
| Email domain | `acme.com` from user addresses | Strong. Public providers are excluded — see below. |
| SSO / IdP tenant | Entra tenant GUID | Decisive. A tenant *is* the company. |
| Cloud account | Azure subscription, AWS account, GCP project | Decisive. Installs in one subscription are one payer. |
| Storage account | the connector's target account or bucket | Strong. Shared storage means shared work. |
| Public hostname | `annotate.acme.com` | Strong when set. |
| Egress network | `/24` of the heartbeat source, ASN | Weak alone: corporate NAT and cloud egress are shared by unrelated parties. Corroboration only. |

**Never** the email address, user name, item path, annotation content, media or
IP address in full. The domain of `anna@acme.com` is `acme.com` — the local part
is dropped before hashing, so the hash cannot be reversed to a person even with
the salt.

### Excluded domains

Hashing `gmail.com` would cluster every unrelated hobbyist in the world into one
enormous false "organisation". Public email providers and consumer cloud
accounts are dropped from the fingerprint entirely, never hashed and never sent.
The list ships with the product and is visible to the admin.

The same goes for values every install shares by default rather than by
organisation: domains reserved for examples and testing (`example.com`,
`*.test`, `*.example`, `*.invalid`, `*.localhost`; RFC 2606 / 6761) and the
storage emulators' well-known accounts (Azurite's `devstoreaccount1`). Only the
storage *account* or *bucket* name is hashed, never a container name: names like
`media` or `data` are shared by unrelated installs.

### Salting

The salt is per-vendor and the hash is HMAC-SHA256. It is deliberately *not* a
per-install salt: the whole point is that two installs hashing `acme.com`
produce the same value. That makes the salt a shared value, not a secret: every
install holds it, so anyone with it can test a guess, and the space of company
domains is small. The hash keeps names off the wire and out of the vendor's
tables; it does not make them unguessable in transit, and the public pages
say so (<https://annotide.com/legal/telemetry/>). The salt is built into the
code (`core/config.py`); turning the heartbeat off is how an install sends no
fingerprint.

The licence server does not store the digests as sent: it hashes each one
again under a secret key only the server holds, and keeps only those. Equal
digests stay equal, so clustering works the same, but stored values can't be
tested against guesses with the public salt, so a copy of the vendor's
database does not reveal domains. The notes on what the fingerprint left out
and why are shown in the admin's preview only; they stay on the install and
are never sent.

## Clustering (LIC-18, LIC-19)

The vendor may group Community installs that share fingerprint hashes, to find
organisations that would be better served by one Team or Business install.
The rules that bind it:

- An egress-network match alone never raises anything.
- A cluster is only a sales lead. A human reads and approves before anything
  is sent; no automated message ever reaches a customer (LIC-19).
- Nothing happens to the installs. Community stays Community, and data is
  never locked, deleted or held hostage, at any stage.

## In-product usage and honesty prompt (LIC-20, LIC-35)

Cheaper and fairer than any detection: the install tells its admin plainly
where they stand, and never interrupts the people annotating.

- The admin always sees usage against the limit: "3 of 3 users" in
  Community, seats in use for a key, with the price of the next step beside it.
- A Community install that notices organisational use (the user's email
  domain is not a public provider, an OIDC provider is configured, a storage
  connector points at a corporate cloud account) shows the admin a one-time
  notice:

> This installation runs the free Community edition, for up to three people.
> It looks like a team is using it: Team adds people (12 €/user/month, or
> 29 €/month for five plus 12 € per extra user), Business adds single
> sign-on, SCIM, folder permissions and quality control. Try everything
> free for 30 days.

- A locked Business feature is visible where it would be configured, with a
  "Business" badge and the trial one click away, instead of being hidden.
- Banners go to admins only, never to annotators mid-task.

Most people comply when told plainly.

## Transparency (LIC-9)

The admin can see the exact heartbeat payload before it is sent, as sent —
hashes and all — on a settings page, plus which signals were collected and which
were excluded and why. Telemetry is on by default and can be switched off with
`APP_TELEMETRY_ENABLED=false`; Enterprise can use offline
renewal instead (LIC-6). An install with telemetry off is not presumed abusive:
it simply provides no signal.

## Enforcement without a vendor dependency

This section is what an unmodified install *enforces*, and how keys
reach it. Three constraints shape
it:

- **No vendor service in the customer's critical path.** Every check runs
  inside the install against the signed key. If the vendor's side is down for a
  month, no customer notices.
- **Honest customers never hit a wall.** Only unambiguous cheating is refused;
  everything else is a banner and an invoice.
- **Source-available means every check can be patched out.** Enforcement stops
  the casual and the careless, which is most of the lost revenue. Patching the
  checks circumvents the licence key functionality, which ELv2 forbids: a
  legal matter, not an engineering one.
  Compiled wheels, Cython or obfuscators would not change that (the source is
  public) and are not used.

### What an unmodified install enforces

| Situation | Response |
| --------- | -------- |
| Community (no key), a fourth human user | Refused: "Community is for up to three people". Service accounts don't count. (LIC-23) |
| Community with more than three users active (after a trial or a lapsed key) | Only the owner signs in until users are deactivated down to three (LIC-36) |
| A Business feature without a key that has it | 403 `license-feature`; what already exists keeps working and stays enforced (LIC-33) |
| Forged or edited key | Invalid, treated as no key (LIC-1) |
| System clock set back | No effect: expiry is evaluated against the latest date the install has ever seen (LIC-25) |
| Active users over seats, within the overage | Admin banner; overage shows in the seat report for true-up (LIC-24) |
| Active users beyond the overage | New users can't be activated; existing users are unaffected (LIC-24) |
| Key expired | LIC-5: banners, 30-day grace, then restricted mode — annotation and new tasks blocked, reading and export always work |
| Key bound to other hosts | Same path as expired (LIC-29) |
| Account sharing, service account doing human-volume work | Admin notice only, never a block (LIC-31) |

**Seats** count distinct human users active in the last 30 days (the LIC-4
window). Deactivating Anna and adding Ben uses two seats until Anna's last
sign-in is 30 days old, so rotating people through a seat does not help. The
overage is 10 % of seats, at least one; Community has none. The check runs at
sign-in, where a user becomes active. Users already active are never refused.
With a key, superusers are never refused either, so an admin can always get
in to sort things out. They still count.

### Keys: validity, delivery, renewal

- **`expires_at` is the end of the paid period** — a month for monthly
  payers, a year for annual. The install adds the LIC-5 grace on top. A key
  copied to an offline install stops working when the customer stops paying.
- **Two sources** (LIC-26): `APP_LICENSE_KEY`, and a key stored in the
  database, either pasted by the admin in settings or fetched by the licence
  refresh. The install uses the valid key that expires last. One licence is
  active at a time; seats never add up across keys.
- **The key is not a secret.** Its protection is the signature; hiding it from
  the people who run the install (they receive it by email) adds nothing. On
  Azure Container Apps a Key Vault secret referenced as `APP_LICENSE_KEY` is
  convenient. An offline install renews by updating that secret and restarting
  the revision. An online install renews by itself and never needs the env var
  touched again.

- **Revocation** (LIC-8). A refund, a chargeback or a leaked key is handled
  by a revocation list the vendor signs with the licence signing key. The
  licence refresh brings it; a build can also carry revocations compiled in.
  A revoked licence takes the expired path from its revocation date, grace
  included, like any other expiry. An install that never refreshes never
  hears of it and keeps working until `expires_at` — the key period still
  bounds use, which is why monthly payers get monthly keys.

### Business features and a lapsed licence (LIC-33)

A feature is *licensed* while the key in force lists it and is `valid`, or
`expired` within its grace period. A Community install, an `invalid` key, an
expired trial and a paid key past its grace have no Business features. An
unlicensed feature answers 403 `license-feature`, but **a lapse never weakens
security and never takes data away**:

| Feature | What stops | What keeps working |
| ------- | ---------- | ------------------ |
| `sso` | OIDC sign-in and group sync | Local sign-in. The owner always gets in (LIC-36) and can give SSO-only users a password |
| `scim` | The SCIM API and token | Users and groups SCIM created, as they are |
| `path_permissions` | Setting or changing a member's folders | Existing folder limits, still enforced; removing a member |
| `audit_history` | Reading audit events older than 30 days | Recording every event; the last 30 days |
| `seat_report` | The export | The seat counts on the licence page |
| `sharepoint` | Creating SharePoint connectors | Existing ones: scans, reads, exports |
| `ml_platforms` | Registering, checking and pulling from platforms | Model versions already imported |
| `quality` | Agreement, resolution, gold tasks, annotator accuracy; turning consensus or gold on | Consensus versions already written, readable as annotations |

### Trial (LIC-34)

An admin starts a 30-day trial from the licence page. The install asks the
licence service (`POST {APP_LICENSE_SERVER_URL}/v1/trial` with its install id
and host, nothing else) and stores the key it gets: tier `trial`, 25 seats,
every Business feature, bound to the host when it is a real one. The licence
service grants one trial per install id and one per host, and keeps that
record for three years after the trial ends. An offline install
asks for a trial key by email. An expired trial key is ignored: the install
is Community again at once, with no grace and no restricted mode, because a
trial was never paid for. Admins see the end date from the start and a
banner in the last 7 days. Asking for a trial is optional and changes
nothing if the licence service can't be reached (LIC-28).

### Community after a trial or a lapsed key (LIC-36)

LIC-23 alone would let a trial's 25 users stay active forever, since users who
are already active are never refused. So in Community mode, while more than
three human users are active, only the **owner** (the first superuser, by
creation) may sign in; everyone else is told the edition and asked to contact
the admin. The owner deactivates users down to three, after which the normal
rule applies. Granting superuser does not help: only the owner is exempt.

### Licence refresh and heartbeat

Two separate calls with different jobs. Neither one's failure changes how the
install behaves.

| | Licence refresh (LIC-27) | Heartbeat (LIC-6, LIC-16) |
| - | ------------------------ | ------------------------- |
| Who | Installs with a key | Any edition |
| Default | **On**; the admin can turn it off and paste keys by hand | **On**; the admin can turn it off (LIC-9) |
| Sends | licence id, install id, host, active user count, version | install id, version, edition, active user count, salted-hash organisation fingerprint |
| Receives | the renewed key, if one exists, and the revocation list | nothing |
| Interval | daily, with retries | weekly |
| Vendor learns | paid installs, copied keys, seat usage | Community clusters; install counts by version and edition, in aggregate |

The licence refresh is licence accounting, not telemetry: no fingerprint, no
user identifiers. It is on by default because that is how a paying customer's
key renews without anyone touching it. This document says so plainly, and the
admin sees the exact payload the same way as the heartbeat's (LIC-21).

On the vendor side, a Paddle webhook calls a small function that signs a key
and emails it. The same key is published at the address the refresh reads. If
that address is unreachable, installs keep working on the key they already
hold. That function is the vendor's licence service, which runs outside every
install.

### Binding a key to its host (LIC-29)

A key used in one install must not simply work in a second one.

- **Not an install id.** `APP_INSTALL_ID` is configuration. Any id or hash
  derived from what the customer holds — install id, first-run date, a
  database row — is copied along with the env or the database. Machine ids
  are meaningless in containers, and binding to one would break every
  reschedule.
- **The host is what users depend on.** A key may carry `hosts`
  (`annotate.acme.com`, `*.acme.com`). The install compares them with the host
  browsers reach it on (`Host` / `X-Forwarded-Host` of user requests).
  Loopback is always allowed. A mismatch takes the expired path, so moving to
  a new domain gives 30 days to get a new key and never breaks anything
  overnight.
- **Binding is automatic.** If the customer names a domain at checkout, the
  key carries it. Otherwise the first licence refresh binds the key to the
  host it reports. A second install refreshing the same licence from a
  different host gets no key for its host, and the vendor sees the copy.
  Extra hosts (staging) are added on request.
- **Unbound keys** (no `hosts`) are for offline Enterprise installs, or on
  request. The seat report covers them.

A reverse proxy that rewrites `Host` defeats the check. That takes deliberate
effort and is a breach, which is the point: binding stops casual copying, not
determined evasion.

### Offline with a key

An install that never reaches the vendor still enforces everything in the
table above. It can use only what was paid for, until `expires_at` plus
grace. Copying the key to another install fails the host check unless the
copier also fakes the host. For offline and Enterprise customers, the admin
exports a **seat report** (LIC-30): distinct active users per period and peak
overage. It goes to the vendor at renewal. The install cannot sign it in any
way the vendor could trust, so the licence terms back it, not cryptography.

### Deliberately not done

- Requiring the vendor to be reachable for the product to work.
- Obfuscation, DRM or compiled builds as protection.
- Limiting concurrent sessions per user — annotators work in several tabs and
  on several machines, and tokens are stateless.
- Locking, deleting or withholding data at any stage.
- Any automatic contact or accusation (LIC-19).

## Requirement IDs added

| ID | Requirement | Prio |
| -- | ----------- | ---- |
| LIC-16 | The heartbeat carries a salted-hash organisation fingerprint (email domain, SSO tenant, cloud account, storage account, public hostname, egress network). No plaintext identifier and no personal data ever leaves the install. | S |
| LIC-17 | Public email providers and consumer cloud accounts are excluded from the fingerprint and never sent. | M |
| LIC-18 | The licence server clusters Community installs by shared fingerprint signals; an egress-network match alone never raises a signal. | S |
| LIC-19 | A clustering signal never triggers automatic contact, billing or restriction. A human approves every outbound message (ADM-7). | M |
| LIC-20 | A Community install that detects organisational use shows the admin a one-time notice about Team, Business and the trial. | M |
| LIC-21 | The admin can view the exact fingerprint payload before and as it is sent, and disable telemetry entirely (LIC-9). | M |
| LIC-22 | No cross-installation federation in Community mode — no shared schema registry, snapshot merge or federated queue. Federation, if built, is Business+ and licensed on total federated seats. | M |
| LIC-23 | Community mode (no key) allows three active human users; activating a fourth is refused with a licence message. Service accounts are exempt. | M |
| LIC-24 | Seats are distinct human users active in the last 30 days (LIC-4 window). Up to 10 % over (min. 1) shows an admin banner and is reported for true-up; beyond that users who are not already active cannot sign in. Active users, and superusers of a keyed install, are never refused. | M |
| LIC-25 | Licence expiry is evaluated against the latest date the install has recorded, so setting the clock back has no effect. | S |
| LIC-26 | Keys come from `APP_LICENSE_KEY` or the database (admin paste or refresh). The valid key that expires last wins. One licence is active at a time; seats never add up across keys. | M |
| LIC-27 | Keyed installs run a daily licence refresh (licence id, install id, host, active user count → renewed key). On by default, can be disabled, payload visible to the admin. Failure or being disabled never changes behaviour. Not telemetry: no fingerprint, no user identifiers. | S |
| LIC-28 | No enforcement depends on reaching a vendor service. Key `expires_at` is the end of the paid period; expiry follows LIC-5 and never blocks reading or export. | M |
| LIC-29 | A key may carry `hosts`; the install compares them with the host users reach it on (loopback always allowed). A mismatch follows the expired path. Keys are bound at checkout or on first refresh. | S |
| LIC-30 | The admin can export a seat report (distinct active users per period, peak overage) for offline true-up. | S |
| LIC-31 | Signs of account sharing (parallel sessions from different networks, parallel task claims, faster than human throughput) and service accounts doing human-volume annotation are shown to the admin as a notice. Never a block. | C |
| LIC-32 | Editions are Community (no key), Team, Business, Enterprise and Trial. Annotation work, data ownership and the security baseline are in every edition; the Business features (`sso`, `scim`, `path_permissions`, `audit_history`, `seat_report`, `sharepoint`, `ml_platforms`, `quality`) need a key that lists them. Identity credentials to customer services are part of the security baseline. Replaces LIC-2. | M |
| LIC-33 | An unlicensed Business feature answers 403 `license-feature`. A lapse never weakens security or takes data away: existing folder limits stay enforced, audit recording continues, existing connectors and imported versions keep working, local sign-in always works. | M |
| LIC-34 | An admin can start a 30-day trial (25 seats, every Business feature) from the licence page; the licence service grants one per install id and per host. An expired trial key is ignored, with no grace. | S |
| LIC-35 | The admin sees usage against the limit (users of 3, seats of N) with the price of the next step, and locked Business features with a badge where they would be configured. Banners never reach annotators. | S |
| LIC-36 | In Community mode with more than three active human users, only the owner (the first superuser) may sign in until users are deactivated down to three. | M |
