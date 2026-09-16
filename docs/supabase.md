# Supabase · Pulso TransMi

## Proyecto

| Propiedad | Valor |
|---|---|
| Nombre | `pulso-transmi` |
| Project ref | `bxokvetjqputudvuentu` |
| Región | `sa-east-1` |
| URL | `https://bxokvetjqputudvuentu.supabase.co` |
| Migración remota | `20260916204552_create_pulso_transmi_core_schema` |

El esquema remoto materializa el modelo entidad–relación documentado en
[`../documentacion/diagrama-entidad-relacion.md`](../documentacion/diagrama-entidad-relacion.md).
La definición SQL reproducible se conserva en [`../supabase/schema.sql`](../supabase/schema.sql).

## Modelo desplegado

- `public.stations`: catálogo; clave primaria `station_id` de tipo `text`.
- `public.context`: clima y eventos; clave primaria `observed_at`.
- `public.observations`: demanda; clave primaria compuesta
  (`station_id`, `observed_at`) y dos claves foráneas.
- `observations_observed_at_idx`: acelera consultas temporales que abarcan
  varias estaciones.

Las claves foráneas usan `ON UPDATE CASCADE` y `ON DELETE RESTRICT`: una estación
o un instante con demanda asociada no se elimina accidentalmente.

## Seguridad

RLS está habilitado en las tres tablas.

| Rol | Lectura | Escritura |
|---|---|---|
| `anon` | Sí | No |
| `authenticated` | Sí | No |
| `service_role` | Sí | Sí |

Esto permite consumir los datos públicos desde la Data API, pero reserva la
ingesta al backend. Una clave `service_role` o secret key nunca debe incluirse en
el navegador, el repositorio o variables con prefijo público.

## Orden de carga

La integridad referencial exige cargar en este orden:

1. `stations`;
2. `context`;
3. `observations`.

Para reemplazar un corte de forma segura, se recomienda una transacción o una
carga incremental mediante `upsert`, siempre desde un proceso backend.

## Verificación inicial

Después de la migración se verificó:

- tres tablas vacías creadas correctamente;
- RLS activo en las tres tablas;
- tres políticas públicas de solo lectura;
- roles `anon` y `authenticated` sin permisos de escritura;
- `service_role` con permiso de ingesta;
- cero observaciones del asesor de seguridad;
- un aviso informativo de índice sin uso, esperado mientras no existan datos.

La creación del esquema no cargó los CSV. La carga de datos es una operación
separada y debe conservar `station_id` como texto y `observed_at` con zona
horaria.
