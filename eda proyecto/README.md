# EDA · Pulso TransMi

Abre [eda_pulso_transmi.ipynb](eda_pulso_transmi.ipynb) en VS Code o Jupyter. El notebook incluye resultados y gráficas de una ejecución verificada.

## Ejecutar nuevamente

Desde la raíz del repositorio, en tu entorno Python:

```bash
python -m pip install -r "eda proyecto/requirements.txt"
python -m jupyterlab "eda proyecto/eda_pulso_transmi.ipynb"
```

Selecciona el kernel de ese entorno y ejecuta todas las celdas en orden. Requiere acceso HTTPS a la API pública; no requiere API key. Puedes cambiar la URL mediante la variable de entorno `PULSO_API_URL`.

Los CSV se leen en memoria y se validan con SHA-256. Los resultados quedan en el notebook al guardarlo; no se guardan CSV ni se modifican datos del servidor. La celda de ingesta registra corte, fecha de consulta, versiones y hashes. Una ejecución posterior consulta los datos disponibles en ese momento; los hashes permiten identificar si cambiaron, pero no conservan una copia del corte.

## Contenido

- Diccionario, procedencia e integridad.
- Nulos, vacíos, duplicados, tipos, claves y cobertura de 15 minutos.
- Distribución y extremos sin eliminación automática.
- Mapa de las 12 estaciones sobre OpenStreetMap, con círculos proporcionales a la demanda media.
- Perfiles diarios y semanales por estación.
- Comparación de semanas completas.
- Clima y eventos; correlaciones globales y ajustadas por calendario.
- Dependencia temporal, conclusiones y datos ausentes.

El análisis usa datos sintéticos y no entrena modelos ni envía predicciones. Las dependencias son propias del EDA; no cambian las del SDK.

El mapa consulta teselas públicas de OpenStreetMap al ejecutarse; su imagen queda incorporada en el notebook para verla sin conexión.
