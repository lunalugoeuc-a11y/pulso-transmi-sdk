# Modelo drift-adaptive Extra Trees 7.0.0

## Motivo

El cambio de régimen observado desde el 18 de septiembre degradó el modelo
4.0.0 aunque las entregas siguieron llegando completas. El problema era de
calidad predictiva, no del conector: un modelo basado principalmente en rezagos
diarios y semanales tarda demasiado en reaccionar cuando cambia el nivel de
demanda de todas las estaciones.

## Diseño sin fuga temporal

El modelo genera ejemplos separados para los horizontes de 15, 30, 45 y 60
minutos. Para un objetivo histórico en `t` y horizonte `h`, la variable
`lag_cutoff` usa exactamente el valor disponible en `t-h`. También incorpora:

- nivel una y tres horas antes del corte;
- rezagos de uno, dos y siete días;
- cambios recientes de nivel;
- estación, intervalo de 15 minutos, día de semana y fin de semana.

La ventana de entrenamiento se limita a los últimos 21 días y cada ejemplo se
pondera con una vida media de tres días. Así los datos posteriores al drift
pesan más sin descartar por completo el patrón histórico. El modelo se
reconstruye en cada ciclo usando únicamente observaciones anteriores o iguales
al `data_cutoff`.

## Promoción segura

La versión se registra primero como candidata. El workflow de selección la
compara con el champion sobre los mismos seis ciclos oficiales completos y solo
la promueve si:

1. produce el 100 % de los objetivos esperados;
2. mejora al menos 0,5 puntos porcentuales de accuracy;
3. no reduce más de tres puntos el accuracy de la peor estación.

Si no cumple las tres condiciones, el modelo 4.0.0 permanece activo.
