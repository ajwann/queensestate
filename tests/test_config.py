"""The configuration contract: what the deploy script sets, validated at startup."""

from __future__ import annotations

from pathlib import Path

import pytest

from queensestate.config import ConfigError, load_http_config, load_transport

#: The minimum a hosted server needs before any other setting is considered.
BASE = {
    "QUEENSESTATE_PUBLIC_URL": "https://queensestate.example.com",
    "QUEENSESTATE_GOOGLE_CLIENT_ID": "client-id",
    "QUEENSESTATE_GOOGLE_CLIENT_SECRET": "client-secret",
    "QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT": "true",
}


def env(**overrides: str) -> dict[str, str]:
    merged = {**BASE, **overrides}
    return {name: value for name, value in merged.items() if value}


# -- The public URL is the OAuth issuer ------------------------------------


def test_the_resource_and_callback_are_derived_from_the_public_url() -> None:
    config = load_http_config(env())
    assert config.public_url == "https://queensestate.example.com"
    assert config.resource_url == "https://queensestate.example.com/mcp"
    assert config.callback_url == "https://queensestate.example.com/auth/google/callback"


def test_a_trailing_slash_or_path_is_stripped_from_the_issuer() -> None:
    """RFC 8414 compares issuers by exact string match, so this must normalise."""
    config = load_http_config(env(QUEENSESTATE_PUBLIC_URL="https://x.example.com/some/path/"))
    assert config.public_url == "https://x.example.com"


def test_a_plain_http_public_url_is_refused() -> None:
    with pytest.raises(ConfigError, match="must be https"):
        load_http_config(env(QUEENSESTATE_PUBLIC_URL="http://x.example.com"))


def test_a_loopback_public_url_may_be_plain_http() -> None:
    config = load_http_config(env(QUEENSESTATE_PUBLIC_URL="http://localhost:8000"))
    assert config.public_url == "http://localhost:8000"


def test_a_public_url_carrying_credentials_is_refused() -> None:
    with pytest.raises(ConfigError, match="must not contain credentials"):
        load_http_config(env(QUEENSESTATE_PUBLIC_URL="https://user:pw@x.example.com"))


# -- Fail closed on the allow list -----------------------------------------


def test_no_allow_list_at_all_refuses_to_start() -> None:
    """Otherwise anyone who finds the URL and has a Google account is in."""
    with pytest.raises(ConfigError, match="No Google accounts are allowed"):
        load_http_config(env(QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT=""))


def test_an_email_allow_list_is_enough() -> None:
    config = load_http_config(
        env(
            QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT="",
            QUEENSESTATE_ALLOWED_EMAILS="a@example.com, b@example.com",
        )
    )
    assert config.google.allowed_emails == {"a@example.com", "b@example.com"}


def test_a_domain_entry_that_looks_like_an_address_is_refused() -> None:
    with pytest.raises(ConfigError, match="bare domains"):
        load_http_config(
            env(
                QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT="",
                QUEENSESTATE_ALLOWED_DOMAINS="me@example.com",
            )
        )


def test_an_email_entry_without_an_at_sign_is_refused() -> None:
    with pytest.raises(ConfigError, match="must be addresses"):
        load_http_config(
            env(QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT="", QUEENSESTATE_ALLOWED_EMAILS="example.com")
        )


# -- The Google client is mandatory ----------------------------------------


@pytest.mark.parametrize(
    "missing",
    ["QUEENSESTATE_GOOGLE_CLIENT_ID", "QUEENSESTATE_GOOGLE_CLIENT_SECRET"],
)
def test_the_google_client_credentials_are_required(missing: str) -> None:
    with pytest.raises(ConfigError, match="is required for the http transport"):
        load_http_config(env(**{missing: ""}))


# -- What the deploy script sets -------------------------------------------


def test_the_firestore_store_and_stateless_flag_are_read() -> None:
    config = load_http_config(
        env(
            QUEENSESTATE_TOKEN_STORE="firestore",  # noqa: S106 (a store kind, not a secret)
            QUEENSESTATE_FIRESTORE_DATABASE="queensestate",
            QUEENSESTATE_STATELESS_HTTP="true",
        )
    )
    assert config.oauth_store == "firestore"
    assert config.firestore_database == "queensestate"
    assert config.stateless is True


def test_an_unknown_token_store_is_refused() -> None:
    with pytest.raises(ConfigError, match="must be one of"):
        load_http_config(env(QUEENSESTATE_TOKEN_STORE="postgres"))  # noqa: S106 (not a secret)


def test_a_non_boolean_flag_is_refused() -> None:
    with pytest.raises(ConfigError, match="must be a boolean"):
        load_http_config(env(QUEENSESTATE_STATELESS_HTTP="maybe"))


def test_serving_tls_needs_both_a_certificate_and_a_key(tmp_path: Path) -> None:
    cert = tmp_path / "cert.pem"
    cert.write_text("not really a certificate")

    with pytest.raises(ConfigError, match="QUEENSESTATE_TLS_KEY must be set too"):
        load_http_config(env(), tls_cert=str(cert))


def test_a_tls_path_that_does_not_exist_is_refused() -> None:
    """Checked at startup, not at the first TLS handshake."""
    with pytest.raises(ConfigError, match="is not a file"):
        load_http_config(env(), tls_cert="/nonexistent/cert.pem")


# -- Transport selection ---------------------------------------------------


def test_the_transport_defaults_to_stdio() -> None:
    assert load_transport({}) == "stdio"


def test_the_transport_is_read_from_the_environment() -> None:
    assert load_transport({"QUEENSESTATE_TRANSPORT": "http"}) == "http"


def test_a_command_line_override_wins() -> None:
    assert load_transport({"QUEENSESTATE_TRANSPORT": "stdio"}, override="http") == "http"


def test_an_unknown_transport_is_refused() -> None:
    with pytest.raises(ConfigError, match="unknown transport"):
        load_transport({"QUEENSESTATE_TRANSPORT": "carrier-pigeon"})
