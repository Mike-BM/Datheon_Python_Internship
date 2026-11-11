"""Tests for chatbot.py.  Run from the project folder with:  python -m pytest -v"""

import io
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chatbot as cb  # noqa: E402

SAMPLE = ROOT / "sample_faq.json"
IST = timezone(timedelta(hours=5, minutes=30), "IST")
EAT = timezone(timedelta(hours=3), "EAT")


def clock(y, m, d, h, mi=0, tz=IST):
    return lambda: datetime(y, m, d, h, mi, tzinfo=tz)


TUE_2PM = clock(2026, 11, 10, 14)        # Tuesday 2 PM IST   -> open
SAT_11AM = clock(2026, 11, 7, 11)        # Saturday           -> closed
FRI_730PM = clock(2026, 11, 6, 19, 30)   # Friday after close -> closed, opens Monday


def kb_sample():
    return cb.KnowledgeBase.from_file(SAMPLE)


def make_bot(now=TUE_2PM, kb=None):
    return cb.Bot(kb or kb_sample(), now_fn=now, local_tz=EAT)


@pytest.fixture
def bot():
    return make_bot()


# Sample store FAQ data to test behavior on a different dataset.
SHOP = {
    "greetings": ["hello", "hi"],
    "faqs": [
        {"question": "How much does shipping cost?", "answer": "Shipping is free over $50, otherwise $5.",
         "keywords": ["postage", "delivery fee"]},
        {"question": "How long does delivery take?", "answer": "Delivery takes 3-7 business days."},
        {"question": "How do I return an item?", "answer": "Start a return from your orders page within 30 days."},
        {"question": "What is your refund policy?", "answer": "Refunds are issued within 5 days of the return arriving."},
        {"question": "How do I reset my password?", "answer": "Use the Forgot password link on the login page."},
        {"question": "Do you ship internationally?", "answer": "Yes, we ship to over 40 countries."},
        {"question": "How can I track my order?", "answer": "Use the tracking link in your confirmation email."},
        {"question": "What are your opening hours?", "answer": "We are open Monday to Saturday, 08:00-17:00 EAT."},
    ],
}


def shop_bot(now=clock(2026, 11, 10, 10, tz=EAT)):
    return cb.Bot(cb.KnowledgeBase.from_data(SHOP), now_fn=now, local_tz=EAT)


def ask(b, text):
    return b.respond(text)


# ---------------------------------------------------------------- text helpers
@pytest.mark.parametrize("raw, expected", [
    ("  What's   your HOURS?? ", "what is your hours??"),
    ("I don't know", "i do not know"),
    ("can't won't", "can not will not"),
])
def test_normalize_basics(raw, expected):
    assert cb.normalize(raw) == expected


def test_normalize_lowercases_and_collapses_whitespace():
    assert "  " not in cb.normalize("Hello\t\tthere\n\nfriend")
    assert cb.normalize("HELLO") == cb.normalize("hello")


def test_stem_merges_word_forms():
    assert cb.stem("hours") == cb.stem("hour")
    assert cb.stem("operate") == cb.stem("operating") == cb.stem("operation")


def test_sanitize_strips_control_characters_and_truncates():
    text, truncated = cb.sanitize("ab\x00c\x07d")
    assert "\x00" not in text and "\x07" not in text and not truncated
    text, truncated = cb.sanitize("x" * (cb.MAX_INPUT_CHARS + 50))
    assert truncated and len(text) == cb.MAX_INPUT_CHARS


def test_split_clauses_on_conjunctions_and_punctuation():
    parts = cb.split_clauses("what are your hours and how do I submit? also certificates")
    assert len(parts) >= 3


def test_has_content():
    assert cb.has_content("hours")
    assert not cb.has_content("?!?")


# ---------------------------------------------------------------- loading data
def test_loads_sample_file():
    kb = kb_sample()
    assert len(kb.faqs) == 3 and "hello" in kb.greetings and not kb.warnings


def test_missing_file_raises_data_error(tmp_path):
    with pytest.raises(cb.DataError, match="not found"):
        cb.KnowledgeBase.from_file(tmp_path / "nope.json")


def test_invalid_json_reports_line_and_column(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"faqs": [ oops ]}')
    with pytest.raises(cb.DataError, match="not valid JSON"):
        cb.KnowledgeBase.from_file(p)


@pytest.mark.parametrize("payload", ["[]", '"text"', "42", "{}", '{"faqs": "x"}', '{"faqs": []}'])
def test_wrong_structure_raises_data_error(tmp_path, payload):
    p = tmp_path / "x.json"
    p.write_text(payload)
    with pytest.raises(cb.DataError):
        cb.KnowledgeBase.from_file(p)


def test_malformed_entries_are_skipped_with_warnings():
    kb = cb.KnowledgeBase.from_data({"faqs": [
        {"question": "How do I reset my password?", "answer": "Use the link."},
        {"question": "", "answer": "x"},
        {"answer": "no question"},
        "just a string",
        {"question": "Only a question"},
    ]})
    assert len(kb.faqs) == 1 and len(kb.warnings) == 4


def test_bom_file_loads(tmp_path):
    p = tmp_path / "bom.json"
    p.write_text(json.dumps(SHOP), encoding="utf-8-sig")
    assert len(cb.KnowledgeBase.from_file(p).faqs) == len(SHOP["faqs"])


def test_directory_path_is_a_data_error(tmp_path):
    with pytest.raises(cb.DataError):
        cb.KnowledgeBase.from_file(tmp_path)


def test_binary_garbage_is_a_data_error(tmp_path):
    p = tmp_path / "bin.json"
    p.write_bytes(b"\xff\xfe\x00\x80\x81")
    with pytest.raises(cb.DataError):
        cb.KnowledgeBase.from_file(p)


def test_optional_keywords_field_is_used():
    b = shop_bot()
    r = ask(b, "what is the postage")
    assert r.faq_ids == (0,)


# ---------------------------------------------------------------- schedule
def test_parse_schedule_from_sample_answer():
    s = cb.parse_schedule("We operate Monday through Friday from 9:00 AM to 6:00 PM IST.")
    assert s is not None and set(s.days) == {0, 1, 2, 3, 4}
    assert s.open.hour == 9 and s.close.hour == 18


def test_parse_schedule_none_when_no_hours():
    assert cb.parse_schedule("Use the Forgot password link.") is None


def test_open_closed_logic_uses_injected_clock():
    s = cb.parse_schedule("We operate Monday through Friday from 9:00 AM to 6:00 PM IST.")
    assert s.is_open(datetime(2026, 11, 10, 14, tzinfo=IST))
    assert not s.is_open(datetime(2026, 11, 10, 8, 59, tzinfo=IST))
    assert not s.is_open(datetime(2026, 11, 10, 18, 0, tzinfo=IST))
    assert not s.is_open(datetime(2026, 11, 7, 11, tzinfo=IST))      # Saturday


def test_open_right_now_answer(bot):
    t = ask(bot, "are you open right now?").text
    assert t.startswith("Yes") and "IST" in t


def test_closed_on_weekend_clock():
    t = ask(make_bot(SAT_11AM), "are you open now?").text
    assert "closed" in t.lower() and "Monday" in t


def test_closed_after_hours_says_when_it_reopens():
    t = ask(make_bot(FRI_730PM), "are you open now?").text
    assert "closed" in t.lower() and "Monday" in t


def test_specific_day_questions(bot):
    assert ask(bot, "are you open on saturday?").text.startswith("No")
    assert ask(bot, "are you open on friday?").text.startswith("Yes")
    assert ask(bot, "are you open on weekends?").text.startswith("No")


def test_plain_hours_question_returns_dataset_answer(bot):
    r = ask(bot, "what are your hours?")
    assert "9:00 AM to 6:00 PM IST" in r.text and r.faq_ids == (0,)


# ---------------------------------------------------------------- greeting / exit / state
def test_opening_message_lists_topics(bot):
    r = bot.opening()
    assert "hours of operation" in r.text and bot.state is cb.State.LISTENING


@pytest.mark.parametrize("word", ["hello", "hi", "hey", "good morning", "Hello!", "HI THERE", "howdy"])
def test_greetings(word):
    r = ask(make_bot(), word)
    assert r.intent == "greeting" and not r.end


def test_greeting_plus_question_answers_the_question(bot):
    r = ask(bot, "hello, when do i get my certificate")
    assert "business days" in r.text and r.text.startswith("Hello")


@pytest.mark.parametrize("word", ["bye", "exit", "quit", "BYE", "  Quit  ", "goodbye", "bye!", "/exit",
                                  "/quit", "/bye", "thanks, bye", "ok bye", "exit please"])
def test_exit_commands_end_the_session(word):
    r = ask(make_bot(), word)
    assert r.end and r.state is cb.State.ENDED


def test_exit_word_inside_a_real_question_does_not_exit(bot):
    r = ask(bot, "how do I exit the program and submit my project")
    assert not r.end


def test_state_transitions(bot):
    assert bot.state is cb.State.GREETING
    ask(bot, "hi")
    assert bot.state is cb.State.LISTENING
    ask(bot, "where can i upload my work")           # medium confidence
    assert bot.state is cb.State.CLARIFYING
    ask(bot, "yes")
    assert bot.state is cb.State.LISTENING
    ask(bot, "bye")
    assert bot.state is cb.State.ENDED


def test_first_message_can_be_a_question_without_greeting(bot):
    r = ask(bot, "how do i submit my project")
    assert r.intent == "faq" and bot.state is cb.State.LISTENING


def test_menu_is_shown_only_once():
    b = make_bot()
    b.opening()
    assert "I can help with" not in ask(b, "hello").text


# ---------------------------------------------------------------- FAQ matching
@pytest.mark.parametrize("question, faq_id", [
    ("What are your hours of operation?", 0),
    ("How do I submit my internship project?", 1),
    ("When will I receive my certificate?", 2),
    ("what time do you open", 0),
    ("opening times", 0),
    ("how do i submit", 1),
    ("certificate", 2),
    ("how many days for certificate", 2),
    ("wher do i upload my intership project", 1),
    ("WHEN WILL I GET MY CERTIFICATE???", 2),
    ("  submit   my   project  ", 1),
])
def test_sample_paraphrases(question, faq_id):
    r = ask(make_bot(), question)
    assert r.faq_ids == (faq_id,), (question, r.intent, r.confidence)


def test_multi_intent_returns_both_answers(bot):
    r = ask(bot, "what are your hours and when do I get my certificate?")
    assert r.faq_ids == (0, 2) or set(r.faq_ids) == {0, 2}
    assert "9:00 AM" in r.text and "business days" in r.text


def test_three_intents_in_one_message(bot):
    r = ask(bot, "hours and submit and certificate")
    assert set(r.faq_ids) == {0, 1, 2}


def test_partial_answer_notes_what_was_not_understood(bot):
    r = ask(bot, "when are you open and how do i get a refund")
    assert 0 in r.faq_ids and "couldn't find an answer" in r.text


@pytest.mark.parametrize("junk", [
    "how do I get a refund?", "tell me a joke", "what's the weather", "asdfghjkl",
    "how much does it cost", "qwertyuiop zxcvbnm", "purple monkey dishwasher",
])
def test_out_of_scope_falls_back(junk):
    r = ask(make_bot(), junk)
    assert r.intent == "fallback" and not r.faq_ids
    assert "hours of operation" in r.text            # it lists what it can do


def test_medium_confidence_asks_to_confirm_then_answers(bot):
    r = ask(bot, "where can i upload my work")
    assert r.intent == "clarify" and "did you mean" in r.text.lower()
    r = ask(bot, "yes")
    assert r.faq_ids == (1,) and "GitHub" in r.text


def test_clarify_no_returns_to_listening_and_offers_topics(bot):
    ask(bot, "where can i upload my work")
    r = ask(bot, "no")
    assert bot.state is cb.State.LISTENING and "rephrase" in r.text


@pytest.mark.parametrize("answer", ["yes", "y", "yeah", "yep", "correct", "sure", "ok yes", "1"])
def test_clarify_accepts_many_yes_forms(answer):
    b = make_bot()
    ask(b, "where can i upload my work")
    assert ask(b, answer).faq_ids == (1,)


def test_clarify_a_new_question_replaces_the_pending_one(bot):
    ask(bot, "where can i upload my work")
    r = ask(bot, "what are your hours")
    assert r.faq_ids == (0,)


def test_typo_tolerance():
    for q in ["wat are yuor hurs", "certifcate", "submitt my proyect"]:
        r = ask(make_bot(), q)
        assert r.faq_ids or r.intent == "clarify", q


def test_unknown_words_do_not_cause_false_answers():
    r = ask(make_bot(), "my cat likes tuna")
    assert r.intent == "fallback"


def test_confidence_bands_are_consistent():
    r = ask(make_bot(), "how do i submit my internship project")
    assert r.confidence >= cb.HIGH
    r = ask(make_bot(), "asdf")
    assert r.confidence < cb.MEDIUM


# ---------------------------------------------------------------- fallback escalation
def test_fallback_messages_escalate_and_reset(bot):
    texts = [ask(bot, f"blargh zork {i}").text for i in range(4)]
    assert len(set(texts[:3])) == 3                       # three different messages
    assert "hours of operation" in texts[0]
    ask(bot, "what are your hours")                        # success resets the streak
    assert bot.fallback_streak == 0


# ---------------------------------------------------------------- built-in intents
def test_help_and_topics(bot):
    for word in ["help", "/help", "topics", "/topics", "what can you do", "what can i ask"]:
        assert "hours of operation" in ask(bot, word).text


def test_identity_and_human_and_frustration(bot):
    assert ask(bot, "who are you").intent == "identity"
    assert ask(bot, "I want to talk to a human").intent == "human"
    assert ask(bot, "this is useless").intent == "frustration"


def test_thanks_does_not_end(bot):
    r = ask(bot, "thank you")
    assert r.intent == "thanks" and not r.end


def test_name_memory(bot):
    assert "Brian" in ask(bot, "my name is Brian").text
    assert "Brian" in ask(bot, "bye").text


def test_time_question(bot):
    t = ask(bot, "what time is it").text
    assert "2:00 PM IST" in t


def test_repeat_before_and_after_answer(bot):
    assert "haven't said anything" in ask(bot, "repeat that").text
    first = ask(bot, "what are your hours").text
    assert first in ask(bot, "say that again").text or "9:00 AM" in ask(bot, "say that again").text


def test_reset_command(bot):
    ask(bot, "my name is Brian")
    ask(bot, "/reset")
    assert bot.user_name == ""


def test_custom_bot_name():
    b = cb.Bot(kb_sample(), name="Datheon Assistant")
    assert "Datheon Assistant" in b.opening().text


# ---------------------------------------------------------------- robustness
@pytest.mark.parametrize("text", ["", " ", "\n\n\t", "\x00\x01\x02", "!!!", "?", "...", "😀😀", "——", "(*&^%$#@!)"])
def test_empty_or_symbol_only_input(text):
    r = ask(make_bot(), text)
    assert not r.end and r.text


def test_non_english_text_is_handled():
    r = ask(make_bot(), "¿dónde envío mi proyecto? 你好")
    assert r.intent in ("fallback", "clarify") and not r.end


def test_very_long_input_is_truncated_with_notice():
    r = ask(make_bot(), "hours " * 1000)
    assert not r.end and "first 500 characters" in r.text


def test_one_enormous_word_is_fast():
    import time
    t0 = time.time()
    ask(make_bot(), "a" * 100_000)
    assert time.time() - t0 < 2


def test_injection_like_text_is_only_text(bot):
    for t in ["__import__('os').system('echo hi')", "{{7*7}}", "'; DROP TABLE faqs;--", "%s %d %(x)s", "\\x00\\n"]:
        assert not ask(bot, t).end


def test_respond_never_raises_on_random_text():
    rng = random.Random(7)
    alphabet = "abcdefghijklmnopqrstuvwxyz     ?!.,'\"-é你😀\t\n/0123456789"
    b = make_bot()
    for _ in range(3000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        r = b.respond(s)
        assert isinstance(r.text, str)
        if r.end:
            b = make_bot()


def test_long_session_is_stable(bot):
    for i in range(1000):
        ask(bot, ["what are your hours", "blargh", "where can i upload", "yes", "no"][i % 5])
    assert bot.state is not cb.State.ENDED


# ---------------------------------------------------------------- alternate FAQ dataset tests
@pytest.mark.parametrize("question, faq_id", [
    ("how much is shipping", 0), ("what is the delivery fee", 0), ("how long will delivery take", 1),
    ("how to return an item", 2), ("what is your refund policy", 3),
    ("i forgot my password", 4), ("reset password", 4), ("do you ship internationally", 5),
    ("where is my order", 6), ("track my package", 6), ("are you open on saturdays", 7),
])
def test_other_faq_paraphrases(question, faq_id):
    r = ask(shop_bot(), question)
    assert faq_id in r.faq_ids or r.intent == "clarify", (question, r.intent, r.faq_ids)


@pytest.mark.parametrize("question", ["what is the capital of france", "can i pay with bitcoin", "tell me a joke",
                                      "do you sell dragons", "my cat likes tuna"])
def test_other_faq_out_of_scope(question):
    assert not ask(shop_bot(), question).faq_ids or ask(shop_bot(), question).intent == "clarify"
    assert ask(shop_bot(), question).intent in ("fallback", "clarify")      # never a confident wrong answer


def test_shop_multi_intent():
    r = ask(shop_bot(), "how much is shipping and how do I reset my password")
    assert set(r.faq_ids) == {0, 4}


def test_shop_hours_use_own_timezone():
    t = ask(shop_bot(), "are you open now?").text
    assert t.startswith("Yes") and "EAT" in t
    sunday = ask(shop_bot(clock(2026, 11, 8, 10, tz=EAT)), "are you open now?").text
    assert "closed" in sunday.lower() and "Monday" in sunday or "tomorrow" in sunday


def test_two_nearly_identical_faqs_do_not_crash_and_clarify_or_answer():
    kb = cb.KnowledgeBase.from_data({"faqs": [
        {"question": "How do I cancel my subscription?", "answer": "A"},
        {"question": "How do I pause my subscription?", "answer": "B"}]})
    r = cb.Bot(kb).respond("subscription")
    assert r.intent in ("faq", "clarify")


def test_scales_to_hundreds_of_faqs():
    import time
    faqs = [{"question": f"What is the policy about widget model {i} warranty?", "answer": f"Answer {i}"}
            for i in range(300)]
    kb = cb.KnowledgeBase.from_data({"faqs": faqs})
    b = cb.Bot(kb)
    t0 = time.time()
    for _ in range(20):
        b.respond("warranty for widget model 17")
    assert (time.time() - t0) / 20 < 0.25


# ---------------------------------------------------------------- run loop and CLI
def run_with(inputs, **kw):
    it = iter(inputs)

    def fake(_prompt):
        try:
            v = next(it)
        except StopIteration:
            raise EOFError
        if isinstance(v, BaseException):
            raise v
        return v
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        cb.run_chat(make_bot(), cb.Style(False), input_fn=fake, **kw)
    finally:
        sys.stdout = old
    return out.getvalue()


def test_run_loop_full_conversation():
    out = run_with(["hello", "what are your hours", "bye"])
    assert "9:00 AM to 6:00 PM IST" in out and "Goodbye" in out


def test_run_loop_handles_eof_cleanly():
    assert "Goodbye" in run_with(["hello"])


def test_run_loop_handles_ctrl_c_cleanly():
    out = run_with(["hello", KeyboardInterrupt()])
    assert "goodbye" in out.lower()


def test_run_loop_survives_internal_errors(monkeypatch):
    b = make_bot()
    calls = {"n": 0}
    real = b.respond

    def flaky(raw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(raw)
    monkeypatch.setattr(b, "respond", flaky)
    it = iter(["x", "bye"])
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        cb.run_chat(b, cb.Style(False), input_fn=lambda _p: next(it))
    finally:
        sys.stdout = old
    assert "something went wrong" in out.getvalue() and "Goodbye" in out.getvalue()


def test_debug_mode_shows_confidence():
    out = run_with(["what are your hours", "bye"], debug=True)
    assert "confidence=" in out and "intent=faq" in out


def test_main_exit_code_2_for_missing_faq(capsys):
    assert cb.main(["--faq", "/no/such/file.json", "--no-color"]) == 2
    assert "not found" in capsys.readouterr().err


def test_main_exit_code_2_for_bad_json(tmp_path, capsys):
    p = tmp_path / "b.json"
    p.write_text("{nope")
    assert cb.main(["--faq", str(p)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_demo_runs_and_is_deterministic(capsys):
    assert cb.main(["--demo", "--no-color"]) == 0
    first = capsys.readouterr().out
    cb.main(["--demo", "--no-color"])
    assert first == capsys.readouterr().out
    assert "Yes, we're open right now" in first and "closed on Saturdays" in first and "Goodbye" in first


def test_colors_off_when_not_a_tty():
    assert cb.Style(False).bot("x") == "x" and "\x1b" in cb.Style(True).bot("x")


def test_render_wraps_long_lines_and_has_no_trailing_whitespace():
    out = cb.render("word " * 60 + "\n\n- item\n  detail", cb.Style(False))
    assert all(len(line) <= 90 for line in out.splitlines())
    assert all(line == line.rstrip() for line in out.splitlines())


def test_piped_stdin_end_to_end():
    import subprocess
    p = subprocess.run([sys.executable, str(ROOT / "chatbot.py"), "--no-color"],
                       input="hello\nwhat are your hours?\nbye\n", capture_output=True, text=True, timeout=20)
    assert p.returncode == 0 and "9:00 AM to 6:00 PM IST" in p.stdout and "Goodbye" in p.stdout


def test_piped_stdin_ends_without_bye():
    import subprocess
    p = subprocess.run([sys.executable, str(ROOT / "chatbot.py"), "--no-color"],
                       input="hello\n", capture_output=True, text=True, timeout=20)
    assert p.returncode == 0 and "Goodbye" in p.stdout


def test_synonym_only_matches_do_not_drag_in_extra_answers():
    r = ask(shop_bot(), "where is my parcel")
    assert r.faq_ids == (6,)


def test_evaluation_never_gives_a_confident_wrong_answer():
    """Regression guard on the independent evaluation sets (see tests/evaluate.py)."""
    sys.path.insert(0, str(ROOT / "tests"))
    import evaluate
    for kb, i, o in [(kb_sample(), evaluate.SAMPLE_IN, evaluate.SAMPLE_OUT),
                     (cb.KnowledgeBase.from_data(SHOP), evaluate.SHOP_IN, evaluate.SHOP_OUT)]:
        tally, _ = evaluate.run(kb, i, o)
        assert tally["wrong_answer"] == 0 and tally["leaked"] == 0
        assert (tally["answered_right"] + tally["clarified_right"]) / len(i) >= 0.75


@pytest.mark.parametrize("weak", ["github", "student workspace", "review", "code review", "business days"])
def test_words_found_only_in_answers_are_weak_evidence(weak):
    """A word that appears only inside an answer must not trigger a confident reply."""
    r = ask(make_bot(), weak)
    assert r.intent == "fallback"


def test_typo_correction_is_conservative():
    kb = kb_sample()
    assert kb.analyze("certifcate")[0].fuzzy                      # a real typo is repaired
    assert kb.analyze("yuor") == []                                # misspelt filler word is dropped
    for unrelated in ["hotel", "mouse", "tuna", "pencil"]:
        assert not kb.analyze(unrelated)[0].fuzzy, unrelated      # unrelated words stay as they are
