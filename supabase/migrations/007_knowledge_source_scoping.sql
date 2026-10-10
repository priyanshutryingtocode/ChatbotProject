-- Track the source directory for each ingested document so an ingestion run
-- can remove stale documents without touching another directory's corpus.

alter table knowledge_documents
    add column if not exists source_directory text;

update knowledge_documents
set source_directory = 'knowledge'
where source_directory is null;

alter table knowledge_documents
    alter column source_directory set not null,
    add column if not exists embedding_model text not null default 'gemini-embedding-001',
    add column if not exists chunker_version text not null default 'markdown-v1';

alter table knowledge_documents drop constraint if exists knowledge_documents_title_key;
alter table knowledge_documents
    add constraint knowledge_documents_source_file_key unique (source_directory, source_file);

alter table knowledge_documents enable row level security;
alter table knowledge_chunks enable row level security;

drop function if exists replace_knowledge_document(text, text, text, jsonb);
drop function if exists replace_knowledge_document(text, text, text, text, jsonb);

create or replace function replace_knowledge_document(
    p_title text,
    p_source_file text,
    p_source_directory text,
    p_embedding_model text,
    p_chunker_version text,
    p_content_hash text,
    p_chunks jsonb
)
returns bigint
language plpgsql
as $$
declare
    v_document_id bigint;
begin
    insert into knowledge_documents (
        title, source_file, source_directory, embedding_model, chunker_version, content_hash
    )
    values (p_title, p_source_file, p_source_directory, p_embedding_model, p_chunker_version, p_content_hash)
    on conflict (source_directory, source_file) do update
        set title = excluded.title,
            embedding_model = excluded.embedding_model,
            chunker_version = excluded.chunker_version,
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
