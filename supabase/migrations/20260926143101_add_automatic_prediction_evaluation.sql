-- Evalua automaticamente las predicciones oficiales cuando aparece su ground truth.
-- El RPC es idempotente y solo puede ejecutarlo el backend service_role.

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
    where exists (
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

comment on function public.evaluate_available_predictions() is
  'Vincula submissions oficiales completos con observaciones disponibles y guarda sus errores de forma idempotente.';

create or replace view public.official_prediction_errors
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

create or replace view public.official_cycle_station_metrics
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

create or replace view public.official_cycle_metrics
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

create or replace view public.official_model_last_six_metrics
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
