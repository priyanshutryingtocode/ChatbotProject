"""Email/password authentication and server-checked support-staff authorization."""

from dataclasses import dataclass
import re

from supabase import Client, create_client

from .config import SUPABASE_ANON_KEY, SUPABASE_URL, order_support_client


class AuthenticationError(RuntimeError):
    """Authentication failed or the Supabase Auth service is unavailable."""


@dataclass(frozen=True)
class AuthIdentity:
    user_id: str
    email: str
    is_staff: bool
    access_token: str
    refresh_token: str


def _auth_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise AuthenticationError("Authentication is not configured. Set SUPABASE_ANON_KEY.")
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def _normalized_email(email: str) -> str:
    normalized = (email or "").strip().casefold()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
        raise AuthenticationError("Enter a valid email address.")
    return normalized


def sign_in_with_password(email: str, password: str, *, require_staff_check: bool = False) -> AuthIdentity:
    normalized_email = _normalized_email(email)
    if not password:
        raise AuthenticationError("Enter your password.")
    try:
        response = _auth_client().auth.sign_in_with_password(
            {"email": normalized_email, "password": password}
        )
    except Exception as exc:
        raise AuthenticationError("Email or password is incorrect. Please try again.") from exc
    return _identity_from_response(response, require_staff_check=require_staff_check)


def register_customer(email: str, password: str) -> AuthIdentity | None:
    """Create a customer account; return None when email confirmation is pending."""
    normalized_email = _normalized_email(email)
    if len(password or "") < 8:
        raise AuthenticationError("Use a password with at least 8 characters.")
    try:
        response = _auth_client().auth.sign_up(
            {"email": normalized_email, "password": password}
        )
    except Exception as exc:
        raise AuthenticationError("We couldn't create the account. Check the details and try again.") from exc

    if not response.session:
        return None
    return _identity_from_response(response)


def restore_identity(
    access_token: str,
    refresh_token: str,
    *,
    require_staff_check: bool = False,
) -> AuthIdentity:
    """Refresh and validate this Streamlit session's server-held Auth tokens."""
    try:
        client = _auth_client()
        client.auth.set_session(access_token, refresh_token)
        session = client.auth.get_session()
        if not session:
            raise AuthenticationError("Your sign-in has expired. Please sign in again.")
        user_response = client.auth.get_user(session.access_token)
        if not user_response or not user_response.user:
            raise AuthenticationError("Your sign-in is no longer valid. Please sign in again.")
        return _identity_from_user(
            user_response.user,
            session.access_token,
            session.refresh_token,
            require_staff_check=require_staff_check,
        )
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError("Your sign-in expired or could not be refreshed. Please sign in again.") from exc


def revoke_session(access_token: str, refresh_token: str) -> None:
    try:
        client = _auth_client()
        client.auth.set_session(access_token, refresh_token)
        client.auth.sign_out({"scope": "local"})
    except Exception:
        return


def _identity_from_response(response, *, require_staff_check: bool = False) -> AuthIdentity:
    if not response.session or not response.user:
        raise AuthenticationError("Confirm your email address, then sign in with your password.")
    return _identity_from_user(
        response.user,
        response.session.access_token,
        response.session.refresh_token,
        require_staff_check=require_staff_check,
    )


def _identity_from_user(
    user,
    access_token: str,
    refresh_token: str,
    *,
    require_staff_check: bool = False,
) -> AuthIdentity:
    email = (getattr(user, "email", None) or "").strip().casefold()
    user_id = str(getattr(user, "id", "") or "")
    if not email or not user_id or not getattr(user, "email_confirmed_at", None):
        raise AuthenticationError("Confirm your email address before signing in.")
    return AuthIdentity(
        user_id=user_id,
        email=email,
        is_staff=_is_active_staff(user_id) if require_staff_check else False,
        access_token=access_token,
        refresh_token=refresh_token,
    )


def _is_active_staff(user_id: str) -> bool:
    """Check the server-managed staff allowlist; user metadata cannot grant access."""
    try:
        result = (
            order_support_client()
            .table("support_staff")
            .select("user_id")
            .eq("user_id", user_id)
            .eq("active", True)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        raise AuthenticationError("Support staff access couldn't be verified. Please try again.") from exc
