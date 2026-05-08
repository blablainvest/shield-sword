from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class HttpError(RuntimeError):
    pass


class JsonHttpClient:
    def __init__(self, timeout: float = 10.0, retries: int = 2, sleep_seconds: float = 0.35) -> None:
        self.timeout = timeout
        self.retries = retries
        self.sleep_seconds = sleep_seconds

    def get_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        full_url = url + ("?" + query if query else "")
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "bybit-altcoin-hype-radar/0.1",
        }
        request_headers.update(headers or {})
        return self._request_json("GET", full_url, None, request_headers)

    def post_json(
        self,
        url: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "bybit-altcoin-hype-radar/0.1",
        }
        request_headers.update(headers or {})
        data = json.dumps(body or {}).encode("utf-8")
        return self._request_json("POST", url, data, request_headers)

    def post_form_json(
        self,
        url: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "bybit-altcoin-hype-radar/0.1",
        }
        request_headers.update(headers or {})
        data = urllib.parse.urlencode(body or {}).encode("utf-8")
        return self._request_json("POST", url, data, request_headers)

    def _request_json(
        self,
        method: str,
        full_url: str,
        data: Optional[bytes],
        headers: Dict[str, str],
    ) -> Dict[str, Any]:
        last_error: Optional[BaseException] = None

        for attempt in range(self.retries + 1):
            try:
                request = urllib.request.Request(
                    full_url,
                    data=data,
                    headers=headers,
                    method=method,
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8")
                return json.loads(payload)
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.sleep_seconds * (attempt + 1))

        raise HttpError("%s failed for %s: %s" % (method, full_url, last_error))
