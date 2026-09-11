-- Knowledge-base tables for policy/FAQ retrieval (RAG).
-- Apply against Supabase after the normalized order schema:
--   supabase db push  (or run the SQL manually).

create extension if not exists vector;

create table if not exists knowledge_documents (
    id bigint generated always as identity primary key,
    title text not null unique,
    source_file text not null,
    content_hash text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists knowledge_chunks (
    id bigint generated always as identity primary key,
    document_id bigint not null references knowledge_documents(id) on delete cascade,
    chunk_index int not null,
    heading text,
    content text not null,
    embedding vector(768) not null,
    created_at timestamptz not null default now(),
    unique (document_id, chunk_index)
);

-- Exact-scan cosine search. At this corpus size (dozens of chunks) a
-- sequential scan beats index overhead; add an index only when the knowledge
-- base grows past ~1,000 chunks:
--   create index on knowledge_chunks using hnsw (embedding vector_cosine_ops);

create or replace function match_knowledge_chunks(
    query_embedding vector(768),
    match_count int default 4
)
returns table (
    chunk_id bigint,
    doc_title text,
    heading text,
    content text,
    similarity float
)
language sql stable
as $$
    select
        kc.id,
        kd.title,
        kc.heading,
        kc.content,
        1 - (kc.embedding <=> query_embedding) as similarity
    from knowledge_chunks kc
    join knowledge_documents kd on kd.id = kc.document_id
    order by kc.embedding <=> query_embedding
    limit match_count;
$$;
