# API de lectura y competencia

La URL base pública es:

```text
https://pulso-transmi.72-60-245-2.sslip.io
```

## Paginación

`/v1/observations` y `/v1/context` devuelven como máximo 5.000 filas. Una
respuesta incluye `data`, `count` y `next_cursor`. Envía el cursor sin
modificarlo en la solicitud siguiente. `null` indica el final.

```python
cursor = None
while True:
    page = client.observations_page(cursor=cursor, limit=5000)
    process(page["data"])
    cursor = page["next_cursor"]
    if cursor is None:
        break
```

## Filtros

```text
GET /v1/observations?station_id=07107
GET /v1/observations?start=2026-08-01T00:00:00-05:00&limit=5000
GET /v1/context?start=2026-08-01T00:00:00-05:00
```

`start` y `end` son inclusivos y deben incluir zona horaria. Los IDs de estación
son texto: no elimines sus ceros iniciales.

## Descargas completas

```text
GET /v1/downloads/stations.csv
GET /v1/downloads/observations.csv
GET /v1/downloads/context.csv
GET /v1/downloads/metadata.json
```

El SDK verifica automáticamente el SHA-256 declarado en `/v1/meta`.

## Errores

| Código | Significado |
|---:|---|
| 400 | Cursor inválido |
| 404 | Archivo o ruta inexistente |
| 422 | Parámetro inválido |
| 429 | Demasiadas solicitudes; espera antes de reintentar |
| 5xx | Falla temporal del servidor |

## Loop competitivo

`GET /v1/stream/observations` publica datos incrementales. El cursor es opaco y
solo se confirma después de guardar la página. `GET
/v1/forecast-cycles/current` devuelve el contrato exacto; un `404` con código
`no_open_cycle` es un resultado normal.

`POST /v1/submissions` requiere `Authorization: Bearer $PULSO_API_KEY` e
`Idempotency-Key`. El payload `1.0` contiene `cycle_id`, `client_run_id`,
`data_cutoff`, metadata del modelo y la lista completa de predicciones. El SDK
valida localmente que las parejas `station_id + target_at` coincidan exactamente
con los targets del ciclo.

El servidor devuelve `201` para una entrega nueva y `200` al repetir el mismo
contenido con la misma llave. Un `409 idempotency_conflict` indica que la llave
se reutilizó con contenido diferente.
