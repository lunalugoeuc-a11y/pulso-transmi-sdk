-- Persistencia operativa para el contrato competitivo Pulso TransMi API 0.7.0.
-- Todas las tablas permanecen privadas al backend; service_role omite RLS.

create table public.collector_state (
  collector_name text primary key,
  cursor_value text,
  last_observed_at timestamptz,
  last_released_at timestamptz,
  last_pipeline_run_id uuid references public.pipeline_runs (pipeline_run_id)
    on delete set null,
  rows_ingested bigint not null default 0 check (rows_ingested >= 0),
  updated_at timestamptz not null default now(),
  constraint collector_state_name_not_blank check (btrim(collector_name) <> '')
);

create index collector_state_last_pipeline_run_idx
  on public.collector_state (last_pipeline_run_id);

comment on table public.collector_state is
  'Cursor confirmado y estado durable de cada collector incremental.';
comment on column public.collector_state.cursor_value is
  'Cursor opaco devuelto por la API; solo avanza después de confirmar la escritura.';

insert into public.collector_state (collector_name)
values ('observations')
on conflict (collector_name) do nothing;

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

create index forecast_cycles_state_closes_idx
  on public.forecast_cycles (state, closes_at desc);

comment on table public.forecast_cycles is
  'Copia auditable del contrato devuelto por /v1/forecast-cycles/current.';

alter table public.predictions
  add column cycle_id text references public.forecast_cycles (cycle_id)
    on update cascade on delete restrict,
  add column horizon_minutes integer;

update public.predictions
set horizon_minutes = horizon_steps * 15
where horizon_minutes is null;

alter table public.predictions
  alter column horizon_minutes set not null,
  add constraint predictions_horizon_minutes_positive check (horizon_minutes > 0);

create index predictions_cycle_id_idx
  on public.predictions (cycle_id)
  where cycle_id is not null;

create unique index predictions_cycle_target_unique
  on public.predictions (cycle_id, model_id, station_id, target_at)
  where cycle_id is not null;

alter table public.submissions
  add column cycle_id text not null references public.forecast_cycles (cycle_id)
    on update cascade on delete restrict,
  add column central_submission_id text,
  add column client_run_id text not null,
  add column idempotency_key text not null,
  add column payload_hash text not null,
  add column attempt smallint,
  add column received_at timestamptz,
  add column closes_at timestamptz,
  add column predictions_received integer,
  add column expected_predictions integer,
  add column validated_contract jsonb,
  add column receipt_json jsonb,
  add column request_id text,
  add column is_official boolean not null default false,
  add column replaced_submission_id text,
  add constraint submissions_central_id_unique unique (central_submission_id),
  add constraint submissions_idempotency_key_unique unique (idempotency_key),
  add constraint submissions_cycle_run_unique unique (cycle_id, client_run_id),
  add constraint submissions_attempt_valid check (attempt is null or attempt between 1 and 3),
  add constraint submissions_predictions_received_valid check (
    predictions_received is null or predictions_received >= 0
  ),
  add constraint submissions_expected_predictions_valid check (
    expected_predictions is null or expected_predictions > 0
  ),
  add constraint submissions_counts_valid check (
    predictions_received is null
    or expected_predictions is null
    or predictions_received <= expected_predictions
  ),
  add constraint submissions_validated_contract_object check (
    validated_contract is null or jsonb_typeof(validated_contract) = 'object'
  ),
  add constraint submissions_receipt_object check (
    receipt_json is null or jsonb_typeof(receipt_json) = 'object'
  );

create index submissions_cycle_status_idx
  on public.submissions (cycle_id, status, submitted_at desc);

comment on table public.submissions is
  'Intentos y recibos trazables enviados a POST /v1/submissions.';
comment on column public.submissions.central_submission_id is
  'submission_id devuelto por la API central; distinto del UUID local.';
comment on column public.submissions.idempotency_key is
  'Llave estable reutilizada únicamente para el mismo payload.';

create table public.submission_predictions (
  submission_id uuid not null references public.submissions (submission_id)
    on delete cascade,
  prediction_id uuid not null references public.predictions (prediction_id)
    on delete restrict,
  primary key (submission_id, prediction_id)
);

create index submission_predictions_prediction_idx
  on public.submission_predictions (prediction_id);

comment on table public.submission_predictions is
  'Predicciones incluidas en cada intento de submission.';

-- El pipeline corre únicamente en backend con service_role. No se concede
-- acceso de Data API a anon/authenticated; las políticas deny-all agregan
-- defensa en profundidad y hacen explícita la decisión de seguridad.
do $policies$
declare
  table_name text;
begin
  foreach table_name in array array[
    'stations', 'time_context', 'pipeline_runs', 'data_batches',
    'observations', 'feature_sets', 'feature_definitions', 'training_runs',
    'models', 'model_metrics', 'predictions', 'prediction_evaluations',
    'drift_checks', 'pipeline_errors', 'submissions', 'collector_state',
    'forecast_cycles', 'submission_predictions'
  ]
  loop
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
