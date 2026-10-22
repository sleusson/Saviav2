import time
from app import init_database, run_mqtt
while True:
    try:
        init_database()
        run_mqtt()
    except Exception as e:
        print('Reiniciando puente MQTT:', e, flush=True)
        time.sleep(10)
