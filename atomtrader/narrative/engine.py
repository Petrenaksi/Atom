"""Narrative Intelligence Engine.

Answers, per narrative (a term or bigram appearing in real-world sources):
  1. what is happening        -> the term + representative headlines
  2. why it attracts attention-> engagement, sources, category (classifier)
  3. rising or falling        -> acceleration recent window vs previous window
  4. emerging or saturated    -> status from age, acceleration and volume
  5. connected tokens         -> tokens whose name/symbol/description mention the term
  6. authentic connection?    -> link grade: description+links > description > name only;
                                 copycat clusters flagged as opportunistic
  7. short-term relevance     -> recency-weighted score, decays as acceleration fades

Organic vs manufactured attention: unique authors, single-author concentration, the share
of linked tokens paying for DexScreener boosts, and bursts of same-named token launches.
Social metrics that are unavailable (X, Reddit, Telegram) are listed in ``data_gaps`` and
never estimated.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from atomtrader.core.models import TokenRef
from atomtrader.narrative.sources import Mention
from atomtrader.narrative.text import contains_term, terms

UNAVAILABLE_SOURCES = ("x_twitter", "reddit", "telegram", "google_trends")


@dataclass(frozen=True)
class TokenMeta:
    token: TokenRef
    name: str
    description: str = ""
    links: tuple[str, ...] = ()
    created_at: datetime | None = None
    boosted: bool = False


@dataclass(frozen=True)
class TokenLink:
    token: TokenRef
    grade: str  # "description_and_links" | "description" | "name_only"
    strength: float  # 0..1


@dataclass(frozen=True)
class Narrative:
    term: str
    status: str  # emerging | growing | saturated | fading | quiet
    mentions_recent: int
    mentions_previous: int
    acceleration: float
    unique_authors: int
    top_author_share: float
    engagement_recent: float
    engagement_growth: float
    credibility: float
    first_seen: datetime
    age_hours: float
    strength: float  # 0..1 overall attention strength used by scoring
    category: str = "uncategorised"
    headlines: tuple[str, ...] = ()
    tokens: tuple[TokenLink, ...] = ()
    flags: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = UNAVAILABLE_SOURCES


@dataclass(frozen=True)
class NarrativeConfig:
    window: timedelta = timedelta(hours=3)
    min_mentions: int = 3
    min_unique_authors: int = 3
    emerging_max_age: timedelta = timedelta(hours=12)
    copycat_launches: int = 5  # >= this many tokens named after the term within 24 h
    max_narratives: int = 40


KEYWORDS = {
    "ai": {"ai", "agent", "agents", "agentic", "llm", "gpt", "openai", "anthropic", "claude", "gemini", "model", "robot"},
    "politics": {"election", "president", "trump", "senate", "congress", "vote", "minister", "tariff", "war", "sanctions"},
    "sports": {"cup", "league", "final", "championship", "nba", "nfl", "fifa", "olympics", "match", "goal"},
    "celebrity": {"musk", "elon", "kanye", "taylor", "swift", "drake", "celebrity", "influencer"},
    "technology": {"apple", "google", "chip", "nvidia", "quantum", "launch", "spacex", "iphone", "release"},
    "animals_memes": {"dog", "cat", "frog", "pepe", "penguin", "hamster", "monkey", "viral", "meme"},
}


def keyword_category(term: str, headlines: Iterable[str]) -> str:
    words = set(term.split()) | {w for h in headlines for w in terms(h, bigrams=False)}
    best, hits = "uncategorised", 0
    for cat, kws in KEYWORDS.items():
        n = len(words & kws)
        if n > hits:
            best, hits = cat, n
    return best


def _term_stats(mentions: Sequence[Mention]) -> dict[str, list[Mention]]:
    by: dict[str, list[Mention]] = defaultdict(list)
    for m in mentions:
        for t in set(terms(m.text)):
            by[t].append(m)
    return by


def link_tokens(term: str, tokens: Sequence[TokenMeta], now: datetime, copycat_threshold: int) -> tuple[list[TokenLink], list[str]]:
    links: list[TokenLink] = []
    flags: list[str] = []
    named_recently = 0
    for t in tokens:
        in_name = contains_term(f"{t.name} {t.token.symbol}", term)
        in_desc = bool(t.description) and contains_term(t.description, term)
        if not (in_name or in_desc):
            continue
        if in_desc and t.links:
            links.append(TokenLink(t.token, "description_and_links", 0.8))
        elif in_desc:
            links.append(TokenLink(t.token, "description", 0.5))
        else:
            links.append(TokenLink(t.token, "name_only", 0.2))
        if in_name and t.created_at and now - t.created_at <= timedelta(hours=24):
            named_recently += 1
    if named_recently >= copycat_threshold:
        flags.append(f"copycat_cluster:{named_recently}_tokens_named_after_term_in_24h")
    linked = [t for t in tokens if any(link.token == t.token for link in links)]
    if linked:
        boosted_share = sum(t.boosted for t in linked) / len(linked)
        if boosted_share >= 0.5:
            flags.append(f"paid_promotion:{boosted_share:.0%}_of_linked_tokens_boosted")
    return links, flags


class NarrativeEngine:
    def __init__(self, config: NarrativeConfig | None = None, classifier: object | None = None) -> None:
        self.cfg = config or NarrativeConfig()
        self.classifier = classifier  # optional LLM classifier (advisory only)

    def analyse(self, mentions: Sequence[Mention], tokens: Sequence[TokenMeta], now: datetime,
                observed_since: datetime | None = None) -> list[Narrative]:
        """``observed_since``: when collection started. If the previous window was not fully
        observed, acceleration is unknown and narratives are marked ``insufficient_history``
        instead of looking like they rose from zero."""
        w = self.cfg.window
        if observed_since is None:
            observed_since = min((m.at for m in mentions), default=now)
        history_ok = observed_since <= now - 2 * w
        world = [m for m in mentions if m.source in ("hn", "gdelt") and now - m.at <= 2 * w]
        recent_all = _term_stats([m for m in world if now - m.at <= w])
        prev_all = _term_stats([m for m in world if w < now - m.at <= 2 * w])
        first_seen: dict[str, datetime] = {}
        for m in sorted(mentions, key=lambda m: m.at):
            for t in set(terms(m.text)):
                first_seen.setdefault(t, m.at)

        out: list[Narrative] = []
        for term in set(recent_all) | set(prev_all):
            recent = recent_all.get(term, [])
            prev = prev_all.get(term, [])
            # Qualifies if either window shows broad attention (so fading narratives stay visible).
            if not any(len(ms) >= self.cfg.min_mentions and len({m.author for m in ms}) >= self.cfg.min_unique_authors
                       for ms in (recent, prev)):
                continue
            if not recent:
                continue  # gone entirely: nothing current to trade on
            authors = Counter(m.author for m in recent)
            accel = (len(recent) + 1) / (len(prev) + 1) if history_ok else 1.0
            eng_r = sum(m.engagement for m in recent)
            eng_p = sum(m.engagement for m in prev)
            top_share = authors.most_common(1)[0][1] / len(recent)
            cred = sum(m.credibility for m in recent) / len(recent)
            fs = first_seen.get(term, recent[0].at)
            age_h = (now - fs).total_seconds() / 3600
            if not history_ok:
                status = "insufficient_history"
            elif accel >= 2 and now - fs <= self.cfg.emerging_max_age:
                status = "emerging"
            elif accel >= 1.3:
                status = "growing"
            elif accel <= 0.7:
                status = "fading"
            elif len(recent) >= 10 * self.cfg.min_mentions:
                status = "saturated"
            else:
                status = "steady"
            # Strength: breadth (unique authors), acceleration and credibility; dampened when
            # one author dominates (possible coordinated promotion).
            breadth = min(1.0, len(authors) / 15)
            momentum = min(1.0, max(0.0, (accel - 1) / 3))
            strength = (0.45 * breadth + 0.35 * momentum + 0.2 * cred) * (1 - max(0.0, top_share - 0.4))
            if status == "fading":
                strength *= 0.5
            links, flags = link_tokens(term, tokens, now, self.cfg.copycat_launches)
            if top_share > 0.5:
                flags.append(f"single_author_dominates:{top_share:.0%}")
            if not history_ok:
                flags.append("previous_window_not_observed")
            heads = tuple(dict.fromkeys(m.text for m in sorted(recent, key=lambda m: -m.engagement)))[:5]
            out.append(Narrative(
                term=term, status=status, mentions_recent=len(recent), mentions_previous=len(prev),
                acceleration=round(accel, 2), unique_authors=len(authors), top_author_share=round(top_share, 2),
                engagement_recent=eng_r, engagement_growth=round((eng_r + 1) / (eng_p + 1), 2),
                credibility=round(cred, 2), first_seen=fs, age_hours=round(age_h, 1), strength=round(strength, 3),
                category=keyword_category(term, heads), headlines=heads, tokens=tuple(links), flags=tuple(flags),
            ))
        # Prefer specific narratives: drop a unigram when a bigram containing it covers
        # (nearly) the same mentions — "penguin robot" instead of "penguin" and "robot".
        bigrams = [n for n in out if " " in n.term]
        kept = [n for n in out if " " in n.term or not any(
            n.term in b.term.split() and b.mentions_recent >= 0.9 * n.mentions_recent for b in bigrams)]
        kept.sort(key=lambda n: (-n.strength, -n.mentions_recent, n.term))
        return kept[: self.cfg.max_narratives]

    @staticmethod
    def token_strength(token: TokenRef, narratives: Sequence[Narrative]) -> tuple[float | None, Narrative | None]:
        """Best (narrative strength x link strength) for a token, or (None, None) if unlinked."""
        best: tuple[float, Narrative] | None = None
        for n in narratives:
            for link in n.tokens:
                if link.token == token:
                    s = n.strength * link.strength
                    if any(f.startswith("copycat_cluster") for f in n.flags):
                        s *= 0.5
                    if best is None or s > best[0]:
                        best = (s, n)
        return (round(best[0], 3), best[1]) if best else (None, None)


@dataclass
class MentionBuffer:
    """Keeps de-duplicated mentions across polling cycles (by source+url/text)."""

    horizon: timedelta = timedelta(hours=24)
    items: dict[str, Mention] = field(default_factory=dict)

    def add(self, mentions: Iterable[Mention], now: datetime) -> int:
        n = 0
        for m in mentions:
            key = f"{m.source}|{m.url or m.text}"
            if key not in self.items:
                self.items[key] = m
                n += 1
        for k in [k for k, m in self.items.items() if now - m.at > self.horizon]:
            del self.items[k]
        return n

    def all(self) -> list[Mention]:
        return sorted(self.items.values(), key=lambda m: m.at)
