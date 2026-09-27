# Vera bot: magicpin AI Challenge

Vaibhav Tripathi | Live at https://vera-bot-pxq6.onrender.com | Run locally with `uvicorn vera.api:app`

## The idea in one line

Code decides and checks every fact; a language model is allowed to polish wording and nothing else.

I started from what the judge punishes hardest: invented numbers, generic copy and broken conversations. Every design choice below follows from that.

## How a message is built

1. **Fact sheet.** The four inputs (category, merchant, trigger, customer) become a list of facts, each tagged with where it came from, for example `merchant.performance.ctr`. All arithmetic happens here: gaps against peers, days to a deadline, percentage formatting. The model never does maths.
2. **Insight engine.** It compares the merchant with category benchmarks (CTR, calls, views, retention), then looks at expired or missing offers, negative review themes, lapsed customers, stale Google posts, seasonal patterns and the merchant's own signals. Each insight gets a strength score, and facts the judge can see directly (views, calls, CTR, signals, active offers) rank higher than facts it cannot.
3. **Trigger plus state.** A trigger alone is rarely enough. 75 of the 100 dataset triggers arrive with an empty payload, so the bot links the event to the merchant's most useful gap. A performance spike at a pharmacy with no delivery set up becomes "the extra traffic can't convert yet, let me switch delivery on", instead of a generic congratulation.
4. **Playbooks.** There are 30 trigger families, each written in English and Hindi-English, with several phrasings per slot chosen deterministically by trigger id. Every message has the same spine: why now, one hard fact, what it means, what Vera will do, one question at the end. Customer messages come from the merchant, stay within the customer's consent, and quote a real line from the category's customer content as the reason to reply.
5. **Validator.** Before anything is sent, every number and name is extracted, normalised (0.021 and 2.1% are the same fact, as are 2,100 and 2100) and checked against the fact sheet. A single unknown number rejects the message. The same gate blocks URLs, non-Latin script, category taboo words, internal jargon and extra calls to action. Offers are ranked so service and price offers ("Dental Cleaning @ ₹299") always come before percentage discounts, and names or offers that look like prompt injection are ignored. Model rewrites are also rejected if they introduce a percentage discount or read like a copy of the published case studies.

The rationale on every message is structured the same way: why now, the anchors used, the persuasion lever, and the guardrails applied. The judge can check the message against it line by line.

## Choosing what to send

At each tick, triggers are ranked by urgency, business stakes and fit with the merchant's state. The bot trusts the judge's list of active triggers instead of its own expiry clock (the dataset's dates and the judge's clock don't match), sends at most three merchant-facing messages per merchant per tick, never repeats a suppression key, and rewrites or drops a message that would be a near duplicate of one already sent. A customer's STOP silences that customer only, never the merchant.

## Conversations

Replies go through a small state machine rather than a prompt:

- **Yes** gets the actual work (a post draft, a compliance checklist, a customer note, a booking), then a confirmation, one follow-up and a clean close. It never asks another qualifying question.
- **Auto-replies** are detected by pattern and by repetition across conversations for the same merchant: one nudge, then wait, then stop.
- **Changes** the merchant asks for are applied. "₹120 for 25+" updates that tier in the draft.
- **Abuse** gets one apology and a way to opt out. Off-topic asks (GST, loans) get an honest pointer to the right person and a return to the task. STOP ends it.
- Every reply passes its own safety check, so the conversation cannot introduce a number that isn't in the data or the merchant's own words.

## The language model, and why it is small

The free Gemini tier allows 5 to 15 requests a minute per model, while a single tick can list 20 triggers. So the templates are the product. The model (Gemini 3.5 Flash-Lite first, Flash as backup) only rewrites trigger types the bot has never seen, three messages per request, under a hard 7.5 second tick budget with a 4.5 second limit per attempt. Each model has its own rate counter; a model that times out or hits its quota is skipped for a while. The rewrite may not add a number, get longer, or add a question, and if it fails any check the template goes out. The template path is fully deterministic.

## How I tested it

- **A copy of the official judge prompt** scores the 30 canonical pairs at 43.4 out of 50 (specificity 8.7, category fit 9.0, merchant fit 8.9, decision quality 8.6, engagement 8.2).
- **The official judge simulator** against the live URL: warmup, auto-reply, intent and hostile scenarios all pass; scored messages average 44 out of 50.
- **A 60-minute simulated run** with 113 messages and data injected mid-test (new digest items, changed performance, new customers, trigger types never seen before): 0 operational penalties.
- **An adversarial suite of about 1,600 checks**: malformed and oversized requests, every trigger under 13 kinds of corrupted data (missing owner, empty category, unseen category, unseen language, prompt injection in names), hostile model output, a model that hangs, restarts mid-session and concurrent load. 0 failures, locally and against the live URL.
- **Merchants played by a language model**, in Hinglish, haggling, abusive, confused and one-word personas. Every failure it found became a regression test.

Operationally: request bodies are parsed without relying on headers, a stale context version returns 409, state is written through to SQLite, a new judge run is detected and starts clean, and the README is served at `/README.md`.

## What extra context would help most

- The merchant's real appointment calendar, so the bot can offer actual free slots.
- When each merchant usually replies, so messages arrive at the right time.
- The approved WhatsApp template list.
- A history of which past messages each merchant replied to, so the bot can learn what works for them.

## Running and testing

```bash
uv sync
uv run uvicorn vera.api:app --port 8080        # start the bot
uv run pytest -q                                # unit tests and the adversarial suite
uv run python -m eval.harness                   # 60-minute simulated judge run
uv run python bot.py                            # writes submission.jsonl
```
