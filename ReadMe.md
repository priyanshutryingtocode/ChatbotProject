# Order Status Assistant

A LangChain-powered Streamlit app for checking order status in natural language.

## How it works

Order data is only released after verification. A signed-in customer provides
an order number, and the server matches it to the email verified by Supabase
Auth. The shared lookup service also supports an order number plus email, phone,
or name for trusted internal entry points. Every supplied field must resolve to
the same order.

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
`conversations` owns account-scoped transcript persistence; and `chat` coordinates
routing, verification, request staging, and response handling.

The current project environment was checked with Python 3.12.5,
LangChain 1.4.4, `langchain-core` 1.6.9, `langchain-google-genai` 4.4.0,
`google-genai` 2.29.0, Streamlit 1.65.0, and Supabase 2.32.0. LangGraph 1.2.14
is an indirect dependency of LangChain; this project does not implement graphs.

## Customer support requests

Return and cancellation requests do not change order state. The agent can stage
one only after recent email or phone verification for that exact order. A
separate Streamlit confirmation submits a support request to the `action_requests`
table for human review. Customers can track account-owned requests and see
staff status notes in the **My support requests** panel. Staff can include a
customer-visible update when reviewing, completing, or declining a case. The enabled
action tools are `request_return` and `cancel_order`; neither changes order state.
A signed-in customer can also submit a new-order request with an item description,
quantity, and optional notes. This creates a staff case only: the app has no
product catalogue, payment, or order-management integration, so it does not
place or charge for the order.
For the missing internal order-operation integration, see the proposed contract
in [ORDER_OPERATIONS_API.md](ORDER_OPERATIONS_API.md); it is a draft and is not
connected to a live service yet.
A general `escalate_to_human` tool is not implemented. Eligibility rules and an order-management
integration are not defined, so the app never claims that a return, refund, or
cancellation has been completed. The request table is not connected to a staff
notification channel yet, and request retention has not been defined. Pending
confirmation is held in server-side session state for five minutes and is not
restored from a saved transcript.

Migration `012_customer_support_case_tracking.sql` adds the authenticated owner
link for support cases and backfills it from owned conversations where possible.
Migration `013_staff_case_queue.sql` adds staff self-assignment, priority, and a
guarded case-routing operation. Migration `014_new_order_requests.sql` enables
new-order cases with structured request details and no linked existing order.
Apply migrations `001`-`017` in order.
Historical requests without an owner link remain unavailable in customer
account tracking. The app does not apply migrations or submit requests
automatically.

## Features

- **Verified order lookup** — order number plus the signed-in account's verified
  email. The shared lookup service also supports email, phone, or name matching
  for trusted internal entry points. Customer names are matched literally:
  `%` and `_` are stripped rather than treated as wildcards.
- **Deterministic order facts** — statuses, tracking numbers and dates come from
  the database, never from the model
- **Account-scoped history** — signed-in users can resume their full transcript;
  conversations are owner-scoped and purged after 30 days. Transcript history
  is never treated as proof of order verification.
- **Policy answers via RAG** — returns, refunds, shipping, cancellations and
  damaged-item questions answered from vector-retrieved policy docs with source
  citations. Policies are public, so no verification is needed for them
- **Authenticated accounts** — customers sign in with an email and password;
  the verified account email is bound server-side to order lookups. Conversation
  history is owned by the Supabase Auth user, not just the browser session.
- **New-order requests** — signed-in customers can ask staff to arrange an item
  and quantity; the request is tracked as a case without creating an order.
- **Case tracking** — customers see request status and customer-visible staff
  updates for cases linked to their authenticated account.
- **Staff review** — support staff must sign in with an allowlisted account to
  search and filter the queue, claim cases, set priority, inspect case history,
  and send customer-visible status updates. Reviews are audited; case actions do
  not themselves modify an order.
- **Quick lookup sidebar** — direct database search uses the signed-in account's
  verified email and the order number.
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
  privacy.py             PII minimization for support-request reasons
  retriever.py           Policy RAG over pgvector
  responses.py           Deterministic database-backed reply formatting
  identity.py            Order and customer identifier extraction
  order_context.py       Query-relevant order context for model prompts
  handoff.py             Verified order snapshot + retrieved policy context for staff
  tools.py               LangChain read-only tool adapters
  agent.py               LangChain create_agent factory
  actions.py             Confirmed support-request persistence
  auth.py                Supabase email/password auth and server-checked staff role
  conversations.py       Account-scoped transcript persistence
  chat.py                Routing state machine and chat application service
  sidebar.py             Sidebar quick-lookup UI
static/app.css         App stylesheet
scripts/               CLI tooling, run as `python scripts/<name>.py`
  seed_orders.py         Faker-based synthetic data generator
  seed_knowledge.py      Markdown chunker + pgvector ingest
  evaluate_rag.py        Retrieval evaluation
eval/                  Retrieval evaluation cases
knowledge/             Policy markdown ingested by seed_knowledge.py
supabase/migrations/   Tracked migrations (001-017; app schema, RAG and hardening)
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
   SUPABASE_ANON_KEY=your_supabase_anon_key
   ```
   The service-role key bypasses RLS and is used only by the server process and
   the seeder. Never expose it to a browser client.

3. **Database** — apply migrations `001`-`017` in order (or run `supabase db
   push`). Migration `001` creates the order schema; `006` scopes conversations
   to browser sessions and configures retention/RLS; `007` scopes policy
   documents; `008` replaces historical transcript content with a redaction
   placeholder and removes unused conversation fields; `009` stores requests after explicit customer
   confirmation; `010` adds authenticated conversation ownership, the staff
   allowlist, and staff review auditing; `012` links support cases to customer
   accounts and enables status tracking; `013` adds queue assignment and
   prioritization; `014` enables new-order request cases; `015` moves this app's
   tables and RPCs into the `order_support` schema; `016` creates a product
   catalog and links order lines and new-order requests to catalog items; `017`
   adds 53 categorized demo products with indicative INR prices. Pending customer
   confirmation remains in session memory and is not persisted.

   After applying migration `015`, add `order_support` to the project's
   **Exposed schemas** in Supabase API settings and reload the PostgREST schema
   cache if needed. This app and its seed scripts select `order_support`
   explicitly; repo/review tables remain in `public`.

   Migration `016` seeds the catalog from existing order items. Migration `017`
   adds the expanded sample assortment and indicative demo prices. Existing
   order-line prices remain unchanged snapshots. Replace demo catalog prices
   with approved prices before treating them as live offers. The order seeder
   uses the same assortment for newly generated synthetic orders.

   Authentication now scopes new transcript reads and writes to the signed-in
   account, so new messages are retained in full for chat resumption. Migration
   `008`'s historical placeholders are irreversible; the original messages
   cannot be recovered from the app.

   RLS is enabled with no permissive policy, so only the service-role key can
   read these tables. If you later point a browser client at this database
   expecting it to read them, it will be denied — that is intended.

   Order numbers are public IDs in the range `0001`-`1000`.

4. **Configure Supabase Auth** — enable the email provider. Customer account
   creation uses email and password (minimum 8 characters in the app). If email
   confirmation is enabled, customers confirm using Supabase's confirmation
   email before signing in. Support staff cannot self-register: create or invite
   their Auth account through a trusted administrator, then add its Auth UUID
   to `public.support_staff` and set `active = true`. Deactivate staff by
   setting `active = false`.
   Never add staff roles to user-editable Auth metadata. The application uses
   its service-role key only on the server; the browser never receives it.

   Migration `010` is tracked but is not applied automatically. Existing
   conversations have no owner and will not appear in authenticated accounts.
   New conversations are associated with the signed-in Supabase user.

5. **Run**
   ```bash
   streamlit run main.py
   ```

Run the focused checks with `python -m pytest`. Lint and
format with `ruff check .` and `ruff format --check .` (configured in
`pyproject.toml`).

## Demo accounts

These are synthetic accounts created from the first two demo customer records.
Both use the shared password below. Their order numbers are included for
order-lookup demos. Apply migration `010` before signing in through the app.

| Demo user | Email | Associated orders |
|---|---|---|
| 1 | `demo.1.diyarout@example.test` | `0001`-`0010` |
| 2 | `demo.2.agastyapatla@example.test` | `0011`-`0020` |

**Shared demo password:** `5PaxdRZ0nZnanndCYTYJBloN`

These shared credentials are for this synthetic demo dataset only. Do not reuse
the password for real or production accounts.

## Synthetic data

`scripts/seed_orders.py` generates non-real customers and orders with Faker.
It writes JSON first so the data can be reviewed before any database write:

```bash
python scripts/seed_orders.py --customers 100 --orders-per-customer 3
python scripts/seed_orders.py --customers 100 --orders-per-customer 3 --insert
```

To generate orders associated with customers already in Supabase without
inserting or updating customer or address rows, use `--existing-customers`:

```bash
python scripts/seed_orders.py --existing-customers --customers 10 --orders-per-customer 1
```

This mode reads customer IDs, names, saved addresses, and the current highest
order ID. Its JSON preview omits names and delivery addresses. Add `--insert`
only when you intend to write the generated order, item, shipment, event, and
payment rows to a development/demo database. The first generated order ID
defaults to the next number after the current maximum.

To replace the current order dataset while keeping every customer and saved
address, add `--replace-orders` and `--insert`. This deletes all existing
orders; the database cascades the deletion to order items, shipment/address
snapshots, events, payments, and support requests linked to those orders. It
does not delete customer or customer-address records. Replacement starts order
numbering at 1. Review the generated JSON before running this destructive mode:

```bash
python scripts/seed_orders.py --existing-customers --customers 1000 --orders-per-customer 5 --replace-orders --insert
```

`--replace-orders` is rejected unless both `--existing-customers` and `--insert`
are present. Run it only against a development/demo database.

`--insert` needs the service-role key and writes to Supabase. Use it only in
development or a demo project, never against real customers, and pick a
`--start-order-id` range that does not overlap existing orders.

## Policy knowledge base

Markdown in `knowledge/` contains the source policies plus common customer
questions and grounded answers. It is chunked with its full heading path,
embedded with `gemini-embedding-001`
(pinned to 768 dimensions to match the pgvector column) and stored in Supabase.
Documents record their source, embedding model, and chunker version; changed
versions are reingested and removed source files are pruned from that source.
At query time `search_policies` combines vector similarity with PostgreSQL
full-text ranking. The customer's original wording is also searched and merged
with the agent's query, then the answer cites the retrieved source headings.
Apply migrations `001`-`012` before using hybrid retrieval. Chunker v2 preserves
the full Markdown heading path with each chunk, so reingest the corpus after
applying migration `011`:

```bash
python scripts/seed_knowledge.py            # ingest new/changed docs only
python scripts/seed_knowledge.py --reingest # re-embed after chunking changes
```

`scripts/evaluate_rag.py` scores retrieval against `eval/rag_eval_cases.json`
using document hit rate, no-match accuracy, mean reciprocal rank, and recall@k.
It probes for a known query first, so a broken retriever fails loudly instead
of passing every no-match case. Run it explicitly with
`python scripts/evaluate_rag.py`; it calls the Gemini embedding API and reads
the configured Supabase vector store, but does not write to the database.
