"""Warnings when a summary is hard to read: long sentences and walls of text.

Warnings only. They are returned with the summary so we can see how often they fire; they
do not trigger a retry, which would cost a model call.
"""

from __future__ import annotations

import re

from tradingagents.summary_check import SummaryProblem

MAX_AVG_SENTENCE_WORDS = 22
MAX_PARAGRAPH_SENTENCES = 5
MAX_PARAGRAPH_WORDS = 90

_MARKUP = re.compile(r"[*_#>`]|^\s*[-+]\s+", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _sentences(paragraph: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(" ".join(paragraph.split())) if s.strip()]


def readability_problems(text: str) -> list[SummaryProblem]:
    paragraphs = [_MARKUP.sub("", p).strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    sentences = [s for p in paragraphs for s in _sentences(p)]
    found = []
    if sentences:
        average = sum(len(s.split()) for s in sentences) / len(sentences)
        if average > MAX_AVG_SENTENCE_WORDS:
            found.append(SummaryProblem(
                "long_sentences", "summary",
                f"sentences average {average:.0f} words; aim for no more than {MAX_AVG_SENTENCE_WORDS}", "warning"))
    if any(len(_sentences(p)) > MAX_PARAGRAPH_SENTENCES or len(p.split()) > MAX_PARAGRAPH_WORDS for p in paragraphs):
        found.append(SummaryProblem("long_paragraph", "summary",
                                    "a paragraph is too long; split it into two or three sentences each", "warning"))
    return found
