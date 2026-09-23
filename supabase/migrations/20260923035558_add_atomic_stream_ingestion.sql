-- Guarda una pagina del stream y confirma su cursor en una sola transaccion.
-- La funcion usa los permisos del caller (service_role); no omite RLS.
create or replace function public.ingest_observation_page(
  p_pipeline_run_id uuid,
  p_cursor text,
  p_rows jsonb,
  p_source_version text default 'competition-stream-v1'
)
returns integer
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_batch_id uuid;
  v_count integer;
  v_last_observed_at timestamptz;
  v_last_released_at timestamptz;
begin
  if jsonb_typeof(p_rows) <> 'array' then
    raise exception 'p_rows must be a JSON array';
  end if;

  select count(*), max(observed_at), max(released_at)
  into v_count, v_last_observed_at, v_last_released_at
  from jsonb_to_recordset(p_rows) as row_data(
    station_id text,
    observed_at timestamptz,
    demand integer,
    released_at timestamptz
  );

  insert into public.data_batches (
    pipeline_run_id, cutoff_at, source_version, cursor_value, row_count
  ) values (
    p_pipeline_run_id,
    coalesce(v_last_observed_at, now()),
    p_source_version,
    p_cursor,
    v_count
  ) returning batch_id into v_batch_id;

  insert into public.time_context (observed_at)
  select distinct row_data.observed_at
  from jsonb_to_recordset(p_rows) as row_data(
    station_id text,
    observed_at timestamptz,
    demand integer,
    released_at timestamptz
  )
  on conflict (observed_at) do nothing;

  insert into public.observations (
    station_id, context_id, batch_id, observed_at, demand
  )
  select row_data.station_id, context.context_id, v_batch_id,
         row_data.observed_at, row_data.demand
  from jsonb_to_recordset(p_rows) as row_data(
    station_id text,
    observed_at timestamptz,
    demand integer,
    released_at timestamptz
  )
  join public.time_context context using (observed_at)
  on conflict (station_id, observed_at) do update
    set demand = excluded.demand,
        context_id = excluded.context_id,
        batch_id = excluded.batch_id,
        ingested_at = now();

  update public.collector_state
  set cursor_value = coalesce(p_cursor, cursor_value),
      last_observed_at = greatest(last_observed_at, v_last_observed_at),
      last_released_at = greatest(last_released_at, v_last_released_at),
      last_pipeline_run_id = p_pipeline_run_id,
      rows_ingested = rows_ingested + v_count,
      updated_at = now()
  where collector_name = 'observations';

  return v_count;
end;
$$;

revoke all on function public.ingest_observation_page(uuid, text, jsonb, text)
  from public, anon, authenticated;
grant execute on function public.ingest_observation_page(uuid, text, jsonb, text)
  to service_role;

comment on function public.ingest_observation_page(uuid, text, jsonb, text) is
  'Upsert atomico de una pagina del stream y confirmacion posterior del cursor.';
