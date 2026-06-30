"""SharePoint connector — sync a document library to a Knowledge Base.

Uses Microsoft Graph API. Auth via SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID,
and one of:
  - SHAREPOINT_CLIENT_SECRET  (client secret auth)
  - SHAREPOINT_CERTIFICATE_PATH  (certificate auth — more secure, recommended
    for production).  Optionally set SHAREPOINT_CERTIFICATE_PASSWORD for
    encrypted PEM keys.

The two auth methods are mutually exclusive.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from typing import Any

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


class SharePointConnector(BaseConnector):
    """Sync files from a SharePoint document library."""

    def __init__(
        self,
        site: str,
        library: str = "Documents",
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        certificate_path: str | None = None,
        certificate_password: str | None = None,
    ):
        self.site = site
        self.library = library

        tid = tenant_id or os.environ.get("SHAREPOINT_TENANT_ID", "")
        cid = client_id or os.environ.get("SHAREPOINT_CLIENT_ID", "")
        secret = client_secret or os.environ.get("SHAREPOINT_CLIENT_SECRET", "")
        cert_path = certificate_path or os.environ.get("SHAREPOINT_CERTIFICATE_PATH", "")
        cert_password = certificate_password or os.environ.get("SHAREPOINT_CERTIFICATE_PASSWORD", "")

        if not tid or not cid:
            raise ValueError(
                "SharePoint credentials required. Set env vars:\n"
                "  SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID, and either\n"
                "  SHAREPOINT_CLIENT_SECRET or SHAREPOINT_CERTIFICATE_PATH"
            )

        if secret and cert_path:
            raise ValueError(
                "SHAREPOINT_CLIENT_SECRET and SHAREPOINT_CERTIFICATE_PATH are "
                "mutually exclusive. Set one or the other, not both."
            )

        if not secret and not cert_path:
            raise ValueError(
                "SharePoint auth method required. Set one of:\n"
                "  SHAREPOINT_CLIENT_SECRET  (client secret)\n"
                "  SHAREPOINT_CERTIFICATE_PATH  (certificate)"
            )

        token_url = f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token"

        if cert_path:
            access_token = _get_token_via_certificate(
                token_url=token_url,
                client_id=cid,
                certificate_path=cert_path,
                certificate_password=cert_password or None,
            )
        else:
            access_token = _get_token_via_secret(
                token_url=token_url,
                client_id=cid,
                client_secret=secret,
            )

        self._http = httpx.Client(
            base_url="https://graph.microsoft.com/v1.0",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=60.0,
            follow_redirects=True,
        )

        # Resolve site ID.
        site_resp = self._http.get(f"/sites/{self.site}")
        site_resp.raise_for_status()
        self._site_id = site_resp.json()["id"]

        # Resolve drive ID.
        drives_resp = self._http.get(f"/sites/{self._site_id}/drives")
        drives_resp.raise_for_status()
        self._drive_id = None
        for drive in drives_resp.json().get("value", []):
            if drive.get("name") == self.library:
                self._drive_id = drive["id"]
                break
        if not self._drive_id:
            drives = [d["name"] for d in drives_resp.json().get("value", [])]
            raise ValueError(f"Library '{self.library}' not found. Available: {drives}")

    def build_manifest(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        self._walk_folder("/", "", entries)
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _walk_folder(self, folder_path: str, prefix: str, entries: list[ManifestEntry]) -> None:
        url = f"/drives/{self._drive_id}/root/children" if folder_path == "/" else f"/drives/{self._drive_id}/root:/{folder_path}:/children"
        resp = self._http.get(url)
        resp.raise_for_status()

        for item in resp.json().get("value", []):
            if "folder" in item:
                sub = f"{prefix}/{item['name']}" if prefix else item["name"]
                child_path = f"{folder_path}/{item['name']}" if folder_path != "/" else item["name"]
                self._walk_folder(child_path, sub, entries)
            elif "file" in item:
                etag = (item.get("eTag") or item.get("cTag", "")).strip('"')
                entries.append(ManifestEntry(
                    filename=item["name"],
                    path=prefix,
                    checksum=etag[:16] if etag else "",
                    size=item.get("size", 0),
                ))

    def read_file(self, path: str, filename: str) -> bytes:
        file_path = f"{path}/{filename}" if path else filename
        resp = self._http.get(
            f"/drives/{self._drive_id}/root:/{file_path}:/content",
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:
        self._http.close()


# ── Auth helpers ────────────────────────────────────────────────


def _get_token_via_secret(token_url: str, client_id: str, client_secret: str) -> str:
    """Obtain an access token using client ID + client secret."""
    token_resp = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    token_resp.raise_for_status()
    return token_resp.json()["access_token"]


def _get_token_via_certificate(
    token_url: str,
    client_id: str,
    certificate_path: str,
    certificate_password: str | None = None,
) -> str:
    """Obtain an access token using client ID + certificate (JWT assertion).

    Reads a PEM file that contains both the private key and the certificate.
    Builds a signed JWT assertion per the Microsoft identity platform spec:
    https://learn.microsoft.com/en-us/entra/identity-platform/certificate-credentials
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise ImportError(
            "Certificate auth requires the 'cryptography' package.\n"
            "Install it with:  pip install oikb[sharepoint-cert]"
        )

    try:
        import jwt
    except ImportError:
        raise ImportError(
            "Certificate auth requires the 'PyJWT' package.\n"
            "Install it with:  pip install oikb[sharepoint-cert]"
        )

    # Load PEM file.
    pem_path = os.path.expanduser(certificate_path)
    if not os.path.isfile(pem_path):
        raise FileNotFoundError(f"Certificate file not found: {pem_path}")

    with open(pem_path, "rb") as f:
        pem_data = f.read()

    password_bytes = certificate_password.encode() if certificate_password else None

    # Load private key.
    private_key = serialization.load_pem_private_key(pem_data, password=password_bytes)

    # Load certificate to extract thumbprint.
    cert = x509.load_pem_x509_certificate(pem_data)
    thumbprint = cert.fingerprint(cert.signature_hash_algorithm or x509.hashes.SHA256())
    x5t = base64.urlsafe_b64encode(thumbprint).rstrip(b"=").decode("ascii")

    # Build JWT assertion.
    now = int(time.time())
    claims = {
        "aud": token_url,
        "iss": client_id,
        "sub": client_id,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + 600,  # 10 minute validity
    }
    headers = {
        "x5t": x5t,
    }

    assertion = jwt.encode(claims, private_key, algorithm="RS256", headers=headers)

    # Exchange assertion for access token.
    token_resp = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    token_resp.raise_for_status()
    return token_resp.json()["access_token"]


# ── Source parser ───────────────────────────────────────────────


def parse_sharepoint_source(source: str) -> dict[str, str | None]:
    """Parse sharepoint:site/library or sharepoint:site."""
    source = source.removeprefix("sharepoint:")
    parts = source.split("/", 1)
    site = parts[0]
    library = parts[1] if len(parts) > 1 else "Documents"
    if not site:
        raise ValueError("Invalid SharePoint source. Expected: sharepoint:<site>[/library]")
    return {"site": site, "library": library}
