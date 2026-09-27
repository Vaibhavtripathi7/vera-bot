# Vera bot: magicpin AI Challenge

Vaibhav Tripathi | Live at https://vera-bot-pxq6.onrender.com | Run locally with `uvicorn vera.api:app`

## Approach

I split the bot into two parts. Code makes every decision and checks every fact. A language model is only allowed to polish wording, and its output is thrown away if it adds anything new.

When a trigger arrives, the bot works through five steps:

1. **Collect the facts.** It turns the merchant, category, trigger and customer data into a list of facts, with every calculation done in code (for example, "CTR 1.8% against a 3.0% average for metro solo clinics").
2. **Find what matters.** It compares the merchant with category peers and looks at offer gaps, review themes, lapsed customers, seasonal patterns and the merchant's own signals. The result is a ranked list of observations.
3. **Pick what to send.** At each tick it ranks triggers by urgency, business stakes and fit with the merchant's current state. It respects consent and never sends the same suppression key twice.
4. **Write the message.** Each trigger type has its own playbook, in English or Hindi-English depending on the merchant. Every message follows the same shape: why now, one hard fact, what it means for the merchant, what Vera will do, and a single question at the end.
5. **Check it.** Before anything goes out, a validator confirms that every number and name exists in the input data, and that there are no URLs, banned words, internal jargon or extra calls to action.

Replies follow a simple conversation flow. When the merchant says yes, the bot sends the actual work (a post draft, a checklist, a customer message) instead of asking another question. It then confirms, offers one follow-up and closes. It also handles WhatsApp auto-replies, "call me later", abuse, off-topic requests, opt-outs and a merchant changing a price.

## Tradeoffs

**Templates over free-form generation.** The free Gemini tier allows only a few requests per minute, and one tick can hold 20 triggers. Relying on the model for every message would mean timeouts or made-up numbers. So the templates do the real work, and the model (Gemini Flash-Lite, with Flash as backup) only rewrites trigger types it has never seen, where template wording is weakest. If the model is slow, rate limited or produces anything invalid, the template version goes out.

**Deterministic by default.** The same input always gives the same message on the template path. Model output is cached within a session, so it only varies for new trigger types.

**Honest over clever.** When the data doesn't answer a merchant's question, the bot says it will confirm rather than guessing.

## Results

Scores come from a local copy of the official judge prompt.

- 30 canonical test pairs: 43.4 out of 50 on average.
- Official judge simulator against the live URL: all four scenarios pass, and the scored messages average 44 out of 50.
- A 60-minute simulated test with 113 messages and mid-run injected data: 0 operational penalties, slowest tick under 6 seconds.
- An adversarial test suite of about 1,600 checks (malformed requests, 13 kinds of corrupted input data, hostile model output, prompt injection, restarts, load) passes with 0 failures, both locally and against the live URL.

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
