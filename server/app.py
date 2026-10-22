import csv
import fcntl
import hmac
import json
import logging
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, request
import psycopg
import paho.mqtt.client as mqtt

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
log = logging.getLogger('savia')
BASE = os.getenv('MQTT_BASE', 'uao/invernadero/477744536654042113/v2').rstrip('/')
BROKER = os.getenv('MQTT_BROKER', 'test.mosquitto.org')
PORT = int(os.getenv('MQTT_PORT', '1883'))
DATABASE_URL = os.environ['DATABASE_URL']
CSV_PATH = Path(os.getenv('CSV_PATH', '/data/lecturas.csv'))
CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
COLS = ['received_at','boot','seq','sensor_ts','temperature','humidity','mode','fan','alarm','reason','source']
app = Flask(__name__, static_folder='frontend', static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024
DEVICE_TOKEN = os.getenv('SAVIA_DEVICE_TOKEN', '')


def connect():
    return psycopg.connect(DATABASE_URL, connect_timeout=5)


def init_database():
    with connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS lecturas (
          id BIGSERIAL PRIMARY KEY,
          received_at TIMESTAMPTZ NOT NULL,
          boot TEXT NOT NULL,
          seq BIGINT NOT NULL,
          sensor_ts BIGINT,
          temperature DOUBLE PRECISION,
          humidity DOUBLE PRECISION,
          mode TEXT,
          fan BOOLEAN,
          alarm TEXT,
          reason TEXT,
          source TEXT NOT NULL,
          UNIQUE(boot, seq)
        )''')


def normalize(sample, source):
    if not isinstance(sample, dict):
        raise ValueError('Payload no es objeto')
    boot = str(sample.get('boot', '')).strip()
    if not 1 <= len(boot) <= 64:
        raise ValueError('boot inválido')
    seq = sample.get('seq')
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= 9223372036854775807:
        raise ValueError('seq inválido')
    def number(key, lo, hi):
        n = sample.get(key)
        if n is None: return None
        if isinstance(n, bool) or not isinstance(n, (float, int)) or not lo <= n <= hi or not math.isfinite(n):
            raise ValueError(key + ' inválido')
        return float(n)
    ts = sample.get('ts')
    if ts is not None and (isinstance(ts, bool) or not isinstance(ts, int) or not 0 <= ts <= 9223372036854775807):
        raise ValueError('ts inválido')
    return dict(received_at=datetime.now(timezone.utc), boot=boot, seq=seq,
                sensor_ts=ts, temperature=number('temperature', -40, 80),
                humidity=number('humidity', 0, 100), mode=str(sample.get('mode',''))[:20],
                fan=sample.get('fan') if isinstance(sample.get('fan'), bool) else None,
                alarm=str(sample.get('alarm',''))[:30], reason=str(sample.get('reason',''))[:80], source=source)


def save(sample, source):
    data = normalize(sample, source)
    with LOCK, CSV_PATH.with_suffix('.lock').open('a') as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        with connect() as conn:
            inserted = conn.execute('''INSERT INTO lecturas
              (received_at,boot,seq,sensor_ts,temperature,humidity,mode,fan,alarm,reason,source)
              VALUES (%(received_at)s,%(boot)s,%(seq)s,%(sensor_ts)s,%(temperature)s,%(humidity)s,%(mode)s,%(fan)s,%(alarm)s,%(reason)s,%(source)s)
              ON CONFLICT (boot,seq) DO NOTHING RETURNING id''', data).fetchone()
            if not inserted:
                row = conn.execute('''SELECT received_at,boot,seq,sensor_ts,temperature,humidity,mode,fan,alarm,reason,source
                    FROM lecturas WHERE boot=%s AND seq=%s''', (data['boot'], data['seq'])).fetchone()
                data = dict(zip(COLS, row))
        existed = CSV_PATH.exists() and CSV_PATH.stat().st_size > 0
        exported = False
        if existed and not inserted:
            with CSV_PATH.open(encoding='utf-8', newline='') as f:
                exported = any(row['boot'] == data['boot'] and row['seq'] == str(data['seq'])
                               for row in csv.DictReader(f))
        if not exported:
            with CSV_PATH.open('a', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=COLS)
                if not existed: writer.writeheader()
                writer.writerow({k: data[k].isoformat() if k == 'received_at' else data[k] for k in COLS})
                f.flush()
                os.fsync(f.fileno())
        return bool(inserted)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe([(BASE + '/telemetry', 1), (BASE + '/history', 1)])
        log.info('Suscrito a %s/telemetry y /history', BASE)
    else:
        log.error('MQTT conexión fallida: %s', reason_code)


def on_message(client, userdata, msg):
    try:
        obj = json.loads(msg.payload.decode('utf-8'))
        samples = obj if isinstance(obj, list) else [obj]
        if len(samples) > 100: raise ValueError('demasiados registros')
        inserted = sum(save(x, 'mqtt_history' if msg.topic.endswith('/history') else 'mqtt_telemetry') for x in samples)
        log.info('MQTT %s: %d nuevos / %d recibidos', msg.topic, inserted, len(samples))
    except (ValueError, UnicodeError, psycopg.Error, OSError, csv.Error) as e:
        log.warning('Payload descartado: %s', e)


def run_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    client.connect_async(BROKER, PORT, 30)
    client.loop_forever(retry_first_connection=True)


@app.get('/health')
def health():
    try:
        with connect() as conn: conn.execute('SELECT 1').fetchone()
        return jsonify(status='ok', database='connected')
    except psycopg.Error:
        return jsonify(status='error', database='unavailable'), 503


@app.get('/api/lecturas')
def lecturas():
    limit = min(max(int(request.args.get('limit', '50')), 1), 500)
    with connect() as conn:
        rows = conn.execute('''SELECT received_at,boot,seq,sensor_ts,temperature,humidity,mode,fan,alarm,reason,source
                  FROM lecturas ORDER BY received_at DESC, id DESC LIMIT %s''', (limit,)).fetchall()
    return jsonify([dict(zip(COLS, [v.isoformat() if isinstance(v,datetime) else v for v in row])) for row in rows])


@app.post('/api/telemetry')
def telemetry():
    if DEVICE_TOKEN and not hmac.compare_digest(
            request.headers.get('X-Device-Token', '').encode(), DEVICE_TOKEN.encode()):
        return jsonify(status='error', error='Token de dispositivo inválido'), 401
    if not request.is_json:
        return jsonify(status='error', error='Se requiere application/json'), 415
    sample = request.get_json()
    try:
        inserted = save(sample, 'http_esp32')
    except ValueError as e:
        return jsonify(status='error', error=str(e)), 400
    except (psycopg.Error, OSError, csv.Error):
        log.exception('No se pudo completar la persistencia HTTP en PostgreSQL y CSV')
        return jsonify(status='error', error='Persistencia incompleta; reintentar la misma lectura'), 503
    return jsonify(status='ok', inserted=inserted, database='saved', csv='saved'), 201 if inserted else 200


@app.get('/')
def root():
    return app.send_static_file('index.html')


@app.get('/historico')
def historico():
    return '''<!doctype html><html lang="es"><meta charset="utf-8"><title>Savia IoT · Históricos</title>
      <style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;background:#f6f9f6;color:#173322}h1{color:#207d4d}table{border-collapse:collapse;width:100%;background:white}th,td{text-align:left;padding:10px;border-bottom:1px solid #ddd}th{background:#dcefe3}</style>
      <a href="/">Volver al dashboard Savia IoT</a>
      <h1>Savia IoT · Históricos PostgreSQL</h1><p>Lecturas recibidas por HTTP del ESP32 o por MQTT → PostgreSQL + CSV. Esta vista es independiente del historial MQTT del navegador.</p>
      <p id="status" role="status" aria-live="polite">Consultando lecturas...</p>
      <table><thead><tr><th>Recepción</th><th>Temperatura °C</th><th>Humedad %</th><th>Modo</th><th>Ventilación</th></tr></thead><tbody id="data"></tbody></table>
      <script>async function load(){try{let response=await fetch('/api/lecturas?limit=30');if(!response.ok)throw new Error('HTTP '+response.status);let d=await response.json();document.getElementById('data').replaceChildren(...d.map(x=>{let tr=document.createElement('tr');for(let k of ['received_at','temperature','humidity','mode','fan']){let td=document.createElement('td');td.textContent=x[k]??'—';tr.append(td)}return tr}));document.getElementById('status').textContent=d.length?d.length+' lecturas guardadas. Actualización cada 5 segundos.':'Sin lecturas guardadas. Inicia la simulación en Wokwi.'}catch(e){console.error(e);document.getElementById('status').textContent='No se pudo actualizar el historial. Se conservan las últimas lecturas mostradas. Reintentando en 5 segundos.'}}load();setInterval(load,5000)</script></html>'''


if __name__ == '__main__':
    init_database()
    threading.Thread(target=run_mqtt, daemon=True).start()
    app.run(host='127.0.0.1', port=8000)
