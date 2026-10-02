# Candidato drift-adaptive por horizonte 7.1.0

## Objetivo

El modelo 7.0.0 reaccionó correctamente al cambio de régimen y mantuvo el
contrato de 48/48 predicciones. Sin embargo, el horizonte de 60 minutos quedó
por debajo de los horizontes de 15, 30 y 45 minutos. La versión 7.1.0 intenta
mejorar ese punto sin cambiar el conector ni el horario de entregas.

## Diseño

La preparación de datos, la ventana de 21 días y la ponderación con vida media
de tres días son las mismas del 7.0.0. La diferencia es que 7.1.0 entrena un
Extra Trees independiente para cada horizonte. Así, los patrones de corto
plazo no dominan el estimador de 60 minutos.

Para el horizonte de 60 minutos se aplica además una corrección conservadora:
85 % de la predicción del modelo y 15 % del último nivel conocido en el corte.
Ese dato es observable antes de la ventana y no introduce fuga temporal.

## Guardrails de promoción

La versión se registra inactiva y se compara con el champion sobre los mismos
seis ciclos oficiales completos. Solo se promueve si:

1. genera todos los objetivos esperados;
2. mejora al menos 0,5 puntos porcentuales el accuracy promedio por estación;
3. no empeora más de tres puntos la peor estación.

Si no cumple esas condiciones, 7.0.0 continúa activo. La validación no modifica
ni duplica entregas oficiales.
