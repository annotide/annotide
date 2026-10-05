"""Tests for storage URL construction against emulators and private endpoints.

Both behaviours here fail *silently* when wrong, which is why they get their
own file:

* A signed URL built from the API's internal hostname is fetched by the
  browser, not the API. The server sees a perfectly successful request and the
  image simply never appears.
* An account name parsed from the wrong part of an emulator URL produces a SAS
  signed for a non-existent account, which comes back as an opaque 403.
"""

from __future__ import annotations

import pytest

from app.connectors.azure_blob import AzureBlobConnector
from app.connectors.registry import build_connector

AZURITE_INTERNAL = "http://azurite:10000/devstoreaccount1"
AZURITE_PUBLIC = "http://localhost:10000/devstoreaccount1"
AZURE_REAL = "https://acmedata.blob.core.windows.net"


class TestAccountNameParsing:
    def test_host_style_azure_url(self) -> None:
        assert AzureBlobConnector._parse_account_name(AZURE_REAL) == "acmedata"

    def test_host_style_with_explicit_port(self) -> None:
        assert (
            AzureBlobConnector._parse_account_name("https://acmedata.blob.core.windows.net:443")
            == "acmedata"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://azurite:10000/devstoreaccount1",
            "http://127.0.0.1:10000/devstoreaccount1",
            "http://localhost:10000/devstoreaccount1/",
        ],
    )
    def test_path_style_emulator_url(self, url: str) -> None:
        # The account is the first PATH segment here, not the hostname.
        # Reading the hostname would give "azurite" / "127" and sign every SAS
        # for an account that does not exist.
        assert AzureBlobConnector._parse_account_name(url) == "devstoreaccount1"

    def test_explicit_account_name_wins(self) -> None:
        connector = AzureBlobConnector(
            account_url=AZURITE_INTERNAL,
            container="media",
            identity_type="account_key",
            secret="key",
            account_name="someotheraccount",
        )
        assert connector._account_name == "someotheraccount"


class TestPublicUrl:
    def test_defaults_to_the_internal_url(self) -> None:
        connector = AzureBlobConnector(
            account_url=AZURE_REAL, container="media", identity_type="managed_identity"
        )
        assert connector._blob_url("a/b.jpg") == connector._blob_url("a/b.jpg", public=True)

    def test_public_url_is_used_for_browser_facing_links(self) -> None:
        connector = AzureBlobConnector(
            account_url=AZURITE_INTERNAL,
            container="media",
            identity_type="account_key",
            secret="key",
            public_account_url=AZURITE_PUBLIC,
        )
        assert connector._blob_url("cat.jpg").startswith("http://azurite:10000/")
        assert connector._blob_url("cat.jpg", public=True).startswith("http://localhost:10000/")

    def test_paths_are_url_encoded_in_both_forms(self) -> None:
        connector = AzureBlobConnector(
            account_url=AZURITE_INTERNAL,
            container="media",
            identity_type="account_key",
            secret="key",
            public_account_url=AZURITE_PUBLIC,
        )
        for url in (
            connector._blob_url("a folder/a file.jpg"),
            connector._blob_url("a folder/a file.jpg", public=True),
        ):
            assert " " not in url
            assert "%20" in url
            # Separators must survive encoding or the blob path changes meaning.
            assert url.count("/") >= 4

    @pytest.mark.asyncio
    async def test_signed_url_points_at_the_public_host(self) -> None:
        """The property that actually matters: what the browser receives."""
        connector = AzureBlobConnector(
            account_url=AZURITE_INTERNAL,
            container="media",
            identity_type="account_key",
            # Azurite's well-known development key. Not a credential for
            # anything real: it is published in Microsoft's own documentation.
            secret=(
                "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
            ),
            public_account_url=AZURITE_PUBLIC,
        )
        url = await connector.signed_url("cat.jpg", expires_in=900)

        assert url.startswith("http://localhost:10000/devstoreaccount1/media/cat.jpg?")
        assert "azurite:10000" not in url, "a browser cannot resolve the compose service name"
        assert "sig=" in url, "a signed URL must carry a signature"

    @pytest.mark.asyncio
    async def test_internal_signed_url_points_at_the_internal_host(self) -> None:
        """The mirror case: a model service inside compose fetches it (§8).

        `localhost` inside the `model` container is the model itself, so a
        browser-facing URL made every prelabel prediction fail with
        "could not load image" (found by `frontend/e2e/prelabel.spec.ts`).
        """
        connector = AzureBlobConnector(
            account_url=AZURITE_INTERNAL,
            container="media",
            identity_type="account_key",
            secret=(
                "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
            ),
            public_account_url=AZURITE_PUBLIC,
        )
        url = await connector.signed_url("cat.jpg", expires_in=900, internal=True)

        assert url.startswith("http://azurite:10000/devstoreaccount1/media/cat.jpg?")
        assert "sig=" in url

    @pytest.mark.asyncio
    async def test_sas_token_identity_honours_internal(self) -> None:
        connector = AzureBlobConnector(
            account_url=AZURITE_INTERNAL,
            container="media",
            identity_type="sas_token",
            secret="sv=2024-01-01&sig=abc",
            public_account_url=AZURITE_PUBLIC,
        )
        assert (await connector.signed_url("a.jpg")).startswith("http://localhost:10000/")
        assert (await connector.signed_url("a.jpg", internal=True)).startswith(
            "http://azurite:10000/"
        )


class TestRegistryWiring:
    def test_registry_passes_the_new_keys_through(self) -> None:
        connector = build_connector(
            "azure_blob",
            {
                "account_url": AZURITE_INTERNAL,
                "public_account_url": AZURITE_PUBLIC,
                "account_name": "devstoreaccount1",
                "container": "media",
                "identity_type": "account_key",
            },
            secret="key",
        )
        assert isinstance(connector, AzureBlobConnector)
        assert connector._public_account_url == AZURITE_PUBLIC
        assert connector._account_name == "devstoreaccount1"

    def test_omitting_them_keeps_the_previous_behaviour(self) -> None:
        connector = build_connector(
            "azure_blob",
            {
                "account_url": AZURE_REAL,
                "container": "media",
                "identity_type": "managed_identity",
            },
        )
        assert isinstance(connector, AzureBlobConnector)
        assert connector._public_account_url == AZURE_REAL
        assert connector._account_name == "acmedata"
