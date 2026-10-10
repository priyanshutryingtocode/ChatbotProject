# Order Status Assistant

A LangChain-powered Streamlit app for checking order status in natural language.

## How it works

Order data is only released after the customer supplies two matching identity
fields: an order number plus their email, phone number, or name on the order.
Every field they give must resolve to the same order, so one known detail can
never expose anyone's records.

Two paths handle a turn. A verified lookup is answered deterministically from
the database — status, tracking, and dates are formatted from actual rows, so
the model can never invent them. Anything unclassified falls through to a Gemini
agent with a server-verified `get_order_status` tool and `search_policies` tool
for public policy questions. Because
verification is enforced in the tool and not the prompt, a prompt injection
still cannot reach unverified data.

Verification state is collected across messages, so `order 42` followed by
`my email is …` works, and a customer can abandon the flow with `skip`. Five
failed attempts start a cooldown.

The package is layered: `config`, `state` and `routing` provide setup and pure
logic; `orders` and `retriever` provide read services; `tools` adapts those
services for LangChain; `agent` assembles the read/action tools;
`conversations` owns redacted transcript persistence; and `chat` coordinates
routing, verification, request staging, and response handling.

The current project environment was checked with Python 3.12.5,
LangChain 1.4.4, `langchain-core` 1.6.9, `langchain-google-genai` 4.4.0,
`google-genai` 2.29.0, Streamlit 1.65.0, and Supabase 2.32.0. LangGraph 1.2.14
is an indirect dependency of LangChain; this project does not implement graphs.

## Order change requests

Return and cancellation requests do not change order state. The agent can stage
one only after recent email or phone verification for that exact order. A
separate Streamlit confirmation submits a support request to the `action_requests`
table for human review. The enabled action tools are `request_return` and
`cancel_order`; neither changes order state. A general `escalate_to_human` tool
and a staff-facing review console are not implemented. Eligibility rules and an order-management
integration are not defined, so the app never claims that a return, refund, or
cancellation has been completed. The request table is not connected to a staff
notification channel yet, and request retention has not been defined. Pending
confirmation is held in server-side session state for five minutes and is not
restored from a saved transcript.

Migration `009_action_requests.sql` adds the server-only request table. Apply it
after migrations `001`-`008` before confirmed submissions can succeed. The app
does not apply migrations or submit requests automatically.

## Features

- **Two-field verification** — order number plus one identity detail, matched
  across one message or several. Customer names are matched literally: `%` and
  `_` are stripped rather than treated as wildcards, so a name can never stand
  in for "any customer"
- **Deterministic order facts** — statuses, tracking numbers and dates come from
  the database, never from the model
- **Privacy-minimized history** — verification details and order responses are
  represented by placeholders in persisted transcripts; conversations are
  purged after 30 days
- **Policy answers via RAG** — returns, refunds, shipping, cancellations and
  damaged-item questions answered from vector-retrieved policy docs with source
  citations. Policies are public, so no verification is needed for them
- **Quick lookup sidebar** — direct database search under the same two-field
  rule, plus a recent-chats list showing only this browser session's
  conversations. That scoping is by session, not by sign-in: it prevents one
  visitor from reading another's history, but anyone holding a given session id
  can still read that session. Real access control needs an authenticated owner.
- **Streaming replies** — conversational replies stream; tool-backed answers
  wait for tool outcomes before display so verification can gate the response

## Project structure

```
main.py                Streamlit entrypoint
orderbot/              Application package
  config.py              Env loading, LLM init, system prompt, constants
  state.py               Session verification state + attempt guard
  routing.py             Message-classification vocabulary and predicates
  database.py            Supabase queries and relational data formatting
  orders.py              Typed order lookup use case shared by every entry point
  privacy.py             Transcript redaction before persistence
  retriever.py           Policy RAG over pgvector
  responses.py           Deterministic database-backed reply formatting
  identity.py            Order and customer identifier extraction
  order_context.py       Query-relevant order context for model prompts
  tools.py               LangChain read-only tool adapters
  agent.py               LangChain create_agent factory
  actions.py             Confirmed support-request persistence
  conversations.py       Redacted transcript persistence
  chat.py                Routing state machine and chat application service
  sidebar.py             Sidebar quick-lookup UI
static/app.css         App stylesheet
scripts/               CLI tooling, run as `python scripts/<name>.py`
  seed_orders.py         Faker-based synthetic data generator
  seed_knowledge.py      Markdown chunker + pgvector ingest
  evaluate_rag.py        Retrieval evaluation
eval/                  Retrieval evaluation cases
knowledge/             Policy markdown ingested by seed_knowledge.py
supabase/migrations/   Tracked migrations (001-009; app schema and hardening)
data/                  Generated seed data (gitignored)
```

## Setup

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment variables** — create a `.env` file:
   ```
   GEMINI_API_KEY=your_api_key
   SUPABASE_URL=your_supabase_url
   SUPABASE_SERVICE_ROLE_KEY=server_only_service_role_key
   ```
   The service-role key bypasses RLS and is used only by the server process and
   the seeder. Never expose it to a browser client.

3. **Database** — apply migrations `001`-`009` in order (or run `supabase db
   push`). Migration `001` creates the order schema; `006` scopes conversations
   to browser sessions and configures retention/RLS; `007` scopes policy
   documents; `008` redacts historical transcript content and removes unused
   conversation fields; `009` stores requests after explicit customer
   confirmation. Pending requests are in session memory and are not persisted.

   RLS is enabled with no permissive policy, so only the service-role key can
   read these tables. If you later point a browser client at this database
   expecting it to read them, it will be denied — that is intended.

   Order numbers are public IDs in the range `0001`-`1000`.

4. **Run**
   ```bash
   streamlit run main.py
   ```

Run the focused checks with `python -m pytest`. Lint and
format with `ruff check .` and `ruff format --check .` (configured in
`pyproject.toml`).

## Synthetic data

`scripts/seed_orders.py` generates non-real customers and orders with Faker.
It writes JSON first so the data can be reviewed before any database write:

```bash
python scripts/seed_orders.py --customers 100 --orders-per-customer 3
python scripts/seed_orders.py --customers 100 --orders-per-customer 3 --insert
```

`--insert` needs the service-role key and writes to Supabase. Use it only in
development or a demo project, never against real customers, and pick a
`--start-order-id` range that does not overlap existing orders.

## Policy knowledge base

Markdown in `knowledge/` is chunked, embedded with `gemini-embedding-001`
(pinned to 768 dimensions to match the pgvector column) and stored in Supabase.
Documents record their source, embedding model, and chunker version; changed
versions are reingested and removed source files are pruned from that source.
At query time `search_policies` embeds the question and pulls the nearest chunks
above a similarity floor, which the answer must cite. Run after applying
migrations `001`-`009`:

```bash
python scripts/seed_knowledge.py            # ingest new/changed docs only
python scripts/seed_knowledge.py --reingest # re-embed after chunking changes
```

`scripts/evaluate_rag.py` scores retrieval against `eval/rag_eval_cases.json`.
It probes for a known query first, so a broken retriever fails loudly instead of
passing every no-match case. Run it explicitly with
`python scripts/evaluate_rag.py`; it calls the Gemini embedding API and reads
the configured Supabase vector store, but does not write to the database.
