-- Snapshot declarativo del esquema remoto después de add_competition_operations.
-- No contiene datos ni secretos.

create table public.stations (
  station_id text primary key,
  station_name text not null,
  corridor text not null,
  latitude double precision not null check (latitude between -90 and 90),
  longitude double precision not null check (longitude between -180 and 180),
  is_active boolean not null default true,
  constraint stations_id_not_blank check (btrim(station_id) <> ''),
  constraint stations_name_not_blank check (btrim(station_name) <> '')
);

create table public.time_context (
  context_id bigint generated always as identity primary key,
  observed_at timestamptz not null unique,
  rain_mm double precision not null default 0 check (rain_mm >= 0),
  rain_forecast double precision check (rain_forecast is null or rain_forecast >= 0),
  temperature_c double precision,
  temperature_forecast double precision,
  event_intensity double precision not null default 0 check (event_intensity >= 0)
);

create table public.pipeline_runs (
  pipeline_run_id uuid primary key default gen_random_uuid(),
  run_type text not null check (run_type in ('ingestion','training','prediction','monitoring','submission','full')),
  status text not null default 'running' check (status in ('pending','running','succeeded','failed','cancelled')),
  commit_sha text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  trigger_reason text,
  constraint pipeline_run_times_valid check (finished_at is null or finished_at >= started_at)
);

create table public.data_batches (
  batch_id uuid primary key default gen_random_uuid(),
  pipeline_run_id uuid not null references public.pipeline_runs on delete restrict,
  cutoff_at timestamptz not null,
  source_version text not null,
  cursor_value text,
  row_count integer not null default 0 check (row_count >= 0),
  checksum text,
  created_at timestamptz not null default now()
);

create table public.observations (
  observation_id bigint generated always as identity primary key,
  station_id text not null references public.stations on update cascade on delete restrict,
  context_id bigint not null references public.time_context on update cascade on delete restrict,
  batch_id uuid not null references public.data_batches on delete restrict,
  observed_at timestamptz not null,
  demand integer not null check (demand >= 0),
  ingested_at timestamptz not null default now(),
  constraint observations_station_time_unique unique (station_id, observed_at)
);

create table public.feature_sets (
  feature_set_id uuid primary key default gen_random_uuid(),
  name text not null,
  version text not null,
  code_commit_sha text,
  created_at timestamptz not null default now(),
  constraint feature_sets_name_version_unique unique (name, version)
);

create table public.feature_definitions (
  feature_id uuid primary key default gen_random_uuid(),
  feature_set_id uuid not null references public.feature_sets on delete cascade,
  feature_name text not null,
  data_type text not null,
  transformation text,
  is_target boolean not null default false,
  constraint feature_definition_unique unique (feature_set_id, feature_name)
);

create table public.training_runs (
  training_run_id uuid primary key default gen_random_uuid(),
  pipeline_run_id uuid not null references public.pipeline_runs on delete restrict,
  feature_set_id uuid not null references public.feature_sets on delete restrict,
  training_batch_id uuid not null references public.data_batches (batch_id) on delete restrict,
  train_start timestamptz not null,
  train_end timestamptz not null,
  validation_start timestamptz not null,
  validation_end timestamptz not null,
  status text not null default 'running' check (status in ('pending','running','succeeded','failed','cancelled')),
  created_at timestamptz not null default now(),
  constraint no_temporal_leakage check (train_end < validation_start),
  constraint training_period_valid check (train_start <= train_end),
  constraint validation_period_valid check (validation_start <= validation_end)
);

create table public.models (
  model_id uuid primary key default gen_random_uuid(),
  training_run_id uuid not null unique references public.training_runs on delete restrict,
  model_name text not null,
  algorithm text not null,
  version text not null,
  artifact_uri text not null,
  is_active boolean not null default false,
  created_at timestamptz not null default now(),
  constraint models_name_version_unique unique (model_name, version)
);

create table public.model_metrics (
  metric_id uuid primary key default gen_random_uuid(),
  model_id uuid not null references public.models on delete cascade,
  training_run_id uuid not null references public.training_runs on delete cascade,
  station_id text references public.stations on update cascade on delete restrict,
  split text not null check (split in ('train','validation','test','production')),
  metric_name text not null,
  metric_value double precision not null,
  measured_at timestamptz not null default now()
);

create table public.forecast_cycles (
  cycle_id text primary key,
  state text not null,
  origin_at timestamptz not null,
  data_cutoff timestamptz not null,
  opens_at timestamptz not null,
  closes_at timestamptz not null,
  forecast_start_at timestamptz not null,
  forecast_end_at timestamptz not null,
  station_count integer not null check (station_count > 0),
  horizons_minutes integer[] not null,
  expected_predictions integer not null check (expected_predictions > 0),
  contract_json jsonb not null,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  constraint forecast_cycles_id_not_blank check (btrim(cycle_id) <> ''),
  constraint forecast_cycles_state_not_blank check (btrim(state) <> ''),
  constraint forecast_cycles_window_valid check (closes_at > opens_at),
  constraint forecast_cycles_period_valid check (forecast_end_at >= forecast_start_at),
  constraint forecast_cycles_horizons_not_empty check (cardinality(horizons_minutes) > 0),
  constraint forecast_cycles_contract_is_object check (jsonb_typeof(contract_json) = 'object')
);

create table public.predictions (
  prediction_id uuid primary key default gen_random_uuid(),
  pipeline_run_id uuid not null references public.pipeline_runs on delete restrict,
  model_id uuid not null references public.models on delete restrict,
  station_id text not null references public.stations on update cascade on delete restrict,
  generated_at timestamptz not null default now(),
  target_at timestamptz not null,
  horizon_steps integer not null check (horizon_steps > 0),
  predicted_demand double precision not null check (predicted_demand >= 0),
  cycle_id text references public.forecast_cycles on update cascade on delete restrict,
  horizon_minutes integer not null check (horizon_minutes > 0),
  constraint predictions_natural_key unique (model_id, station_id, target_at, horizon_steps)
);

create table public.prediction_evaluations (
  evaluation_id uuid primary key default gen_random_uuid(),
  prediction_id uuid not null unique references public.predictions on delete cascade,
  observation_id bigint not null references public.observations on delete restrict,
  absolute_error double precision not null check (absolute_error >= 0),
  ape double precision check (ape is null or ape >= 0),
  evaluated_at timestamptz not null default now()
);

create table public.drift_checks (
  drift_check_id uuid primary key default gen_random_uuid(),
  model_id uuid not null references public.models on delete cascade,
  station_id text references public.stations on update cascade on delete restrict,
  drift_type text not null check (drift_type in ('data','concept','performance')),
  metric_name text not null,
  metric_value double precision not null,
  threshold double precision not null,
  drift_detected boolean not null,
  checked_at timestamptz not null default now()
);

create table public.pipeline_errors (
  error_id uuid primary key default gen_random_uuid(),
  pipeline_run_id uuid not null references public.pipeline_runs on delete cascade,
  stage text not null,
  error_type text not null,
  error_message text not null,
  retryable boolean not null default false,
  occurred_at timestamptz not null default now()
);

create table public.submissions (
  submission_id uuid primary key default gen_random_uuid(),
  pipeline_run_id uuid not null references public.pipeline_runs on delete restrict,
  model_id uuid not null references public.models on delete restrict,
  commit_sha text not null,
  status text not null check (status in ('pending','accepted','rejected','failed')),
  leaderboard_score double precision,
  submitted_at timestamptz not null default now(),
  response_message text,
  cycle_id text not null references public.forecast_cycles on update cascade on delete restrict,
  central_submission_id text unique,
  client_run_id text not null,
  idempotency_key text not null unique,
  payload_hash text not null,
  attempt smallint check (attempt is null or attempt between 1 and 3),
  received_at timestamptz,
  closes_at timestamptz,
  predictions_received integer check (predictions_received is null or predictions_received >= 0),
  expected_predictions integer check (expected_predictions is null or expected_predictions > 0),
  validated_contract jsonb check (validated_contract is null or jsonb_typeof(validated_contract) = 'object'),
  receipt_json jsonb check (receipt_json is null or jsonb_typeof(receipt_json) = 'object'),
  request_id text,
  is_official boolean not null default false,
  replaced_submission_id text,
  constraint submissions_cycle_run_unique unique (cycle_id, client_run_id),
  constraint submissions_counts_valid check (
    predictions_received is null or expected_predictions is null
    or predictions_received <= expected_predictions
  )
);

create table public.collector_state (
  collector_name text primary key,
  cursor_value text,
  last_observed_at timestamptz,
  last_released_at timestamptz,
  last_pipeline_run_id uuid references public.pipeline_runs on delete set null,
  rows_ingested bigint not null default 0 check (rows_ingested >= 0),
  updated_at timestamptz not null default now(),
  constraint collector_state_name_not_blank check (btrim(collector_name) <> '')
);

create table public.submission_predictions (
  submission_id uuid not null references public.submissions on delete cascade,
  prediction_id uuid not null references public.predictions on delete restrict,
  primary key (submission_id, prediction_id)
);

create index time_context_observed_at_idx on public.time_context (observed_at);
create index data_batches_pipeline_run_idx on public.data_batches (pipeline_run_id);
create index observations_batch_id_idx on public.observations (batch_id);
create index observations_context_id_idx on public.observations (context_id);
create index observations_observed_at_idx on public.observations (observed_at);
create index observations_station_time_idx on public.observations (station_id, observed_at desc);
create index training_runs_pipeline_run_idx on public.training_runs (pipeline_run_id);
create index training_runs_feature_set_id_idx on public.training_runs (feature_set_id);
create index training_runs_training_batch_id_idx on public.training_runs (training_batch_id);
create index model_metrics_model_station_idx on public.model_metrics (model_id, station_id);
create index model_metrics_training_run_id_idx on public.model_metrics (training_run_id);
create index model_metrics_station_id_idx on public.model_metrics (station_id);
create index forecast_cycles_state_closes_idx on public.forecast_cycles (state, closes_at desc);
create index predictions_pipeline_run_idx on public.predictions (pipeline_run_id);
create index predictions_station_id_idx on public.predictions (station_id);
create index predictions_target_station_idx on public.predictions (target_at, station_id);
create index predictions_cycle_id_idx on public.predictions (cycle_id) where cycle_id is not null;
create unique index predictions_cycle_target_unique
  on public.predictions (cycle_id, model_id, station_id, target_at) where cycle_id is not null;
create index prediction_evaluations_observation_id_idx on public.prediction_evaluations (observation_id);
create index drift_checks_model_checked_idx on public.drift_checks (model_id, checked_at desc);
create index drift_checks_station_id_idx on public.drift_checks (station_id);
create index pipeline_errors_run_idx on public.pipeline_errors (pipeline_run_id);
create index submissions_pipeline_run_id_idx on public.submissions (pipeline_run_id);
create index submissions_model_idx on public.submissions (model_id);
create index submissions_cycle_status_idx on public.submissions (cycle_id, status, submitted_at desc);
create index collector_state_last_pipeline_run_idx on public.collector_state (last_pipeline_run_id);
create index submission_predictions_prediction_idx on public.submission_predictions (prediction_id);

insert into public.collector_state (collector_name) values ('observations')
on conflict (collector_name) do nothing;

do $policies$
declare table_name text;
begin
  foreach table_name in array array[
    'stations','time_context','pipeline_runs','data_batches','observations',
    'feature_sets','feature_definitions','training_runs','models','model_metrics',
    'predictions','prediction_evaluations','drift_checks','pipeline_errors',
    'submissions','collector_state','forecast_cycles','submission_predictions'
  ] loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('revoke all on table public.%I from anon, authenticated', table_name);
    execute format('grant all privileges on table public.%I to service_role', table_name);
    execute format(
      'create policy backend_only on public.%I for all to anon, authenticated using (false) with check (false)',
      table_name
    );
  end loop;
end
$policies$;

grant usage, select on all sequences in schema public to service_role;

comment on table public.forecast_cycles is 'Copia auditable del contrato del ciclo vigente.';
comment on table public.collector_state is 'Cursor confirmado y estado durable del collector.';
comment on table public.submissions is 'Intentos y recibos trazables enviados al evaluador.';
comment on table public.submission_predictions is 'Predicciones incluidas en cada intento.';

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
    station_id text, observed_at timestamptz, demand integer, released_at timestamptz
  );
  insert into public.data_batches (
    pipeline_run_id, cutoff_at, source_version, cursor_value, row_count
  ) values (
    p_pipeline_run_id, coalesce(v_last_observed_at, now()),
    p_source_version, p_cursor, v_count
  ) returning batch_id into v_batch_id;
  insert into public.time_context (observed_at)
  select distinct row_data.observed_at
  from jsonb_to_recordset(p_rows) as row_data(
    station_id text, observed_at timestamptz, demand integer, released_at timestamptz
  )
  on conflict (observed_at) do nothing;
  insert into public.observations (
    station_id, context_id, batch_id, observed_at, demand
  )
  select row_data.station_id, context.context_id, v_batch_id,
         row_data.observed_at, row_data.demand
  from jsonb_to_recordset(p_rows) as row_data(
    station_id text, observed_at timestamptz, demand integer, released_at timestamptz
  )
  join public.time_context context using (observed_at)
  on conflict (station_id, observed_at) do update
    set demand = excluded.demand, context_id = excluded.context_id,
        batch_id = excluded.batch_id, ingested_at = now();
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
