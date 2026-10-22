# Savia IoT — Extensión de persistencia para Google Cloud

**Base original:** https://github.com/nathernandez1189/savia-iot (rama `main`).

Este paquete es una **ampliación complementaria**, no un reemplazo del frontend o firmware de Savia. Revisado contra `wokwi/sketch.ino` y el README del repositorio el 17/10/2026. No modificar los comandos JSON ni los tópicos del firmware original: la interfaz depende de ellos.

## Qué añade

- Servicio `collector` escucha MQTT en `test.mosquitto.org:1883`.
- Tópicos: `uao/invernadero/477744536654042113/v2/telemetry` y `/history`.
- Persiste cada muestra única por `(boot,seq)` en PostgreSQL y, por cada inserción, la añade a CSV.
- Servicio Flask `web` sirve el frontend original en `/`, el historial PostgreSQL en `/historico`, `/health` y `/api/lecturas?limit=30`.
- El ESP32 puede enviar cada muestra directamente con `POST /api/telemetry`; solo se confirma después de completar PostgreSQL y CSV.
- No envía comandos al dispositivo; el dashboard original puede continuar funcionando por su cuenta.
- Datos del broker **públicos y no autenticados**: solo simulación, no emplear para controlar un ventilador real.

## Integración en el repositorio original

### ESP32: envío HTTP directo y almacenamiento doble

Flujo: **DHT22 → ESP32 → POST /api/telemetry → PostgreSQL + CSV**.
MQTT se conserva para los controles y las gráficas originales, pero no es
necesario para el envío HTTP. Ambos canales deduplican por `(boot, seq)`.

1. Arrancar el servidor con `docker compose up -d --build`.
2. Abrir https://wokwi.com/projects/477744536654042113 y copiar el contenido de
   [wokwi/sketch.ino](wokwi/sketch.ino) en la pestaña `sketch.ino`. El circuito
   [diagram.json](wokwi/diagram.json) y las bibliotecas
   [libraries.txt](wokwi/libraries.txt) se conservan del repositorio original,
   commit `9183abefe7b3ad0497708d48a6f59bd195cdf787`.
3. Descargar y ejecutar [Wokwi IoT Gateway](https://github.com/wokwi/wokwigw/releases/latest)
   para macOS. En el editor Wokwi, pulsar F1 y seleccionar
   **Enable Private Wokwi IoT Gateway**. Comprobar `Client connected` en la pasarela.
   **Requiere cuenta de pago de Wokwi; no funciona en Safari.** Usar Chrome,
   Firefox o Edge. [Documentación oficial](https://docs.wokwi.com/guides/esp32-wifi#the-private-gateway).
4. Iniciar la simulación. El firmware apunta a
   `http://host.wokwi.internal:8000/api/telemetry`; `localhost` dentro del ESP32
   no es el Mac. Sin la pasarela privada este destino no es accesible.
5. Cambiar temperatura/humedad del DHT22. El monitor serial debe confirmar
   `HTTP guardado en PostgreSQL y CSV: <boot> #<seq>`.
6. Consultar http://localhost:8000/historico y verificar los mismos valores,
   `boot` y `seq` en los dos almacenes:

```bash
docker compose exec db psql -U savia -d savia -c \
  'SELECT boot,seq,temperature,humidity,source FROM lecturas ORDER BY id DESC LIMIT 10;'
docker compose exec web tail -n 10 /data/lecturas.csv
curl 'http://localhost:8000/api/lecturas?limit=10'
```

El firmware conserva hasta 720 muestras en RAM, reintenta HTTP sin avanzar hasta
recibir confirmación de ambos almacenes y acelera los envíos pendientes cada
1,5 segundos. Reiniciar borra la RAM; si se llena, registra en serial la pérdida
de muestras antiguas. Los fallos de sensor se envían con valores `null`, no con
mediciones inventadas.

La API acepta un objeto con `boot`, `seq` y los campos `ts`, `temperature`,
`humidity`, `mode`, `fan`, `alarm`, `reason` del firmware. Responde `201` al insertar
y `200` ante un duplicado, con `status: "ok"`, `database: "saved"` y `csv: "saved"`.
Si falla alguno de los almacenes responde `503`; al reintentar la misma lectura
puede reparar una exportación CSV faltante sin duplicar PostgreSQL.
Si MQTT guardó primero la muestra, se conserva su `source: "mqtt_telemetry"`;
un `200` HTTP confirma la persistencia aun cuando la fuente no diga `http_esp32`.

El puerto sigue limitado a `127.0.0.1`. En este laboratorio, `SAVIA_DEVICE_TOKEN`
vacío permite peticiones locales. **No publicar este endpoint sin autenticación
y HTTPS.** Para activar autenticación, configurar `SAVIA_DEVICE_TOKEN` en el
archivo local `.env`, recrear los servicios y usar el mismo valor en `API_TOKEN`
del firmware privado. No guardar ese valor en Git ni en un proyecto público
de Wokwi. Con token configurado se exige la cabecera `X-Device-Token`.

Pruebas de la API y persistencia, con PostgreSQL real y CSV temporal:

```bash
docker compose exec web python -m unittest discover -v
```

Las pruebas eliminan sus filas identificadas con UUID. La prueba automatizada
del servidor no demuestra que el firmware se haya compilado o que Wokwi haya
enviado una lectura: esa comprobación requiere cargarlo y activar la pasarela.

### Frontend integrado localmente

Ejecutar `docker compose up -d --build` y abrir http://localhost:8000.
El enlace **Historial PostgreSQL**, en la esquina inferior derecha, abre
http://localhost:8000/historico y consulta la API del mismo servidor cada cinco
segundos. No requiere CORS ni expone la base de datos.

El frontend conserva sus controles y gráficas MQTT originales; su historial del
navegador no se sustituye por PostgreSQL. La vista `/historico` muestra las últimas
30 lecturas persistidas y permite volver al dashboard. Si todavía no hay datos,
iniciar la simulación en https://wokwi.com/projects/477744536654042113.
El colector y el dashboard utilizan el mismo tópico MQTT.

Los archivos de [server/frontend/](server/frontend/) proceden de
[nathernandez1189/savia-iot](https://github.com/nathernandez1189/savia-iot),
commit `9183abefe7b3ad0497708d48a6f59bd195cdf787`. Se conservan sus assets
compilados sin cambios; únicamente se añade el enlace al historial en
[index.html](server/frontend/index.html). El código fuente editable del frontend
no está incluido en el repositorio original. Esta integración no modifica la
página pública de GitHub Pages ni sube cambios a GitHub.

1. Crear un *fork* de `nathernandez1189/savia-iot`, respetando su atribución y licencia si se añade posteriormente.
2. Copiar a la raíz del fork `server/`, `compose.yaml`, `.env.example` y `.gitignore` (fusionar este último con el existente, no sustituirlo).
3. Mantener `index.html`, `assets/`, `brand/` y `wokwi/` sin cambios.
4. Crear una rama para la integración y solicitar revisión al propietario si deseas contribuir al repositorio original.

## Fase 1: Google Compute Engine, una máquina Ubuntu 24.04

Usar una VM de prueba con al menos 2 GB RAM (las e2-micro pueden quedarse cortas). Activar facturación y alertas de presupuesto. Para instalar Docker/Compose en Ubuntu:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
git clone https://github.com/TU_USUARIO/savia-iot.git
cd savia-iot
cp .env.example .env
nano .env              # configurar password sólido; NUNCA subirlo a GitHub
sudo docker compose up -d --build
curl http://localhost:8000/health
sudo docker compose logs -f collector
```

Para comprobar lecturas:

```bash
sudo docker compose exec db psql -U savia -d savia -c 'SELECT received_at,temperature,humidity,mode,fan FROM lecturas ORDER BY id DESC LIMIT 10;'
sudo docker compose exec collector head /data/lecturas.csv
curl 'http://localhost:8000/api/lecturas?limit=5'
```

**Acceso web seguro de laboratorio:** realizar un túnel SSH local a `127.0.0.1:8000` o instalar Nginx como proxy inverso en HTTPS 443, con autenticación. No abrir 8000 ni 5432 a Internet. El dashboard existente en GitHub Pages es independiente de esta nueva pantalla histórica.

Si tienes acceso SSH desde Mac, ejemplo:

```bash
ssh -L 8000:127.0.0.1:8000 USUARIO@IP_PUBLICA_VM
# Navegador Mac: http://localhost:8000
```

## Fase 2: base de datos en VM privada de Google Cloud

1. Crear VPC personalizada con subred de aplicación `10.20.1.0/24` y subred de datos `10.20.2.0/24`, ambas en una región admitida.
2. VM web: Ubuntu en subred de aplicación, IP externa estática, acceso SSH restringido.
3. VM de datos: Ubuntu en subred de datos, sin IP externa, instalar PostgreSQL 16 mediante paquetes Ubuntu o Docker. Una VM sin IP externa necesita acceso privado a repositorios, un mecanismo de NAT o transferencia de paquetes.
4. Firewall: TCP 5432 al destino VM de datos **solo desde la IP interna de la VM web**; nunca 0.0.0.0/0.
5. Cambiar DATABASE_URL de `postgresql://savia:...@db:5432/savia` a la IP privada de la VM de datos en servicios `web` y `collector`. Configurar `pg_hba.conf`, `listen_addresses` y TLS del DB si corresponde.
6. Eliminar servicio `db` local en la composición y su dependencia de arranque. Hacer copia y migrar datos de la primera base con `pg_dump` y `pg_restore`.
7. Verificar que API, CSV y consultas SQL funcionan tras la migración; documentar reglas de firewall y ausencia de IP externa en VM DB.

**Precisión:** Google Cloud no clasifica literalmente las subredes como «públicas» o «privadas»: la exposición depende de IP externas, rutas, firewalls y proxies. El enunciado original menciona AWS; solicitar aprobación para Google Cloud.

## Evidencias del taller

- Wokwi mostrando temperatura, humedad, LCD, ventilación simulada y serial.
- Conexión MQTT recibida por `collector`.
- `/health` respondiendo OK.
- Filas SQL y CSV después de enviar 3-5 lecturas.
- Dashboard histórico de Flask.
- Capturas de ambas VM y firewall de VPC.
- Repositorio GitHub y presentación explicando que no hay IA entrenada ni actuador físico real.

## Limitaciones

- El repositorio original contiene HTML y assets compilados, no el código editable del frontend.
- En el broker público puede haber mensajes de terceros en el mismo tópico: los datos **no son confiables ni auténticos**. No sirve para pruebas de seguridad o control de cargas reales.
- La escritura PostgreSQL y CSV no constituye una transacción atómica. PostgreSQL es la fuente de verdad; HTTP no confirma si falla el CSV y el reintento repara filas CSV faltantes. El bloqueo compartido entre procesos evita escrituras simultáneas del colector y HTTP. Una corrupción parcial del archivo o pérdida del volumen requiere reconstruir el CSV desde PostgreSQL.
- Se recomienda limpiar datos de prueba y configurar políticas de retención.
