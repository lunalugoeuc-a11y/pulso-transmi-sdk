# Perfil adaptativo HL14 6.0.0

## Implementación

El modelo reproduce de forma independiente el enfoque sugerido por el nombre
`adaptive-profile-hl14`. Para cada estación y objetivo selecciona observaciones
anteriores del mismo día de semana e intervalo de 15 minutos. Las combina con
ponderación exponencial y vida media de 14 días, de modo que los patrones
recientes pesan más sin eliminar completamente la historia.

Si no existe todavía una coincidencia exacta de día e intervalo, usa el perfil
del mismo intervalo para la estación. Como último respaldo usa la observación
más reciente. Todo el cálculo se limita al `data_cutoff` del ciclo.

## Evidencia y riesgo

En el backtest local de siete días obtuvo 87,03 % de accuracy promedio, frente
a 87,31 % del ensemble 5.0.0. La promoción se realiza por decisión operativa
para contrastar el enfoque con ciclos oficiales. El modelo 5.0.0 permanece
registrado e inactivo para permitir una reversión inmediata si 6.0.0 degrada
los resultados oficiales.

Esta implementación no copia código, parámetros ni artefactos de otros
participantes; únicamente reproduce una interpretación técnica del nombre
visible en el portal.
