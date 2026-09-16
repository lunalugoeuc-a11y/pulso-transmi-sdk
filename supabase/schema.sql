-- Esquema reproducible aplicado al proyecto Supabase pulso-transmi.
-- Project ref: bxokvetjqputudvuentu

create table public.stations (
  station_id text primary key,
  station_name text not null,
  corridor text not null,
  latitude double precision not null check (latitude between -90 and 90),
  longitude double precision not null check (longitude between -180 and 180),
  constraint stations_station_id_not_blank check (btrim(station_id) <> ''),
  constraint stations_station_name_not_blank check (btrim(station_name) <> ''),
  constraint stations_corridor_not_blank check (btrim(corridor) <> '')
);

create table public.context (
  observed_at timestamptz primary key,
  rain_mm double precision not null check (rain_mm >= 0),
  rain_forecast double precision not null check (rain_forecast >= 0),
  temperature_c double precision not null,
  temperature_forecast double precision not null,
  event_intensity double precision not null check (event_intensity >= 0)
);

create table public.observations (
  station_id text not null,
  observed_at timestamptz not null,
  demand integer not null check (demand >= 0),
  primary key (station_id, observed_at),
  constraint observations_station_id_fkey
    foreign key (station_id) references public.stations (station_id)
    on update cascade on delete restrict,
  constraint observations_observed_at_fkey
    foreign key (observed_at) references public.context (observed_at)
    on update cascade on delete restrict
);

create index observations_observed_at_idx
  on public.observations (observed_at);

alter table public.stations enable row level security;
alter table public.context enable row level security;
alter table public.observations enable row level security;

revoke all on table public.stations from anon, authenticated;
revoke all on table public.context from anon, authenticated;
revoke all on table public.observations from anon, authenticated;

grant select on table public.stations to anon, authenticated;
grant select on table public.context to anon, authenticated;
grant select on table public.observations to anon, authenticated;

grant all privileges on table public.stations to service_role;
grant all privileges on table public.context to service_role;
grant all privileges on table public.observations to service_role;

create policy "stations_are_publicly_readable"
  on public.stations for select to anon, authenticated using (true);

create policy "context_is_publicly_readable"
  on public.context for select to anon, authenticated using (true);

create policy "observations_are_publicly_readable"
  on public.observations for select to anon, authenticated using (true);

comment on table public.stations is
  'Catálogo de estaciones de Pulso TransMi. station_id es texto para conservar ceros iniciales.';
comment on table public.context is
  'Contexto sintético compartido por todas las estaciones en un instante.';
comment on table public.observations is
  'Demanda sintética por estación e intervalo; clave compuesta station_id + observed_at.';
comment on column public.context.event_intensity is
  'Intensidad sintética de eventos; el contrato público todavía no define una cota superior.';
