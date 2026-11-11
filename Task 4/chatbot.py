#!/usr/bin/env python3
"""
Terminal FAQ Support Assistant
==============================

A terminal-based support assistant that answers questions from an FAQ file.

Features:
* Loads questions and answers from a JSON file (default: sample_faq.json).
* Normalizes user input (handles spelling typos, common synonyms, contractions).
* Matches questions using weighted keyword scoring and handles multi-part questions.
* Clarifies ambiguous questions ("did you mean...?").
* Checks business hours dynamically based on FAQ schedule info.
* Manages conversation flow and exits cleanly on bye / exit / quit / Ctrl-C.

Usage:
  python chatbot.py                      # chat using sample_faq.json
  python chatbot.py --faq my_faq.json    # use custom FAQ file
  python chatbot.py --debug              # show intent and confidence details
  python chatbot.py --demo               # run scripted demo conversation
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import shutil
import sys
import textwrap
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_FAQ = BASE_DIR / "sample_faq.json"
DEFAULT_NAME = "Support Assistant"

MAX_INPUT_CHARS = 500   # longer messages are cut (protects the matcher and the screen)
HIGH = 0.60             # confidence at or above this: answer directly
MEDIUM = 0.30           # between MEDIUM and HIGH: ask "did you mean ...?"
UNKNOWN_MASS = 0.60     # weight of a word the FAQ knows nothing about
MAX_PICKS = 3           # most FAQ answers returned for one clause


class DataError(Exception):
    """The FAQ file is missing, unreadable or in the wrong format."""


# ----------------------------------------------------------------------------
# Text normalization
# ----------------------------------------------------------------------------
_CONTRACTIONS = [(re.compile(p), r) for p, r in (
    (r"\bwon't\b", "will not"), (r"\bcan't\b", "can not"), (r"\bcannot\b", "can not"),
    (r"n't\b", " not"), (r"'re\b", " are"), (r"'ve\b", " have"), (r"'ll\b", " will"),
    (r"'m\b", " am"), (r"'d\b", " would"),
    (r"\b(what|that|who|where|how|there|here|it|let)'s\b", r"\1 is"),
)]

STOPWORDS = frozenset("""
a an the is are was were be been am do does did can could would should will shall may might
to of in on at for with about from by as it its this that these those i me my mine we us our
you your yours he she they them their please kindly thanks thank hi hello hey ok okay yes
yeah yep sure well really actually basically maybe probably quite rather bit lot many
what when where who whom which how why and or but if then than so not no nor too very just
tell know want wanna need needed like wondering wonder wish hope hoping see look looking
possible whether get got give go going gonna let say said ask asking asked question questions
answer answers info information details detail regarding regards concerning one thing things
stuff anything something everything someone anyone now today there here also any some more
most other such only own same into over under again further once out off up down before after
help dear sir madam guys folks im ive ill id long until take takes taking
""".split())

# Groups of words that mean (nearly) the same thing in a support conversation.
# A word in a message also matches FAQ entries containing any word of its group.
SYNONYM_GROUPS = [
    "hour hours hrs timing timings schedule opening open close closing closed "
    "available availability time operate operating operation",
    "certificate certification cert credential diploma proof",
    "submit submission upload send hand deliver post provide share push",
    "receive obtain issue issued download collect find locate access",
    "github repo repository git",
    "project task assignment",
    "price cost fee charge pricing pay payment paid",
    "refund reimbursement cancel cancellation money",
    "order parcel package",
    "track tracking follow trace status",
    "international internationally overseas abroad foreign worldwide countries",
    "workspace portal dashboard",
    "contact email phone call reach",
    "password login signin account",
    "start begin commence",
    "deadline due",
    "internship intern traineeship",
    "review evaluation feedback assessment grade grading",
    "location address office",
]

_SUFFIXES = (
    ("ations", "at"), ("ation", "at"), ("ingly", ""), ("ings", ""), ("ing", ""),
    ("ments", ""), ("ment", ""), ("ies", "y"), ("ied", "y"), ("ers", ""),
    ("ed", ""), ("es", ""), ("s", ""),
)


def words_of(text: str) -> list:
    """Split text into lowercase words (letters and digits, any language)."""
    return re.findall(r"[^\W_]+", text)


def squash(word: str) -> str:
    """Collapse repeated letters so 'helloo', 'helo' and 'hello' compare equal."""
    return re.sub(r"(.)\1+", r"\1", word)


def stem(word: str) -> str:
    """A tiny suffix stripper: hours->hour, operating/operation->operat."""
    w = word
    if len(w) < 4 or w.isdigit():
        return w
    for suffix, replacement in _SUFFIXES:
        if not w.endswith(suffix):
            continue
        base = w[: -len(suffix)]
        if len(base) < (3 if suffix in ("ations", "ation", "ies", "ied", "ments", "ment", "s") else 4):
            continue
        if suffix == "s" and w.endswith(("ss", "us", "is")):
            continue
        base += replacement
        if (suffix in ("ing", "ings", "ingly", "ed", "ers", "es") and len(base) >= 4
                and base[-1] == base[-2] and base[-1] not in "lsz"):
            base = base[:-1]                      # submitt -> submit
        w = base
        break
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def _build_synonyms() -> dict:
    table: dict = {}
    for group in SYNONYM_GROUPS:
        stems = {stem(w) for w in group.split()}
        for s in stems:
            table.setdefault(s, set()).update(stems - {s})
    return {k: frozenset(v) for k, v in table.items()}


SYNONYMS = _build_synonyms()


def _is_transposition(a: str, b: str) -> bool:
    """True when b is a with two neighbouring letters swapped ("yuor"/"your", "recieve"/"receive")."""
    if len(a) != len(b) or len(a) < 4 or a == b:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]


def _is_typo_of_stopword(word: str) -> bool:
    if word in STOPWORDS or len(word) < 3:
        return False
    for stop in STOPWORDS:
        if len(stop) >= 3 and stop[0] == word[0] and (
                _is_transposition(word, stop)
                or difflib.SequenceMatcher(None, word, stop).ratio() >= 0.85):
            return True
    return False



def sanitize(raw: str) -> tuple:
    """Remove control characters and cap the length. Returns (text, was_truncated)."""
    text = unicodedata.normalize("NFKC", raw)
    text = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    if len(text) > MAX_INPUT_CHARS:
        return text[:MAX_INPUT_CHARS], True
    return text, False


def normalize(text: str) -> str:
    """Lowercase, expand contractions, drop apostrophes, squeeze repeats and spaces.

    Sentence punctuation is kept on purpose: it is used to split a message into clauses.
    """
    s = text.replace("’", "'").replace("‘", "'").lower()
    for pattern, replacement in _CONTRACTIONS:
        s = pattern.sub(replacement, s)
    s = s.replace("'", "")
    s = re.sub(r"(.)\1{2,}", r"\1\1", s)           # coooool -> cool
    return re.sub(r"\s+", " ", s).strip()


def has_content(text: str) -> bool:
    """True if the text contains at least one meaningful (non-stopword) word."""
    return any(w not in STOPWORDS for w in words_of(text))


_CLAUSE_SPLIT = re.compile(
    r"[.?!;\n]+|,|\b(?:and also|and then|as well as|also|plus|and|then)\b")


def split_clauses(text: str) -> list:
    """Split a normalized message into separate requests."""
    return [c.strip() for c in _CLAUSE_SPLIT.split(text) if c and c.strip()]


# ----------------------------------------------------------------------------
# Knowledge base
# ----------------------------------------------------------------------------
@dataclass
class FAQ:
    id: int
    question: str
    answer: str
    keywords: tuple = ()
    q_stems: frozenset = frozenset()     # question words (stemmed, no stopwords)
    qk_stems: frozenset = frozenset()    # question + keyword words
    a_stems: frozenset = frozenset()     # answer words


@dataclass
class Token:
    word: str            # as typed (normalized)
    stem: str            # as matched (after typo correction)
    alts: frozenset      # stems of synonyms
    fuzzy: bool = False  # True if corrected for a typo


@dataclass
class Pick:
    faq: FAQ
    gain: float          # weighted amount of the message this FAQ explains
    evidence: float      # same, counting only strong (question/keyword) matches
    doc_cov: float       # share of the FAQ question's words found in the message
    score: float
    tokens: list


@dataclass
class Match:
    level: str                       # high | medium | low | empty
    confidence: float
    picks: list = field(default_factory=list)
    options: list = field(default_factory=list)
    leftover: list = field(default_factory=list)
    text: str = ""


class KnowledgeBase:
    """FAQ entries plus the word statistics used to match messages against them."""

    def __init__(self, entries: list, greetings: Optional[list] = None, warnings: Optional[list] = None):
        self.greetings = [g for g in (greetings or []) if g]
        self.warnings = list(warnings or [])
        self.faqs: list = []
        self.surface: dict = {}
        for i, (question, answer, keywords) in enumerate(entries):
            q_words = [w for w in words_of(normalize(question)) if w not in STOPWORDS]
            q_stems = frozenset(stem(w) for w in q_words)
            for w in q_words:
                self.surface.setdefault(stem(w), w)
            k_stems = frozenset(stem(w) for w in words_of(normalize(" ".join(keywords)))
                                if w not in STOPWORDS)
            a_stems = frozenset(stem(w) for w in words_of(normalize(answer)) if w not in STOPWORDS)
            self.faqs.append(FAQ(i, question, answer, tuple(keywords), q_stems,
                                 q_stems | k_stems, a_stems))
        if not self.faqs:
            raise DataError("The FAQ file does not contain any usable questions.")

        self.df: dict = {}
        for faq in self.faqs:
            for s in faq.qk_stems:
                self.df[s] = self.df.get(s, 0) + 1
        self.vocab = set(self.df)
        for faq in self.faqs:
            self.vocab |= faq.a_stems
        self._fuzzy_pool = sorted(self.vocab | set(SYNONYMS))
        self.schedule = None
        self.schedule_faq: Optional[FAQ] = None
        for faq in self.faqs:
            sched = parse_schedule(faq.answer)
            if sched:
                self.schedule, self.schedule_faq = sched, faq
                break

    # ---- loading --------------------------------------------------------
    @classmethod
    def from_data(cls, data) -> "KnowledgeBase":
        if not isinstance(data, dict):
            raise DataError("The FAQ file must contain a JSON object with a 'faqs' list.")
        raw_faqs = data.get("faqs")
        if not isinstance(raw_faqs, list):
            raise DataError("The FAQ file must have a 'faqs' list.")
        greetings = data.get("greetings", [])
        greetings = [g.strip().lower() for g in greetings if isinstance(g, str)] \
            if isinstance(greetings, list) else []
        entries, warnings = [], []
        for n, item in enumerate(raw_faqs, start=1):
            question = item.get("question") if isinstance(item, dict) else None
            answer = item.get("answer") if isinstance(item, dict) else None
            if not (isinstance(question, str) and question.strip()
                    and isinstance(answer, str) and answer.strip()):
                warnings.append(f"Skipped FAQ #{n}: it needs a non-empty 'question' and 'answer'.")
                continue
            keywords = item.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [keywords]
            if not isinstance(keywords, list):
                keywords = []
            entries.append((question.strip(), answer.strip(),
                            [k for k in keywords if isinstance(k, str)]))
        return cls(entries, greetings, warnings)

    @classmethod
    def from_file(cls, path) -> "KnowledgeBase":
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            raise DataError(f"FAQ file not found: {path}")
        except (OSError, UnicodeDecodeError) as exc:
            raise DataError(f"Could not read {path}: {exc}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DataError(f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg}).")
        return cls.from_data(data)

    # ---- analysis -------------------------------------------------------
    def analyze(self, text: str) -> list:
        """Turn a normalized clause into content tokens (typos fixed, synonyms attached)."""
        tokens = []
        for w in words_of(text):
            if w in STOPWORDS:
                continue
            s, fuzzy = stem(w), False
            if s not in self.vocab and s not in SYNONYMS and len(s) >= 3 and not s.isdigit():
                if _is_typo_of_stopword(w):
                    continue                       # "wat", "yuor": a misspelt filler word
                close = difflib.get_close_matches(s, self._fuzzy_pool, n=1, cutoff=0.82 if len(s) >= 4 else 0.85)
                if not close:
                    close = [c for c in self._fuzzy_pool if _is_transposition(s, c)][:1]
                if close and close[0][0] == s[0]:
                    s, fuzzy = close[0], True
            tokens.append(Token(w, s, SYNONYMS.get(s, frozenset()), fuzzy))
        return tokens

    def idf(self, s: str) -> float:
        n = len(self.faqs)
        return (math.log((n + 1) / (self.df[s] + 1)) + 1) / (math.log(n + 1) + 1)

    def mass(self, tok: Token) -> float:
        """How much a word 'counts': rare words more, unknown words a fixed amount."""
        if tok.stem in self.df:
            return self.idf(tok.stem)
        known = [self.idf(a) for a in tok.alts if a in self.df]
        return max(known) if known else UNKNOWN_MASS

    @staticmethod
    def _weight(tok: Token, faq: FAQ) -> float:
        if tok.stem in faq.qk_stems:
            w = 1.0
        elif tok.alts & faq.qk_stems:
            w = 0.85
        elif tok.stem in faq.a_stems:
            w = 0.35
        elif tok.alts & faq.a_stems:
            w = 0.30
        else:
            return 0.0
        return w * (0.92 if tok.fuzzy else 1.0)

    def _rank(self, tokens: list) -> list:
        picks = []
        for faq in self.faqs:
            gain = evidence = 0.0
            matched = []
            for t in tokens:
                w = self._weight(t, faq)
                if w:
                    m = self.mass(t) * w
                    gain += m
                    if w >= 0.85:
                        evidence += m
                    matched.append(t)
            if not matched:
                continue
            total_q = sum(self.idf(s) for s in faq.q_stems) or 1.0
            hit_q = sum(self.idf(s) for s in faq.q_stems
                        if any(t.stem == s or s in t.alts for t in tokens))
            doc_cov = hit_q / total_q
            picks.append(Pick(faq, gain, evidence, doc_cov, gain * (0.9 + 0.1 * doc_cov), matched))
        picks.sort(key=lambda p: (-p.score, len(p.faq.q_stems), p.faq.id))
        return picks

    @staticmethod
    def _factor(evidence: float) -> float:
        return min(1.0, 0.45 + 0.8 * evidence)

    def match(self, tokens: list, text: str = "") -> Match:
        """Find the FAQ entries that best explain a clause (greedy cover)."""
        if not tokens:
            return Match("empty", 0.0, text=text)
        total = sum(self.mass(t) for t in tokens)
        remaining = list(tokens)
        sequence: list = []      # the best FAQ for each round, each explaining new words
        ties: list = []          # FAQs that explain the same words equally well
        first_ranking: list = []
        for round_no in range(MAX_PICKS):
            ranking = [p for p in self._rank(remaining)
                       if all(p.faq is not s.faq for s in sequence)]
            if not ranking:
                break
            best = ranking[0]
            rem_mass = sum(self.mass(t) for t in remaining)
            if round_no == 0:
                first_ranking = ranking
            elif (rem_mass < 0.25 * total or best.evidence < 0.5 or best.doc_cov < 0.5
                  or best.gain / rem_mass < HIGH):
                break                    # a second answer needs strong evidence of its own
            sequence.append(best)
            if round_no == 0:
                same = {id(t) for t in best.tokens}
                for other in ranking[1:MAX_PICKS]:
                    direct = all(t.stem in other.faq.qk_stems for t in other.tokens)
                    if (other.score >= 0.97 * best.score and other.evidence >= 0.5 and direct
                            and {id(t) for t in other.tokens} == same):
                        ties.append(other)
            used = {id(t) for t in best.tokens}
            remaining = [t for t in remaining if id(t) not in used]
            if not remaining or len(sequence) >= MAX_PICKS:
                break
        if not sequence:
            return Match("low", 0.0, leftover=[t.word for t in tokens], text=text)

        coverage = min(1.0, sum(p.gain for p in sequence) / total)
        confidence = coverage * self._factor(min(p.evidence for p in sequence))
        top = sequence[0]
        if top.evidence >= 0.5 and len(top.faq.q_stems) >= 2 and top.doc_cov >= 0.66:
            confidence = max(confidence, 0.92 * top.doc_cov)   # the user restated the question
        confidence = min(confidence, 1.0)

        level = "high" if confidence >= HIGH else "medium" if confidence >= MEDIUM else "low"
        options: list = []
        if level == "medium":
            for p in first_ranking[:MAX_PICKS]:
                c = p.gain / total * self._factor(p.evidence)
                if c >= MEDIUM and p.score >= 0.7 * first_ranking[0].score:
                    options.append(p)
            options = options or [first_ranking[0]]
        explained_ids = {id(t) for p in sequence for t in p.tokens}
        leftover = [t.word for t in tokens if id(t) not in explained_ids]
        picks = (sequence + ties) if level == "high" else sequence[:1]
        return Match(level, round(confidence, 3), picks, options, leftover, text)

    # ---- helpers for the conversation layer -------------------------------
    def topic_lines(self, limit: int = 8) -> str:
        lines = [f"- {f.question}" for f in self.faqs[:limit]]
        if len(self.faqs) > limit:
            lines.append(f"- ...and {len(self.faqs) - limit} more (type 'topics' for the full list)")
        return "\n".join(lines)

    def example_keywords(self, count: int = 3) -> list:
        """One distinctive word per FAQ entry, as typed in the FAQ, for hint messages."""
        words = []
        for faq in self.faqs:
            if not faq.q_stems:
                continue
            best = max(faq.q_stems, key=lambda s: (self.idf(s) if s in self.df else 0, len(s)))
            words.append(self.surface.get(best, best))
            if len(words) == count:
                break
        return words


# ----------------------------------------------------------------------------
# Opening hours: read them from the FAQ text, answer "are you open now?"
# ----------------------------------------------------------------------------
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_INDEX = {name.lower(): i for i, name in enumerate(DAY_NAMES)}
_DAY_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
             "fri": 4, "sat": 5, "sun": 6}
# Fixed UTC offsets in hours (no daylight-saving rules). IST means India Standard Time.
TZ_OFFSETS = {"UTC": 0, "GMT": 0, "IST": 5.5, "EAT": 3, "CAT": 2, "WAT": 1, "SAST": 2,
              "CET": 1, "EET": 2, "EST": -5, "EDT": -4, "MST": -7, "MDT": -6,
              "PST": -8, "PDT": -7, "JST": 9, "SGT": 8, "AEST": 10}
_DAY_WORD = r"(mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)[a-z]*"
_DAY_RANGE = re.compile(rf"\b{_DAY_WORD}\s*(?:through|thru|to|until|till|-|–|—)\s*{_DAY_WORD}\b", re.I)
_TIME12 = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*(?:to|until|till|-|–|—)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.I)
_TIME24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(?:to|until|till|-|–|—)\s*([01]?\d|2[0-3]):([0-5]\d)\b")
_TZ = re.compile(r"\b(" + "|".join(TZ_OFFSETS) + r")\b")
_EVERY_DAY = re.compile(r"\b(?:daily|every day|7 days a week|seven days a week)\b", re.I)


@dataclass(frozen=True)
class Schedule:
    days: frozenset
    open: "datetime.time"
    close: "datetime.time"
    label: str                 # e.g. "IST", or "local time" when the text names no zone
    tz: Optional[timezone]     # None = the machine's local zone

    def is_open(self, local: datetime) -> bool:
        return local.weekday() in self.days and self.open <= local.time() < self.close

    def next_open(self, local: datetime) -> Optional[datetime]:
        """The first opening time after `local`."""
        for add in range(8):
            day = (local + timedelta(days=add)).date()
            if day.weekday() in self.days:
                candidate = datetime.combine(day, self.open, tzinfo=local.tzinfo)
                if candidate > local:
                    return candidate
        return None

    def closing_today(self, local: datetime) -> datetime:
        return datetime.combine(local.date(), self.close, tzinfo=local.tzinfo)


def parse_schedule(text: str) -> Optional[Schedule]:
    """Extract 'Monday through Friday from 9:00 AM to 6:00 PM IST' style hours, or None."""
    day_range = _DAY_RANGE.search(text)
    if day_range:
        start, end = _DAY_ABBR[day_range.group(1).lower()], _DAY_ABBR[day_range.group(2).lower()]
        days = set()
        i = start
        while True:
            days.add(i)
            if i == end:
                break
            i = (i + 1) % 7
    elif _EVERY_DAY.search(text):
        days = set(range(7))
    else:
        return None

    m12 = _TIME12.search(text)
    if m12:
        h1, m1, ap1, h2, m2, ap2 = m12.groups()
        t1 = ((int(h1) % 12) + (12 if ap1.lower() == "p" else 0), int(m1 or 0))
        t2 = ((int(h2) % 12) + (12 if ap2.lower() == "p" else 0), int(m2 or 0))
    else:
        m24 = _TIME24.search(text)
        if not m24:
            return None
        t1, t2 = (int(m24.group(1)), int(m24.group(2))), (int(m24.group(3)), int(m24.group(4)))
    if not (0 <= t1[0] < 24 and 0 <= t2[0] < 24):
        return None
    opens, closes = datetime(2000, 1, 1, *t1).time(), datetime(2000, 1, 1, *t2).time()
    if closes <= opens:
        return None                                   # overnight hours are not supported

    tz_match = _TZ.search(text)
    if tz_match:
        label = tz_match.group(1)
        tz = timezone(timedelta(hours=TZ_OFFSETS[label]), label)
    else:
        label, tz = "local time", None
    return Schedule(frozenset(days), opens, closes, label, tz)


def fmt_clock(t) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def fmt_duration(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''}" + (f" {hours}h" if hours else "")
    if hours:
        return f"{hours}h" + (f" {mins}m" if mins else "")
    return f"{mins} min"


def days_phrase(days) -> str:
    ordered = sorted(days)
    if len(ordered) == 7:
        return "every day"
    runs, start = [], ordered[0]
    for prev, cur in zip(ordered, ordered[1:] + [None]):
        if cur is None or cur != prev + 1:
            runs.append((start, prev))
            start = cur
    return ", ".join(DAY_NAMES[a] if a == b else f"{DAY_NAMES[a]} to {DAY_NAMES[b]}" for a, b in runs)


_TIME_REF = (r"now|right now|currently|at the moment|at present|today|tonight|tomorrow|weekends?|weekdays?|"
             r"this (?:week|weekend|evening|afternoon|morning)|"
             r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?")
_HOURS_TOPIC = (r"open|opens|opened|opening|closed?|closes|closing|available|availability|hours|"
                r"working|operat\w*|online|reachable|staffed|around")
HOURS_STATUS_RE = re.compile(
    rf"(?=.*\b(?:{_TIME_REF})\b)(?=.*\b(?:{_HOURS_TOPIC})\b)"
    rf"|\bare you (?:open|closed|available|working|around|operating|there)\b"
    rf"|\bis (?:anyone|someone|somebody|support|the team|the office) (?:there|available|around|online|open|working)\b"
    rf"|\b(?:do|does) (?:you|the team|support|the office) (?:work|operate|open|close)\b.*\b(?:{_TIME_REF})\b"
    rf"|\bwhen (?:do|does|will|are) (?:you|the team|support|the office) (?:open|reopen|close|back)\b")


# ----------------------------------------------------------------------------
# Conversation
# ----------------------------------------------------------------------------
class State(Enum):
    GREETING = "greeting"      # welcome shown, waiting for the first message
    LISTENING = "listening"    # normal conversation
    CLARIFYING = "clarifying"  # asked "did you mean ...?", waiting for yes / no / choice
    ENDED = "ended"            # goodbye said


@dataclass
class Reply:
    text: str
    intent: str
    state: State
    confidence: Optional[float] = None
    faq_ids: tuple = ()
    end: bool = False
    trace: str = ""


_ORDINALS = {"1": 0, "one": 0, "first": 0, "a": 0, "2": 1, "two": 1, "second": 1, "b": 1,
             "3": 2, "three": 2, "third": 2, "c": 2}

_YES_RE = re.compile(r"^(?:yes|yeah|yep|yup|ya|y|sure|correct|right|exactly|definitely|of course|"
                     r"that is right|that one|that is it|ok|okay|please do|go ahead)(?: please| thanks| thank you)?[ !.]*$")
_NO_RE = re.compile(r"^(?:no|nope|nah|n|not really|never ?mind|wrong|cancel|none|neither|"
                    r"no thanks|no thank you|that is not it|not that|not that one)[ !.]*$")
_BOTH_RE = re.compile(r"^(?:both|all|all of them|both of them|every one)[ !.]*$")
_CHOICE_RE = re.compile(r"^(?:(?:number|option|choice|the)\s+)?(\w+?)(?:\s+one)?(?:\s+please)?[ !.)]*$")

_ACK_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|alright|all right|right|well|so|great|awesome|perfect|cool|nice|good|fine|"
    r"excellent|wonderful|got it|i see|understood|makes sense|sounds good|thanks|thank you|thank u|"
    r"thx|ty|cheers|much appreciated|appreciate it|many thanks|lovely)\b[\s,!.:;-]*)+")
_FAREWELL_CLAUSE = re.compile(
    r"^(?:by+e+(?: ?by+e+)?|goo+d ?by+e+|ex+i+t+|qu+i+t+|stop|see (?:you|ya)(?: later| soon| around| tomorrow)?|"
    r"cya|good ?night|i am (?:done|leaving|off)|we are done|that is (?:all|it)|that will be all|"
    r"nothing else|no more questions|take care|have a (?:good|nice|great) (?:day|one|night|evening|weekend)|"
    r"end (?:chat|conversation|session)|talk (?:to you )?later|i am out)"
    r"(?: now| then| for now| everyone| all| bot| team| assistant| please)*[ !.]*$")
_SMALLTALK_RE = re.compile(
    r"^(?:how are you(?: doing| today)?|how is it going|how are things|how do you do|what is up|sup)[ ?!.]*$")
_HELP_RE = re.compile(
    r"^(?:please )?(?:(?:can|could) you )?(?:help(?: me)?(?: out)?|menu|options|topics|commands|faqs?|"
    r"what can you (?:do|help (?:me )?with|answer)|what do you know(?: about)?|what can i ask(?: you)?|"
    r"how (?:does this|do you) work|(?:show|list)(?: me)?(?: all)?(?: the)? ?(?:topics|menu|options|questions|faqs?|commands)?)"
    r"[ ?!.]*$")
_IDENTITY_RE = re.compile(
    r"\b(?:who|what) are you\b|\byour name\b|\bwho (?:made|built|created|programmed) you\b|"
    r"\bare you (?:a |an )?(?:bot|robot|human|real|person|ai|machine|computer|program)\b")
_HUMAN_RE = re.compile(
    r"\b(?:talk|speak|chat|connect|transfer|escalate|get|reach|need|want)\b.*\b(?:human|person|agent|"
    r"representative|operator|manager|supervisor)\b|\b(?:human|agent|representative|operator) (?:please|help)\b")
_FRUSTRATION_RE = re.compile(
    r"\b(?:useless|stupid|dumb|terrible|awful|worst|rubbish|garbage|hate you|you suck|not helpful|"
    r"unhelpful|no help|does not work|wrong answer|that is wrong|not what i asked)\b")
_NAME_RE = re.compile(r"\b(?:my name is|call me|i go by)\s+([a-z][a-z-]{1,20})\b")
_TIME_RE = re.compile(
    r"\bwhat(?: is|s)? the (?:current |local )?(?:time|date)\b|\bwhat time is it\b|"
    r"\bwhat (?:day|date) is (?:it|today)\b|\bcurrent time\b|\btodays date\b")
_REPEAT_RE = re.compile(
    r"^(?:please )?(?:(?:can|could) you )?(?:repeat(?: that| it)?|say (?:that|it) again|come again|"
    r"pardon|what|sorry|again|one more time)[ ?!.]*$")
_MORE_RE = re.compile(
    r"^(?:tell me more|more(?: info| information| details)?|go on|continue|elaborate|"
    r"explain(?: more| further)?|what else|anything else)[ ?!.]*$")
_NOT_NAMES = {"not", "here", "fine", "sorry", "good", "ok", "okay", "stuck", "confused", "lost"}


class Bot:
    """The conversation engine. `respond()` turns one user message into one Reply."""

    def __init__(self, kb: KnowledgeBase, name: str = DEFAULT_NAME,
                 now_fn: Optional[Callable[[], datetime]] = None,
                 local_tz: Optional[timezone] = None):
        self.kb = kb
        self.name = name
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.local_tz = local_tz or datetime.now().astimezone().tzinfo
        self.state = State.GREETING
        self.turns = 0
        self.welcomed = False
        self.greetings_seen = 0
        self.fallback_streak = 0
        self.user_name = ""
        self.last_text = ""
        self.last_faq: Optional[FAQ] = None
        self.pending: list = []
        sched = kb.schedule
        self.schedule = replace(sched, tz=self.local_tz) if sched and sched.tz is None else sched
        greetings = set(kb.greetings) | {"hello", "hi", "hey", "good morning", "good afternoon",
                                          "good evening", "greetings", "howdy", "hiya", "hola", "yo"}
        alternation = "|".join(re.escape(g) for g in sorted(greetings, key=len, reverse=True))
        self._greeting_re = re.compile(
            rf"^(?:{alternation})\b(?:\s+(?:there|bot|team|everyone|again|assistant|all))?[\s,!.:;-]*")
        self._greeting_squashed = {squash(g) for g in greetings if " " not in g}

    # ---- public API -----------------------------------------------------
    def opening(self) -> Reply:
        """The welcome message shown when the program starts."""
        text = (f"Hello! I'm {self.name}. I can answer questions about:\n{self.kb.topic_lines()}\n"
                "Ask in your own words, or type 'help'. Say 'bye' when you're done.")
        self.state = State.LISTENING
        self.welcomed = True
        return self._reply(text, "welcome")

    def respond(self, raw: str) -> Reply:
        self.turns += 1
        cleaned, truncated = sanitize(raw if isinstance(raw, str) else str(raw))
        if not cleaned:
            return self._reply("I didn't catch anything. Type a question, or 'help' to see what I can do.",
                               "empty")
        low = normalize(cleaned)
        if not any(ch.isalnum() for ch in low):
            return self._reply("I can only read words, so I couldn't make anything of that. "
                               "Try asking a question, or type 'help'.", "unreadable")
        if self.state is State.GREETING:
            self.state = State.LISTENING
        reply = self._dispatch(cleaned, low)
        if reply.intent not in ("fallback", "empty", "unreadable", "clarify"):
            self.fallback_streak = 0
        if truncated:
            reply.text += f"\n(I only read the first {MAX_INPUT_CHARS} characters of your message.)"
        self.last_text = reply.text
        return reply

    # ---- dispatch -------------------------------------------------------
    def _dispatch(self, cleaned: str, low: str) -> Reply:
        command = re.fullmatch(r"/(\w+)", low)
        if command:
            return self._command(command.group(1))

        if self.state is State.CLARIFYING:
            resolved = self._resolve_pending(low)
            if resolved:
                return resolved
            self.pending, self.state = [], State.LISTENING      # a brand-new question

        if self._is_farewell(low):
            return self._farewell(low)

        greeted = acked = False
        rest = low
        for _ in range(3):
            m = self._greeting_re.match(rest)
            if m:
                greeted, rest = True, rest[m.end():]
                continue
            m = _ACK_RE.match(rest)
            if m:
                acked, rest = True, rest[m.end():]
                continue
            break
        if greeted and not has_content(rest) and rest.strip(" ,.!?;:-") == "":
            return self._greet()
        single = words_of(low)
        if (not greeted and len(single) == 1 and len(single[0]) >= 3
                and squash(single[0]) in self._greeting_squashed):
            return self._greet()                                  # 'helloooo', 'helo'
        if (not greeted and len(single) == 1 and len(single[0]) >= 4 and single[0][0] in "hg"
                and difflib.get_close_matches(squash(single[0]), self._greeting_squashed, n=1, cutoff=0.8)):
            return self._greet()
        if acked and not has_content(rest):
            return self._reply("You're welcome! Anything else you'd like to know?", "thanks")
        if (greeted or acked) and self._is_farewell(rest):
            return self._farewell(rest)

        prefix = ""
        if greeted:
            prefix = "Hi there! " if self.turns > 1 else "Hello! "
        reply = self._builtin(rest) or self._answer(cleaned, rest)
        if prefix and reply.intent not in ("farewell",):
            reply.text = prefix + reply.text
        return reply

    # ---- small, fixed intents -------------------------------------------
    def _is_farewell(self, text: str) -> bool:
        clauses = split_clauses(text)
        if not clauses:
            return False
        saw_farewell = False
        for clause in clauses:
            clause = clause.strip()
            if _FAREWELL_CLAUSE.match(clause):
                saw_farewell = True
                continue
            stripped = _ACK_RE.sub("", clause, count=1).strip(" ,.!?;:-")
            if stripped == "":
                continue
            if _FAREWELL_CLAUSE.match(stripped):
                saw_farewell = True
                continue
            return False
        return saw_farewell

    def _farewell(self, low: str) -> Reply:
        self.state = State.ENDED
        who = f", {self.user_name}" if self.user_name else ""
        thanks = "You're welcome! " if re.search(r"\b(?:thanks|thank|thx|cheers)\b", low) else ""
        return self._reply(f"{thanks}Goodbye{who}! Thanks for chatting - come back any time.",
                           "farewell", end=True)

    def _greet(self) -> Reply:
        self.greetings_seen += 1
        if not self.welcomed:                       # nobody has shown the menu yet
            self.welcomed = True
            return self._reply(f"Hello! I'm {self.name}. I can help with:\n{self.kb.topic_lines()}\n"
                               "What would you like to know?", "greeting")
        lead = "Hi there!" if self.greetings_seen <= 1 else "Hi again!"
        return self._reply(f"{lead} What would you like to know?", "greeting")

    def _command(self, name: str) -> Reply:
        if name in ("exit", "quit", "bye"):
            return self._farewell("bye")
        if name in ("help", "topics", "menu"):
            return self._help(topics_only=(name == "topics"))
        if name == "reset":
            self.pending, self.state = [], State.LISTENING
            self.last_faq, self.fallback_streak, self.user_name = None, 0, ""
            return self._reply("Okay, I've cleared our conversation. What would you like to know?", "reset")
        return self._reply(f"I don't know the command /{name}. Try /help, /topics, /reset or /exit.", "command")

    def _help(self, topics_only: bool = False) -> Reply:
        self.fallback_streak = 0
        if topics_only:
            return self._reply(f"Here's everything I can answer:\n{self._all_topics()}", "help")
        return self._reply(
            "I answer questions from my FAQ. Ask in your own words, for example:\n"
            f"{self.kb.topic_lines(4)}\n"
            "You can ask several things at once, like \"hours and certificate\". "
            "Type 'topics' for the full list, or 'bye' to leave.", "help")

    def _all_topics(self) -> str:
        return "\n".join(f"- {f.question}" for f in self.kb.faqs)

    def _builtin(self, rest: str) -> Optional[Reply]:
        short = len(words_of(rest)) <= 8
        if _SMALLTALK_RE.match(rest):
            return self._reply("I'm doing well, thanks for asking! What can I help you with?", "smalltalk")
        if _HELP_RE.match(rest):
            return self._help()
        if _MORE_RE.match(rest):
            self.fallback_streak = 0
            others = "\n".join(f"- {f.question}" for f in self.kb.faqs if f is not self.last_faq)
            lead = ("That's everything I have on that topic. " if self.last_faq else "") + \
                   "Other things you can ask me:"
            return self._reply(f"{lead}\n{others}" if others else "That's all I have.", "more")
        if _REPEAT_RE.match(rest):
            return self._reply(self.last_text or "I haven't said anything yet - ask me something!", "repeat")
        if _YES_RE.match(rest):
            return self._reply("Great! What would you like to know?", "yes_no")
        if _NO_RE.match(rest):
            return self._reply("Okay - let me know if there's anything I can help with.", "yes_no")
        name = _NAME_RE.search(rest)
        if name and name.group(1) not in _NOT_NAMES:
            self.user_name = name.group(1).capitalize()
            return self._reply(f"Nice to meet you, {self.user_name}! What can I help you with?", "name")
        if _TIME_RE.search(rest):
            return self._reply(self._time_text(), "time")
        if short and _IDENTITY_RE.search(rest):
            return self._reply(
                f"I'm {self.name}, your virtual support assistant. I can help answer questions based on our FAQ.\n"
                "Here are some topics you can ask me about:\n" + self.kb.topic_lines(4), "identity")
        if _HUMAN_RE.search(rest):
            return self._reply("I can't connect you to a live representative from this terminal, but I can answer "
                               f"questions from our FAQ. Here is what I can cover:\n{self._all_topics()}", "human")
        if _FRUSTRATION_RE.search(rest):
            return self._reply("Sorry about that! I might not understand everything right away. "
                               "Try rephrasing with a couple of keywords, or type 'topics' to see what I can answer.",
                               "frustration")
        return None

    def _time_text(self) -> str:
        now = self.now_fn()
        local = now.astimezone(self.local_tz)
        text = f"Your local time is {fmt_clock(local)} on {local:%A, %d %B %Y}."
        if self.schedule and self.schedule.tz is not None and self.schedule.label != "local time":
            there = now.astimezone(self.schedule.tz)
            text = (f"It's {fmt_clock(there)} {self.schedule.label} ({there:%A, %d %B %Y}). "
                    f"Your local time is {fmt_clock(local)}.")
        return text

    # ---- clarification state --------------------------------------------
    def _resolve_pending(self, low: str) -> Optional[Reply]:
        options = self.pending
        text = re.sub(r"^(?:ok|okay|well|so|um|oh|hmm)[\s,]+", "", low.strip())
        if _BOTH_RE.match(text) and len(options) > 1:
            return self._deliver([p.faq for p in options], "faq", 0.5, resolved=True)
        if _YES_RE.match(text):
            if len(options) == 1:
                return self._deliver([options[0].faq], "faq", 0.5, resolved=True)
            return self._reply("Which one did you mean? Reply with a number"
                               f" (1-{len(options)}), 'both' or 'none'.", "clarify")
        if _NO_RE.match(text):
            self.pending, self.state = [], State.LISTENING
            return self._reply("No problem. Could you rephrase it, or type 'topics' to see what I can answer?",
                               "clarify_no")
        choice = _CHOICE_RE.match(text)
        if choice and choice.group(1) in _ORDINALS:
            idx = _ORDINALS[choice.group(1)]
            if idx < len(options):
                return self._deliver([options[idx].faq], "faq", 0.5, resolved=True)
        return None

    # ---- answering ------------------------------------------------------
    def _answer(self, cleaned: str, rest: str) -> Reply:
        clauses = split_clauses(rest)
        if not clauses:
            return self._fallback(rest, Match("empty", 0.0))

        results = []              # (kind, payload, clause text)
        for clause in clauses:
            if self.schedule and HOURS_STATUS_RE.search(clause):
                results.append(("hours", clause, clause))
            else:
                tokens = self.kb.analyze(clause)
                if tokens:
                    results.append(("faq", self.kb.match(tokens, clause), clause))
        if not results:
            return self._fallback(rest, Match("empty", 0.0))

        # One whole-message match handles phrases that contain a joining word ("terms and conditions").
        if len(clauses) > 1 and all(kind == "faq" for kind, _, _ in results):
            whole = self.kb.match(self.kb.analyze(rest), rest)
            parts = [r for k, r, _ in results if r.level == "high"]
            part_ids = {p.faq.id for r in parts for p in r.picks}
            whole_ids = {p.faq.id for p in whole.picks}
            if whole.level == "high" and whole.confidence >= 0.85 and (whole_ids >= part_ids):
                results = [("faq", whole, rest)]

        answered_faqs: list = []
        hours_texts: list = []
        notes: list = []
        medium: Optional[Match] = None
        confidences = []
        for kind, payload, clause in results:
            if kind == "hours":
                hours_texts.append(self._hours_text(clause))
                if self.kb.schedule_faq and self.kb.schedule_faq not in answered_faqs:
                    answered_faqs.append(self.kb.schedule_faq)
                confidences.append(1.0)
                continue
            if payload.level == "high":
                for pick in payload.picks:
                    if pick.faq not in answered_faqs:
                        answered_faqs.append(pick.faq)
                confidences.append(payload.confidence)
            elif payload.level == "medium":
                if medium is None:
                    medium = payload
                notes.append(clause)
            else:
                notes.append(clause)

        if not answered_faqs and not hours_texts:
            if medium is not None:
                return self._clarify(medium)
            return self._fallback(rest, results[0][1])

        body = self._compose(answered_faqs, hours_texts)
        if notes:
            quoted = "; ".join(f'"{n}"' for n in notes[:3])
            body += f"\n\nI couldn't find an answer for: {quoted}. Type 'topics' to see what I can help with."
        faq_ids = tuple(f.id for f in answered_faqs)
        self.last_faq = answered_faqs[-1] if answered_faqs else self.last_faq
        self.state = State.LISTENING
        only_hours = bool(hours_texts) and all(kind == "hours" for kind, _, _ in results)
        return self._reply(body, "hours_status" if only_hours else "faq",
                           min(confidences) if confidences else None, faq_ids,
                           trace=f"clauses={len(clauses)}")

    def _compose(self, faqs: list, hours_texts: list) -> str:
        """Join one or more answers into a single reply."""
        entries = []
        for faq in faqs:
            if hours_texts and faq is self.kb.schedule_faq:
                entries.append((faq.question, "\n".join(hours_texts)))
                hours_texts = []
            else:
                entries.append((faq.question, faq.answer))
        for text in hours_texts:
            entries.append(("", text))
        if len(entries) == 1:
            return entries[0][1]
        blocks = []
        for question, answer in entries:
            blocks.append(f"- {question}\n  {answer}" if question else f"- {answer}")
        return "Here's what I found:\n\n" + "\n\n".join(blocks)

    def _deliver(self, faqs: list, intent: str, confidence: float, resolved: bool = False) -> Reply:
        self.pending, self.state = [], State.LISTENING
        self.fallback_streak = 0
        self.last_faq = faqs[-1]
        return self._reply(self._compose(faqs, []), intent, confidence, tuple(f.id for f in faqs))

    def _clarify(self, match: Match) -> Reply:
        options = match.options[:MAX_PICKS]
        self.pending = options
        self.state = State.CLARIFYING
        if len(options) == 1:
            text = (f"I'm not completely sure, but did you mean:\n  \"{options[0].faq.question}\"\n"
                    "(yes / no)")
        else:
            listing = "\n".join(f"  {i}) {p.faq.question}" for i, p in enumerate(options, 1))
            text = ("I found a few possibilities. Which one did you mean?\n"
                    f"{listing}\nReply with a number, 'both' or 'none'.")
        return self._reply(text, "clarify", match.confidence, tuple(p.faq.id for p in options))

    def _fallback(self, rest: str, match: Match) -> Reply:
        self.fallback_streak += 1
        n = self.fallback_streak
        if match.level == "empty":
            lead = "I couldn't pick out a specific topic in that."
        else:
            lead = "Sorry, I don't have an answer for that."
        if n == 1:
            text = f"{lead} I can help with:\n{self.kb.topic_lines()}\nTry rephrasing, or pick one of those."
        elif n == 2:
            hints = ", ".join(f"'{w}'" for w in self.kb.example_keywords())
            text = (f"I'm still not sure what you mean. Try a few keywords instead (for example {hints}), "
                    "or type 'topics' to see everything I can answer.")
        else:
            text = ("I'm having trouble finding an answer for that. I can only answer "
                    "questions covered in our FAQ. Type 'topics' to see them, or 'bye' to exit.")
        self.state = State.LISTENING
        return self._reply(text, "fallback", match.confidence if match.level != "empty" else None)

    # ---- live hours -----------------------------------------------------
    def _your_time(self, dt: datetime) -> str:
        offset = self.local_tz.utcoffset(dt.astimezone(self.local_tz))
        if offset == dt.utcoffset():
            return ""
        return f" ({fmt_clock(dt.astimezone(self.local_tz))} your time)"

    def _when(self, target: datetime, local_now: datetime) -> str:
        delta_days = (target.date() - local_now.date()).days
        day = "today" if delta_days == 0 else "tomorrow" if delta_days == 1 else DAY_NAMES[target.weekday()]
        return f"{day} at {fmt_clock(target)} {self.schedule.label}{self._your_time(target)}"

    def _hours_text(self, clause: str) -> str:
        sched = self.schedule
        now = self.now_fn()
        local = now.astimezone(sched.tz)
        label = sched.label
        regular = (f"Our regular hours are {days_phrase(sched.days)}, "
                   f"{fmt_clock(sched.open)} to {fmt_clock(sched.close)} {label}.")

        day_match = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", clause)
        target_day, name = None, ""
        if day_match:
            target_day, name = _DAY_INDEX[day_match.group(1)], DAY_NAMES[_DAY_INDEX[day_match.group(1)]]
        elif re.search(r"\btomorrow\b", clause):
            target_day = (local.weekday() + 1) % 7
            name = "Tomorrow"
        elif re.search(r"\bweekends?\b", clause):
            if any(d in sched.days for d in (5, 6)):
                return f"Yes - we're open on the weekend. {regular}"
            return f"No - we're closed on weekends. {regular}"
        elif re.search(r"\bweekdays?\b", clause):
            return regular

        if target_day is not None:
            if target_day in sched.days:
                return (f"Yes - {name}: we're open {fmt_clock(sched.open)} to "
                        f"{fmt_clock(sched.close)} {label}.")
            return f"No - we're closed on {name}{'' if name == 'Tomorrow' else 's'}. {regular}"

        if sched.is_open(local):
            closing = sched.closing_today(local)
            return (f"Yes, we're open right now. It's {fmt_clock(local)} {label}, and we close at "
                    f"{fmt_clock(sched.close)} {label}{self._your_time(closing)} - in "
                    f"{fmt_duration(closing - local)}.")
        upcoming = sched.next_open(local)
        if upcoming is None:
            return f"We're closed right now. {regular}"
        return (f"We're closed right now (it's {fmt_clock(local)} {label} on {DAY_NAMES[local.weekday()]}). "
                f"We open again {self._when(upcoming, local)}, in {fmt_duration(upcoming - local)}.")

    # ---- utilities ------------------------------------------------------
    def _reply(self, text: str, intent: str, confidence: Optional[float] = None,
               faq_ids: tuple = (), end: bool = False, trace: str = "") -> Reply:
        return Reply(text, intent, self.state, confidence, faq_ids, end, trace)


# ----------------------------------------------------------------------------
# Terminal interface
# ----------------------------------------------------------------------------
class Style:
    """ANSI colours, switched off for pipes, NO_COLOR and --no-color."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bot(self, t): return self.paint(t, "1;36")
    def user(self, t): return self.paint(t, "1;32")
    def dim(self, t): return self.paint(t, "2")
    def warn(self, t): return self.paint(t, "33")


def wrap_block(text: str, width: int) -> list:
    """Wrap text to `width`, keeping line breaks and hanging indents for bullets."""
    lines = []
    for line in text.split("\n"):
        if not line.strip():
            lines.append("")
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.lstrip()
        hang = indent + (2 if stripped.startswith("- ") else 3 if re.match(r"\d\) ", stripped) else 0)
        lines.extend(textwrap.wrap(line, width=max(width, 20), subsequent_indent=" " * hang,
                                   break_long_words=False, break_on_hyphens=False) or [""])
    return lines


def render(reply_text: str, style: Style, prompt: str = "Bot > ") -> str:
    width = min(shutil.get_terminal_size((88, 20)).columns, 96) - len(prompt)
    lines = wrap_block(reply_text, width)
    out = [style.bot(prompt) + (lines[0] if lines else "")]
    out += [(" " * len(prompt) + line) if line else "" for line in lines[1:]]
    return "\n".join(out)


def banner(bot: Bot, style: Style) -> str:
    title = f" {bot.name} "
    bar = "=" * 60
    return "\n".join([style.dim(bar), style.bot(title) + style.dim("  type 'help' for topics, 'bye' to exit"),
                      style.dim(bar)])


def run_chat(bot: Bot, style: Style, input_fn=input, debug: bool = False, echo: bool = False) -> None:
    """The interactive loop. Returns when the user says goodbye or input ends."""
    print(banner(bot, style))
    for warning in bot.kb.warnings:
        print(style.warn(f"Note: {warning}"))
    print(render(bot.opening().text, style))
    while bot.state is not State.ENDED:
        try:
            line = input_fn("You > ")
        except EOFError:
            print()
            print(render(bot.respond("bye").text, style))
            return
        except KeyboardInterrupt:
            print()
            print(render("Interrupted - goodbye!", style))
            return
        if echo:
            print(line)
        try:
            reply = bot.respond(line)
        except Exception as exc:                       # never crash the conversation
            if debug:
                raise
            print(render("Sorry, something went wrong on my side. Please try again.", style))
            continue
        print(render(reply.text, style))
        if debug:
            conf = "-" if reply.confidence is None else f"{reply.confidence:.2f}"
            detail = f"state={reply.state.value} intent={reply.intent} confidence={conf} faq={list(reply.faq_ids)}"
            print(style.dim(f"      [{detail}{' ' + reply.trace if reply.trace else ''}]"))
        if reply.end:
            return


DEMO_SCRIPT = [
    "hello",
    "what are your hours and when do I get my certificate?",
    "wher do i upload my intership project",
    "are you open right now?",
    "are you open on saturday?",
    "where can i upload my work?",
    "yes",
    "how do I get a refund?",
    "thanks, bye",
]


def run_demo(bot: Bot, style: Style) -> None:
    """Play a scripted conversation (fixed clock) so the behaviour can be seen without typing."""
    print(banner(bot, style))
    print(render(bot.opening().text, style))
    for line in DEMO_SCRIPT:
        print(style.user("You > ") + line)
        reply = bot.respond(line)
        print(render(reply.text, style))
        if reply.end:
            break


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Terminal support assistant and FAQ chatbot.")
    p.add_argument("--faq", default=str(DEFAULT_FAQ), help="path to the FAQ JSON file")
    p.add_argument("--name", default=DEFAULT_NAME, help="name the bot introduces itself with")
    p.add_argument("--debug", action="store_true", help="show state, intent and confidence for each reply")
    p.add_argument("--no-color", action="store_true", help="turn off colours")
    p.add_argument("--demo", action="store_true", help="play a scripted example conversation and exit")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        kb = KnowledgeBase.from_file(args.faq)
    except DataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    color = (sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ)
    if color and os.name == "nt":
        os.system("")                                  # enables ANSI colours in the Windows console
    style = Style(color)

    if args.demo:
        fixed = datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)      # Wednesday 11:30 AM IST
        bot = Bot(kb, args.name, now_fn=lambda: fixed, local_tz=timezone(timedelta(hours=3), "EAT"))
        run_demo(bot, style)
        return 0

    bot = Bot(kb, args.name)
    run_chat(bot, style, debug=args.debug, echo=not sys.stdin.isatty())
    return 0


if __name__ == "__main__":
    sys.exit(main())
