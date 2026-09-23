# Pulso TransMi — SDK para estudiantes

Starter kit oficial del reto MLOps **Pulso TransMi**. Incluye un cliente Python,
ejemplos reproducibles y una plantilla de GitHub Actions para construir un
pipeline que sincroniza datos, descubre ciclos y envía predicciones trazables.

## Documentación

- [Documentación técnica integral](docs/documentacion-tecnica.md): arquitectura,
  instalación, componentes, datos, EDA, pruebas, automatización, seguridad,
  estado y pendientes.
- [Modelo entidad–relación](documentacion/diagrama-entidad-relacion.md): entidades,
  claves, cardinalidades, evidencia y límites del modelo lógico.
- [Referencia de la API](docs/api.md): paginación, filtros, descargas y errores.
- [Guía del proyecto estudiantil](docs/student-project.md): etapas y entregables.
- [Guía del EDA](eda%20proyecto/README.md): ejecución y contenido del notebook.
- [Proyecto Supabase](docs/supabase.md): instancia, esquema desplegado, seguridad
  y orden de carga.
- [Operación del pipeline](docs/operacion-pipeline.md): loop, secretos,
  guardrails, automatización y verificación.

> **Disponible públicamente:** la API de lectura está en
> `https://pulso-transmi.72-60-245-2.sslip.io` y su documentación interactiva en
> [`/docs`](https://pulso-transmi.72-60-245-2.sslip.io/docs).

## El reto

Se pronostica demanda sintética cada 15 minutos para 12 estaciones reales de
TransMilenio. El sistema liberará observaciones con el tiempo y cambiará algunos
patrones durante la competencia. Un modelo entrenado una sola vez puede perder
desempeño: el objetivo es operar un pipeline capaz de medir, decidir y
reentrenar.

La demanda, clima y eventos son sintéticos. Los nombres y coordenadas de las
estaciones provienen de datos oficiales de TransMilenio.

## Inicio rápido

Requiere Python 3.11 o superior.

```bash
git clone https://github.com/uexternadojz/pulso-transmi-sdk.git
cd pulso-transmi-sdk
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[ml]'
cp .env.example .env
python examples/01_download.py
python examples/02_naive_baseline.py
```

En Windows PowerShell, la activación es `.venv\Scripts\Activate.ps1`.

## Uso del SDK

```python
from pulso_transmi import PulsoTransmiClient

client = PulsoTransmiClient()

print(client.meta())
stations = client.stations()
observations = client.observations_dataframe(station_id="07107")
context = client.context_dataframe()

print(stations.head())
print(observations.tail())
```

El SDK recorre automáticamente todas las páginas. Si prefieres controlar cada
página, usa `client.observations_page(...)` y conserva `next_cursor` exactamente
como lo entrega la API.

## Datos iniciales

| Recurso | Tamaño |
|---|---:|
| Estaciones | 12 |
| Frecuencia | 15 minutos |
| Historia | 45 días |
| Periodos por estación | 4.320 |
| Observaciones | 51.840 |

Para evaluación local, usa una división temporal: por ejemplo, primeros 38 días
para entrenamiento y últimos 7 para validación. Una partición aleatoria mezcla
futuro y pasado y genera métricas engañosas.

## API y competencia

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/health` | Estado básico |
| `GET` | `/v1/meta` | Versión, rango, hashes y enlaces |
| `GET` | `/v1/stations` | Catálogo geográfico |
| `GET` | `/v1/observations` | Demanda paginada |
| `GET` | `/v1/context` | Clima y eventos |
| `GET` | `/v1/downloads/{filename}` | Descarga completa |
| `GET` | `/v1/stream/observations` | Observaciones incrementales |
| `GET` | `/v1/forecast-cycles/current` | Ciclo y targets exactos |
| `POST` | `/v1/submissions` | Batch atómico e idempotente |

Swagger está disponible en `/docs`. Consulta [docs/api.md](docs/api.md) para
filtros, paginación y errores.

## Estructura esperada del proyecto estudiantil

```text
mi-pulso-transmi/
├── src/
│   ├── ingest.py
│   ├── features.py
│   ├── train.py
│   ├── predict.py
│   └── monitor.py
├── tests/
├── artifacts/
├── requirements.txt o pyproject.toml
└── .github/workflows/pipeline.yml
```

El repositorio de cada equipo debe dejar trazabilidad de:

- cutoff de datos usado;
- versión o commit del código;
- features y modelo entrenado;
- métricas de validación temporal;
- momento y razón de cada reentrenamiento;
- errores de ingesta o inferencia.

## GitHub Actions

El workflow [`.github/workflows/predict.yml`](.github/workflows/predict.yml)
consulta la API cada 10 minutos. El cron únicamente despierta el proceso: la API
decide si existe un ciclo abierto. Configura `PULSO_API_KEY`, `SUPABASE_URL` y
`SUPABASE_SERVICE_KEY` como GitHub Actions Secrets.

Nunca escribas API keys, contraseñas de Supabase ni tokens dentro del código.

## Supabase y Vercel

Supabase conserva ejecuciones, cursores, modelos, predicciones y recibos. Vercel
continúa siendo opcional y corresponde al bono de visualización.

Consulta [docs/student-project.md](docs/student-project.md) para el flujo completo
y los entregables.

## Métrica

La referencia actual es:

```text
WAPE = sum(abs(real - predicción)) / sum(real)
Accuracy = 100 × max(0, 1 - WAPE)
```

La métrica se calcula por estación y luego se promedia. El contrato vigente de
submissions usa `schema_version: "1.0"`; siempre prevalece la respuesta del ciclo
actual y la documentación del repositorio central.

## Desarrollo del SDK

```bash
python -m pip install -e '.[dev,ml]'
pytest -q
```

Este repositorio es público para estudiantes. No debe contener ground truth
futuro, semillas, configuración privada del escenario ni parámetros de drift.
