# Trying single sign-on with Entra ID

Sign-in and group sync (AUTH-1, AUTH-3) are tested automatically against a
fake provider (`backend/tests/test_api_oidc.py`, including Entra-shaped
tokens) and against a real Keycloak (`frontend/e2e/sso.spec.ts`). This is
the one-time manual check against a real Entra ID tenant.

## 1. App registration and groups

`infra/entra/` is a small Terraform module (azuread provider) that creates the
app registration, its client secret and three security groups. Copy it into
your infra repo or apply it from here with an account that may register apps:

```sh
cd infra/entra
cp terraform.tfvars.example terraform.tfvars   # set redirect_uri; never commit it
terraform init && terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
terraform output                                # issuer, client_id, group_object_ids
terraform output -raw client_secret             # into your secret store
```

Then add a test user to the `annotators` group (Entra portal → Groups →
Members, or `groups.annotators.members` in the tfvars and apply again), and a
second one to `admins` if you want to check the superuser flag.

Doing it by hand instead: register a web app with redirect URI
`<APP_FRONTEND_URL>/api/v1/auth/oidc/callback`, add a client secret, under
*Token configuration* add the groups claim (Security groups, ID token, Group
ID) and the optional ID-token claims `email` and `upn`.

## 2. Configure the platform

```sh
APP_OIDC_ISSUER=<terraform output issuer>     # https://login.microsoftonline.com/<tenant>/v2.0
APP_OIDC_CLIENT_ID=<client_id>
APP_OIDC_CLIENT_SECRET=<client_secret>
APP_OIDC_REDIRECT_URI=<APP_FRONTEND_URL>/api/v1/auth/oidc/callback
APP_OIDC_DISPLAY_NAME=Microsoft
APP_OIDC_GROUPS_CLAIM=groups
APP_OIDC_ADMIN_GROUPS=<object id of the admins group>
```

In a project's settings, map the annotators group's **object id** (Entra
sends ids, not names) to a role:
`settings.idp_groups = {"<annotators object id>": "annotator"}`.

The redirect URI must be https, except for `http://localhost`; for a local
try-out use the compose frontend on `http://localhost:5173` and register that
callback.

## 3. What to check

| Step | Expected |
| ---- | -------- |
| Sign in with the annotator user | Lands on the project list; the project shows the user as annotator with an "SSO group" badge |
| Sign in with the admin user | The Users / Connectors / Models links are visible (superuser) |
| Remove the annotator from the group, sign out and in again | The membership is gone (Entra omits `groups` for a user in none) |
| Put the user in more than 200 groups (or use a tenant where they are) | Sign-in works and memberships stay as they were: Entra sends `_claim_names` instead of `groups` ("group overage"), which sync treats as "unknown". Set `group_claim = "ApplicationGroup"` and assign the groups to the app to avoid overage |
| Sign out | Also signs out of Microsoft (`end_session_endpoint`), back on `/login` |

If sign-in fails, the login page shows the error code (`?error=`); the
backend logs the reason under `oidc.callback_failed`. Common ones:
`invalid_id_token` (wrong issuer — must end in `/v2.0`), `no_email` (the user
has no mailbox and no UPN-like `preferred_username`), `token_exchange_failed`
(secret expired or redirect URI mismatch).

Report back what you saw; the Entra integration counts as verified when the
table above passes.
