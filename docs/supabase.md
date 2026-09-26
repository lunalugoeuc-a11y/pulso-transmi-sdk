# Supabase · Pulso TransMi

## Proyecto enlazado

| Propiedad | Valor |
|---|---|
| Nombre | `pulso-transmi` |
| Project ref | `bxokvetjqputudvuentu` |
| Región | `sa-east-1` |
| URL | `https://bxokvetjqputudvuentu.supabase.co` |
| Última migración remota | `20260926143101_add_automatic_prediction_evaluation` |

El repositorio contiene dos representaciones complementarias:

- [`../supabase/schema.sql`](../supabase/schema.sql): fotografía declarativa de
  las 18 tablas y sus relaciones;
- [`../supabase/migrations/`](../supabase/migrations/): cambios incrementales que
  se aplican con Supabase CLI.

El modelo entidad–relación conceptual está documentado en
[`../documentacion/diagrama-entidad-relacion.md`](../documentacion/diagrama-entidad-relacion.md).

## Modelo desplegado

| Área | Tablas | Responsabilidad |
|---|---|---|
| Datos | `stations`, `time_context`, `observations`, `data_batches` | Catálogo, contexto, demanda e ingestas reproducibles. |
| Orquestación | `pipeline_runs`, `pipeline_errors`, `collector_state` | Ejecuciones, fallos y cursor incremental confirmado. |
| Features y entrenamiento | `feature_sets`, `feature_definitions`, `training_runs` | Linaje de variables y ventanas sin fuga temporal. |
| Modelos | `models`, `model_metrics`, `drift_checks` | Artefactos, métricas y señales de deriva. |
| Competencia | `forecast_cycles`, `predictions`, `submissions`, `submission_predictions`, `prediction_evaluations` | Contrato del ciclo, pronósticos, recibos, contenido enviado y evaluación. |

La migración de operaciones competitivas agrega:

- un cursor durable que solo debe avanzar después de confirmar la escritura;
- una copia auditable del contrato recibido de
  `GET /v1/forecast-cycles/current`;
- `cycle_id` y `horizon_minutes` en cada predicción;
- una `Idempotency-Key` única, hash del payload, número de intento y recibo de
  la API por submission;
- la relación exacta entre cada submission y sus predicciones.

## Flujo persistente recomendado

1. El workflow consulta el ciclo vigente. Un `404 no_open_cycle` termina
   correctamente y no crea un envío.
2. El recolector sincroniza datos desde `collector_state.cursor_value`. El
   cursor se confirma únicamente después de persistir todo el lote.
3. El pipeline guarda su ejecución, lote, modelo y predicciones.
4. Antes de enviar, valida que el conjunto coincida exactamente con el contrato
   guardado en `forecast_cycles.contract_json`.
5. Calcula un hash canónico del payload y una clave de idempotencia estable.
6. Envía el batch completo y conserva tanto el intento como el recibo, incluso
   si la API lo rechaza.
7. Cuando aparece el ground truth, vincula cada predicción con su observación y
   registra la evaluación. Este paso ya es automático: cada ejecución llama a
   `evaluate_available_predictions()` después de sincronizar el stream. El RPC
   solo considera submissions oficiales, aceptados y completos; repetirlo no
   duplica evaluaciones.

## Evaluación automática

La evaluación se persiste en `prediction_evaluations` en cuanto existe una
observación con la misma estación y `target_at`. Las siguientes vistas privadas
permiten analizar el resultado sin recalcular uniones manualmente:

- `official_prediction_errors`: detalle por ciclo, estación y horizonte;
- `official_cycle_station_metrics`: WAPE y accuracy por estación en cada ciclo;
- `official_cycle_metrics`: cobertura y accuracy de cada ciclo;
- `official_model_last_six_metrics`: ventana comparable de los últimos seis
  ciclos completamente evaluados de cada modelo.

La fórmula de las vistas sigue el contrato oficial: calcula WAPE por estación,
lo convierte a accuracy con piso en cero y después promedia estaciones. Las
vistas y el RPC están restringidos a `service_role`.

## Seguridad

RLS está habilitado en las 18 tablas. Los roles de Data API `anon` y
`authenticated` no tienen permisos y además están cubiertos por una política
`backend_only` que siempre deniega. `service_role` tiene los privilegios que
necesita el pipeline y omite RLS.

`PULSO_API_KEY` y la clave `service_role` deben vivir únicamente en GitHub
Actions Secrets o en un gestor de secretos del backend. Nunca deben aparecer en
el código, commits, logs, frontend ni variables públicas de Vercel.

El token usado para disparar GitHub Actions está cifrado en Supabase Vault bajo
`github_actions_dispatch_token`; no forma parte de la migración ni del código.
Solo tiene acceso al repositorio del proyecto y permiso de escritura sobre
Actions. Debe rotarse antes de su expiración el 25 de octubre de 2026.

## Cron redundante

La migración `20260925150736_add_github_actions_dispatch_cron.sql` habilita
`pg_cron` y `pg_net` y registra `pulso-transmi-github-dispatch` con frecuencia
de cinco minutos. Cada ejecución solicita a GitHub el workflow `predict.yml` en
la rama `main`. GitHub respondió `204` a la prueba de despacho y el workflow
`#84` concluyó correctamente. Si no existe ciclo abierto, el pipeline termina
en verde sin enviar predicciones; si ya existe recibo, no duplica el submission.

## Desarrollo y migraciones

Desde la raíz del repositorio:

```bash
supabase link --project-ref bxokvetjqputudvuentu
supabase migration list
supabase db push --dry-run
supabase db push
```

El enlace local y los tokens no se versionan. Para un cambio nuevo, crear otra
migración; no modificar una que ya fue aplicada remotamente.

## Verificación realizada

Después de aplicar la migración se comprobó que:

- permanecen 12 estaciones, 4.320 contextos y 51.840 observaciones;
- las 8.064 predicciones históricas recibieron un `horizon_minutes` consistente;
- existe el cursor inicial `observations` y las nuevas tablas están vacías hasta
  el primer ciclo o envío;
- las 18 tablas tienen RLS y política `backend_only`;
- `anon` no puede leer ciclos y `authenticated` no puede insertar submissions;
- `service_role` sí conserva acceso operativo;
- el asesor de seguridad no reporta observaciones.

El asesor de rendimiento solo informa índices aún no usados. Es esperable antes
de que el pipeline competitivo empiece a consultar las tablas nuevas y debe
revisarse de nuevo después de acumular ejecuciones reales.
