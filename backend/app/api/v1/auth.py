"""Authentication endpoints.

Password sign-in (admins, management, faculty; students from Phase 4) and
mobile OTP sign-in (intended for parents) both yield the same token pair.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import (
    DbSession,
    OtpSenderDep,
    RequestCtx,
    SettingsDep,
    require_permission,
)
from app.repositories.users import UserRepository
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    MeResponse,
    OtpRequest,
    OtpRequestResponse,
    OtpVerifyRequest,
    RefreshRequest,
    TokenResponse,
)
from app.schemas.common import ErrorResponse
from app.security.permissions import PermissionName
from app.security.principal import Principal
from app.services.auth import AuthService, TokenPair
from app.services.otp_auth import OtpAuthService

router = APIRouter(prefix="/auth", tags=["auth"])

_ERRORS: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_429_TOO_MANY_REQUESTS: {"model": ErrorResponse},
}


def _token_response(pair: TokenPair, response: Response) -> TokenResponse:
    # RFC 6749 section 5.1: token responses must not be cached.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TokenResponse(
        access_token=pair.access_token,
        expires_in=pair.access_expires_in,
        refresh_token=pair.refresh_token,
        refresh_expires_in=pair.refresh_expires_in,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in with email/mobile and password",
    responses=_ERRORS,
)
def login(
    body: LoginRequest,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> TokenResponse:
    service = AuthService(session, settings, context)
    pair = service.login_with_password(
        body.identifier, body.password.get_secret_value(), school_code=body.school_code
    )
    return _token_response(pair, response)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the refresh token and obtain a new access token",
    responses=_ERRORS,
)
def refresh(
    body: RefreshRequest,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> TokenResponse:
    pair = AuthService(session, settings, context).refresh(body.refresh_token.get_secret_value())
    return _token_response(pair, response)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the session that owns the refresh token",
    responses={status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse}},
)
def logout(
    body: LogoutRequest, session: DbSession, settings: SettingsDep, context: RequestCtx
) -> Response:
    AuthService(session, settings, context).logout(body.refresh_token.get_secret_value())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Current user's profile, roles and permissions",
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
def me(
    principal: Annotated[Principal, Depends(require_permission(PermissionName.AUTH_ME))],
    session: DbSession,
) -> MeResponse:
    user = UserRepository(session).get(principal.user_id)
    assert user is not None  # noqa: S101 - guaranteed by authentication
    return MeResponse(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
        school_id=user.school_id,
        status=user.status,
        last_login_at=user.last_login_at,
        roles=sorted(principal.roles),
        permissions=sorted(principal.permissions),
    )


@router.post(
    "/otp/request",
    response_model=OtpRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a one-time sign-in code to a mobile number",
    description=(
        "Intended for parent sign-in. The response is identical whether or not the number "
        "belongs to an account. Subject to a resend cooldown and hourly limits."
    ),
    responses={**_ERRORS, status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}},
)
def request_otp(
    body: OtpRequest,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
    sender: OtpSenderDep,
) -> OtpRequestResponse:
    result = OtpAuthService(session, settings, context, sender).request_login_otp(body.phone)
    return OtpRequestResponse(expires_in=result.expires_in, resend_after=result.resend_after)


@router.post(
    "/otp/verify",
    response_model=TokenResponse,
    summary="Exchange a one-time code for a token pair",
    responses=_ERRORS,
)
def verify_otp(
    body: OtpVerifyRequest,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
    sender: OtpSenderDep,
) -> TokenResponse:
    service = OtpAuthService(session, settings, context, sender)
    pair = service.verify_login_otp(body.phone, body.code.get_secret_value())
    return _token_response(pair, response)
