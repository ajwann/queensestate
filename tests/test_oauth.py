"""The OAuth authorization server: the Google round trip, the allow list, tokens."""

from __future__ import annotations

import pytest

from queensestate.config import GoogleOAuthConfig
from queensestate.oauth import GoogleIdentity

pytestmark = pytest.mark.anyio

RESOURCE_URL = "https://queensestate.test/mcp"
CALLBACK_URL = "https://queensestate.test/auth/google/callback"
CLIENT_REDIRECT = "http://127.0.0.1:33418/callback"


def google_config(**overrides: object) -> GoogleOAuthConfig:
    settings: dict[str, object] = {
        "client_id": "google-client-id",
        "client_secret": "google-client-secret",
        "allowed_emails": frozenset({"resident@example.com"}),
        "allowed_domains": frozenset(),
        "allow_any_account": False,
        "authorization_url": "https://accounts.google.test/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.test/token",
        "jwks_url": "https://www.googleapis.test/oauth2/v3/certs",
        "access_token_ttl_seconds": 3600.0,
        "refresh_token_ttl_seconds": 86400.0,
    }
    settings.update(overrides)
    return GoogleOAuthConfig(**settings)  # type: ignore[arg-type]


class StubResolver:
    """Stands in for Google's token endpoint."""

    def __init__(self, identity: GoogleIdentity | Exception) -> None:
        self._identity = identity
        self.calls: list[dict[str, str]] = []

    async def resolve(self, *, code: str, redirect_uri: str, code_verifier: str) -> GoogleIdentity:
        self.calls.append(
            {"code": code, "redirect_uri": redirect_uri, "code_verifier": code_verifier}
        )
        if isinstance(self._identity, Exception):
            raise self._identity
        return self._identity


ALLOWED = GoogleIdentity(subject="google-sub-1", email="resident@example.com", email_verified=True)


# -- The allow list is the authorization policy ----------------------------


def test_an_allowed_address_is_admitted() -> None:
    assert google_config().permits("resident@example.com", email_verified=True)


def test_an_unverified_address_is_never_admitted() -> None:
    """On a Workspace domain an unverified address does not prove control of the mailbox."""
    assert not google_config().permits("resident@example.com", email_verified=False)
    assert not google_config(allow_any_account=True).permits("anyone@x.test", email_verified=False)


def test_an_unlisted_address_is_refused() -> None:
    assert not google_config().permits("stranger@elsewhere.test", email_verified=True)


def test_a_domain_allow_list_matches_on_the_part_after_the_at_sign() -> None:
    config = google_config(allowed_emails=frozenset(), allowed_domains=frozenset({"example.com"}))
    assert config.permits("anyone@example.com", email_verified=True)
    assert not config.permits("anyone@notexample.com", email_verified=True)


def test_comparison_ignores_case_and_surrounding_space() -> None:
    assert google_config().permits("  Resident@Example.COM  ", email_verified=True)


def test_allow_any_account_admits_a_stranger() -> None:
    assert google_config(allow_any_account=True).permits("stranger@x.test", email_verified=True)


def test_a_malformed_address_is_refused() -> None:
    assert not google_config(allow_any_account=True).permits("not-an-address", email_verified=True)
