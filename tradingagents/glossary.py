"""Plain meanings for the market terms a summary may use.

Written once, so a term means the same on every report, and found in the text by code, so the
model is never asked to explain a term (and can never explain it wrongly). The page shows the
meanings as tooltips or a list.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class Entry(NamedTuple):
    term: str
    meaning: str
    pattern: re.Pattern[str]


def _e(term: str, meaning: str, pattern: str) -> Entry:
    return Entry(term, meaning, re.compile(pattern, re.IGNORECASE))


GLOSSARY: tuple[Entry, ...] = (
    _e("RSI", "A 0 to 100 gauge of how fast the price has moved lately. Low readings mean heavy recent selling.",
       r"\bRSI\b|relative strength index"),
    _e("Moving average", "The average price over the last number of days shown. Traders watch whether the price is above or below it.",
       r"\b(?:\d+[- ]day )?(?:simple |exponential )?moving averages?\b|\b\d+-day averages?\b|\b(?:SMA|EMA)\b"),
    _e("MACD", "A trend gauge that compares two moving averages. A rising MACD suggests momentum is building.",
       r"\bMACD\b"),
    _e("ATR", "The average distance the price moves in a day. A higher number means a bumpier stock.",
       r"\bATR\b|average true range"),
    _e("Bollinger Bands", "Lines drawn around the price to show its usual range. A move outside them is unusual.",
       r"bollinger bands?"),
    _e("Support and resistance", "Prices where a stock has often stopped falling (support) or stopped rising (resistance).",
       r"\bsupport (?:level|zone)s?\b|\bresistance\b"),
    _e("Overbought and oversold", "Overbought means the price rose fast and may be due a pause. Oversold means it fell fast and may bounce.",
       r"\boverbought\b|\boversold\b"),
    _e("52-week range", "The highest and lowest prices of the last year.", r"\b52-week (?:high|low|range)\b"),
    _e("Drawdown", "How far the price has fallen from its recent high.", r"\bdrawdowns?\b"),
    _e("Market cap", "What the whole company is worth on the stock market: the share price times the number of shares.",
       r"\bmarket cap(?:italization)?\b"),
    _e("Enterprise value", "A company's market cap plus its debt minus its cash: the price of buying the whole business.",
       r"\benterprise value\b"),
    _e("EV/EBITDA", "Enterprise value divided by operating profit before some costs. A lower number means cheaper.",
       r"\bEV/EBITDA\b"),
    _e("EBITDA", "Profit from running the business, before interest, taxes and some accounting costs.",
       r"(?<!/)\bEBITDA\b"),
    _e("P/E", "The share price divided by a year of earnings per share. A high number means investors pay a lot for each dollar of profit.",
       r"\bP/E\b|price[- ]to[- ]earnings"),
    _e("PEG", "The P/E divided by the earnings growth rate. Below 1 can mean the growth is cheap.", r"\bPEG\b"),
    _e("EPS", "Earnings per share: the company's profit divided by its number of shares.",
       r"\bEPS\b|earnings per share"),
    _e("Trailing twelve months", "The most recent four quarters added together.",
       r"trailing[- ]twelve[- ]months?|\bTTM\b"),
    _e("Free cash flow", "The cash left after paying to run and build the business. It is what can be saved, paid out or invested.",
       r"free cash flow|\bFCF\b"),
    _e("Operating cash flow", "The cash the business brings in from its normal operations.", r"operating cash flow"),
    _e("Capital spending", "Money spent on equipment, factories and buildings. Also called capex.",
       r"\bcapital (?:spending|expenditures?)\b|\bcapex\b"),
    _e("Gross margin", "The share of each sales dollar left after the direct cost of making the product.",
       r"\bgross margins?\b"),
    _e("Operating margin", "The share of each sales dollar left as profit after running the business.",
       r"\boperating margins?\b"),
    _e("Net margin", "The share of each sales dollar left as profit after every cost, including tax.",
       r"\bnet margins?\b"),
    _e("Debt to equity", "How much a company owes compared with what its owners have put in. Higher means more borrowing.",
       r"\bdebt[- ]to[- ]equity\b"),
    _e("Liabilities to equity", "Everything a company owes, compared with what its owners have put in.",
       r"\bliabilities[- ]to[- ]equity\b"),
    _e("Working capital", "The cash and near-cash a company has, minus the bills due soon. It shows short-term breathing room.",
       r"\bworking capital\b"),
    _e("Cash burn", "How fast a company is spending more cash than it brings in.", r"\bcash burn\b"),
    _e("Runway", "How long a company's cash lasts at its current burn rate.", r"\brunway\b"),
    _e("Dilution", "When a company issues new shares, each existing share becomes a smaller slice of the company.",
       r"\bdilut(?:ion|ive|ed|e)\b"),
    _e("Growth from the previous quarter", "How much sales grew compared with the quarter just before. Also called sequential growth.",
       r"\bsequential(?:ly)? growth\b|quarter[- ]over[- ]quarter|\bQoQ\b"),
    _e("Year over year", "A comparison with the same period a year earlier.", r"\byear[- ]over[- ]year\b|\bYoY\b"),
    _e("Percentile", "Where today's number ranks against its own history. The 90th percentile means higher than 90% of past readings.",
       r"\bpercentile\b"),
    _e("13F", "A quarterly filing where big funds list what they own. It is filed up to 45 days after the quarter ends.",
       r"\b13F\b"),
    _e("Form 4", "The filing a company's insiders make when they buy or sell its stock.", r"\bForm 4\b"),
    _e("VIX", "A measure of how much volatility the market expects. Higher means more fear.", r"\bVIX\b"),
    _e("Fear & Greed", "A 0 to 100 reading of market mood. Low means fear, high means greed.",
       r"fear (?:&|and) greed"),
    _e("Credit spread", "The extra interest risky companies pay over safe borrowers. It widens when investors get nervous.",
       r"\bcredit spreads?\b"),
    _e("Basis points", "Hundredths of a percent. 100 basis points is 1%.", r"\bbasis points?\b|\bbps\b"),
)


def glossary_for(text: str) -> list[dict[str, str]]:
    """The defined terms that appear in ``text``, in the order they first appear."""
    found = []
    for entry in GLOSSARY:
        match = entry.pattern.search(text)
        if match:
            found.append((match.start(), entry))
    return [{"term": e.term, "meaning": e.meaning} for _, e in sorted(found, key=lambda pair: pair[0])]
