"""Minimal, deterministic text normalisation for narrative terms."""

from __future__ import annotations

import re

STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren as at be because been before being below
between both but by can cannot could did do does doing down during each few for from further had has have
having he her here hers herself him himself his how i if in into is it its itself just let me more most my
myself new no nor not now of off on once only or other our ours ourselves out over own same she should so some
such than that the their theirs them themselves then there these they this those through to too under until up
very was we were what when where which while who whom why will with would you your yours yourself yourselves
show ask tell says said say via get got one two three first last year years day days week weeks month
time way make made use using used like just still even many much may might must need want back
today yesterday tomorrow people thing things don doesn didn isn aren wasn weren won wouldn shouldn couldn
hn com www http https org net html amp vs per pdf video watch read open free best top big small good bad
token tokens coin coins crypto meme memecoin launch launched community official solana sol eth base bnb pump fun
""".split())

_WORD = re.compile(r"\$?[a-z][a-z0-9]{2,24}")


def terms(text: str, bigrams: bool = True) -> list[str]:
    """Lowercased content words (+ adjacent bigrams). ``$TICKER`` keeps its marker stripped."""
    words = [w.lstrip("$") for w in _WORD.findall(text.lower())]
    words = [w for w in words if w not in STOPWORDS and not w.isdigit()]
    out = list(words)
    if bigrams:
        out += [f"{a} {b}" for a, b in zip(words, words[1:]) if a != b]
    return out


def contains_term(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text.lower()) is not None
