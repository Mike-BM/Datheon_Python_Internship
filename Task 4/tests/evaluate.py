"""Evaluation script to measure query matching accuracy across test queries.

Run from the project folder:
    python tests/evaluate.py
"""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import chatbot as cb  # noqa: E402
from test_chatbot import SHOP  # noqa: E402

# Test queries and expected FAQ IDs for the sample dataset
SAMPLE_IN = [
    ("when are you guys open", 0), ("what time do you close", 0), ("are your offices open on weekdays", 0),
    ("working hours please", 0), ("what are the business timings", 0), ("hours?", 0),
    ("till what time can I contact you", 0), ("opening hours", 0), ("when can i reach support", 0),
    ("how can I send in my project", 1), ("where do I put my github link", 1), ("submit project", 1),
    ("i finished my internship project, how do i hand it in", 1), ("how to turn in my work", 1),
    ("where is the student workspace", 1), ("upload my repository", 1), ("how do i submit my github repo", 1),
    ("my project is ready, what now", 1), ("how do I give you my project link", 1),
    ("when do certificates come out", 2), ("how long for my certificate", 2), ("certificate timeline", 2),
    ("when will i get my internship certificate", 2), ("how many days until I receive the certificate", 2),
    ("do you issue certificates after code review", 2), ("when is my cert ready", 2),
    ("how soon is the certificate sent", 2), ("i want my certificate", 2), ("certificate delivery time", 2),
]
SAMPLE_OUT = [
    "how do I get a refund", "what is the stipend", "can I extend my internship", "do you offer jobs",
    "what programming languages should I learn", "who is the CEO", "tell me a joke", "what is the weather today",
    "how do I reset my password", "where is your office located", "how much does the program cost",
    "can I change my project topic", "what is the capital of Kenya", "play some music", "i love pizza",
    "how do I cook rice", "can you help me with my homework", "what is python", "do you have a mobile app",
    "how do I delete my account", "is the internship paid", "which companies hire interns",
    "what is 2 plus 2", "sing me a song", "how are the stock markets doing", "I lost my laptop",
    "when is the next cohort", "who won the match last night", "translate hello to french", "blorp frindle wump",
]
SHOP_IN = [
    ("what does delivery cost", 0), ("is shipping free", 0), ("how fast will my order arrive", 1),
    ("how many days does shipping take", 1), ("i want to send an item back", 2), ("can I return what I bought", 2),
    ("how do i get my money back", 3), ("refund policy", 3), ("forgot my password", 4), ("change my password", 4),
    ("do you ship abroad", 5), ("do you deliver to europe", 5), ("where is my parcel", 6),
    ("how do I follow my shipment", 6), ("when do you open", 7), ("what are your working hours", 7),
]
SHOP_OUT = ["do you sell gift cards", "how do I become a seller", "what is the best laptop", "can I pay with bitcoin",
            "do you have a store in paris", "tell me about your company history", "what is the meaning of life",
            "how do I delete my account"]


def run(kb, in_scope, out_scope):
    tally, bad = Counter(), []
    for q, want in in_scope:
        r = cb.Bot(kb).respond(q)
        if r.intent in ("faq", "hours_status") and r.faq_ids:
            ok = want in r.faq_ids and (len(r.faq_ids) == 1)
            tally["answered_right" if ok else "wrong_answer"] += 1
            if not ok:
                bad.append(("WRONG", q, r.faq_ids))
        elif r.intent == "clarify":
            ids = r.faq_ids
            ok = bool(ids) and ids[0] == want
            tally["clarified_right" if ok else "missed"] += 1
            if not ok:
                bad.append(("MISS(clarify)", q, ids))
        else:
            tally["missed"] += 1
            bad.append(("MISS", q, r.intent))
    for q in out_scope:
        r = cb.Bot(kb).respond(q)
        if r.intent in ("faq", "hours_status"):
            tally["out_scope_answered"] += 1
            bad.append(("UNEXPECTED_ANSWER", q, r.faq_ids))
        elif r.intent == "clarify":
            tally["out_scope_clarify"] += 1
            bad.append(("CLARIFIED_OUT_SCOPE", q, r.faq_ids))
        else:
            tally["rejected"] += 1
    return tally, bad


def report(name, kb, i, o):
    t, bad = run(kb, i, o)
    n_in, n_out = len(i), len(o)
    good_in = t["answered_right"] + t["clarified_right"]
    print(f"\n{name}: {n_in} in-scope, {n_out} out-of-scope")
    print(f"  in-scope  correct answer          : {t['answered_right']:>3}/{n_in}")
    print(f"  in-scope  correct clarification   : {t['clarified_right']:>3}/{n_in}")
    print(f"  in-scope  addressed (either)      : {good_in:>3}/{n_in}  ({100 * good_in / n_in:.0f}%)")
    print(f"  in-scope  wrong answer            : {t['wrong_answer']:>3}/{n_in}")
    print(f"  in-scope  unmatched (fallback)    : {t['missed']:>3}/{n_in}")
    print(f"  out-of-scope correctly unhandled  : {t['rejected']:>3}/{n_out}")
    print(f"  out-of-scope clarification asked  : {t['out_scope_clarify']:>3}/{n_out}")
    print(f"  out-of-scope answered unexpectedly: {t['out_scope_answered']:>3}/{n_out}")
    for b in bad:
        print("   ", *b)


if __name__ == "__main__":
    report("Sample FAQ (3 entries)", cb.KnowledgeBase.from_file(ROOT / "sample_faq.json"), SAMPLE_IN, SAMPLE_OUT)
    report("Shop FAQ (8 entries)", cb.KnowledgeBase.from_data(SHOP), SHOP_IN, SHOP_OUT)
