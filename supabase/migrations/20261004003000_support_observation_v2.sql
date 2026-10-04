-- Adapta la ingesta incremental al contrato mixto v1/v2.
-- Los faltantes v2 se conservan para auditoria y nunca se convierten en cero.

-- PostgreSQL no permite cambiar el tipo de una columna referenciada por vistas.
-- Se recrean dentro de esta misma transaccion, conservando definiciones y ACL.
drop view if exists public.official_model_last_six_metrics;
drop view if exists public.official_cycle_metrics;
drop view if exists public.official_cycle_station_metrics;
drop view if exists public.official_prediction_errors;

alter table public.observations
  alter column demand type numeric using demand::numeric,
  alter column demand drop not null;

alter table public.observations
  add column if not exists source_schema_version smallint not null default 1,
  add column if not exists quality text not null default 'observed',
  add column if not exists unit text not null default 'passengers',
  add column if not exists released_at timestamptz;

alter table public.observations
  drop constraint if exists observations_demand_check,
  add constraint observations_source_schema_version_check
    check (source_schema_version in (1, 2)),
  add constraint observations_quality_check
    check (quality in ('observed', 'missing')),
  add constraint observations_unit_check
    check (unit = 'passengers'),
  add constraint observations_measurement_check
    check (
      (quality = 'observed' and demand is not null and demand >= 0)
      or (quality = 'missing' and demand is null)
    );

create or replace function public.ingest_observation_page(
  p_pipeline_run_id uuid,
  p_cursor text,
  p_rows jsonb,
  p_source_version text default 'competition-stream-v1-v2'
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
    released_at timestamptz,
    demand numeric,
    source_schema_version smallint,
    quality text,
    unit text
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
    released_at timestamptz,
    demand numeric,
    source_schema_version smallint,
    quality text,
    unit text
  )
  on conflict (observed_at) do nothing;

  insert into public.observations (
    station_id,
    context_id,
    batch_id,
    observed_at,
    demand,
    source_schema_version,
    quality,
    unit,
    released_at
  )
  select
    row_data.station_id,
    context.context_id,
    v_batch_id,
    row_data.observed_at,
    row_data.demand,
    row_data.source_schema_version,
    row_data.quality,
    row_data.unit,
    row_data.released_at
  from jsonb_to_recordset(p_rows) as row_data(
    station_id text,
    observed_at timestamptz,
    released_at timestamptz,
    demand numeric,
    source_schema_version smallint,
    quality text,
    unit text
  )
  join public.time_context context using (observed_at)
  on conflict (station_id, observed_at) do update
    set demand = excluded.demand,
        source_schema_version = excluded.source_schema_version,
        quality = excluded.quality,
        unit = excluded.unit,
        released_at = excluded.released_at,
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
  'Upsert atomico de paginas mixtas v1/v2 y confirmacion posterior del cursor.';

create or replace function public.evaluate_available_predictions()
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_affected integer;
begin
  with candidates as (
    select distinct on (prediction.prediction_id)
      prediction.prediction_id,
      observation.observation_id,
      abs(prediction.predicted_demand - observation.demand::double precision)
        as absolute_error,
      case
        when observation.demand = 0 then null
        else abs(prediction.predicted_demand - observation.demand::double precision)
          / observation.demand::double precision
      end as ape
    from public.predictions as prediction
    join public.observations as observation
      on observation.station_id = prediction.station_id
     and observation.observed_at = prediction.target_at
    where observation.quality = 'observed'
      and observation.demand is not null
      and exists (
        select 1
        from public.submission_predictions as submitted_prediction
        join public.submissions as submission
          on submission.submission_id = submitted_prediction.submission_id
        where submitted_prediction.prediction_id = prediction.prediction_id
          and submission.status = 'accepted'
          and submission.is_official
          and submission.predictions_received = submission.expected_predictions
      )
    order by prediction.prediction_id, observation.ingested_at desc
  )
  insert into public.prediction_evaluations as evaluation (
    prediction_id,
    observation_id,
    absolute_error,
    ape,
    evaluated_at
  )
  select
    candidate.prediction_id,
    candidate.observation_id,
    candidate.absolute_error,
    candidate.ape,
    now()
  from candidates as candidate
  on conflict (prediction_id) do update
    set observation_id = excluded.observation_id,
        absolute_error = excluded.absolute_error,
        ape = excluded.ape,
        evaluated_at = excluded.evaluated_at
  where evaluation.observation_id is distinct from excluded.observation_id
     or evaluation.absolute_error is distinct from excluded.absolute_error
     or evaluation.ape is distinct from excluded.ape;

  get diagnostics v_affected = row_count;
  return v_affected;
end;
$$;

revoke all on function public.evaluate_available_predictions()
  from public, anon, authenticated;
grant execute on function public.evaluate_available_predictions()
  to service_role;

create view public.official_prediction_errors
with (security_invoker = true)
as
select
  prediction.prediction_id,
  prediction.model_id,
  model.model_name,
  model.version as model_version,
  prediction.cycle_id,
  cycle.origin_at,
  prediction.station_id,
  prediction.horizon_minutes,
  prediction.target_at,
  prediction.predicted_demand,
  observation.demand as observed_demand,
  evaluation.absolute_error,
  evaluation.ape,
  evaluation.evaluated_at
from public.predictions as prediction
join public.models as model
  on model.model_id = prediction.model_id
join public.forecast_cycles as cycle
  on cycle.cycle_id = prediction.cycle_id
join public.prediction_evaluations as evaluation
  on evaluation.prediction_id = prediction.prediction_id
join public.observations as observation
  on observation.observation_id = evaluation.observation_id
where exists (
  select 1
  from public.submission_predictions as submitted_prediction
  join public.submissions as submission
    on submission.submission_id = submitted_prediction.submission_id
  where submitted_prediction.prediction_id = prediction.prediction_id
    and submission.status = 'accepted'
    and submission.is_official
    and submission.predictions_received = submission.expected_predictions
);

revoke all on table public.official_prediction_errors from anon, authenticated;
grant select on table public.official_prediction_errors to service_role;

create view public.official_cycle_station_metrics
with (security_invoker = true)
as
select
  error.model_id,
  error.model_name,
  error.model_version,
  error.cycle_id,
  error.origin_at,
  error.station_id,
  count(*)::integer as evaluated_targets,
  cardinality(cycle.horizons_minutes)::integer as expected_targets,
  count(*)::double precision
    / nullif(cardinality(cycle.horizons_minutes), 0)::double precision as coverage,
  sum(error.absolute_error) / nullif(sum(error.observed_demand), 0)::double precision
    as wape,
  greatest(
    0::double precision,
    1::double precision
      - sum(error.absolute_error)
        / nullif(sum(error.observed_demand), 0)::double precision
  ) as accuracy
from public.official_prediction_errors as error
join public.forecast_cycles as cycle
  on cycle.cycle_id = error.cycle_id
group by
  error.model_id,
  error.model_name,
  error.model_version,
  error.cycle_id,
  error.origin_at,
  error.station_id,
  cycle.horizons_minutes;

revoke all on table public.official_cycle_station_metrics from anon, authenticated;
grant select on table public.official_cycle_station_metrics to service_role;

create view public.official_cycle_metrics
with (security_invoker = true)
as
select
  station_metric.model_id,
  station_metric.model_name,
  station_metric.model_version,
  station_metric.cycle_id,
  station_metric.origin_at,
  sum(station_metric.evaluated_targets)::integer as evaluated_targets,
  sum(station_metric.expected_targets)::integer as expected_targets,
  sum(station_metric.evaluated_targets)::double precision
    / nullif(sum(station_metric.expected_targets), 0)::double precision as coverage,
  avg(station_metric.wape) as mean_station_wape,
  avg(station_metric.accuracy) as accuracy
from public.official_cycle_station_metrics as station_metric
group by
  station_metric.model_id,
  station_metric.model_name,
  station_metric.model_version,
  station_metric.cycle_id,
  station_metric.origin_at;

revoke all on table public.official_cycle_metrics from anon, authenticated;
grant select on table public.official_cycle_metrics to service_role;

create view public.official_model_last_six_metrics
with (security_invoker = true)
as
with complete_cycles as (
  select
    cycle_metric.*,
    row_number() over (
      partition by cycle_metric.model_id
      order by cycle_metric.origin_at desc, cycle_metric.cycle_id desc
    ) as recency
  from public.official_cycle_metrics as cycle_metric
  where cycle_metric.evaluated_targets = cycle_metric.expected_targets
), recent_errors as (
  select error.*
  from public.official_prediction_errors as error
  join complete_cycles as cycle
    on cycle.model_id = error.model_id
   and cycle.cycle_id = error.cycle_id
  where cycle.recency <= 6
), station_window as (
  select
    error.model_id,
    error.model_name,
    error.model_version,
    error.station_id,
    count(distinct error.cycle_id)::integer as cycle_count,
    count(*)::integer as evaluated_targets,
    sum(error.absolute_error) / nullif(sum(error.observed_demand), 0)::double precision
      as wape,
    greatest(
      0::double precision,
      1::double precision
        - sum(error.absolute_error)
          / nullif(sum(error.observed_demand), 0)::double precision
    ) as accuracy
  from recent_errors as error
  group by
    error.model_id,
    error.model_name,
    error.model_version,
    error.station_id
)
select
  station.model_id,
  station.model_name,
  station.model_version,
  max(station.cycle_count)::integer as cycle_count,
  sum(station.evaluated_targets)::integer as evaluated_targets,
  avg(station.wape) as mean_station_wape,
  avg(station.accuracy) as accuracy
from station_window as station
group by station.model_id, station.model_name, station.model_version;

revoke all on table public.official_model_last_six_metrics from anon, authenticated;
grant select on table public.official_model_last_six_metrics to service_role;

comment on view public.official_prediction_errors is
  'Errores oficiales por target, consultables por ciclo, estacion y horizonte.';
comment on view public.official_cycle_metrics is
  'Cobertura y accuracy oficial por ciclo, promediada por estacion.';
comment on view public.official_model_last_six_metrics is
  'Accuracy por estacion del modelo sobre sus ultimos seis ciclos completamente evaluados.';
