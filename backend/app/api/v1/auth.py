"""Local authentication (AUTH-2).

Phase 1 ships username-and-password login so a Compose install works with no
identity provider. OIDC (AUTH-1) sits beside it under ``/auth/oidc/*`` when
``APP_OIDC_ISSUER`` is set (the flow itself is in ``services/oidc.py``), and
SAML 2.0 under ``/auth/saml/*`` when ``APP_SAML_IDP_METADATA_*`` is set
(``services/saml.py``). Every path ends at
:func:`app.core.security.create_access_token`, and the rest of the API only
ever sees the resulting token.

Installations that disable local accounts leave every user's ``password_hash``
null, which this router treats as "cannot log in locally".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Cookie, Form, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from app.api.deps import (
    ClientIpDep,
    CurrentUserDep,
    LicenceDep,
    OidcClientDep,
    RequestHostDep,
    SamlClientDep,
    SessionDep,
    SettingsDep,
    enforce_rate_limit,
)
from app.api.errors import (
    MfaInvalidError,
    MfaRequiredError,
    NotFoundError,
    SeatLimitError,
    UnauthorizedError,
)
from app.core.config import Settings
from app.core.security import create_access_token, hash_password, verify_password
from app.models import User
from app.schemas import (
    AuthProviders,
    LoginRequest,
    OidcProviderInfo,
    TokenResponse,
    UserPreferencesUpdate,
    UserRead,
)
from app.services import audit, mfa, rate_limit
from app.services import saml as saml_flow
from app.services.licensing import seats
from app.services.licensing.features import Feature, has_feature
from app.services.licensing.state import effective_license
from app.services.oidc import (
    FLOW_TTL,
    OidcDisabledError,
    OidcError,
    complete_sign_in,
    decode_flow,
    encode_flow,
    new_flow,
)

log = structlog.get_logger(__name__)

#: Provider errors that mean "no session for a silent sign-in" (OIDC Core §3.1.2.6).
_SILENT_SIGN_IN_ERRORS = frozenset(
    {"login_required", "interaction_required", "consent_required", "account_selection_required"}
)

router = APIRouter(prefix="/auth", tags=["auth"])

#: Holds the OIDC flow state between the login redirect and the callback.
#: Scoped to the OIDC routes so it rides along with nothing else.
_FLOW_COOKIE = "oidc_flow"
_FLOW_COOKIE_PATH = "/api/v1/auth/oidc"
_SAML_COOKIE = "saml_flow"
_SAML_COOKIE_PATH = "/api/v1/auth/saml"
_SAML_NOT_CONFIGURED = "SAML single sign-on is not configured on this installation."

# Verifying a hash takes ~50 ms by design. Comparing against this dummy when the
# email is unknown keeps the response time of "no such user" and "wrong
# password" indistinguishable, so the endpoint cannot be used to enumerate
# accounts.
_DUMMY_HASH = hash_password("timing-equalisation-placeholder")


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange email and password for an access token",
)
async def login(
    payload: LoginRequest,
    session: SessionDep,
    settings: SettingsDep,
    client_ip: ClientIpDep,
    host: RequestHostDep,
) -> TokenResponse:
    """Authenticate a local account.

    Returns the same error for an unknown email, a wrong password, a disabled
    account and an SSO-only account: telling them apart would leak which emails
    have accounts here.

    Attempts are rate limited per (client IP, e-mail) before the password
    is looked at, so guessing costs a minute per few tries (API-5).
    """
    await enforce_rate_limit(
        settings,
        "login",
        rate_limit.login_identity(client_ip, str(payload.email)),
        settings.rate_limit_login_per_minute,
    )
    user = await session.scalar(select(User).where(User.email == payload.email))

    if user is None or user.password_hash is None:
        # Spend the same time as a real verification before refusing. Unknown
        # (or SSO-only) accounts have no organisation to file an audit row
        # under, so none is written (SEC-3).
        verify_password(payload.password, _DUMMY_HASH)
        raise UnauthorizedError("Incorrect email or password.")

    if not verify_password(payload.password, user.password_hash):
        audit.record(
            session,
            organization_id=user.organization_id,
            actor_id=None,
            action="auth.login_failed",
            target_type="user",
            target_id=user.id,
            after={"reason": "bad_password"},
            ip=client_ip,
        )
        await session.commit()
        raise UnauthorizedError("Incorrect email or password.")

    if not user.is_active:
        audit.record(
            session,
            organization_id=user.organization_id,
            actor_id=None,
            action="auth.login_failed",
            target_type="user",
            target_id=user.id,
            after={"reason": "inactive"},
            ip=client_ip,
        )
        await session.commit()
        raise UnauthorizedError("Incorrect email or password.")

    # After the password, so only the account's owner learns that MFA is on.
    if mfa.is_enabled(user):
        if not payload.otp:
            raise MfaRequiredError("Enter the code from your authenticator app.")
        if not mfa.verify(user, payload.otp):
            audit.record(
                session,
                organization_id=user.organization_id,
                actor_id=None,
                action="auth.login_failed",
                target_type="user",
                target_id=user.id,
                after={"reason": "mfa"},
                ip=client_ip,
            )
            await session.commit()
            raise MfaInvalidError("The authentication code is wrong or was already used.")

    # After the password check, so only the account's owner learns about seats.
    licence = await effective_license(session, settings, host=host, record=True)
    if not await seats.may_sign_in(session, user, licence):
        audit.record(
            session,
            organization_id=user.organization_id,
            actor_id=None,
            action="auth.login_failed",
            target_type="user",
            target_id=user.id,
            after={"reason": "seat_limit"},
            ip=client_ip,
        )
        await session.commit()
        raise SeatLimitError(await seats.refusal_message(session, licence))

    # AUTH-2 policy: an administrator without MFA may only go and set it up.
    setup_only = settings.mfa_required_for_admins and user.is_superuser and not mfa.is_enabled(user)
    user.last_seen_at = datetime.now(UTC)
    audit.record(
        session,
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.login",
        target_type="user",
        target_id=user.id,
        after={"mfa_setup_required": True} if setup_only else None,
        ip=client_ip,
    )
    await session.commit()

    return TokenResponse(
        access_token=_access_token_for(user, mfa_setup=setup_only),
        expires_in=settings.access_token_ttl,
        mfa_setup_required=setup_only,
    )


def _access_token_for(user: User, *, sso: bool = False, mfa_setup: bool = False) -> str:
    """The platform's own bearer token; local login and SSO issue the same one.

    `sso` tells the frontend to sign out at the provider as well and to
    re-authenticate silently when the token expires. `mfa_setup` limits the
    token to setting up MFA (`APP_MFA_REQUIRED_FOR_ADMINS`, see `deps`).
    """
    claims: dict[str, object] = {
        "org": str(user.organization_id),
        "email": user.email,
        "superuser": user.is_superuser,
        "service": False,
    }
    if sso:
        claims["sso"] = True
    if mfa_setup:
        claims["mfa_setup"] = True
    return create_access_token(
        subject=str(user.id),
        extra_claims=claims,
    )


@router.get(
    "/providers",
    response_model=AuthProviders,
    summary="Which sign-in methods this installation offers",
)
async def providers(settings: SettingsDep, licence: LicenceDep) -> AuthProviders:
    """Unauthenticated: the login page needs it before anyone has signed in.

    Single sign-on is listed only while the licence has it (LIC-33).
    """
    oidc = saml = None
    if has_feature(licence, Feature.SSO):
        if settings.oidc_enabled:
            oidc = OidcProviderInfo(display_name=settings.oidc_display_name)
        if settings.saml_enabled:
            saml = OidcProviderInfo(
                display_name=settings.saml_display_name, login_path="/auth/saml/login"
            )
    return AuthProviders(local=True, oidc=oidc, saml=saml)


def _login_error_redirect(
    settings: Settings,
    code: str,
    *,
    cookie: str = _FLOW_COOKIE,
    cookie_path: str = _FLOW_COOKIE_PATH,
    secure: bool = False,
) -> RedirectResponse:
    """Send the browser back to the login page with a short error code."""
    url = f"{settings.frontend_url.rstrip('/')}/login?{urlencode({'error': code})}"
    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    _clear_cookie(response, cookie, cookie_path, secure=secure)
    return response


def _clear_cookie(response: Response, cookie: str, path: str, *, secure: bool) -> None:
    """Expire a flow cookie with the attributes it was set with."""
    response.delete_cookie(
        cookie, path=path, httponly=True, secure=secure, samesite="none" if secure else "lax"
    )


def _token_redirect(
    settings: Settings,
    user: User,
    next_path: str,
    *,
    sso: bool,
    cookie: str,
    cookie_path: str,
    secure: bool = False,
) -> RedirectResponse:
    """Hand the platform's token to the frontend in a URL fragment, never a query."""
    fragment = urlencode(
        {
            "access_token": _access_token_for(user, sso=sso),
            "expires_in": settings.access_token_ttl,
            "next": next_path,
        }
    )
    url = f"{settings.frontend_url.rstrip('/')}/auth/callback#{fragment}"
    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    _clear_cookie(response, cookie, cookie_path, secure=secure)
    return response


@router.get(
    "/oidc/login",
    summary="Start single sign-on (AUTH-1)",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def oidc_login(
    request: Request,
    settings: SettingsDep,
    oidc: OidcClientDep,
    licence: LicenceDep,
    next: str | None = Query(default=None, description="Path to open after sign-in"),  # noqa: A002
    prompt: Literal["none"] | None = Query(
        default=None, description="`none`: silent sign-in from the provider's session"
    ),
) -> RedirectResponse:
    """Redirect the browser to the identity provider.

    A full-page navigation, not an XHR: the flow cookie set here must be sent
    back by the browser on the callback, and the provider's pages are not
    embeddable.
    """
    if not settings.oidc_enabled:
        raise NotFoundError("Single sign-on is not configured on this installation.")
    if not has_feature(licence, Feature.SSO):
        return _login_error_redirect(settings, "license_feature")
    flow = new_flow(next)
    try:
        location = await oidc.authorization_url(flow, prompt=prompt)
    except OidcError as exc:
        log.warning("oidc.login_failed", code=exc.code, detail=exc.detail)
        return _login_error_redirect(settings, exc.code)
    response = RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        _FLOW_COOKIE,
        encode_flow(flow, settings),
        max_age=int(FLOW_TTL.total_seconds()),
        path=_FLOW_COOKIE_PATH,
        httponly=True,
        # Lax, not Strict: the callback is a top-level navigation *from* the
        # provider's site, and Strict would drop the cookie on exactly that hop.
        samesite="lax",
        # Always in production; in development too when the request arrived
        # over TLS (directly, or via a proxy listed in APP_TRUSTED_PROXIES).
        secure=settings.env == "production" or request.url.scheme == "https",
    )
    return response


@router.get(
    "/oidc/logout",
    summary="Sign out at the identity provider too (RP-initiated logout)",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def oidc_logout(settings: SettingsDep, oidc: OidcClientDep) -> RedirectResponse:
    """A full-page navigation after the frontend has dropped its own token.

    Platform tokens are stateless, so there is nothing to revoke here; this
    only ends the provider session, so the next sign-in asks again.
    """
    if not settings.oidc_enabled:
        raise NotFoundError("Single sign-on is not configured on this installation.")
    login_page = f"{settings.frontend_url.rstrip('/')}/login"
    try:
        location = await oidc.logout_url(login_page) or login_page
    except OidcError as exc:
        # The provider is unreachable: the local sign-out already happened.
        log.warning("oidc.logout_failed", code=exc.code, detail=exc.detail)
        location = login_page
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)


@router.get(
    "/oidc/callback",
    summary="Complete single sign-on (AUTH-1)",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def oidc_callback(
    session: SessionDep,
    settings: SettingsDep,
    oidc: OidcClientDep,
    licence: LicenceDep,
    client_ip: ClientIpDep,
    host: RequestHostDep,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    flow_cookie: str | None = Cookie(default=None, alias=_FLOW_COOKIE),
) -> RedirectResponse:
    """The provider's redirect target.

    Every failure ends at ``{frontend}/login?error=<code>``: the browser is
    mid-navigation and cannot render a problem-details body. The token is
    handed over in the URL *fragment* of ``{frontend}/auth/callback``, which
    browsers keep out of the request they make for that page.
    """
    if not settings.oidc_enabled:
        raise NotFoundError("Single sign-on is not configured on this installation.")
    if not has_feature(licence, Feature.SSO):
        return _login_error_redirect(settings, "license_feature")
    if error:
        log.info("oidc.provider_error", error=error)
        if error in _SILENT_SIGN_IN_ERRORS:
            # A `prompt=none` attempt without a provider session: the login
            # page, not an error the person caused.
            return _login_error_redirect(settings, "login_required")
        # The user cancelled or the provider refused; nothing to verify.
        return _login_error_redirect(settings, "provider_denied")
    try:
        if flow_cookie is None:
            raise OidcError("flow_expired", "no flow cookie on the callback")
        flow = decode_flow(flow_cookie, settings)
        if not state or state != flow.state:
            raise OidcError("state_mismatch", "state does not match the flow cookie")
        if not code:
            raise OidcError("token_exchange_failed", "callback carries no code")
        identity = await oidc.exchange_code(code, flow)
        user = await complete_sign_in(
            session,
            identity,
            settings=settings,
            policy=None,
            admin_groups=settings.oidc_admin_groups,
            client_ip=client_ip,
            host=host,
        )
        await session.commit()
    except OidcDisabledError:
        raise NotFoundError("Single sign-on is not configured on this installation.") from None
    except OidcError as exc:
        # The failed-login audit row (inactive account, no seat) must survive the
        # rollback of everything else, so commit what resolve_user recorded.
        await session.commit()
        log.warning("oidc.callback_failed", code=exc.code, detail=exc.detail)
        return _login_error_redirect(settings, exc.code)

    return _token_redirect(
        settings,
        user,
        flow.next_path,
        sso=True,
        cookie=_FLOW_COOKIE,
        cookie_path=_FLOW_COOKIE_PATH,
    )


# --- SAML 2.0 (AUTH-1) -----------------------------------------------------------


def _saml_secure(settings: Settings, request: Request) -> bool:
    """The SAML flow cookie is SameSite=None; Secure whenever TLS is in play."""
    return settings.env == "production" or request.url.scheme == "https"


def _saml_error_redirect(settings: Settings, request: Request, code: str) -> RedirectResponse:
    return _login_error_redirect(
        settings,
        code,
        cookie=_SAML_COOKIE,
        cookie_path=_SAML_COOKIE_PATH,
        secure=_saml_secure(settings, request),
    )


@router.get(
    "/saml/login",
    summary="Start SAML single sign-on (AUTH-1)",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def saml_login(
    request: Request,
    settings: SettingsDep,
    saml: SamlClientDep,
    licence: LicenceDep,
    next: str | None = Query(default=None, description="Path to open after sign-in"),  # noqa: A002
) -> RedirectResponse:
    """Redirect the browser to the IdP with an AuthnRequest (HTTP-Redirect binding)."""
    if not settings.saml_enabled:
        raise NotFoundError(_SAML_NOT_CONFIGURED)
    if not has_feature(licence, Feature.SSO):
        return _saml_error_redirect(settings, request, "license_feature")
    try:
        location, flow = await saml.login_url(next)
    except OidcError as exc:
        log.warning("saml.login_failed", code=exc.code, detail=exc.detail)
        return _saml_error_redirect(settings, request, exc.code)
    secure = _saml_secure(settings, request)
    response = RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        _SAML_COOKIE,
        saml_flow.encode_flow(flow, settings),
        max_age=int(saml_flow.FLOW_TTL.total_seconds()),
        path=_SAML_COOKIE_PATH,
        httponly=True,
        # The response comes back as a cross-site POST from the IdP, which a
        # Lax cookie does not ride on; None needs Secure. Plain-http dev with
        # an IdP on localhost is same-site, where Lax is enough.
        samesite="none" if secure else "lax",
        secure=secure,
    )
    return response


@router.post(
    "/saml/acs",
    summary="Complete SAML single sign-on: the Assertion Consumer Service (AUTH-1)",
    status_code=status.HTTP_303_SEE_OTHER,
    response_class=RedirectResponse,
)
async def saml_acs(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    saml: SamlClientDep,
    licence: LicenceDep,
    client_ip: ClientIpDep,
    host: RequestHostDep,
    saml_response: str | None = Form(default=None, alias="SAMLResponse"),
    flow_cookie: str | None = Cookie(default=None, alias=_SAML_COOKIE),
) -> RedirectResponse:
    """The IdP's HTTP-POST target.

    Every failure ends at ``{frontend}/login?error=<code>``, like the OIDC
    callback. A response is accepted only if it answers the request id in
    this browser's flow cookie, so IdP-initiated sign-in is refused.
    """
    if not settings.saml_enabled:
        raise NotFoundError(_SAML_NOT_CONFIGURED)
    if not has_feature(licence, Feature.SSO):
        return _saml_error_redirect(settings, request, "license_feature")
    try:
        if flow_cookie is None:
            raise saml_flow.SamlError("flow_expired", "no flow cookie on the response")
        flow = saml_flow.decode_flow(flow_cookie, settings)
        if not saml_response:
            raise saml_flow.SamlError("invalid_response", "no SAMLResponse in the form")
        identity = await saml.identity(saml_response, flow)
        user = await complete_sign_in(
            session,
            identity,
            settings=settings,
            policy=saml_flow.saml_policy(settings),
            admin_groups=settings.saml_admin_groups,
            client_ip=client_ip,
            host=host,
        )
        await session.commit()
    except OidcError as exc:
        # As for OIDC: keep the failed-login audit row resolve_user recorded.
        await session.commit()
        log.warning("saml.acs_failed", code=exc.code, detail=exc.detail)
        return _saml_error_redirect(settings, request, exc.code)
    # No `sso` claim: the frontend's silent renewal and provider sign-out
    # speak OIDC. A SAML session that expires goes back to the login page.
    return _token_redirect(
        settings,
        user,
        flow.next_path,
        sso=False,
        cookie=_SAML_COOKIE,
        cookie_path=_SAML_COOKIE_PATH,
        secure=_saml_secure(settings, request),
    )


@router.get(
    "/saml/metadata",
    summary="This service provider's SAML metadata, for the IdP (AUTH-1)",
    response_class=Response,
)
async def saml_metadata(settings: SettingsDep, saml: SamlClientDep) -> Response:
    """Entity id, ACS URL and binding: what an IdP admin uploads or points at."""
    if not settings.saml_enabled:
        raise NotFoundError(_SAML_NOT_CONFIGURED)
    try:
        xml = await saml.sp_metadata()
    except OidcError as exc:
        log.warning("saml.metadata_failed", code=exc.code, detail=exc.detail)
        raise NotFoundError(_SAML_NOT_CONFIGURED) from exc
    return Response(content=xml, media_type="application/samlmetadata+xml")


@router.get(
    "/me",
    response_model=UserRead,
    summary="The authenticated caller",
    status_code=status.HTTP_200_OK,
)
async def me(current_user: CurrentUserDep, session: SessionDep) -> User:
    """Return the caller's own user record.

    The token carries enough to authorise a request, but the frontend needs the
    display name and timestamps, so this one endpoint does hit the database.
    """
    user = await session.get(User, current_user.id)
    if user is None or not user.is_active:
        # The token is validly signed but the account is gone or disabled.
        raise UnauthorizedError("This account is no longer active.")
    return user


@router.patch(
    "/me",
    response_model=UserRead,
    summary="Change the caller's own preferences",
    status_code=status.HTTP_200_OK,
)
async def update_me(
    payload: UserPreferencesUpdate, current_user: CurrentUserDep, session: SessionDep
) -> User:
    """Only keys present in the body change (API-7: `email_notifications`)."""
    user = await session.get(User, current_user.id)
    if user is None or not user.is_active:
        raise UnauthorizedError("This account is no longer active.")
    if payload.email_notifications is not None:
        user.email_notifications = payload.email_notifications
    await session.commit()
    await session.refresh(user)
    return user
