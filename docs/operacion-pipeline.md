# Operación automática del pipeline

El champion vigente se documenta en
[`modelo-extra-trees-v4.md`](modelo-extra-trees-v4.md). Se reentrena en cada
ciclo únicamente con observaciones cuyo timestamp no supera el `data_cutoff`.

## Implementación disponible

El módulo `pulso_transmi.pipeline` implementa el loop operativo del contrato
Pulso TransMi `1.0`:

1. crea una ejecución trazable en `pipeline_runs`;
2. lee el cursor confirmado y sincroniza `/v1/stream/observations`;
3. persiste cada página y su cursor en una transacción de PostgreSQL;
4. consulta `/v1/forecast-cycles/current` y termina en verde ante
   `404 no_open_cycle`;
5. evita un nuevo POST si ya existe un recibo aceptado para ciclo y champion;
6. carga el modelo promovido en Supabase;
7. genera y valida exactamente los targets publicados por la API;
8. conserva las predicciones antes del envío;
9. envía el batch con una `Idempotency-Key` derivada del payload canónico;
10. guarda el recibo, el hash, el commit y la relación con las predicciones.

El pipeline admite Extra Trees y baselines estacionales diarios (`lag 96`) y semanales
(`lag 672`). En una validación temporal sobre los últimos siete días de las
51.840 observaciones iniciales, el baseline diario obtuvo 77,89 % de accuracy
promedio por estación y el semanal obtuvo 83,11 %. Por ello, el candidato
recomendado para promoción es `seasonal_naive_lag_672:2.0.0`. Si el instante
estacional falta, usa el último valor conocido hasta `data_cutoff`.

Tras observar resultados revelados de cinco ciclos oficiales, se evaluó un
candidato híbrido que promedia los lags diario y semanal. En esos targets pasó
de 79,85 % para el semanal a 82,41 %, y también superó al semanal en la mayoría
de los días del backtesting reciente. La versión promovida siguiente es
`hybrid_lag_96_672:3.0.0`; si uno de los dos lags falta, usa el disponible.

## Ejecución local controlada

```bash
export PULSO_API_KEY='...'
export SUPABASE_URL='https://bxokvetjqputudvuentu.supabase.co'
export SUPABASE_SERVICE_KEY='...'
python -m pulso_transmi.pipeline
```

No ejecutes este comando con credenciales copiadas en el historial de una
terminal compartida. Un `404 no_open_cycle` imprime el estado y devuelve código
cero.

## GitHub Actions

El horario vive en GitHub, no en el computador del estudiante. El workflow se
despierta cada cinco minutos (cron en UTC), por lo que sigue operando aunque el
computador personal esté apagado. Esta frecuencia compensa retrasos u omisiones
del scheduler y ofrece varios intentos dentro de cada ventana de 25 minutos. La
consulta del ciclo y la idempotencia impiden duplicar entregas.

El workflow [`.github/workflows/predict.yml`](../.github/workflows/predict.yml)
despierta cada cinco minutos y también admite ejecución manual. Usa
`concurrency` para no solapar dos ejecuciones y un timeout de ocho minutos.

Como respaldo ante retrasos u omisiones del scheduler nativo de GitHub,
Supabase ejecuta cada cinco minutos el job
`pulso-transmi-github-dispatch`. El job usa `pg_cron` y `pg_net` para invocar
`workflow_dispatch`; la credencial vive cifrada en Vault con el nombre
`github_actions_dispatch_token`. El token está limitado al repositorio
`lunalugoeuc-a11y/pulso-transmi-sdk` y al permiso `Actions: read/write`.
La API sigue decidiendo si existe un ciclo y la idempotencia evita envíos
duplicados. El token vigente expira el 25 de octubre de 2026 y debe rotarse en
Vault antes de esa fecha.

Configura estos valores en **Settings → Secrets and variables → Actions**:

| Nombre | Tipo | Estado |
|---|---|---|
| `PULSO_API_URL` | Variable | Configurada |
| `SUPABASE_URL` | Secret | Configurado |
| `PULSO_API_KEY` | Secret | Configurado |
| `SUPABASE_SERVICE_KEY` | Secret | Configurado |

No uses la clave `anon` para el pipeline y no expongas `SUPABASE_SERVICE_KEY` en
el navegador. Las claves nuevas `sb_secret_*` se envían a Supabase únicamente en
el encabezado `apikey`; las claves antiguas JWT también requieren
`Authorization: Bearer`.

## Guardrails

- El ciclo, corte, deadline y targets siempre vienen de la API.
- `training_data_end` debe ser menor o igual que `data_cutoff`.
- El conjunto de parejas estación/instante debe coincidir exactamente.
- No se aceptan duplicados, `NaN`, infinitos, negativos ni valores mayores a
  100.000.
- El payload se rechaza localmente si supera 64 KB.
- Los reintentos por timeout, `429` o `5xx` conservan la misma llave.
- `401`, `409` y `422` no se reintentan a ciegas.
- Los logs muestran cantidades e identificadores, nunca claves ni el payload
  completo.

## Verificación

La suite cubre el caso sin ciclo, coincidencia exacta de targets, rechazo de
duplicados, encabezados seguros de Supabase, RPC atómico y envío idempotente.
La consulta de solo lectura contra la API en vivo confirmó que el stream está
publicando datos y que la ausencia temporal de ciclo se interpreta normalmente.

La automatización está operativa tanto por el cron nativo del workflow como por
el disparador redundante de Supabase. La ejecución de verificación `#84` fue
aceptada por GitHub y terminó en verde con `collector: 4572 rows processed` y
`cycle: no_open_cycle`, que es el comportamiento correcto cuando no hay ventana.
