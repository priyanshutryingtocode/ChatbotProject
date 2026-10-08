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
tool loop, where `lookup_order` re-enforces the two-field rule server-side and
`search_policy` answers public policy questions from retrieved documents. Because
verification is enforced in the tool and not the prompt, a prompt injection
still cannot reach unverified data.

Verification state is collected across messages, so `order 42` followed by
`my email is …` works, and a customer can abandon the flow with `skip`. Five
failed attempts start a cooldown.

The package is layered: `config`, `state` and `routing` are leaves with no
database or model access, and `chat` sits above `tools`, `query`, `responses`,
`database` and `retriever`.

## Features

- **Two-field verification** — order number plus one identity detail, matched
  across one message or several. Customer names are matched literally: `%` and
  `_` are stripped rather than treated as wildcards, so a name can never stand
  in for "any customer"
- **Deterministic order facts** — statuses, tracking numbers and dates come from
  the database, never from the model
- **No customer data at rest** — the conversation log keeps only order id,
  status and amounts. Names, emails, phone numbers, tracking numbers and driver
  names are never written, and conversations are purged after 30 days
- **Policy answers via RAG** — returns, refunds, shipping, cancellations and
  damaged-item questions answered from vector-retrieved policy docs with source
  citations. Policies are public, so no verification is needed for them
- **Quick lookup sidebar** — direct database search under the same two-field
  rule, plus a recent-chats list showing only this browser session's
  conversations. That scoping is by session, not by sign-in: it prevents one
  visitor from reading another's history, but anyone holding a given session id
  can still read that session. Real access control needs an authenticated owner.
- **Streaming replies** — answers stream token-by-token, so the first words
  appear immediately

## Project structure

```
main.py                Streamlit entrypoint
orderbot/              Application package
  config.py              Env loading, LLM init, system prompt, constants
  state.py               Session verification state + attempt guard
  routing.py             Message-classification vocabulary and predicates
  database.py            Supabase queries, two-field verification, formatting
  retriever.py           Policy RAG over pgvector
  responses.py           Deterministic database-backed reply formatting
  query.py               Text parsing and context formatting
  tools.py               LangChain tools (lookup_order, search_policy)
  chat.py                Routing state machine and tool loop
  sidebar.py             Sidebar quick-lookup UI
static/app.css         App stylesheet
scripts/               CLI tooling, run as `python scripts/<name>.py`
  seed_orders.py         Faker-based synthetic data generator
  seed_knowledge.py      Markdown chunker + pgvector ingest
  evaluate_rag.py        Retrieval evaluation
eval/                  Retrieval evaluation cases
knowledge/             Policy markdown ingested by seed_knowledge.py
supabase/migrations/   Tracked migrations (002-006; 006 hardens conversations)
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

3. **Database** — point the app at a Supabase project holding the normalized
   schema: `customers`, `orders`, `order_items`, `shipments`, `order_events`,
   `payments`, plus `conversations` and `messages` from migration `002`.

   Migration numbering starts at `002`; there is no `001` here, so a fresh clone
   cannot provision the base schema on its own. Migrations `002`-`006` assume
   the tables above already exist. Apply them in order after creating the base
   schema.

   Migration `006` must be applied before running the app — it adds the
   `session_id` column that stored conversations are scoped by, so lookups
   against a database without it will fail. It also scrubs personal data already
   stored in `messages.db_results`, adds a 30-day retention purge, and enables
   row-level security on `conversations` and `messages`. Apply it in the
   Supabase SQL editor:

   ```sql
   -- paste contents of supabase/migrations/006_conversation_hardening.sql
   ```

   RLS is enabled with no permissive policy, so only the service-role key can
   read these tables. If you later point a browser client at this database
   expecting it to read them, it will be denied — that is intended.

   Order numbers are public IDs in the range `0001`-`1000`.

4. **Run**
   ```bash
   streamlit run main.py
   ```

Lint and format with `ruff check .` and `ruff format --check .` (configured in
`pyproject.toml`). Behaviour is otherwise verified by hand — `orderbot/tools.py`
and `orderbot/chat.py` hold the two-field verification and the routing guards,
so changes there warrant a manual pass over the security cases.

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
At query time `search_policy` embeds the question and pulls the nearest chunks
above a similarity floor, which the answer must cite. Run after applying
migrations `003`-`005`:

```bash
python scripts/seed_knowledge.py            # ingest new/changed docs only
python scripts/seed_knowledge.py --reingest # re-embed after chunking changes
```

`scripts/evaluate_rag.py` scores retrieval against `eval/rag_eval_cases.json`.
It probes for a known query first, so a broken retriever fails loudly instead of
passing every no-match case.