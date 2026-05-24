from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlsplit


class OAuth1Signer:
    def __init__(
        self,
        api_key: str,
        api_key_secret: str,
        access_token: str,
        access_token_secret: str,
    ) -> None:
        self.api_key = api_key
        self.api_key_secret = api_key_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret

    def authorization_header(
        self,
        method: str,
        url: str,
        query_params: dict[str, str | int],
    ) -> str:
        oauth_params = {
            "oauth_consumer_key": self.api_key,
            "oauth_nonce": secrets.token_urlsafe(24),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self.access_token,
            "oauth_version": "1.0",
        }
        signature = self._signature(method, url, query_params, oauth_params)
        oauth_params["oauth_signature"] = signature
        header_params = ", ".join(
            f'{_percent_encode(key)}="{_percent_encode(value)}"'
            for key, value in sorted(oauth_params.items())
        )
        return f"OAuth {header_params}"

    def _signature(
        self,
        method: str,
        url: str,
        query_params: dict[str, str | int],
        oauth_params: dict[str, str],
    ) -> str:
        normalized_url = _normalize_url(url)
        params = [(str(key), str(value)) for key, value in query_params.items()]
        params.extend(oauth_params.items())
        params.sort(key=lambda item: (_percent_encode(item[0]), _percent_encode(item[1])))

        parameter_string = "&".join(
            f"{_percent_encode(key)}={_percent_encode(value)}" for key, value in params
        )
        signature_base = "&".join(
            [
                method.upper(),
                _percent_encode(normalized_url),
                _percent_encode(parameter_string),
            ]
        )
        signing_key = (
            f"{_percent_encode(self.api_key_secret)}&"
            f"{_percent_encode(self.access_token_secret)}"
        )
        digest = hmac.new(
            signing_key.encode(),
            signature_base.encode(),
            hashlib.sha1,
        ).digest()
        return base64.b64encode(digest).decode()


def _normalize_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""
    port = parsed.port
    include_port = port and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    )
    authority = f"{host}:{port}" if include_port else host
    return f"{scheme}://{authority}{parsed.path or '/'}"


def _percent_encode(value: str) -> str:
    return quote(str(value), safe="~")
