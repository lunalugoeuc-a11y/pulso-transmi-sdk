# Fase final: observaciones v2 y adaptación

## Cambio detectado

- Contrato nuevo a partir de `observed_at > 2026-09-20T12:00:00Z` (tiempo
  virtual).
- Las páginas del stream pueden mezclar registros v1 y v2.
- v2 reemplaza `demand` por `measurement.value`, un decimal en texto, y agrega
  `measurement.quality` (`observed` o `missing`).
- Un registro `missing` representa ausencia de información de entrenamiento y
  no demanda cero.

## Impacto observado

Las ejecuciones iniciadas el 3 de octubre de 2026 a las 18:40, 18:50 y 19:09
America/Bogota fallaron durante `sync`. La primera observación v2 fue la estación
`02300` a las `2026-09-20T12:15:00Z`; el colector anterior buscó el campo plano
`demand` y Supabase rechazó el valor nulo por la restricción vigente.

El fallo ocurrió antes de consultar el ciclo. Por ello no debe interpretarse
como drift del modelo ni como prueba de un ciclo ausente.

## Reparación

El commit `b02ef39` incorporó:

1. normalización estricta y compatible con páginas mixtas v1/v2;
2. conversión validada del decimal v2 sin separadores;
3. persistencia de `source_schema_version`, `quality`, `unit` y `released_at`;
4. almacenamiento de `missing` con demanda nula, nunca con cero;
5. exclusión explícita de faltantes en consultas de entrenamiento y evaluación;
6. cursor y escritura de página dentro de la misma transacción;
7. pruebas de páginas mixtas, faltantes, unidades, timestamps y valores inválidos.

La migración `support_observation_v2` quedó aplicada en Supabase. Su prueba
transaccional confirmó que un registro v2 `missing` se conserva con `demand`
nulo. El asesor de seguridad no reportó hallazgos.

## Primera recuperación de ingesta

La ejecución `03ac2c8c-b736-4d0d-8034-516a74fb1027` terminó correctamente el
3 de octubre de 2026 a las 19:34 America/Bogota:

- página mixta procesada: 3.092 registros;
- cursor virtual recuperado hasta `2026-09-20T13:30:00Z`;
- observaciones v2 persistidas: 72, todas con calidad `observed`;
- faltantes v2 recibidos hasta ese momento: 0;
- ciclo oficial abierto durante la recuperación: ninguno;
- submission generada: ninguna, correctamente.

La primera entrega recuperada se registrará cuando la API anuncie un ciclo
abierto. El intervalo sin ciclo no cuenta como ausencia.

## Referencia previa y modelo vigente

La referencia congelada de los seis ciclos completamente resueltos hasta
`2026-09-20T12:00:00Z` tiene:

- cobertura media: 100 %;
- accuracy media: 82,99 %;
- accuracy mínima: 48,74 %.

El champion vigente continúa siendo `7.0.0`, algoritmo **Drift adaptive Extra
Trees short-lag recency weighted**. Sus rangos registrados son:

- entrenamiento: `2026-07-26T05:00:00Z` a `2026-09-19T04:00:00Z`;
- validación temporal: `2026-09-19T05:15:00Z` a
  `2026-09-19T11:00:00Z`.

La reparación de ingesta no se presentó como un reentrenamiento. Un candidato
nuevo solo se promoverá después de disponer de ground truth v2 suficiente,
compararlo temporalmente contra el champion sobre la misma ventana y confirmar
que mejora accuracy sin reducir cobertura.

## Seguimiento

La vigilancia horaria distingue tres fenómenos:

- **cobertura operativa:** ciclo abierto con submission aceptada 48/48;
- **missingness de la fuente:** proporción de observaciones v2 con
  `quality=missing`;
- **drift de demanda:** degradación sobre ground truth con cobertura suficiente,
  comparada contra la referencia de seis ciclos.

Esto evita reentrenar por un fallo del conector o interpretar un faltante como
un cambio real de demanda.
