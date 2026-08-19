# Order Status Assistant

A LangChain-powered Streamlit application for checking order status using natural language queries.

## Project Structure

```
├── main.py                # Main Streamlit application
├── setup.py               # Environment setup
├── database.py            # Database operations and queries
├── query.py               # Query handling and text processing
├── chat_handler.py        # Chat logic and LLM integration
├── sidebar.py             # Sidebar UI components
├── requirements.txt       # Python dependencies
├── .env                   # Environment variables (create this)
└── README.md              # This file
```

## Setup Instructions

1. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Environment Variables**
   Create a `.env` file with:
   ```
   GEMINI_API_KEY=your_api_key
   SUPABASE_URL=your_supabase_url
   SUPABASE_KEY=your_supabase_key
   SUPABASE_SERVICE_ROLE_KEY=server_only_service_role_key
   ```

3. **Database Schema**
   Apply `supabase/migrations/20260819_normalized_order_schema.sql`. The app
   uses the normalized `customers`, `orders`, `order_items`, `shipments`, and
   `order_events` tables. `SUPABASE_SERVICE_ROLE_KEY` is required only by the
   server-side support app and the seeder; never expose it in a browser client.

   The legacy `Order_Status` table schema was:
   - order_id (integer)
   - order_status (text)
   - customer_name (text)
   - customer_email (text)
   - customer_phone (text)
   - customer_address (text)
   - city (text)
   - state (text)
   - postal_code (text)
   - order_date (date)
   - delivery_date (date)
   - delivery_time_slot (text)
   - items_ordered (text)
   - item_quantity (integer)
   - total_amount (decimal)
   - payment_status (text)
   - delivery_driver (text)
   - driver_phone (text)
   - special_instructions (text)
   - priority (text)
   - estimated_delivery_time (timestamp)
   - actual_delivery_time (timestamp)

4. **Run Application**
   ```bash
   streamlit run main.py
   ```

## Synthetic development data

Use the included Faker seeder to create realistic, non-real customer and order
records. It writes JSON by default so the generated data can be reviewed before
any database change:

```bash
python seed_orders.py --customers 100 --orders-per-customer 3
```

This creates 300 records in `data/fake_orders.json`, using reproducible order
IDs beginning at `900000`. Confirm this range does not overlap your existing
orders, then insert them into Supabase:

```bash
python seed_orders.py --customers 100 --orders-per-customer 3 --insert
```

The normalized-schema seeder requires `SUPABASE_URL` and a separate
`SUPABASE_SERVICE_ROLE_KEY` only when `--insert` is specified. This privileged
key bypasses RLS, so use it only for local/admin seeding and never expose it to
the Streamlit client. Use this data only in development or a dedicated demo
project—not in a production database with real customers.

## Features

- **Natural Language Queries**: Ask questions like "Check order 12345" or "Orders for john@email.com"
- **Multiple Search Methods**: Search by order ID, email, phone, or customer name
- **LangChain Integration**: Uses LangChain for prompt management and LLM interaction
- **Quick Lookup Sidebar**: Direct database queries without chat interface
- **Session Management**: Maintains conversation context and database results

## File Descriptions

### `setup.py`
- Environment variable loading
- LLM and database client initialization
- System prompt configuration

### `database.py`
- Direct database query functions
- Order formatting utilities
- Error handling for database operations

### `query.py`
- Text parsing and information extraction
- Database query orchestration
- Context formatting for LLM

### `chat_handler.py`
- LangChain chat logic
- Message processing
- Response generation

### `sidebar.py`
- Streamlit sidebar components
- Quick lookup functionality
- UI element rendering

### `main.py`
- Main Streamlit application
- Session state management
- UI layout and coordination

## Usage Examples

**Chat Interface:**
- "Check my order 12345"
- "Orders for customer@email.com"
- "Show me orders for John Smith"
- "Status of order #67890"

**Sidebar Quick Lookup:**
- Enter order ID directly
- Search by email address
- Look up by phone number

## Error Handling

- Database connection errors are logged and user-friendly messages displayed
- Invalid queries return helpful suggestions
- LLM errors are caught and handled gracefully

## Customization

- Modify `SYSTEM_PROMPT` in `setup.py` to change assistant behavior
- Update database functions in `database.py` for different schemas
- Extend `query.py` for additional text parsing patterns
- Customize UI in `sidebar.py` and `main.py`
