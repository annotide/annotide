# Single sign-on with SAML 2.0

Annotide signs people in through any SAML 2.0 identity provider: Entra ID,
Okta, Google Workspace, ADFS, Keycloak, Shibboleth. It works beside OIDC
([ENTRA.md](ENTRA.md) covers OIDC with Entra ID) and local accounts, and
needs a licence with the `sso` feature (Business). If your IdP offers OIDC
as well, either works; SAML is here for the IdPs and procurement checklists
that ask for it.

What is supported: service-provider-initiated sign-in (the user starts on
Annotide's login page), signed assertions or responses, account creation on
first sign-in, linking to existing accounts by e-mail, and group sync for
roles (AUTH-3), the same as OIDC. SCIM provisioning works alongside it.

Not supported: sign-in started from the IdP's app launcher (IdP-initiated;
the response would not be tied to the browser that receives it), encrypted
assertions (TLS protects the transport), single logout, and transient
NameIDs.

## 1. The two URLs your IdP needs

With the API at `https://annotate.acme.com`:

| IdP field | Value |
| --------- | ----- |
| Entity ID / Identifier / Audience URI | `https://annotate.acme.com/api/v1/auth/saml/metadata` |
| ACS / Reply URL / Single sign-on URL | `https://annotate.acme.com/api/v1/auth/saml/acs` |

Most IdPs can also read both from the metadata URL itself (the first one).
Use the HTTP-POST binding, sign the assertion (or the response), and leave
assertion encryption off.

## 2. Configure Annotide

```sh
APP_SAML_IDP_METADATA_URL=<the IdP's metadata URL>   # or APP_SAML_IDP_METADATA_FILE
APP_SAML_ACS_URL=https://annotate.acme.com/api/v1/auth/saml/acs
APP_SAML_TRUST_EMAIL=true          # see below
APP_SAML_ADMIN_GROUPS=<group id>   # optional: members become superusers
```

In the Helm chart: `saml.idpMetadataUrl`, `saml.trustEmail`,
`saml.adminGroups`; the ACS URL comes from `publicUrl`. The login page then
shows *Sign in with Single sign-on (SAML)* (`APP_SAML_DISPLAY_NAME`).

**`APP_SAML_TRUST_EMAIL`.** SAML has no `email_verified`. Off, a first SAML
sign-in creates a new account or is refused when an account with that e-mail
already exists (`email_unverified`). Turn it on when the IdP's addresses come
from a directory users cannot edit, which is true of Entra ID, Okta and
Google Workspace; then existing local accounts are linked on first sign-in.
Accounts that SCIM created are linked either way.

**Attributes.** Annotide looks for the e-mail in `email`, `mail` or
`emailaddress`, the display name in `displayname` or `name`, and groups in
`groups`. A name matches exactly or as the last segment of a URI name, so
Entra's `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress`
and `…/claims/groups` work as they are. Change the lists with
`APP_SAML_EMAIL_ATTRIBUTE`, `APP_SAML_NAME_ATTRIBUTE` and
`APP_SAML_GROUPS_ATTRIBUTE`. With no e-mail attribute, an `emailAddress`
NameID is used.

**The NameID** identifies the person from then on (stored as
`saml:<NameID>`). Prefer a persistent one that never changes, such as an
object id; an e-mail NameID works but breaks the link if the address
changes. A transient NameID is refused.

**One protocol per person.** An account links to one IdP identity. Someone
who already signs in through OIDC gets `account_conflict` on a SAML sign-in
with the same e-mail. There is no unlink action in the app yet, so moving
an existing install from OIDC to SAML means clearing `user.idp_subject` for
the people concerned in the database; ask support before doing that.

## 3. Entra ID

1. Entra admin center → Enterprise applications → New application → Create
   your own application → *Integrate any other application you don't find in
   the gallery*.
2. Single sign-on → SAML. *Basic SAML Configuration*: Identifier and Reply
   URL from step 1.
3. *Attributes & Claims*: set *Unique User Identifier* to `user.objectid`,
   format *Persistent*. Add a claim `displayname` = `user.displayname`
   (Entra's default `name` claim is the UPN). For roles, *Add a group claim*
   → Security groups; the values are group object ids, which is what
   `APP_SAML_ADMIN_GROUPS` and project group mappings then name.
4. *SAML Certificates* → copy *App Federation Metadata Url* into
   `APP_SAML_IDP_METADATA_URL`. Entra signs the assertion by default.
5. Users and groups → assign who may sign in.

## 4. Okta

1. Applications → Create App Integration → SAML 2.0.
2. *Single sign-on URL* and *Audience URI (SP Entity ID)* from step 1.
   *Name ID format*: Persistent (or EmailAddress); *Application username*:
   Okta username or email.
3. *Attribute Statements*: `email` = `user.email`, `displayname` =
   `user.displayName`. *Group Attribute Statements*: `groups`, with a filter
   for the groups that matter.
4. Sign On tab → copy the *Metadata URL* into `APP_SAML_IDP_METADATA_URL`.
5. Assignments → assign people or groups.

## 5. Google Workspace

1. Admin console → Apps → Web and mobile apps → Add app → Add custom SAML app.
2. Download the IdP metadata and set `APP_SAML_IDP_METADATA_FILE` to it
   (Google offers no metadata URL; mount the file, e.g. from a ConfigMap).
3. Service provider details: ACS URL and Entity ID from step 1, Name ID
   format *EMAIL*, Name ID *Basic Information → Primary email*.
4. Attribute mapping: *Primary email* → `email`; for roles, *Group
   membership* → `groups`.
5. Turn the app on for the organisational units that should use it.

## 6. Trying it locally with Keycloak

The dev Keycloak in `docker-compose.sso.yml` has a SAML client
(`annotide-saml`) beside the OIDC one, and the override configures both, so
its login page shows two buttons. User `erin` / `erin-dev-password` is for
SAML; alice and carol are for OIDC:

```sh
docker compose -f docker-compose.yml -f docker-compose.sso.yml up -d --wait keycloak backend worker
```

`frontend/e2e/sso-saml.spec.ts` drives the sign-in end to end
(`E2E_SSO=1 npx playwright test sso-saml`).

## 7. When it fails

The login page says why, and the API logs `saml.acs_failed` with the detail:

| Code | Usually |
| ---- | ------- |
| `invalid_response` | signature does not verify against the metadata (certificate rolled over: the metadata URL catches up within the hour), clocks more than a minute apart, Entity ID or ACS mismatch, or an encrypted assertion |
| `state_mismatch` | the response answers another sign-in: started from the IdP's launcher, or in another tab |
| `flow_expired` | more than 10 minutes at the IdP, or the cookie was dropped: over https the API must be reached on the same site it sets cookies for |
| `unsupported_name_id` | the IdP sends a transient NameID |
| `no_email` | no e-mail attribute and no e-mail NameID |
| `email_unverified` | an account with that e-mail exists and `APP_SAML_TRUST_EMAIL` is off |
| `no_idp` | the metadata describes several IdPs: set `APP_SAML_IDP_ENTITY_ID` |
| `provider_unreachable` | the metadata URL or file cannot be read |
