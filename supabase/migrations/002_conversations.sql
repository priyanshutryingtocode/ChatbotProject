-- Conversation persistence for the Order Status Assistant.
-- Apply against Supabase with:  supabase db push  (or run the SQL manually).

create table if not exists conversations (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    channel text not null default 'streamlit',
    customer_email text,
    ended_at timestamptz
);

create table if not exists messages (
    id uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references conversations(id) on delete cascade,
    role text not null check (role in ('user', 'assistant', 'system')),
    content text not null,
    db_results jsonb,
    feedback text check (feedback in ('up', 'down')),
    created_at timestamptz not null default now()
);

create index if not exists messages_conversation_created_idx
    on messages (conversation_id, created_at);