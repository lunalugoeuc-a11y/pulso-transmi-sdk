# Modelo apilado HGB + perfil 5.0.0

## Decisión

El candidato adopta la idea de un gradient boosting combinado con un perfil
histórico, pero usa una implementación propia y reproducible. No copia
artefactos, parámetros ni predicciones de otros participantes.

La predicción final combina:

- 75 % Extra Trees con rezagos y calendario;
- 15 % Histogram Gradient Boosting;
- 10 % mediana histórica por estación, día de semana e intervalo de 15 minutos.

Los pesos equivalen a mezclar 75 % del Extra Trees con 25 % de un candidato
HGB/perfil cuyo peso interno del perfil es 40 %. Todos los datos de entrenamiento
y perfiles se limitan al `data_cutoff` oficial.

## Validación temporal

Se usaron las mismas 51.840 observaciones y el mismo corte de siete días del
modelo 4.0.0. Los rezagos de cada objetivo contienen únicamente observaciones
anteriores a ese objetivo, simulando inferencia con origen móvil.

| Modelo | Accuracy promedio | Peor estación | MAE |
|---|---:|---:|---:|
| Extra Trees 4.0.0 | 87,26 % | 85,07 % | 45,72 |
| HGB + perfil puro | 87,07 % | 85,01 % | 46,45 |
| Apilado HGB + perfil 5.0.0 | **87,31 %** | **85,20 %** | **45,58** |

La mejora promedio frente a 4.0.0 es pequeña, de 0,04 puntos. La promoción se
considera experimental y debe reevaluarse con ciclos oficiales; si degrada el
WAPE reciente, se conserva la posibilidad de volver a 4.0.0.

## Parámetros

- Extra Trees: 240 árboles, `min_samples_leaf=8`, `max_features=0.8`;
- HGB: `learning_rate=0.05`, 400 iteraciones, 31 hojas máximas,
  `min_samples_leaf=20`, regularización L2 de 1,0;
- semillas deterministas: 42;
- clipping final: intervalo permitido por el contrato, de 0 a 100.000.

La comparación completa puede reproducirse con
[`../experiments/compare_pulso_models.py`](../experiments/compare_pulso_models.py).
