# Evaluación de producción del champion 6.0.0

## Reparación aplicada

El 26 de septiembre de 2026 se habilitó la evaluación automática de toda
predicción incluida en una submission oficial, aceptada y completa. El proceso
se ejecuta después de sincronizar el stream, cruza por estación y `target_at`, y
guarda el error de forma idempotente en `prediction_evaluations`.

El backfill inicial encontró 888 targets evaluables de 6.0.0 y los guardó. Los
24 targets restantes correspondían al ciclo más reciente, cuyo ground truth aún
no estaba completo. Una segunda ejecución afectó cero filas, confirmando que el
proceso no duplica evaluaciones.

## Resultado de los últimos seis ciclos completos

La ventana contiene 288 de 288 targets evaluados. Aplicando WAPE por estación,
accuracy con piso en cero y promedio de las doce estaciones, 6.0.0 obtuvo
69,53 %. Los WAPE agregados por horizonte fueron:

| Horizonte | WAPE |
|---:|---:|
| 15 minutos | 30,98 % |
| 30 minutos | 28,51 % |
| 45 minutos | 31,24 % |
| 60 minutos | 31,67 % |

El problema no está concentrado en un horizonte. La estación Banderas (`05100`)
sí es un outlier, con 127,65 % de WAPE; las siguientes fueron Ricaurte - NQS
(`07111`) con 36,46 % y Calle 72 (`09122`) con 34,96 %.

## Diagnóstico del modelo

Las 288 predicciones de 6.0.0 en la ventana coincidieron exactamente con el
rezago semanal. Esto demuestra que, durante esos ciclos, el perfil adaptativo
no contó con suficiente profundidad histórica y terminó comportándose como una
única observación semanal, aunque el registro del champion indicara un perfil
HL14.

Se simularon alternativas sobre los mismos targets, usando exclusivamente
valores anteriores al objetivo:

| Candidato | Coverage | Accuracy |
|---|---:|---:|
| Champion observado 6.0.0 / rezago semanal | 100 % | 69,53 % |
| Perfil HL14 con historia completa | 100 % | 72,51 % |
| Perfil HL45 con historia completa | 100 % | 72,83 % |
| Promedio diario/semanal preexistente | 100 % | **73,71 %** |

El promedio diario/semanal mejora 4,18 puntos en esta ventana, pero no alcanza
el desempeño histórico esperado ni justifica por sí solo prometer una posición
en el ranking. Primero debe confirmarse en otra ventana posterior. Mientras
tanto, la evaluación automática permitirá tomar esa decisión con datos reales
y detectar si Banderas requiere una calibración específica.

## Consultas operativas

- `official_prediction_errors`: detalle por target para estación y horizonte;
- `official_cycle_station_metrics`: WAPE y accuracy por estación/ciclo;
- `official_cycle_metrics`: coverage y accuracy por ciclo;
- `official_model_last_six_metrics`: resumen comparable de seis ciclos completos.

Estas vistas son privadas y solo `service_role` puede consultarlas.
