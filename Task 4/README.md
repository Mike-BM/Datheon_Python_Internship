# Support Assistant Chatbot - Task 4

A terminal-based FAQ support assistant built in Python using standard library modules (`re`, `json`, `difflib`, `datetime`, etc.) with no external runtime dependencies.

The assistant reads question-and-answer pairs from a JSON file, handles messy user phrasing, answers multiple questions within a single message, asks for clarification when unsure, and answers live operating hour queries based on the schedule defined in the FAQ.

---

## Quick Start

```bash
# Start the interactive assistant
python chatbot.py

# Run a scripted demonstration
python chatbot.py --demo

# Run with a custom FAQ file or custom name
python chatbot.py --faq sample_faq.json --name "Support Assistant"

# Run with debug output (shows intent and confidence scores)
python chatbot.py --debug

# Run unit tests
python -m pytest -v

# Run the query evaluation script
python tests/evaluate.py
```

To exit the chat, type `bye`, `exit`, `quit`, or press `Ctrl+C`.

---

## Example Conversation (`--demo`)

```text
============================================================
 Support Assistant   type 'help' for topics, 'bye' to exit
============================================================
Bot > Hello! I'm Support Assistant. I can answer questions about:
      - What are your hours of operation?
      - How do I submit my internship project?
      - When will I receive my certificate?
      Ask in your own words, or type 'help'. Say 'bye' when you're done.
You > hello
Bot > Hi there! What would you like to know?
You > what are your hours and when do I get my certificate?
Bot > Here's what I found:

      - What are your hours of operation?
        We operate Monday through Friday from 9:00 AM to 6:00 PM IST.

      - When will I receive my certificate?
        Certificates are reviewed and issued within 3-5 business days after your code
        review.
You > wher do i upload my intership project
Bot > You can submit your public GitHub repository link through your Student Workspace.
You > are you open right now?
Bot > Yes, we're open right now. It's 11:30 AM IST, and we close at 6:00 PM IST (3:30 PM
      your time) - in 6h 30m.
You > are you open on saturday?
Bot > No - we're closed on Saturdays. Our regular hours are Monday to Friday, 9:00 AM to
      6:00 PM IST.
You > where can i upload my work?
Bot > I'm not completely sure, but did you mean:
        "How do I submit my internship project?"
      (yes / no)
You > yes
Bot > You can submit your public GitHub repository link through your Student Workspace.
You > how do I get a refund?
Bot > Sorry, I don't have an answer for that. I can help with:
      - What are your hours of operation?
      - How do I submit my internship project?
      - When will I receive my certificate?
      Try rephrasing, or pick one of those.
You > thanks, bye
Bot > You're welcome! Goodbye! Thanks for chatting - come back any time.
```

---

## Features & Implementation

1. **Input Normalization**:
   - Converts input to lowercase, trims whitespace, removes control characters, expands contractions (e.g., *don't* &rarr; *do not*), and removes repeated characters (*helloooo* &rarr; *hello*).
   - Applies basic stemming (e.g., *operating* &rarr; *operate*, *hours* &rarr; *hour*) and strips common stopwords.

2. **Synonym Matching & Typo Correction**:
   - Groups related terms (e.g., *upload / submit / send / hand in*, *cert / certificate*, *timings / hours*).
   - Uses `difflib.get_close_matches` and character transposition checks to recover from common spelling mistakes.

3. **Multi-Clause & Compound Question Support**:
   - Splits complex inputs by conjunctions and punctuation (*and*, *also*, commas, question marks).
   - Matches each part individually so users can ask multiple questions in a single sentence (e.g., *"what are your hours and how do I submit my project?"*).

4. **Confidence-Based Decision Flow**:
   - **High confidence**: Answers the question directly.
   - **Medium confidence**: Asks a clarification question (*"did you mean...?"*) and waits for confirmation (*yes*, *no*, or option number).
   - **Low confidence / Unmatched**: Politely indicates the question is not recognized and lists available topics.

5. **Schedule & Operating Hours Lookup**:
   - Automatically detects business hours mentioned in the FAQ answer (days of week, opening/closing times, and timezones like IST, EAT, UTC).
   - Resolves dynamic questions such as *"are you open right now?"* or *"are you open on Saturday?"* comparing against the current clock and local time.

6. **Conversation State & Commands**:
   - Remembers the user's name (*"my name is Brian"*).
   - Supports repeating answers (*"repeat that"*), small talk (*"how are you"*), and help prompts (*"topics"*).
   - Built-in slash commands: `/help`, `/topics`, `/reset`, `/exit`.

---

## FAQ File Format

FAQ data is stored in a clean JSON format:

```json
{
  "greetings": ["hello", "hi", "hey"],
  "faqs": [
    {
      "question": "What are your hours of operation?",
      "answer": "We operate Monday through Friday from 9:00 AM to 6:00 PM IST.",
      "keywords": ["timings", "opening times", "schedule"]
    },
    {
      "question": "How do I submit my internship project?",
      "answer": "You can submit your public GitHub repository link through your Student Workspace."
    }
  ]
}
```

- `keywords` is optional and helps provide additional search triggers for questions that might be phrased differently.
- Missing or malformed files display clear error messages and exit with status code 2.

---

## Testing & Verification

- **Automated Tests (`pytest`)**:
  - Run with `python -m pytest -v`.
  - Comprehensive unit test suite covering text normalization, data loading errors, scheduling logic, multi-intent matching, clarification flows, edge cases (empty strings, special symbols, long inputs), and conversation lifecycle.

- **Evaluation Script (`tests/evaluate.py`)**:
  - Run with `python tests/evaluate.py`.
  - Tests various query phrasings (both in-scope questions and out-of-scope queries) against the knowledge base to ensure high accuracy and proper handling of unknown topics.
