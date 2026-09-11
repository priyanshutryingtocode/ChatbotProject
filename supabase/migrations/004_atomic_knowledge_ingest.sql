-- Replace a knowledge document and all of its embeddings atomically.
-- Called by seed_knowledge.py after embeddings have been generated locally.

create or replace function replace_knowledge_document(
    p_title text,
    p_source_file text,
    p_content_hash text,
    p_chunks jsonb
)
returns bigint
language plpgsql
as $$
declare
    v_document_id bigint;
begin
    insert into knowledge_documents (title, source_file, content_hash)
    values (p_title, p_source_file, p_content_hash)
    on conflict (title) do update
        set source_file = excluded.source_file,
            content_hash = excluded.content_hash,
            updated_at = now()
    returning id into v_document_id;

    delete from knowledge_chunks where document_id = v_document_id;

    insert into knowledge_chunks (document_id, chunk_index, heading, content, embedding)
    select
        v_document_id,
        (chunk ->> 'chunk_index')::int,
        nullif(chunk ->> 'heading', ''),
        chunk ->> 'content',
        ((chunk -> 'embedding')::text)::vector
    from jsonb_array_elements(coalesce(p_chunks, '[]'::jsonb)) as chunk;

    return v_document_id;
end;
$$;
