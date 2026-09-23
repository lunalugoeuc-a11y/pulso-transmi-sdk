# Modelo Extra Trees 4.0.0

## Objetivo

Superar el umbral académico de 85 % de accuracy sin usar información posterior
al `data_cutoff`. El modelo anterior, un promedio de los rezagos diario y
semanal, obtuvo 83,46 % en el mismo corte temporal.

## Variables

- estación codificada de forma determinista;
- seno y coseno del intervalo de 15 minutos;
- seno y coseno del día de la semana e indicador de fin de semana;
- demanda de la misma estación hace 1, 2, 3, 4, 5, 6, 7, 8 y 14 días;
- media y mediana de los seis rezagos diarios más recientes;
- tendencias diaria y semanal.

El rezago mínimo es 96 intervalos (un día). Por eso las variables de los cuatro
horizontes, 15, 30, 45 y 60 minutos, ya existen al momento del corte. No se usan
los valores reales del horizonte pronosticado ni variables publicadas después.

## Validación temporal

Se entrenó con los primeros 38 días del archivo público y se reservaron los
últimos 7 días completos (8.076 observaciones) para validación. La métrica se
calculó como exige el portal: WAPE por estación, conversión a accuracy y
promedio de las 12 estaciones.

| Modelo | Accuracy promedio | Peor estación | MAE |
|---|---:|---:|---:|
| Semanal, lag 672 | 83,11 % | 79,15 % | 60,73 |
| Híbrido diario/semanal | 83,46 % | 79,37 % | 58,08 |
| Extra Trees 4.0.0 | **87,30 %** | **85,11 %** | **45,59** |

Configuración: 240 árboles, `min_samples_leaf=8`, `max_features=0.8` y semilla
42. En cada ejecución el pipeline reconstruye el entrenamiento con los datos
disponibles en Supabase hasta el corte y genera exactamente los targets del
ciclo vigente.

## Criterio de promoción

El modelo se promueve porque supera 85 %, mejora 3,84 puntos frente al híbrido
y también supera 85 % en la estación con menor desempeño. El porcentaje del
portal seguirá dependiendo de los ciclos futuros ya evaluados; la validación no
modifica resultados históricos.
