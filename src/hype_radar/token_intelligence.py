from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from html import unescape
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from .integrations import CoinGeckoClient


class TokenIntelligenceClient(Protocol):
    def configured(self) -> bool:
        ...

    def research(self, base_coin: str) -> Dict[str, Any]:
        ...


@dataclass
class TokenIdentity:
    coin_id: Optional[str]
    symbol: str
    name: str
    confidence: float
    reason: str
    raw_match: Dict[str, Any] = field(default_factory=dict)


class NullTokenIntelligenceClient:
    def configured(self) -> bool:
        return False

    def research(self, base_coin: str) -> Dict[str, Any]:
        raise RuntimeError("Token intelligence is not configured.")


class OpenAiTextNormalizer:
    def __init__(self, timeout_seconds: int = 18) -> None:
        self.api_key = os.getenv("OPENAI_API_KEY") or ""
        self.model = os.getenv("OPENAI_TRANSLATION_MODEL") or "gpt-4.1-nano"
        self.base_url = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.timeout_seconds = timeout_seconds

    def configured(self) -> bool:
        return bool(self.api_key)

    def normalize_fundamental(
        self,
        project_summary: Optional[str],
        top_posts: List[str],
        supportive: List[str],
        suspicious: List[str],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not self.configured():
            return {}

        payload = {
            "project_summary": project_summary or "",
            "top_posts": top_posts[:5],
            "supportive_factors": supportive[:6],
            "suspicious_factors": suspicious[:6],
            "context": context,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты редактор русскоязычного crypto research dashboard. "
                    "Переводи и сжимай факты, не добавляй новых фактов и не давай торговых рекомендаций. "
                    "CoinGecko-категории и platforms являются источником истины для сектора/экосистемы; "
                    "LunarCrush topics описывают только соцтему и не должны подменять экосистему проекта. "
                    "Верни только валидный JSON с ключами: project_brief_ru, top_posts_ru, "
                    "movement_supportive_ru, movement_suspicious_ru. project_brief_ru должен быть до 500 символов. "
                    "Пункты списков должны быть короткими и читабельными на русском."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        data = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=data,
            method="POST",
            headers={
                "Authorization": "Bearer %s" % self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ShieldSword/0.1 (+local research dashboard)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
            parsed = json.loads(raw)
            content = parsed["choices"][0]["message"]["content"]
            normalized = json.loads(content)
            return normalized if isinstance(normalized, dict) else {}
        except Exception:  # noqa: BLE001 - translation must never break research.
            return {}


class MppTokenIntelligenceClient:
    def __init__(
        self,
        tempo_bin: Optional[str] = None,
        max_spend: Optional[str] = None,
        timeout_seconds: int = 20,
    ) -> None:
        self.tempo_bin = tempo_bin or os.getenv("TEMPO_BIN") or os.path.expanduser("~/.tempo/bin/tempo")
        self.max_spend = max_spend or os.getenv("MPP_MAX_SPEND_PER_RESEARCH") or "0.50"
        self.timeout_seconds = timeout_seconds
        self.coingecko_url = os.getenv("MPP_COINGECKO_URL") or "https://coingecko.mpp.paywithlocus.com"
        self.lunarcrush_url = os.getenv("LUNARCRUSH_URL") or "https://lunarcrush.com/api4"
        self.lunarcrush_key = os.getenv("LUNARCRUSH_API_KEY") or ""
        self.direct_coingecko = CoinGeckoClient()
        self.identity_cache: Dict[str, TokenIdentity] = {}
        explicit = os.getenv("ENABLE_MPP_ENRICHMENT")
        self.enabled = explicit is None or explicit.lower() not in {"0", "false", "no", "off"}

    def configured(self) -> bool:
        return self.enabled

    def research(self, base_coin: str) -> Dict[str, Any]:
        if not self.configured():
            raise RuntimeError("Token intelligence enrichment is disabled.")

        if self._tempo_configured():
            return self._research_via_mpp(base_coin)
        return self._research_direct(base_coin)

    def _research_via_mpp(self, base_coin: str) -> Dict[str, Any]:
        errors: List[str] = []
        identity, search = self._cached_identity(
            base_coin,
            lambda: self._safe_post(self.coingecko_url, "/coingecko/search", {"query": base_coin}, errors),
        )
        if not identity.coin_id:
            return {
                "base_coin": base_coin,
                "identity": identity.__dict__,
                "coingecko": {"search": search, "provider": "mpp"},
                "lunarcrush": self._lunarcrush_research(base_coin, errors),
                "errors": errors,
            }

        coin_data = self._safe_get(
            self.coingecko_url,
            "/coingecko/coin-data",
            {
                "id": identity.coin_id,
                "community_data": "false",
                "developer_data": "false",
            },
            errors,
        )
        market = self._safe_get(
            self.coingecko_url,
            "/coingecko/coins-markets",
            {"vs_currency": "usd", "ids": identity.coin_id, "price_change_percentage": "24h,7d"},
            errors,
        )
        lunarcrush = self._lunarcrush_research(base_coin, errors)

        return {
            "base_coin": base_coin,
            "identity": identity.__dict__,
            "coingecko": {
                "provider": "mpp",
                "search": search,
                "coin_data": coin_data,
                "market": market,
            },
            "lunarcrush": lunarcrush,
            "errors": errors,
        }

    def _research_direct(self, base_coin: str) -> Dict[str, Any]:
        errors: List[str] = []
        identity, search = self._cached_identity(
            base_coin,
            lambda: self._safe_direct("coingecko/search", lambda: self.direct_coingecko.search(base_coin), errors),
        )
        lunarcrush = self._lunarcrush_research(base_coin, errors)
        if not identity.coin_id:
            return {
                "base_coin": base_coin,
                "identity": identity.__dict__,
                "coingecko": {"provider": "direct", "search": search},
                "lunarcrush": lunarcrush,
                "errors": errors,
            }

        coin_data = self._safe_direct("coingecko/coin", lambda: self.direct_coingecko.coin(identity.coin_id or ""), errors)
        market = self._safe_direct("coingecko/markets", lambda: self.direct_coingecko.markets(identity.coin_id or ""), errors)

        return {
            "base_coin": base_coin,
            "identity": identity.__dict__,
            "coingecko": {
                "provider": "direct",
                "search": search,
                "coin_data": coin_data,
                "market": market,
            },
            "lunarcrush": lunarcrush,
            "errors": errors,
        }

    def _cached_identity(self, base_coin: str, search_callback: Any) -> tuple[TokenIdentity, Dict[str, Any]]:
        key = base_coin.upper()
        if key in self.identity_cache:
            return self.identity_cache[key], {"cached": True, "query": base_coin}
        search = search_callback()
        identity = select_coingecko_identity(base_coin, search)
        self.identity_cache[key] = identity
        return identity, search if isinstance(search, dict) else {}

    def _tempo_configured(self) -> bool:
        return bool(shutil.which(self.tempo_bin) or os.path.exists(self.tempo_bin))

    def _lunarcrush_research(self, base_coin: str, errors: List[str]) -> Dict[str, Any]:
        if not self.lunarcrush_key:
            return {"skipped": True, "reason": "LUNARCRUSH_API_KEY is not configured."}
        topic = "%24" + urllib.parse.quote(base_coin.lower())
        coin = urllib.parse.quote(base_coin.lower())
        endpoints = {
            "coin": "/public/coins/%s/v1" % coin,
            "coin_meta": "/public/coins/%s/meta/v1" % coin,
            "topic": "/public/topic/%s/v1" % topic,
            "whatsup": "/public/topic/%s/whatsup/v1" % topic,
            "posts": "/public/topic/%s/posts/v1" % topic,
            "news": "/public/topic/%s/news/v1" % topic,
            "creators": "/public/topic/%s/creators/v1" % topic,
        }
        payload: Dict[str, Any] = {"topic_key": "$" + base_coin.upper()}
        for key, path in endpoints.items():
            payload[key] = self._lunarcrush_get(path, errors)
        if not any(payload.get(key) for key in endpoints):
            return {"skipped": True, "reason": "LunarCrush returned no usable topic data.", **payload}
        return payload

    def _lunarcrush_get(self, path: str, errors: List[str]) -> Any:
        separator = "&" if "?" in path else "?"
        query = urllib.parse.urlencode({"format": "json", "key": self.lunarcrush_key})
        url = "%s%s%s%s" % (self.lunarcrush_url.rstrip("/"), path, separator, query)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer %s" % self.lunarcrush_key,
                "User-Agent": "ShieldSword/0.1 (+local research dashboard)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout_seconds, 12)) as response:
                payload = response.read().decode("utf-8")
            parsed = json.loads(payload) if payload else {}
            return parsed
        except Exception as exc:  # noqa: BLE001 - optional enrichment must degrade cleanly.
            errors.append("%s: %s" % (path, exc))
            return {}

    def _safe_get(self, base_url: str, path: str, params: Dict[str, Any], errors: List[str]) -> Any:
        return self._safe_post(base_url, path, params, errors)

    def _safe_post(self, base_url: str, path: str, params: Dict[str, Any], errors: List[str]) -> Any:
        try:
            return self._tempo_post(base_url, path, params)
        except Exception as exc:  # noqa: BLE001 - optional enrichment must degrade per endpoint.
            errors.append("%s: %s" % (path, exc))
            return {}

    def _safe_direct(self, name: str, callback: Any, errors: List[str]) -> Any:
        try:
            return callback()
        except Exception as exc:  # noqa: BLE001 - optional enrichment must degrade per endpoint.
            errors.append("%s: %s" % (name, exc))
            return {}

    def _tempo_post(self, base_url: str, path: str, params: Dict[str, Any]) -> Any:
        url = base_url.rstrip("/") + path
        command = [
            self.tempo_bin,
            "request",
            "--max-spend",
            self.max_spend,
            "-s",
            "-X",
            "POST",
            "--json",
            json.dumps({key: value for key, value in params.items() if value is not None}),
            "-m",
            str(self.timeout_seconds),
            url,
        ]
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=self.timeout_seconds + 5)
        payload = completed.stdout.strip()
        parsed = json.loads(payload) if payload else {}
        if isinstance(parsed, dict) and parsed.get("success") is True and "data" in parsed:
            return parsed["data"]
        if isinstance(parsed, dict) and parsed.get("code"):
            raise RuntimeError("%s: %s" % (parsed.get("code"), parsed.get("message") or "MPP request failed"))
        return parsed


def select_coingecko_identity(base_coin: str, search_payload: Any) -> TokenIdentity:
    coins = list((search_payload or {}).get("coins") or []) if isinstance(search_payload, dict) else []
    base = base_coin.lower()
    exact = [coin for coin in coins if str(coin.get("symbol", "")).lower() == base]
    candidates = exact or coins
    if not candidates:
        return TokenIdentity(None, base_coin, "", 0.0, "No CoinGecko search result matched the Bybit base coin.")

    def rank_key(coin: Dict[str, Any]) -> int:
        rank = coin.get("market_cap_rank")
        return int(rank) if isinstance(rank, int) and rank > 0 else 1_000_000

    selected = sorted(candidates, key=rank_key)[0]
    confidence = 0.95 if selected in exact else 0.55
    return TokenIdentity(
        coin_id=str(selected.get("id") or ""),
        symbol=str(selected.get("symbol") or base_coin).upper(),
        name=str(selected.get("name") or ""),
        confidence=confidence,
        reason="Exact ticker match." if selected in exact else "Fallback to highest-ranked CoinGecko search result.",
        raw_match=selected,
    )


def first_contract_address(coin_data: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    platforms = coin_data.get("platforms") if isinstance(coin_data, dict) else None
    if not isinstance(platforms, dict):
        return None, None
    preferred = ["starknet", "ethereum", "base", "arbitrum-one", "polygon-pos", "binance-smart-chain", "solana"]
    for chain in preferred + sorted(platforms):
        address = platforms.get(chain)
        if address:
            return chain, str(address)
    return None, None


def fundamentals_stage_payload(token_data: Optional[Dict[str, Any]], hype_cause: List[str]) -> Dict[str, Any]:
    if not token_data:
        return {
            "status": "skipped",
            "score": None,
            "reason": "Фундаментал пропущен: CoinGecko/LunarCrush intelligence не настроен или недоступен.",
            "metrics": {
                "fundamental_label": "Недостаточно данных",
                "fundamental_label_reason": "Источники не дали достаточно данных для уверенного фундаментального вывода.",
                "hype_cause": hype_cause,
                "circulating_supply_warn_threshold": 0.30,
            },
        }
    identity = token_data.get("identity") or {}
    if not identity.get("coin_id") or float(identity.get("confidence") or 0.0) < 0.7:
        return {
            "status": "skipped",
            "score": None,
            "reason": "Фундаментал пропущен: не удалось надежно сопоставить тикер с CoinGecko coin_id.",
            "metrics": {
                "fundamental_label": "Недостаточно данных",
                "fundamental_label_reason": "Источники не дали достаточно данных для уверенного фундаментального вывода.",
                "identity_confidence": identity.get("confidence"),
                "identity_reason": identity.get("reason"),
                "circulating_supply_warn_threshold": 0.30,
            },
        }

    metrics = extract_fundamental_metrics(token_data)
    score = round(
        metrics["project_quality_score"] * 0.40
        + metrics["narrative_score"] * 0.35
        + (100.0 - metrics["tokenomics_risk_score"]) * 0.25,
        2,
    )
    label, status, label_reason = classify_fundamental(metrics)
    metrics.update(
        {
            "fundamental_label": label,
            "fundamental_label_reason": label_reason,
            "score_components": {
                "project_quality_score": metrics["project_quality_score"],
                "narrative_score": metrics["narrative_score"],
                "tokenomics_risk_score": metrics["tokenomics_risk_score"],
            },
            "hype_cause": hype_cause,
            "circulating_supply_warn_threshold": 0.30,
        }
    )
    return {
        "status": status,
        "score": score,
        "reason": "Фундаментал: %s. См. структурированные факторы в карточке." % label,
        "metrics": metrics,
    }


def extract_fundamental_metrics(token_data: Dict[str, Any], normalize_text: bool = True) -> Dict[str, Any]:
    coingecko = token_data.get("coingecko") or {}
    coin_data = coingecko.get("coin_data") or {}
    market_data = coin_data.get("market_data") or {}
    market_row = first_market_row(coingecko.get("market"))
    identity = token_data.get("identity") or {}
    categories = list(coin_data.get("categories") or [])
    links = coin_data.get("links") or {}
    market_cap = first_number(market_row.get("market_cap"), nested_number(market_data.get("market_cap"), "usd"))
    fdv = first_number(market_row.get("fully_diluted_valuation"), nested_number(market_data.get("fully_diluted_valuation"), "usd"))
    volume = first_number(market_row.get("total_volume"), nested_number(market_data.get("total_volume"), "usd"))
    circulating = first_number(market_row.get("circulating_supply"), market_data.get("circulating_supply"))
    total_supply = first_number(market_row.get("total_supply"), market_data.get("total_supply"), market_data.get("max_supply"))
    price_change_24h = first_number(
        market_row.get("price_change_percentage_24h"),
        market_row.get("price_change_percentage_24h_in_currency"),
        nested_number(market_data.get("price_change_percentage_24h_in_currency"), "usd"),
    )
    price_change_7d = first_number(
        market_row.get("price_change_percentage_7d_in_currency"),
        nested_number(market_data.get("price_change_percentage_7d_in_currency"), "usd"),
    )
    circ_ratio = circulating / total_supply if circulating and total_supply and total_supply > 0 else None
    fdv_to_market_cap = fdv / market_cap if fdv and market_cap and market_cap > 0 else None
    market_cap_to_fdv = market_cap / fdv if market_cap and fdv and fdv > 0 else None
    volume_to_market_cap = volume / market_cap if volume and market_cap and market_cap > 0 else None
    size_tier = fdv_tier(fdv)
    supply_profile = market_cap_to_fdv_profile(market_cap_to_fdv)
    lunar_metrics = extract_lunarcrush_metrics(token_data.get("lunarcrush") or {})
    description = clean_text(nested_text(coin_data.get("description"), "en"))
    project_summary = first_text(description, lunar_metrics.get("project_summary"))
    sector = categories[0] if categories else None
    chain, address = first_contract_address(coin_data)
    link_metrics = coingecko_link_metrics(links)
    trend = trend_profile(categories, lunar_metrics)
    source_conflict = taxonomy_source_conflict(sector, chain, trend.get("social_topic"))
    attention = attention_phase_profile(lunar_metrics)
    unlock = unlock_risk_profile(coin_data, lunar_metrics)
    red_flags = fundamental_red_flags(circ_ratio, fdv_to_market_cap, volume_to_market_cap, lunar_metrics, unlock)
    supporting_factors = fundamental_supporting_factors(identity, categories, links, trend, lunar_metrics)
    suspicious_factors = red_flags + list(lunar_metrics.get("critical_causes") or [])
    project_quality = project_quality_score(identity, categories, links, project_summary)
    narrative = narrative_score(trend, lunar_metrics)
    tokenomics_risk = tokenomics_risk_score(circ_ratio, fdv_to_market_cap, volume_to_market_cap, unlock)
    normalized = (
        OpenAiTextNormalizer().normalize_fundamental(
            project_summary,
            list(lunar_metrics.get("top_posts") or []),
            supporting_factors,
            suspicious_factors,
            {
                "symbol": token_data.get("base_coin"),
                "name": identity.get("name"),
                "sector": sector,
                "trend": trend,
                "source_conflict": source_conflict,
                "attention_phase": attention,
                "market_cap": market_cap,
                "fdv": fdv,
                "market_cap_to_fdv_ratio": market_cap_to_fdv,
                "volume_to_market_cap": volume_to_market_cap,
            },
        )
        if normalize_text
        else {}
    )
    project_brief = compact_ui_text(normalized.get("project_brief_ru") or fallback_project_brief(project_summary), 500)
    movement_supportive_ru = normalized_list(normalized.get("movement_supportive_ru"), supporting_factors, max_items=6)
    movement_suspicious_ru = normalized_list(normalized.get("movement_suspicious_ru"), suspicious_factors, max_items=6)
    top_posts_ru = normalized_list(normalized.get("top_posts_ru"), lunar_metrics.get("top_posts") or [], max_items=5)
    movement_type_reasons = movement_type_reasons_for(circ_ratio, volume_to_market_cap, trend, lunar_metrics, tokenomics_risk)
    thesis = build_fundamental_thesis(project_brief, trend, movement_supportive_ru, movement_suspicious_ru)
    return {
        "coin_id": identity.get("coin_id"),
        "identity_confidence": identity.get("confidence"),
        "name": identity.get("name"),
        "project_summary": project_summary,
        "project_brief_ru": project_brief,
        "sector": sector,
        "sector_source": "CoinGecko" if sector else "нет данных",
        "narrative": trend.get("label"),
        "chain_ecosystem": chain or sector,
        "chain_source": "CoinGecko platforms" if chain else "CoinGecko category" if sector else "нет данных",
        "contract_address": address,
        "categories": categories[:8],
        "market_cap": market_cap,
        "fdv": fdv,
        "fdv_tier": size_tier["tier"],
        "fdv_tier_label": size_tier["label"],
        "fdv_tier_reason": size_tier["reason"],
        "fdv_to_market_cap": fdv_to_market_cap,
        "market_cap_to_fdv_ratio": market_cap_to_fdv,
        "market_cap_to_fdv_level": supply_profile["level"],
        "market_cap_to_fdv_label": supply_profile["label"],
        "market_cap_to_fdv_reason": supply_profile["reason"],
        "volume_24h": volume,
        "volume_to_market_cap": volume_to_market_cap,
        "price_change_24h": price_change_24h,
        "price_change_7d": price_change_7d,
        "circulating_supply": circulating,
        "total_or_max_supply": total_supply,
        "circulating_supply_ratio": circ_ratio,
        "trend_label": trend.get("label"),
        "trend_source": trend.get("source"),
        "social_topic": trend.get("social_topic"),
        "social_topic_source": trend.get("social_topic_source"),
        "source_conflict": source_conflict,
        "trend_strength": trend.get("strength"),
        "attention_phase": attention.get("phase"),
        "attention_phase_reasons": attention.get("reasons"),
        "social_velocity_level": social_strength_level(social_velocity_score(lunar_metrics)) if lunar_metrics.get("available") else "нет данных",
        "social_quality_level": social_strength_level(social_quality_score(lunar_metrics)) if lunar_metrics.get("available") else "нет данных",
        "hype_freshness_level": social_strength_level(hype_freshness_score(lunar_metrics)) if lunar_metrics.get("available") else "нет данных",
        "why_moved": lunar_metrics.get("why_moved") or trend.get("why_moved"),
        "social_posts_24h": lunar_metrics.get("social_activity_24h"),
        "social_authors_24h": lunar_metrics.get("social_contributors_24h"),
        "social_interactions_24h": lunar_metrics.get("social_interactions_24h"),
        "top_posts": lunar_metrics.get("top_posts"),
        "top_posts_ru": top_posts_ru,
        "movement_supportive": supporting_factors[:8],
        "movement_supportive_ru": movement_supportive_ru,
        "movement_suspicious": suspicious_factors[:8],
        "movement_suspicious_ru": movement_suspicious_ru,
        "movement_type_reasons": movement_type_reasons,
        "red_flags": red_flags[:8],
        "project_quality_score": round(project_quality, 2),
        "narrative_score": round(narrative, 2),
        "tokenomics_risk_score": round(tokenomics_risk, 2),
        "project_quality_level": score_level(project_quality, "quality"),
        "narrative_level": score_level(narrative, "narrative"),
        "tokenomics_risk_level": score_level(tokenomics_risk, "risk"),
        "lunarcrush_available": bool(lunar_metrics.get("available")),
        "lunarcrush_summary": lunar_metrics.get("summary"),
        "lunarcrush_alerts": lunar_metrics.get("alerts"),
        "social_growth": lunar_metrics.get("social_growth"),
        "social_dominance": lunar_metrics.get("social_dominance"),
        "sentiment": lunar_metrics.get("sentiment"),
        "influencers_count": lunar_metrics.get("influencers_count"),
        **link_metrics,
        "unlock_risk_label": unlock.get("label"),
        "unlock_mentions": unlock.get("mentions"),
        "unlock_relevance": unlock.get("relevance"),
        "data_coverage": data_coverage_label(coingecko, lunar_metrics),
        "thesis": thesis,
    }


def classify_fundamental(metrics: Dict[str, Any]) -> tuple[str, str, str]:
    project_score = float(metrics.get("project_quality_score") or 0.0)
    narrative = float(metrics.get("narrative_score") or 0.0)
    tokenomics_risk = float(metrics.get("tokenomics_risk_score") or 0.0)
    volume_to_market_cap = metrics.get("volume_to_market_cap")
    overheated_volume = volume_to_market_cap is not None and volume_to_market_cap > 0.50
    extreme_tokenomics = tokenomics_risk >= 65

    if project_score < 35 and narrative < 45:
        return (
            "Недостаточно данных",
            "skipped",
            "Источники не дали достаточно данных для уверенного фундаментального вывода.",
        )
    if narrative >= 55 and project_score >= 50 and not overheated_volume and not extreme_tokenomics:
        return (
            "Нарратив подтвержден",
            "pass",
            "Есть достаточное совпадение описания проекта, категории/экосистемы и рыночного или социального нарратива.",
        )
    return (
        "Спекулятивный риск",
        "warn",
        "Движение требует осторожности: слабое подтверждение, перегрев объема или риск токеномики.",
    )


def fdv_tier(fdv: Optional[float]) -> Dict[str, str]:
    value = first_number(fdv)
    if value is None or value <= 0:
        return {
            "tier": "unknown",
            "label": "FDV: нет данных",
            "reason": "CoinGecko не дал fully diluted valuation.",
        }
    if value < 50_000_000:
        return {
            "tier": "tiny",
            "label": "Tiny cap / крошечная",
            "reason": "FDV ниже $50M: очень малая оценка, максимальная чувствительность к манипуляциям.",
        }
    if value < 100_000_000:
        return {
            "tier": "low",
            "label": "Low cap / мелкая",
            "reason": "FDV от $50M до $100M: мелкая оценка, выше риск манипуляций.",
        }
    if value < 1_000_000_000:
        return {
            "tier": "mid",
            "label": "Mid cap / средняя",
            "reason": "FDV от $100M до $1B.",
        }
    if value < 10_000_000_000:
        return {
            "tier": "big",
            "label": "Big cap / крупная",
            "reason": "FDV от $1B до $10B.",
        }
    return {
        "tier": "giant",
        "label": "Giant cap / гигант",
        "reason": "FDV от $10B и выше.",
    }


def market_cap_tier(market_cap: Optional[float]) -> Dict[str, str]:
    return fdv_tier(market_cap)


def market_cap_to_fdv_profile(ratio: Optional[float]) -> Dict[str, str]:
    value = first_number(ratio)
    if value is None or value < 0:
        return {
            "level": "unknown",
            "label": "MC/FDV: нет данных",
            "reason": "Недостаточно данных, чтобы оценить долю supply в рынке.",
        }
    pct_value = value * 100.0
    if pct_value > 100:
        return {
            "level": "anomaly",
            "label": "MC/FDV выше 100%",
            "reason": "MC выше FDV: возможна аномалия данных, не используем как надежный сигнал.",
        }
    if pct_value < 20:
        return {
            "level": "0-20",
            "label": "0-20% supply в рынке",
            "reason": "Очень низкая доля MC к FDV: сильный unlock/overhang risk.",
        }
    if pct_value < 40:
        return {
            "level": "20-40",
            "label": "20-40% supply в рынке",
            "reason": "Низкая доля MC к FDV: риск будущего размывания высокий.",
        }
    if pct_value < 60:
        return {
            "level": "40-60",
            "label": "40-60% supply в рынке",
            "reason": "Средняя доля MC к FDV.",
        }
    if pct_value < 80:
        return {
            "level": "60-80",
            "label": "60-80% supply в рынке",
            "reason": "Хорошая доля supply уже отражена в рынке.",
        }
    return {
        "level": "80-100",
        "label": "80-100% supply в рынке",
        "reason": "Почти весь supply уже в рынке.",
    }


def coingecko_link_metrics(links: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(links, dict):
        return {}
    repos = links.get("repos_url") if isinstance(links.get("repos_url"), dict) else {}
    return {
        "homepage_url": first_link(links.get("homepage")),
        "whitepaper_url": first_link(links.get("whitepaper")),
        "twitter_screen_name": first_text(links.get("twitter_screen_name")),
        "telegram_channel_identifier": first_text(links.get("telegram_channel_identifier")),
        "subreddit_url": first_text(links.get("subreddit_url")),
        "github_repos": compact_texts(repos.get("github") or [], 5) if isinstance(repos, dict) else [],
    }


def extract_lunarcrush_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not payload or payload.get("skipped"):
        return {"available": False, "reason": payload.get("reason") if isinstance(payload, dict) else None}
    root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    coin_data = lunar_endpoint_data(payload, "coin")
    topic_data = lunar_endpoint_data(payload, "topic")
    meta_data = lunar_endpoint_data(payload, "coin_meta")
    post_rows = lunar_endpoint_list(payload, "posts")
    ai_summary = find_first_dict(root, ["ai_summary", "summary", "aiSummary"])
    metrics = find_first_dict(root, ["metrics", "market", "social_metrics"]) or {}
    asset = coin_data or topic_data or find_first_dict(root, ["asset", "coin", "topic"]) or {}
    alerts = find_first_list(root, ["alerts", "events"]) or []
    topics = lunar_topic_labels(topic_data) or find_first_list(root, ["top_topics", "topics", "categories"]) or []
    influencers = find_first_list(root, ["influencers", "creators", "top_influencers"]) or []
    top_posts = compact_texts([nested_text(post, "post_title") for post in post_rows], 8)
    post_sentiments = [first_number(post.get("post_sentiment")) for post in post_rows if isinstance(post, dict)]
    post_sentiments = [value for value in post_sentiments if value is not None]
    avg_post_sentiment = sum(post_sentiments) / len(post_sentiments) if post_sentiments else None
    social_activity = first_number(topic_data.get("num_posts"))
    contributors = first_number(topic_data.get("num_contributors"))
    interactions = first_number(topic_data.get("interactions_24h"))

    summary = clean_text(
        first_text(
            nested_text(ai_summary, "summary"),
            nested_text(ai_summary, "description"),
            nested_text(ai_summary, "overview"),
            nested_text(meta_data, "short_summary"),
            nested_text(meta_data, "description"),
            safe_lunar_summary(find_text_by_key(meta_data, ["short_summary", "description", "summary"])),
        )
    )
    why_moved = clean_text(
        first_text(
            nested_text(ai_summary, "why"),
            nested_text(ai_summary, "whatsup"),
            nested_text(ai_summary, "reason"),
            nested_text(ai_summary, "price_action"),
            lunar_why_moved(topic_data, top_posts),
        )
    )
    supportive = extract_cause_list(ai_summary, ["supportive", "bullish", "positive", "supporting"])
    critical = extract_cause_list(ai_summary, ["critical", "bearish", "negative", "risks"])
    critical.extend(lunar_suspicious_post_titles(top_posts))
    alert_titles = compact_texts([nested_text(alert, "title") or nested_text(alert, "message") or nested_text(alert, "description") for alert in alerts], 5)
    topic_names = compact_texts([nested_text(topic, "name") or nested_text(topic, "topic") or str(topic) for topic in topics], 8)
    social_growth = first_number(
        find_number_by_key(metrics, ["social_growth", "social_change", "social_volume_24h_change"]),
        find_number_by_key(root, ["social_growth", "social_change", "social_volume_24h_change"]),
    )
    sentiment = first_number(
        topic_sentiment_score(topic_data),
        avg_post_sentiment * 20.0 if avg_post_sentiment is not None and avg_post_sentiment <= 5 else avg_post_sentiment,
        find_number_by_key(metrics, ["sentiment"]),
        find_number_by_key(root, ["sentiment"]),
    )
    galaxy_score = first_number(coin_data.get("galaxy_score"), find_number_by_key(metrics, ["galaxy_score"]), find_number_by_key(root, ["galaxy_score"]))
    alt_rank = first_number(coin_data.get("alt_rank"), find_number_by_key(metrics, ["alt_rank"]), find_number_by_key(root, ["alt_rank"]))
    social_dominance = first_number(
        find_number_by_key(metrics, ["social_dominance"]),
        find_number_by_key(root, ["social_dominance"]),
    )
    return {
        "available": True,
        "project_summary": summary,
        "summary": summary,
        "why_moved": why_moved,
        "supportive_causes": supportive + alert_titles,
        "critical_causes": critical,
        "alerts": alert_titles,
        "top_posts": top_posts,
        "topics": topic_names,
        "influencers_count": len(influencers),
        "sector": first_text(*(topic_names[:2])) or nested_text(asset, "category") or nested_text(asset, "sector"),
        "social_growth": social_growth,
        "social_activity_24h": social_activity,
        "social_contributors_24h": contributors,
        "social_interactions_24h": interactions,
        "topic_trend": nested_text(topic_data, "trend"),
        "sentiment": sentiment,
        "galaxy_score": galaxy_score,
        "alt_rank": alt_rank,
        "social_dominance": social_dominance,
    }


def lunar_endpoint_data(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    endpoint = payload.get(key) if isinstance(payload, dict) else None
    if isinstance(endpoint, dict) and isinstance(endpoint.get("data"), dict):
        return endpoint["data"]
    return {}


def lunar_endpoint_list(payload: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    endpoint = payload.get(key) if isinstance(payload, dict) else None
    data = endpoint.get("data") if isinstance(endpoint, dict) else None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def safe_lunar_summary(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return None
    if len(value) < 30:
        return None
    return value


def lunar_topic_labels(topic_data: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    raw = list(topic_data.get("related_topics") or []) + list(topic_data.get("categories") or [])
    ignored = {"coins", "cryptocurrencies", "bitcoin", "ethereum", "usdt", "usdc", "binance", "bullish", "liquidity", "money"}
    for item in raw:
        if not isinstance(item, str):
            continue
        label = item.strip().lower()
        if label.startswith("$"):
            continue
        label = re.sub(r"^coins\\s+", "", label)
        label = re.sub(r"^stocks\\s+", "", label)
        if label in ignored or len(label) < 3:
            continue
        labels.append(label)
    return dedupe_texts(labels)[:10]


def lunar_why_moved(topic_data: Dict[str, Any], top_posts: List[str]) -> Optional[str]:
    fragments: List[str] = []
    trend = nested_text(topic_data, "trend")
    posts = first_number(topic_data.get("num_posts"))
    contributors = first_number(topic_data.get("num_contributors"))
    interactions = first_number(topic_data.get("interactions_24h"))
    if trend:
        fragments.append("тренд темы LunarCrush: %s" % trend)
    if posts is not None and contributors is not None:
        fragments.append("%.0f постов и %.0f авторов за 24ч" % (posts, contributors))
    elif posts is not None:
        fragments.append("%.0f постов за 24ч" % posts)
    if interactions is not None:
        fragments.append("%.0f взаимодействий за 24ч" % interactions)
    if top_posts:
        fragments.append("топ-посты: %s" % "; ".join(top_posts[:3]))
    return "LunarCrush: " + ", ".join(fragments) + "." if fragments else None


def lunar_suspicious_post_titles(post_titles: List[str]) -> List[str]:
    terms = ["pump", "pumped", "dump", "x3", "target", "сигнал", "signal", "shill", "airdrop", "unlock"]
    suspicious = []
    for title in post_titles:
        lowered = title.lower()
        matched = [term for term in terms if term in lowered]
        if matched:
            suspicious.append("Соцпост содержит риск-лексику: %s." % ", ".join(matched[:4]))
    return compact_texts(suspicious, 4)


def topic_sentiment_score(topic_data: Dict[str, Any]) -> Optional[float]:
    sentiment = topic_data.get("types_sentiment")
    if isinstance(sentiment, dict):
        numbers = [first_number(value) for value in sentiment.values()]
        numbers = [value for value in numbers if value is not None]
        if numbers:
            return sum(numbers) / len(numbers)
    return None


def trend_profile(
    categories: List[str],
    lunar: Dict[str, Any],
) -> Dict[str, str]:
    topic = first_text(*(lunar.get("topics") or []))
    category = categories[0] if categories else None
    label = category or topic or "нет данных"
    if category:
        source = "CoinGecko category"
    elif lunar.get("available") and topic:
        source = "LunarCrush social topic"
    else:
        source = "нет данных"
    social_growth = first_number(lunar.get("social_growth")) or 0.0
    if lunar.get("available") and (social_growth >= 70 or (lunar.get("galaxy_score") or 0) >= 70):
        strength = "высокая"
    elif social_growth >= 35:
        strength = "средняя"
    elif label != "нет данных":
        strength = "низкая"
    else:
        strength = "нет данных"

    return {
        "label": label,
        "source": source,
        "social_topic": topic,
        "social_topic_source": "LunarCrush" if lunar.get("available") and topic else "нет данных",
        "strength": strength,
        "why_moved": "Тренд найден через %s: %s." % (source, label) if source != "нет данных" else "",
    }


def taxonomy_source_conflict(
    sector: Optional[str],
    chain: Optional[str],
    social_topic: Optional[str],
) -> Optional[str]:
    if not social_topic:
        return None
    social_ecosystem = ecosystem_keyword(social_topic)
    if not social_ecosystem:
        return None
    project_text = " ".join([sector or "", chain or ""]).lower()
    if social_ecosystem in project_text:
        return None
    if chain or sector:
        return (
            "LunarCrush соцтема содержит %s, но CoinGecko указывает сектор/chain: %s%s. "
            "В карточке используем CoinGecko как источник идентичности проекта."
            % (social_ecosystem, sector or "нет сектора", " / " + chain if chain else "")
        )
    return None


def ecosystem_keyword(text: str) -> Optional[str]:
    lowered = text.lower()
    for keyword in ["solana", "starknet", "ethereum", "base", "arbitrum", "polygon", "bnb", "binance"]:
        if keyword in lowered:
            return keyword
    return None


def attention_phase_profile(lunar: Dict[str, Any]) -> Dict[str, Any]:
    if not lunar.get("available"):
        return {"phase": "нет данных", "reasons": []}
    velocity = social_velocity_score(lunar)
    freshness = hype_freshness_score(lunar)
    quality = social_quality_score(lunar)
    coordination = coordination_risk_score(lunar)
    reasons = [
        "скорость соцтемы: %s" % social_strength_level(velocity),
        "свежесть внимания: %s" % social_strength_level(freshness),
        "качество обсуждения: %s" % social_strength_level(quality),
    ]
    if coordination >= 65:
        reasons.append("риск координации: %s" % social_strength_level(coordination))
    if velocity >= 85 and freshness >= 75:
        return {"phase": "эйфория", "reasons": reasons}
    if velocity >= 70 and freshness >= 60:
        return {"phase": "массовое внимание", "reasons": reasons}
    if velocity >= 45 and freshness >= 45:
        return {"phase": "растущее внимание", "reasons": reasons}
    return {"phase": "слабый соцсигнал", "reasons": reasons}


def fundamental_red_flags(
    circ_ratio: Optional[float],
    fdv_to_market_cap: Optional[float],
    volume_to_market_cap: Optional[float],
    lunar: Dict[str, Any],
    unlock: Dict[str, Any],
) -> List[str]:
    flags: List[str] = []
    if circ_ratio is not None and circ_ratio < 0.30:
        flags.append("Циркуляция ниже 30%%: %.1f%% в обращении." % (circ_ratio * 100.0))
    if fdv_to_market_cap is not None and fdv_to_market_cap > 5:
        flags.append("FDV/MC выше 5: сильный риск размывания.")
    elif fdv_to_market_cap is not None and fdv_to_market_cap > 3:
        flags.append("FDV заметно выше market cap: %.2fx." % fdv_to_market_cap)
    if volume_to_market_cap is not None and volume_to_market_cap > 1.0:
        flags.append("Vol/MC выше 100%%: экстремальный интерес или спекулятивный перегрев.")
    if not lunar.get("available"):
        flags.append("LunarCrush social intelligence: нет данных.")
    if unlock.get("risk") == "warn":
        flags.append("Разлок/vesting упоминается в публичных данных: нужна ручная проверка близости события.")
    flags.extend(compact_texts(lunar.get("critical_causes") or [], 3))
    return dedupe_texts(flags)


def fundamental_supporting_factors(
    identity: Dict[str, Any],
    categories: List[str],
    links: Dict[str, Any],
    trend: Dict[str, str],
    lunar: Dict[str, Any],
) -> List[str]:
    factors: List[str] = []
    if categories:
        factors.append("Категория: %s." % categories[0])
    if trend.get("source") != "нет данных":
        factors.append("Тренд: %s через %s." % (trend.get("label"), trend.get("source")))
    factors.extend(compact_texts(lunar.get("supportive_causes") or [], 4))
    return dedupe_texts(factors)


def compact_ui_text(value: Any, limit: int) -> str:
    text = clean_text(str(value)) if value is not None else ""
    if not text:
        return "Данных пока нет."
    return text if len(text) <= limit else text[: max(limit - 1, 1)].rstrip() + "…"


def fallback_project_brief(project_summary: Optional[str]) -> str:
    if not project_summary:
        return "Данных пока нет."
    first_sentence = re.split(r"(?<=[.!?])\s+", project_summary.strip())[0]
    return compact_ui_text(first_sentence, 500)


def normalized_list(translated: Any, fallback: Any, max_items: int = 6) -> List[str]:
    source = translated if isinstance(translated, list) and translated else fallback
    if not isinstance(source, list):
        return []
    rows = []
    for item in source:
        text = clean_text(str(item))
        if text:
            rows.append(compact_ui_text(text, 180))
    return dedupe_texts(rows)[:max_items]


def score_level(value: float, kind: str) -> str:
    value = float(value or 0.0)
    if kind == "risk":
        if value >= 65:
            return "высокий"
        if value >= 35:
            return "средний"
        return "низкий"
    if kind == "narrative":
        if value >= 70:
            return "высокий"
        if value >= 40:
            return "средний"
        return "слабый"
    if value >= 70:
        return "высокое"
    if value >= 40:
        return "среднее"
    return "низкое"


def social_strength_level(value: Optional[float]) -> str:
    if value is None:
        return "нет данных"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "нет данных"
    if number >= 70:
        return "высокая"
    if number >= 40:
        return "средняя"
    return "низкая"


def social_theses(translated: Any, top_posts_ru: List[str], lunar: Dict[str, Any]) -> List[str]:
    candidates: List[Any] = []
    if isinstance(translated, list):
        candidates.extend(translated)
    if not candidates:
        candidates.extend(top_posts_ru or [])
    if not candidates:
        candidates.extend(lunar.get("top_posts") or [])
        candidates.extend(lunar.get("supportive_causes") or [])
        candidates.extend(lunar.get("critical_causes") or [])
        if lunar.get("summary"):
            candidates.append(lunar.get("summary"))
        if lunar.get("why_moved"):
            candidates.append(lunar.get("why_moved"))

    rows: List[str] = []
    for item in candidates:
        text = clean_text(str(item))
        if not text or is_market_alert_text(text):
            continue
        rows.append(compact_ui_text(text, 220))
        if len(rows) >= 4:
            break
    return dedupe_texts(rows)


def is_market_alert_text(text: str) -> bool:
    lowered = text.lower()
    market_terms = [
        "binance futures",
        "bybit spot",
        "bybit futures",
        "top-3",
        "топ-3",
        "top gainers",
        "top losers",
        "топ роста",
        "топ падения",
        "последние 15",
        "последние 60",
        "last 15",
        "last 60",
    ]
    return any(term in lowered for term in market_terms)


def movement_type_reasons_for(
    circ_ratio: Optional[float],
    volume_to_market_cap: Optional[float],
    trend: Dict[str, str],
    lunar: Dict[str, Any],
    tokenomics_risk: float,
) -> List[str]:
    reasons: List[str] = []
    if trend.get("source") != "нет данных":
        reasons.append(
            "Есть подтвержденный нарратив: %s, источник %s, сила %s."
            % (trend.get("label"), trend.get("source"), trend.get("strength"))
        )
    if lunar.get("available"):
        posts = lunar.get("social_activity_24h")
        contributors = lunar.get("social_contributors_24h")
        if posts is not None and contributors is not None:
            reasons.append("Соцактивность: %.0f постов и %.0f авторов за 24ч." % (posts, contributors))
    if circ_ratio is not None and circ_ratio < 0.30:
        reasons.append("Циркуляция ниже 30%%: низкий float усиливает риск резких пампов и коррекций.")
    if volume_to_market_cap is not None and volume_to_market_cap > 1.0:
        reasons.append("Vol/MC выше 100%%: объем больше капитализации, это признак перегрева или спекуляции.")
    if tokenomics_risk >= 65:
        reasons.append("Риск токеномики высокий: фундаментал нельзя считать чистым подтверждением движения.")
    return reasons[:6]


def project_quality_score(
    identity: Dict[str, Any],
    categories: List[str],
    links: Dict[str, Any],
    project_summary: Optional[str],
) -> float:
    score = min(float(identity.get("confidence") or 0.0) * 30.0, 30.0)
    if identity.get("name"):
        score += 15.0
    if project_summary and len(project_summary) >= 40:
        score += 25.0
    elif project_summary:
        score += 12.0
    if categories:
        score += 15.0
    homepage = links.get("homepage") if isinstance(links, dict) else None
    if isinstance(homepage, list) and any(homepage):
        score += 10.0
    if isinstance(links, dict) and (links.get("twitter_screen_name") or links.get("telegram_channel_identifier")):
        score += 5.0
    return min(score, 100.0)


def narrative_score(trend: Dict[str, str], lunar: Dict[str, Any]) -> float:
    strength_points = {"нет данных": 0.0, "низкая": 25.0, "средняя": 55.0, "высокая": 75.0}
    score = strength_points.get(trend.get("strength") or "нет данных", 0.0)
    if lunar.get("available"):
        score += 10.0
    if lunar.get("why_moved"):
        score += 10.0
    if lunar.get("supportive_causes"):
        score += min(len(lunar["supportive_causes"]) * 3.0, 10.0)
    if lunar.get("critical_causes"):
        score -= min(len(lunar["critical_causes"]) * 5.0, 15.0)
    return min(max(score, 0.0), 100.0)


def tokenomics_risk_score(
    circ_ratio: Optional[float],
    fdv_to_market_cap: Optional[float],
    volume_to_market_cap: Optional[float],
    unlock: Dict[str, Any],
) -> float:
    risk = 0.0
    if circ_ratio is not None and circ_ratio < 0.30:
        risk += 35.0
    elif circ_ratio is not None and circ_ratio < 0.50:
        risk += 15.0
    if fdv_to_market_cap is not None:
        risk += min(max(fdv_to_market_cap - 1.0, 0.0) * 8.0, 35.0)
    if volume_to_market_cap is not None and volume_to_market_cap > 1.0:
        risk += 25.0
    elif volume_to_market_cap is not None and volume_to_market_cap > 0.50:
        risk += 12.0
    if unlock.get("risk") == "warn":
        risk += 15.0
    return min(risk, 100.0)


def build_fundamental_thesis(
    project_brief: Optional[str],
    trend: Dict[str, str],
    supporting_factors: List[str],
    suspicious_factors: List[str],
) -> str:
    summary = project_brief or "Описание проекта ограничено."
    trend_text = "Нарратив: %s, источник: %s, сила: %s." % (
        trend.get("label") or "нет данных",
        trend.get("source") or "нет данных",
        trend.get("strength") or "нет данных",
    )
    support = "Усиливает движение: %s." % supporting_factors[0] if supporting_factors else "Подтверждающих факторов пока мало."
    suspicion = "Подозрительно: %s." % suspicious_factors[0] if suspicious_factors else "Критичных красных флагов в доступных данных нет."
    return " ".join([summary[:240], trend_text, support, suspicion]).strip()


def fundamental_components(metrics: Dict[str, Any]) -> Dict[str, float]:
    identity = min(float(metrics.get("identity_confidence") or 0.0) * 20.0, 20.0)
    market_cap = float(metrics.get("market_cap") or 0.0)
    volume_ratio = float(metrics.get("volume_to_market_cap") or 0.0)
    market_liquidity = min(8.0 if market_cap >= 50_000_000 else market_cap / 50_000_000 * 8.0, 8.0)
    market_liquidity += min(volume_ratio / 0.10 * 7.0, 7.0)
    circ_ratio = metrics.get("circulating_supply_ratio")
    fdv_ratio = metrics.get("fdv_to_market_cap")
    supply = 8.0 if circ_ratio is None else min(max(circ_ratio, 0.0) / 0.60 * 9.0, 9.0)
    supply += 6.0 if fdv_ratio is None else max(0.0, 6.0 - max(fdv_ratio - 1.0, 0.0) * 1.5)
    trend = min(float(metrics.get("narrative_score") or 0.0) / 100.0 * 15.0, 15.0)
    social = min(float(metrics.get("narrative_score") or 0.0) / 100.0 * 15.0, 15.0)
    public_risk = max(0.0, 10.0 - float(metrics.get("tokenomics_risk_score") or 0.0) / 10.0)
    peers = 5.0 + (5.0 if metrics.get("categories") else 0.0)
    return {
        "identity_data_quality": round(identity, 2),
        "market_cap_liquidity": round(min(market_liquidity, 15.0), 2),
        "fdv_supply_health": round(min(supply, 15.0), 2),
        "category_trend_strength": round(min(trend, 15.0), 2),
        "lunarcrush_social_quality": round(social, 2),
        "public_tokenomics_risk": round(public_risk, 2),
        "peer_comparison": round(peers, 2),
    }


def social_stage_payload(token_data: Optional[Dict[str, Any]], fallback_score: float) -> Dict[str, Any]:
    lunar = extract_lunarcrush_metrics((token_data or {}).get("lunarcrush") or {})
    if not lunar.get("available"):
        return {
            "status": "skipped",
            "score": None,
            "reason": "Соцфильтр пропущен: LunarCrush не настроен или не вернул usable data.",
            "metrics": {
                "social_label": "Недостаточно данных",
                "data_coverage": "none",
                "fallback_market_social_quality": fallback_score,
            },
        }

    velocity = social_velocity_score(lunar)
    quality = social_quality_score(lunar)
    freshness = hype_freshness_score(lunar)
    coordination = coordination_risk_score(lunar)
    score = round(velocity * 0.30 + quality * 0.30 + freshness * 0.25 + (100.0 - coordination) * 0.15, 2)
    if coordination >= 65:
        status = "warn"
        label = "Подозрительный соцшум"
    elif freshness >= 65 and velocity >= 55:
        status = "pass"
        label = "Живой соцсигнал"
    elif velocity >= 50:
        status = "warn"
        label = "Массовый хайп"
    else:
        status = "warn"
        label = "Слабый соцсигнал"
    reason = "Соцфильтр: %s. Источник LunarCrush; скорость %s, качество %s, риск координации %s." % (
        label,
        social_strength_level(velocity),
        social_strength_level(quality),
        social_strength_level(coordination),
    )
    return {
        "status": status,
        "score": score,
        "reason": reason,
        "metrics": {
            "social_label": label,
            "social_velocity_score": round(velocity, 2),
            "social_velocity_level": social_strength_level(velocity),
            "social_quality_score": round(quality, 2),
            "social_quality_level": social_strength_level(quality),
            "hype_freshness_score": round(freshness, 2),
            "hype_freshness_level": social_strength_level(freshness),
            "coordination_risk_score": round(coordination, 2),
            "coordination_risk_level": social_strength_level(coordination),
            "sentiment": lunar.get("sentiment"),
            "social_growth": lunar.get("social_growth"),
            "social_dominance": lunar.get("social_dominance"),
            "galaxy_score": lunar.get("galaxy_score"),
            "alt_rank": lunar.get("alt_rank"),
            "alerts": lunar.get("alerts"),
            "topics": lunar.get("topics"),
            "why_moved": lunar.get("why_moved"),
            "supportive_causes": lunar.get("supportive_causes"),
            "critical_causes": lunar.get("critical_causes"),
            "data_coverage": "lunarcrush",
        },
    }


def unlock_risk_profile(coin_data: Dict[str, Any], lunar: Dict[str, Any]) -> Dict[str, Any]:
    text_parts = [
        clean_text(nested_text(coin_data.get("description"), "en")),
        json.dumps(coin_data.get("links") or {}, ensure_ascii=False),
        " ".join(lunar.get("critical_causes") or []),
        " ".join(lunar.get("supportive_causes") or []),
        " ".join(lunar.get("alerts") or []),
    ]
    text = " ".join(part for part in text_parts if part).lower()
    terms = ["unlock", "vesting", "token unlock", "cliff", "emission", "разлок", "вестинг"]
    mentions = [term for term in terms if term in text]
    if mentions:
        return {
            "label": "есть публичные упоминания",
            "risk": "warn",
            "mentions": mentions,
            "relevance": "нужна ручная проверка близости события; полный schedule не строим",
        }
    return {
        "label": "нет данных по ближайшему разлоку",
        "risk": "unknown",
        "mentions": [],
        "relevance": "CoinGecko/LunarCrush не дали явного публичного unlock/vesting сигнала",
    }


def data_coverage_label(coingecko: Dict[str, Any], lunar: Dict[str, Any]) -> str:
    has_coingecko = bool(coingecko.get("coin_data") or coingecko.get("market"))
    has_lunar = bool(lunar.get("available"))
    if has_coingecko and has_lunar:
        return "coingecko+lunarcrush"
    if has_coingecko:
        return "coingecko_only"
    if has_lunar:
        return "lunarcrush_only"
    return "none"


def social_velocity_score(lunar: Dict[str, Any]) -> float:
    growth = normalized_ratio(lunar.get("social_growth"))
    if growth is None:
        activity = first_number(lunar.get("social_activity_24h")) or 0.0
        contributors = first_number(lunar.get("social_contributors_24h")) or 0.0
        if str(lunar.get("topic_trend") or "").lower() == "up":
            return min(45.0 + activity / 40.0 + contributors / 12.0, 85.0)
        if activity or contributors:
            return min(25.0 + activity / 60.0 + contributors / 20.0, 65.0)
        return 35.0 if lunar.get("alerts") else 20.0
    return min(max(growth * 100.0, 0.0), 100.0)


def social_quality_score(lunar: Dict[str, Any]) -> float:
    score = 35.0
    galaxy = normalized_ratio(lunar.get("galaxy_score"))
    sentiment = normalized_ratio(lunar.get("sentiment"))
    if galaxy is not None:
        score += galaxy * 35.0
    if sentiment is not None:
        score += min(max(sentiment * 20.0, 0.0), 20.0)
    if lunar.get("influencers_count"):
        score += min(float(lunar.get("influencers_count") or 0.0) * 2.0, 10.0)
    if lunar.get("summary"):
        score += 10.0
    return min(score, 100.0)


def hype_freshness_score(lunar: Dict[str, Any]) -> float:
    score = 30.0
    if lunar.get("why_moved"):
        score += 25.0
    if lunar.get("alerts"):
        score += 20.0
    if lunar.get("topics"):
        score += 15.0
    if str(lunar.get("topic_trend") or "").lower() == "up":
        score += 10.0
    if lunar.get("critical_causes"):
        score -= min(len(lunar.get("critical_causes") or []) * 8.0, 25.0)
    return min(max(score, 0.0), 100.0)


def coordination_risk_score(lunar: Dict[str, Any]) -> float:
    text = " ".join((lunar.get("critical_causes") or []) + (lunar.get("alerts") or [])).lower()
    risk = 0.0
    for term in ["spam", "bot", "coordinated", "pump", "paid", "shill", "manipulation", "fake"]:
        if term in text:
            risk += 18.0
    dominance = normalized_ratio(lunar.get("social_dominance"))
    if dominance is not None and dominance > 0.08:
        risk += min(dominance * 220.0, 30.0)
    return min(risk, 100.0)


def clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def first_text(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def first_link(value: Any) -> Optional[str]:
    if isinstance(value, list):
        return first_text(*value)
    return first_text(value)


def nested_text(payload: Any, key: str) -> Optional[str]:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def compact_texts(values: List[Any], limit: int) -> List[str]:
    result: List[str] = []
    for value in values:
        text = clean_text(value) if isinstance(value, str) else None
        if text:
            result.append(text[:220])
        if len(result) >= limit:
            break
    return dedupe_texts(result)


def dedupe_texts(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        normalized = value.strip()
        key = normalized.lower()
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return result


def find_first_dict(payload: Any, keys: List[str]) -> Dict[str, Any]:
    found: List[Dict[str, Any]] = []

    def walk(value: Any) -> None:
        if found:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, dict):
                    found.append(item)
                    return
                walk(item)
        elif isinstance(value, list):
            for item in value[:50]:
                walk(item)

    walk(payload)
    return found[0] if found else {}


def find_first_list(payload: Any, keys: List[str]) -> List[Any]:
    found: List[List[Any]] = []

    def walk(value: Any) -> None:
        if found:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, list):
                    found.append(item)
                    return
                walk(item)
        elif isinstance(value, list):
            for item in value[:50]:
                walk(item)

    walk(payload)
    return found[0] if found else []


def extract_cause_list(payload: Any, keys: List[str]) -> List[str]:
    values: List[str] = []
    if not isinstance(payload, dict):
        return values
    for key in keys:
        item = payload.get(key)
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, dict):
            values.extend(
                compact_texts(
                    [
                        nested_text(item, "title"),
                        nested_text(item, "description"),
                        nested_text(item, "summary"),
                        nested_text(item, "reason"),
                    ],
                    4,
                )
            )
        elif isinstance(item, list):
            for entry in item[:5]:
                if isinstance(entry, str):
                    values.append(entry)
                elif isinstance(entry, dict):
                    values.extend(
                        compact_texts(
                            [
                                nested_text(entry, "title"),
                                nested_text(entry, "description"),
                                nested_text(entry, "summary"),
                                nested_text(entry, "reason"),
                            ],
                            4,
                        )
                    )
    return dedupe_texts(compact_texts(values, 8))


def first_market_row(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, list) and payload:
        return payload[0] if isinstance(payload[0], dict) else {}
    if isinstance(payload, dict):
        rows = payload.get("coins") or payload.get("data")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    return {}


def nested_number(payload: Any, key: str) -> Optional[float]:
    if isinstance(payload, dict):
        return first_number(payload.get(key))
    return None


def first_number(*values: Any) -> Optional[float]:
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def normalized_ratio(value: Any) -> Optional[float]:
    number = first_number(value)
    if number is None:
        return None
    return number / 100.0 if number > 1.0 else number


def find_number_by_key(payload: Any, key_terms: List[str]) -> Optional[float]:
    found: List[float] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if any(term in key.lower() for term in key_terms):
                    number = first_number(item)
                    if number is not None:
                        found.append(number)
                walk(item)
        elif isinstance(value, list):
            for item in value[:50]:
                walk(item)

    walk(payload)
    return found[0] if found else None


def find_text_by_key(payload: Any, key_terms: List[str]) -> Optional[str]:
    found: List[str] = []

    def walk(value: Any) -> None:
        if found:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if any(term in key.lower() for term in key_terms):
                    if isinstance(item, str):
                        text = clean_text(item)
                        if text:
                            found.append(text)
                            return
                    if isinstance(item, list):
                        text = clean_text(", ".join(str(entry) for entry in item[:8]))
                        if text:
                            found.append(text)
                            return
                walk(item)
        elif isinstance(value, list):
            for item in value[:50]:
                walk(item)

    walk(payload)
    return found[0] if found else None
