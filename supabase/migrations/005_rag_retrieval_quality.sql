-- Reject weak semantic matches rather than treating the nearest chunk as relevant.

create or replace function match_knowledge_chunks(
    query_embedding vector(768),
    match_count int default 4,
    min_similarity float default 0.45
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
    where 1 - (kc.embedding <=> query_embedding) >= min_similarity
    order by kc.embedding <=> query_embedding
    limit match_count;
$$;
