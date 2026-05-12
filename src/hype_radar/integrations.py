from __future__ import annotations

import os
import base64
from typing import Any, Dict, List, Optional

from .http import JsonHttpClient


class CoinGeckoClient:
    """Thin client for later token metadata enrichment."""

    def __init__(self, api_key: Optional[str] = None, http: Optional[JsonHttpClient] = None) -> None:
        self.api_key = api_key or os.getenv("COINGECKO_API_KEY")
        self.http = http or JsonHttpClient()
        self.base_url = "https://api.coingecko.com/api/v3"

    def search(self, query: str) -> Dict[str, Any]:
        return self.http.get_json(self.base_url + "/search", self._params({"query": query}))

    def coin(self, coin_id: str) -> Dict[str, Any]:
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false",
        }
        return self.http.get_json(self.base_url + "/coins/" + coin_id, self._params(params))

    def markets(self, coin_id: str) -> List[Dict[str, Any]]:
        payload = self.http.get_json(
            self.base_url + "/coins/markets",
            self._params(
                {
                    "vs_currency": "usd",
                    "ids": coin_id,
                    "price_change_percentage": "24h,7d",
                }
            ),
        )
        return payload if isinstance(payload, list) else []

    def trending(self) -> Dict[str, Any]:
        return self.http.get_json(self.base_url + "/search/trending", self._params({}))

    def categories(self) -> List[Dict[str, Any]]:
        payload = self.http.get_json(self.base_url + "/coins/categories", self._params({}))
        return payload if isinstance(payload, list) else []

    def _params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.api_key:
            return {**params, "x_cg_demo_api_key": self.api_key}
        return params


class GdeltClient:
    """GDELT DOC API client for news/catalyst sanity checks."""

    def __init__(self, http: Optional[JsonHttpClient] = None) -> None:
        self.http = http or JsonHttpClient()
        self.base_url = "https://api.gdeltproject.org/api/v2/doc/doc"

    def search_articles(self, query: str, max_records: int = 20) -> Dict[str, Any]:
        return self.http.get_json(
            self.base_url,
            {
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": max_records,
            },
        )


class XApiClient:
    """X API client for recent-search based social ingestion."""

    def __init__(self, bearer_token: Optional[str] = None, http: Optional[JsonHttpClient] = None) -> None:
        self.bearer_token = bearer_token or os.getenv("X_BEARER_TOKEN")
        self.http = http or JsonHttpClient()
        self.base_url = "https://api.x.com/2"

    def configured(self) -> bool:
        return bool(self.bearer_token)

    def recent_search(self, query: str, max_results: int = 50) -> Dict[str, Any]:
        if not self.configured():
            raise RuntimeError("X API is not configured. Set X_BEARER_TOKEN.")
        return self.http.get_json(
            self.base_url + "/tweets/search/recent",
            {
                "query": query,
                "max_results": max(10, min(max_results, 100)),
                "tweet.fields": "created_at,author_id,public_metrics,lang,referenced_tweets",
                "expansions": "author_id",
                "user.fields": "public_metrics,verified,username",
            },
            headers={"Authorization": "Bearer %s" % self.bearer_token},
        )


class XaiGrokClient:
    """xAI chat-completions client used as classifier after social/news ingest."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, http: Optional[JsonHttpClient] = None) -> None:
        self.api_key = api_key or os.getenv("XAI_API_KEY")
        self.model = model or os.getenv("XAI_MODEL") or "grok-3-mini"
        self.http = http or JsonHttpClient()
        self.base_url = "https://api.x.ai/v1"

    def configured(self) -> bool:
        return bool(self.api_key)

    def classify(self, system_prompt: str, user_payload: str) -> Dict[str, Any]:
        if not self.configured():
            raise RuntimeError("xAI is not configured. Set XAI_API_KEY.")
        return self.http.post_json(
            self.base_url + "/chat/completions",
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
            },
            headers={"Authorization": "Bearer %s" % self.api_key},
        )


class RedditClient:
    """Reddit API client for slower discussion sanity checks."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        http: Optional[JsonHttpClient] = None,
    ) -> None:
        self.client_id = client_id or os.getenv("REDDIT_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("REDDIT_CLIENT_SECRET")
        self.http = http or JsonHttpClient()
        self.base_url = "https://oauth.reddit.com"

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def search_posts(self, query: str, limit: int = 25, subreddit: str = "CryptoCurrency") -> Dict[str, Any]:
        token = self._access_token()
        return self.http.get_json(
            self.base_url + "/r/%s/search" % subreddit,
            {"q": query, "restrict_sr": "true", "sort": "new", "limit": limit},
            headers={"Authorization": "Bearer %s" % token},
        )

    def _access_token(self) -> str:
        if not self.configured():
            raise RuntimeError("Reddit is not configured. Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET.")
        raw = ("%s:%s" % (self.client_id, self.client_secret)).encode("utf-8")
        auth = base64.b64encode(raw).decode("ascii")
        payload = self.http.post_form_json(
            "https://www.reddit.com/api/v1/access_token",
            {"grant_type": "client_credentials"},
            headers={"Authorization": "Basic %s" % auth},
        )
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Reddit token response did not include access_token.")
        return str(token)


class TelegramNotifier:
    """Telegram Bot API notifier for later actionable alerts."""

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        http: Optional[JsonHttpClient] = None,
    ) -> None:
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.http = http or JsonHttpClient()

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, text: str) -> Dict[str, Any]:
        if not self.configured():
            raise RuntimeError("Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        url = "https://api.telegram.org/bot%s/sendMessage" % self.token
        return self.http.get_json(url, {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"})
