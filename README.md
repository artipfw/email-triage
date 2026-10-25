# Bayline email triage agent

Reads a shared business inbox and works each email automatically.
Five categories: sales inquiry, new order, order status, support, spam/suspicious.

Pure Python standard library (3.9+). No pip installs. Talks only to your LOCAL Ollama.

## Design rule
The model only reads and writes language (classify, extract fields, draft wording).
Ordinary code does everything that matters: stock checks, order creation, routing, approvals.
Nothing is ever sent automatically. Every outbound message waits in an outbox for approval.

```
email -> security rules -> classify -> extract -> handler (code) -> outbox -> approve/reject
              |                                        |
        quarantine (injection,                  auto | needs_approval | escalate
        lookalike domain, bank-detail                   route | reply | archive | quarantine
        change, phishing, risky files)
```

## Run it (on the GB10)
```bash
cd bayline-triage
export TRIAGE_MODEL=nemotron-3-nano:30b      # any model you pulled with ollama
python3 -m src.cli check                     # reaches Ollama? model present? one test call
python3 -m src.cli eval                      # all 30 emails, prints accuracy
python3 -m src.cli run                       # process for real (state + outbox)
python3 -m src.cli outbox                    # review drafts
python3 -m src.cli approve all               # simulated send; confirms linked orders
python3 -m src.cli status
python3 -m src.cli reset                     # wipe state between demo takes
```
No model yet? `LLM_MOCK=1 python3 -m src.cli eval` returns gold labels to test the plumbing.
Ollama on another host/port: `export OLLAMA_HOST=http://HOST:11434`.

## Always-on mode
```bash
python3 -m src.cli watch --interval 5
```
Drop `.eml` or `.json` files into `data/inbox/` and they are processed within seconds.
(`.json` format: `{"from": "...", "subject": "...", "body": "..."}`.)

## For the OpenClaw agent
See `agent_instructions.md`. Every command has `--json` output so the agent can relay results.

## Files
- `data/products.csv`, `accounts.csv`, `orders_seed.json` – the fake company's systems
- `data/emails.json` – 30 test emails with expected category and expected decision
- `src/triage.py` – prompts, schemas, security rules, handlers (start here)
- `src/store.py` – state, SKU matching, orders, outbox
- `src/llm.py` – Ollama client
- `src/cli.py` – commands
- `gen_data.py` – regenerates the data; edit it to change the company or add emails

## Tuning the policy
In `src/triage.py`: `SMALL_ORDER_LIMIT` (auto-approve ceiling), `CONF_FLOOR` (min classifier confidence),
the regexes in the security layer, and the prompts. Decisions: auto, needs_approval, escalate,
route, reply, archive, quarantine.

## Known limits (be honest in the writeup)
- Replies are simulated sends; nothing connects to a real mailbox or SMTP.
- SKU matching by description is simple word overlap; ambiguous matches escalate rather than guess.
- Stock is a single local ledger; no concurrency control.
- Security rules are regex heuristics plus domain similarity, not a full fraud system.
