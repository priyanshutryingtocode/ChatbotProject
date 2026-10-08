-- Harden stored conversations: scope them to a browser session, remove the
-- personal data they were keeping in clear, bound their lifetime, and lock the
-- tables down so only the service-role key can read them.
--
-- Apply after 002_conversations.sql.   
--
-- Background, all four problems were found together:
--
--   1. list_conversations() had no filter, so every visitor saw the most recent
--      conversations of every other visitor, and any id could be loaded from the
--      ?chat= query parameter.
--   2. Each assistant turn stored the full verified order row in
--      messages.db_results — name, email, phone, tracking number, delivery
--      driver — in the clear, indefinitely. Nothing ever read that column back,
--      so it was pure exposure with no benefit.
--   3. Nothing ever deleted conversations, so the data grew forever.
--   4. No row-level security, so a leaked anon key could read customer data
--      directly, bypassing the app's two-field verification entirely.

-- ---------------------------------------------------------------------------
-- 1. Session scoping
-- ---------------------------------------------------------------------------
--
-- session_id identifies a browser session, NOT a person. It stops cross-visitor
-- reads; it is not authentication. Anyone who obtains a session id can still
-- load that session's conversations. Real access control needs an owner column
-- populated from a sign-in flow.

alter table conversations
    add column if not exists session_id uuid;

-- The sidebar lists "this session's conversations, newest first" on every
-- render, so the filter and the sort both ride on this index.
create index if not exists conversations_session_created_idx
    on conversations (session_id, created_at desc);

-- Rows predating this migration keep a null session_id and therefore match no
-- session, which is the intended fail-closed outcome: invisible to everyone.

-- ---------------------------------------------------------------------------
-- 2. Remove personal data already stored
-- ---------------------------------------------------------------------------
--
-- Rewrite db_results down to the non-identifying fields so the audit trail
-- survives while names, emails, phone numbers, tracking numbers and driver
-- names do not. New writes are redacted in orderbot/chat.py.

update messages
set db_results = jsonb_build_object(
    'matched',
    coalesce((
        select jsonb_agg(
            jsonb_build_object(
                'public_order_id', entry.order_row -> 'public_order_id',
                'status', entry.order_row -> 'status',
                'payment_status', entry.order_row -> 'payment_status',
                'currency', entry.order_row -> 'currency',
                'total_amount', entry.order_row -> 'total_amount',
                'ordered_at', entry.order_row -> 'ordered_at'
            )
            order by entry.position
        )
        from jsonb_array_elements(messages.db_results -> 'matched')
             with ordinality as entry(order_row, position)
    ), '[]'::jsonb)
)
where db_results is not null
  and jsonb_typeof(db_results -> 'matched') = 'array';

-- Anything still holding db_results that is not the expected shape is dropped
-- outright rather than left in place for inspection. This covers a 'matched'
-- key of the wrong type, which the statement above must not try to expand, and
-- a payload with no 'matched' key at all — nothing redacted it, so nothing
-- vouches for what it still contains.
update messages
set db_results = null
where db_results is not null
  and not (
      db_results ? 'matched'
      and jsonb_typeof(db_results -> 'matched') = 'array'
  );

-- ---------------------------------------------------------------------------
-- 3. Retention
-- ---------------------------------------------------------------------------
--
-- Deletes whole conversations; messages cascade via the constraint declared in
-- 002_conversations.sql. Raising the interval keeps more history — this only
-- sets the default for the scheduled call.

create or replace function purge_old_conversations(retention_days int default 30)
returns bigint
language plpgsql
as $$
declare
    v_deleted bigint;
begin
    delete from conversations
    where created_at < now() - make_interval(days => greatest(retention_days, 1));

    get diagnostics v_deleted = row_count;
    return v_deleted;
end;
$$;

-- pg_cron is not present on every Supabase project, and enabling it is a
-- dashboard-level change, so schedule the job only when it is available and
-- say so loudly when it is not. The function above is always created, so the
-- purge can be run by hand regardless.
do $$
begin
    if exists (select 1 from pg_available_extensions where name = 'pg_cron') then
        create extension if not exists pg_cron;
    end if;

    if exists (select 1 from pg_extension where extname = 'pg_cron') then
        -- 03:17 rather than 03:00 so this does not pile onto every other
        -- project's nightly maintenance window. The inner dollar-quote tag must
        -- differ from this block's, or it would close the block early.
        perform cron.schedule(
            'purge-old-conversations',
            '17 3 * * *',
            $cron$select purge_old_conversations()$cron$
        );
        raise notice 'Retention scheduled: conversations older than 30 days are purged daily.';
    else
        raise warning
            'pg_cron is not installed, so no automatic purge was scheduled. Run SELECT purge_old_conversations(); periodically, or enable pg_cron and re-apply this migration.';
    end if;
exception
    when others then
        raise warning
            'Could not schedule conversation retention (%). The purge function exists; run SELECT purge_old_conversations(); manually.', sqlerrm;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. Lock the tables down
-- ---------------------------------------------------------------------------
--
-- RLS is enabled with no permissive policy, which means anon and authenticated
-- get nothing while the service-role key — the only thing this app uses — is
-- unaffected, because it bypasses RLS. This is the backstop for a leaked anon
-- key: even if one turns up in the repo or a browser bundle, it cannot read
-- customer data without going through the app's two-field verification.

alter table conversations enable row level security;
alter table messages enable row level security;

-- ---------------------------------------------------------------------------
-- 5. Drop the dead column
-- ---------------------------------------------------------------------------
--
-- feedback was declared in 002 with a check constraint but has never been
-- written or read by any code path.

alter table messages drop column if exists feedback;