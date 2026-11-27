"""Narrative mention sources (read-only, keyless).

* Hacker News (Algolia API): newest stories; author and engagement (points + comments).
* GDELT DOC 2.0: global news articles; the publishing domain acts as the "author".
  GDELT asks for at most one request per 5 s — enforced with a 6 s spacing.
* Token metadata: DexScreener profile descriptions and pool/token names (crypto side).

X/Twitter, Reddit and Telegram need the owner's credentials/approval and are not wired in.
A failing source yields an error string, never an empty "no mentions" result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from atomtrader.marketdata.http import ApiError, JsonHttpClient

# Prior credibility weights per source type (0..1). Engagement adjusts within a source.
CREDIBILITY = {"gdelt": 0.8, "hn": 0.7, "token_profile": 0.2, "token_name": 0.1}


@dataclass(frozen=True, slots=True)
class Mention:
    source: str
    text: str
    author: str  # user, domain or token address — used for unique-author counts
    at: datetime
    engagement: float = 0.0
    url: str = ""

    @property
    def credibility(self) -> float:
        return CREDIBILITY.get(self.source, 0.3)


def parse_hn(raw: dict[str, Any]) -> list[Mention]:
    out = []
    for h in raw.get("hits") or []:
        title = h.get("title") or h.get("story_title")
        if not title or not h.get("created_at_i"):
            continue
        out.append(Mention("hn", title, h.get("author") or "?", datetime.fromtimestamp(int(h["created_at_i"]), UTC),
                           float((h.get("points") or 0) + (h.get("num_comments") or 0)), h.get("url") or ""))
    return out


def parse_gdelt(raw: dict[str, Any]) -> list[Mention]:
    out = []
    for a in raw.get("articles") or []:
        seen = a.get("seendate")
        if not a.get("title") or not seen:
            continue
        at = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        out.append(Mention("gdelt", a["title"], a.get("domain") or "?", at, 0.0, a.get("url") or ""))
    return out


class NewsSources:
    def __init__(self, hn: JsonHttpClient | None = None, gdelt: JsonHttpClient | None = None) -> None:
        self.hn = hn or JsonHttpClient("https://hn.algolia.com/api/v1", per_minute=30, max_retries=1)
        self.gdelt = gdelt or JsonHttpClient("https://api.gdeltproject.org/api/v2", per_minute=10, max_retries=0)

    async def aclose(self) -> None:
        await self.hn.aclose()
        await self.gdelt.aclose()

    async def hn_latest(self, since: datetime | None = None, pages: int = 2) -> tuple[list[Mention], str | None]:
        out: list[Mention] = []
        try:
            for page in range(pages):
                params: dict[str, Any] = {"tags": "story", "hitsPerPage": 100, "page": page}
                if since is not None:
                    params["numericFilters"] = f"created_at_i>{int(since.timestamp())}"
                batch = parse_hn(await self.hn.get_json("/search_by_date", params))
                out += batch
                if len(batch) < 100:
                    break
        except ApiError as err:
            return out, f"hn: {err.kind.value}"
        return out, None

    async def gdelt_articles(self, query: str, timespan: str = "6h", max_records: int = 75) -> tuple[list[Mention], str | None]:
        try:
            raw = await self.gdelt.get_json("/doc/doc", {"query": query, "mode": "artlist", "format": "json",
                                                         "maxrecords": max_records, "timespan": timespan})
        except ApiError as err:
            return [], f"gdelt: {err.kind.value}"
        return parse_gdelt(raw if isinstance(raw, dict) else {}), None
