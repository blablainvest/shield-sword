from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class BlacklistHit:
    term: str
    category: str
    severity: str
    note: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "term": self.term,
            "category": self.category,
            "severity": self.severity,
            "note": self.note,
        }


class BlacklistConfig:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or "config/blacklists.yaml")
        self.entries = self._load_entries()

    def scan_text(self, text: str) -> List[BlacklistHit]:
        haystack = text.lower()
        hits: List[BlacklistHit] = []
        for entry in self.entries:
            term = entry["term"]
            if term.lower() in haystack:
                hits.append(
                    BlacklistHit(
                        term=term,
                        category=entry.get("category", "unknown"),
                        severity=entry.get("severity", "warn"),
                        note=entry.get("note", ""),
                    )
                )
        return hits

    def to_dict(self) -> Dict[str, object]:
        return {"path": str(self.path), "entries": self.entries}

    def _load_entries(self) -> List[Dict[str, str]]:
        if not self.path.exists():
            return DEFAULT_BLACKLISTS
        entries: List[Dict[str, str]] = []
        current: Dict[str, str] = {}
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line == "entries:":
                continue
            if line.startswith("- "):
                if current:
                    entries.append(current)
                current = {}
                line = line[2:].strip()
                if ":" in line:
                    key, value = line.split(":", 1)
                    current[key.strip()] = _clean(value)
            elif ":" in line and current:
                key, value = line.split(":", 1)
                current[key.strip()] = _clean(value)
        if current:
            entries.append(current)
        return [entry for entry in entries if "term" in entry] or DEFAULT_BLACKLISTS


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        return value[1:-1]
    return value


DEFAULT_BLACKLISTS = [
    {
        "term": "DWF Labs",
        "category": "market_maker",
        "severity": "warn",
        "note": "Known market maker mention; raise manipulation/social risk and require stronger confirmation.",
    },
    {
        "term": "DWF",
        "category": "market_maker",
        "severity": "warn",
        "note": "Short form mention of DWF Labs.",
    },
    {
        "term": "pump group",
        "category": "coordinated_activity",
        "severity": "fail",
        "note": "Explicit coordinated pump language.",
    },
    {
        "term": "signal group",
        "category": "coordinated_activity",
        "severity": "warn",
        "note": "Potential low-quality social source.",
    },
    {
        "term": "paid call",
        "category": "suspicious_kol",
        "severity": "warn",
        "note": "Influencer-style promotion language; check timing versus price move.",
    },
    {
        "term": "listing soon",
        "category": "venue_risk",
        "severity": "warn",
        "note": "Unconfirmed listing rumor; require primary source confirmation.",
    },
    {
        "term": "unlock",
        "category": "tokenomics_risk",
        "severity": "warn",
        "note": "Potential supply pressure; check unlock size and date.",
    },
    {
        "term": "AI agent",
        "category": "narrative_risk",
        "severity": "warn",
        "note": "Crowded narrative term; require freshness and real catalyst.",
    },
]
