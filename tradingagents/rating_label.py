"""The five internal ratings in plain words for retail readers.

The model rates on the institutional scale (Buy, Overweight, Hold, Underweight,
Sell), which the checks, the memory log and the plan rules all use. What a
customer reads is this fixed table, not model wording, so it is the same on every
report. Each meaning says what to do whether or not you already own the stock.
"""

from __future__ import annotations

from typing import NamedTuple


class PlainRating(NamedTuple):
    label: str
    meaning: str


_LABELS = {
    "buy": PlainRating("Strong Buy", "Open a position or add to the one you have."),
    "overweight": PlainRating("Weak Buy", "Build a position gradually, in stages, rather than all at once."),
    "hold": PlainRating("Hold", "Keep what you have. Don't add and don't sell."),
    "underweight": PlainRating(
        "Weak Sell", "If you own it, sell some of it. If you don't, don't buy it yet."),
    "sell": PlainRating("Strong Sell", "If you own it, sell it. If you don't, stay away."),
}


def plain_rating(rating: str | None) -> PlainRating | None:
    """The plain label and meaning for a rating, or None when it is not one of the five."""
    return _LABELS.get((rating or "").strip().lower())
