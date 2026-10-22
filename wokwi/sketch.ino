/* INVERNADERO CONECTADO - ESP32 + DHT22 - Etapa 2
   Prototipo academico. LED verde = ventilacion simulada, no motor real.
   MQTT publico de pruebas: solo datos simulados. No conectar cargas reales.
   Control local independiente del dashboard. Sin red: regresa a AUTO.
   Buffer RAM: 720 registros (~1 hora a 5 s); se pierde al reiniciar.
   Serial: a=AUTO, 1=ON, 0=OFF, c=corte de Wi-Fi durante 20 segundos.
*/
#include <WiFi.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <DHTesp.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <time.h>

const uint8_t PIN_DHT=15, PIN_RELE=26, PIN_ALERTA=27;
const uint8_t PIN_AUTO=18, PIN_ON=19, PIN_OFF=23;
const char* BROKER="test.mosquitto.org";
const char* BASE="uao/invernadero/477744536654042113/v2";
// Requiere Private IoT Gateway activo en Wokwi y el servidor local en puerto 8000.
const char* API_URL="http://host.wokwi.internal:8000/api/telemetry";
const char* API_TOKEN=""; // Mismo valor de SAVIA_DEVICE_TOKEN en el servidor
const uint16_t CAPACITY=720;
const unsigned long READ_MS=2200, SEND_MS=5000, RETRY_MS=5000;
DHTesp dht; LiquidCrystal_I2C lcd(0x27,20,4);
WiFiClient network; PubSubClient mqtt(network);
String topicState,topicHistory,topicCommand,topicAck,topicStatus;
char bootId[9]; bool automatico=true,ventilador=false,sensorValido=false;
float temperatura=0,humedad=0;
unsigned long lastRead=0,lastSample=0,lastTry=0,lastWifiTry=0,lastHistory=0;
unsigned long cutUntil=0,cutAt=0,manualUntil=0;
bool mqttWasConnected=false,historyActive=false;
uint32_t sequence=0,historyNext=0,historyEnd=0;
uint32_t httpNextSeq=1; unsigned long lastHttpTry=0;
String lastCommandId;
struct Sample {uint32_t seq,up;uint64_t ts;float temp,hum;bool valid,automatic,fan;uint8_t alarm;char reason[24];};
Sample buffer[CAPACITY]; uint16_t head=0,count=0;
struct Boton {uint8_t pin;bool prev=HIGH,stable=HIGH;unsigned long changed=0;};
Boton bAuto{PIN_AUTO},bOn{PIN_ON},bOff{PIN_OFF};
bool pushed(Boton &b){bool x=digitalRead(b.pin);if(x!=b.prev){b.changed=millis();b.prev=x;}if(millis()-b.changed>=30&&x!=b.stable){b.stable=x;return x==LOW;}return false;}
uint8_t alarmCode(){return !sensorValido?3:humedad<50?1:humedad>85?2:0;}
const char* alarmText(uint8_t code){return code==3?"SENSOR":code==1?"BAJA":code==2?"ALTA":"NORMAL";}
void line(uint8_t row,String s){lcd.setCursor(0,row);s=s.substring(0,20);while(s.length()<20)s+=' ';lcd.print(s);}
void control(){if(!sensorValido){ventilador=false;return;}if(automatico){if(temperatura>30)ventilador=true;else if(temperatura<27)ventilador=false;}}
void outputs(){digitalWrite(PIN_RELE,ventilador?HIGH:LOW);digitalWrite(PIN_ALERTA,alarmCode()!=0);
 line(0,sensorValido?"Temp: "+String(temperatura,1)+" C":"DHT22 SIN LECTURA");
 line(1,sensorValido?"Humedad: "+String(humedad,1)+" %":"Revisar sensor");
 line(2,String(automatico?"AUTO":"MANUAL")+" | VENT "+(ventilador?"ON":"OFF"));
 line(3,String(mqtt.connected()?"RED OK | ":"LOCAL | ")+alarmText(alarmCode()));}
void jsonSample(JsonObject j,const Sample&s){j["v"]=2;j["boot"]=bootId;j["seq"]=s.seq;j["up"]=s.up;j["ts"]=s.ts;
 if(s.valid){j["temperature"]=s.temp;j["humidity"]=s.hum;}else{j["temperature"]=nullptr;j["humidity"]=nullptr;}
 j["mode"]=s.automatic?"AUTO":"MANUAL";j["fan"]=s.fan;j["alarm"]=alarmText(s.alarm);j["reason"]=s.reason;}
void publishSample(const Sample&s){if(!mqtt.connected())return;JsonDocument d;jsonSample(d.to<JsonObject>(),s);String payload;serializeJson(d,payload);mqtt.publish(topicState.c_str(),payload.c_str(),true);}
void sendHttp(){
 if(!API_URL[0] || WiFi.status()!=WL_CONNECTED || millis()-lastHttpTry<1500 || !count)return;
 if(httpNextSeq>sequence)return;
 lastHttpTry=millis();
 uint32_t oldest=buffer[(head+CAPACITY-count)%CAPACITY].seq;
 if(httpNextSeq<oldest){Serial.printf("HTTP: %lu lecturas antiguas excedieron el buffer RAM\n",(unsigned long)(oldest-httpNextSeq));httpNextSeq=oldest;}
 for(uint16_t i=0;i<count;i++){
  Sample &s=buffer[(head+CAPACITY-count+i)%CAPACITY];if(s.seq!=httpNextSeq)continue;
  JsonDocument d;jsonSample(d.to<JsonObject>(),s);String payload;serializeJson(d,payload);
  HTTPClient http;http.setConnectTimeout(1200);http.setTimeout(1200);
  if(!http.begin(API_URL)){Serial.println("HTTP: no se pudo iniciar la peticion");return;}
  http.addHeader("Content-Type","application/json");if(API_TOKEN[0])http.addHeader("X-Device-Token",API_TOKEN);
  int code=http.POST(payload);
  if(code==200 || code==201){
   JsonDocument response;DeserializationError error=deserializeJson(response,http.getString());
   if(!error && response["status"]=="ok" && response["database"]=="saved" && response["csv"]=="saved"){
    Serial.printf("HTTP guardado en PostgreSQL y CSV: %s #%lu\n",bootId,(unsigned long)s.seq);httpNextSeq++;
   }else Serial.printf("HTTP: confirmacion invalida para #%lu; se reintentara\n",(unsigned long)s.seq);
  }else Serial.printf("HTTP pendiente: #%lu, estado %d\n",(unsigned long)s.seq,code);
  http.end();
  return;
 }
}
void record(const char*reason){Sample &s=buffer[head];s.seq=++sequence;s.up=millis();time_t t=time(nullptr);s.ts=t>1700000000?(uint64_t)t*1000:0;
 s.temp=temperatura;s.hum=humedad;s.valid=sensorValido;s.automatic=automatico;s.fan=ventilador;s.alarm=alarmCode();strlcpy(s.reason,reason,sizeof(s.reason));
 head=(head+1)%CAPACITY;if(count<CAPACITY)count++;publishSample(s);
 Serial.printf("T=%.1f C | HR=%.1f %% | MODO=%s | VENT=%s | RED=%s | %s | #%lu\n",temperatura,humedad,automatico?"AUTO":"MANUAL",ventilador?"ON":"OFF",mqtt.connected()?"OK":"LOCAL",reason,(unsigned long)s.seq);}
void setMode(char c,const char*reason){if(c=='a'){automatico=true;manualUntil=0;}else if(c=='1'){automatico=false;ventilador=sensorValido;}else if(c=='0'){automatico=false;ventilador=false;}else return;control();outputs();record(reason);}
void ack(String id,bool ok,const char*error=""){JsonDocument d;d["id"]=id;d["ok"]=ok;d["boot"]=bootId;d["mode"]=automatico?"AUTO":"MANUAL";d["fan"]=ventilador;if(!ok)d["error"]=error;String s;serializeJson(d,s);mqtt.publish(topicAck.c_str(),s.c_str(),false);}
void startHistory(){if(!count)return;historyNext=buffer[(head+CAPACITY-count)%CAPACITY].seq;historyEnd=sequence;historyActive=true;}
void callback(char*,byte*payload,unsigned int length){if(length>512)return;JsonDocument d;if(deserializeJson(d,payload,length))return;
 String op=d["op"]|"";String target=d["boot"]|"";if(target!=bootId)return;
 if(op=="SYNC"){startHistory();return;}
 String id=d["id"]|"";if(id.length()<1||id.length()>64)return;
 if(id==lastCommandId){ack(id,false,"orden_duplicada");return;}
 if(!d["ref_up"].is<uint32_t>() || (uint32_t)(millis()-d["ref_up"].as<uint32_t>())>30000){ack(id,false,"orden_vencida");return;}
 if(op!="AUTO"&&op!="ON"&&op!="OFF"&&op!="CUT"){ack(id,false,"orden_desconocida");return;}
 if(op=="ON"&&!sensorValido){ack(id,false,"sensor_sin_lectura");return;}
 lastCommandId=id;
 if(op=="CUT"){cutAt=millis()+1200;ack(id,true);record("prueba_corte");return;}
 if(op=="AUTO")setMode('a',"remoto");else {setMode(op=="ON"?'1':'0',"remoto");manualUntil=millis()+180000;}
 ack(id,true);
}
void sendHistory(){if(!historyActive||!mqtt.connected()||millis()-lastHistory<300)return;lastHistory=millis();JsonDocument d;JsonArray a=d.to<JsonArray>();
 uint16_t sent=0;uint32_t next=historyNext;
 for(uint16_t i=0;i<count&&sent<20;i++){Sample&s=buffer[(head+CAPACITY-count+i)%CAPACITY];if(s.seq>=historyNext&&s.seq<=historyEnd){jsonSample(a.add<JsonObject>(),s);next=s.seq+1;sent++;}}
 if(!sent){historyActive=false;return;}String message;serializeJson(d,message);if(mqtt.publish(topicHistory.c_str(),message.c_str(),false)){historyNext=next;if(historyNext>historyEnd)historyActive=false;}}
void cutNetwork(){cutAt=0;cutUntil=millis()+20000;mqtt.publish(topicStatus.c_str(),"{\"online\":false}",true);mqtt.disconnect();WiFi.setAutoReconnect(false);WiFi.disconnect();automatico=true;manualUntil=0;control();outputs();record("desconexion");mqttWasConnected=false;Serial.println("PRUEBA: Wi-Fi detenido 20 s. Control automatico local activo.");}
void connectNetwork(){unsigned long now=millis();if(cutAt&&(int32_t)(now-cutAt)>=0)cutNetwork();if(cutUntil&&(int32_t)(now-cutUntil)<0)return;if(cutUntil){cutUntil=0;lastWifiTry=0;WiFi.setAutoReconnect(true);}
 if(WiFi.status()!=WL_CONNECTED){if(!lastWifiTry||now-lastWifiTry>RETRY_MS){lastWifiTry=now;WiFi.begin("Wokwi-GUEST","",6);}return;}
 if(!mqtt.connected()&&now-lastTry>=RETRY_MS){lastTry=now;String id="invernadero-"+String(bootId);
  if(mqtt.connect(id.c_str(),topicStatus.c_str(),0,true,"{\"online\":false}")){mqtt.subscribe(topicCommand.c_str());mqtt.publish(topicStatus.c_str(),"{\"online\":true}",true);mqttWasConnected=true;outputs();record("conexion");startHistory();Serial.println("MQTT conectado: dashboard habilitado.");}}
 if(mqtt.connected())mqtt.loop();
}
void setup(){Serial.begin(115200);snprintf(bootId,sizeof(bootId),"%08lx",(unsigned long)esp_random());topicState=String(BASE)+"/telemetry";topicHistory=String(BASE)+"/history";topicCommand=String(BASE)+"/command";topicAck=String(BASE)+"/ack";topicStatus=String(BASE)+"/status";
 digitalWrite(PIN_RELE,LOW);pinMode(PIN_RELE,OUTPUT);pinMode(PIN_ALERTA,OUTPUT);pinMode(PIN_AUTO,INPUT_PULLUP);pinMode(PIN_ON,INPUT_PULLUP);pinMode(PIN_OFF,INPUT_PULLUP);
 Wire.begin(21,22);lcd.init();lcd.backlight();dht.setup(PIN_DHT,DHTesp::DHT22);outputs();
 network.setConnectionTimeout(1000);mqtt.setServer(BROKER,1883);mqtt.setBufferSize(8192);mqtt.setKeepAlive(10);mqtt.setSocketTimeout(1);mqtt.setCallback(callback);
 WiFi.mode(WIFI_STA);WiFi.setAutoReconnect(true);WiFi.begin("Wokwi-GUEST","",6);configTime(0,0,"pool.ntp.org","time.nist.gov");
 Serial.println("INVERNADERO CONECTADO | Canal publico de pruebas | Solo simulacion");Serial.println("a=AUTO, 1=ON, 0=OFF, c=CORTE WIFI 20s");Serial.printf("SESION %s\n",bootId);}
void loop(){
 if(pushed(bAuto))setMode('a',"boton");if(pushed(bOn)){manualUntil=0;setMode('1',"boton");}if(pushed(bOff)){manualUntil=0;setMode('0',"boton");}
 while(Serial.available()){char c=Serial.read();if(c=='c')cutAt=millis()+100;else setMode(c,"boton");}
 unsigned long now=millis();
 if(now-lastRead>=READ_MS){lastRead=now;TempAndHumidity v=dht.getTempAndHumidity();sensorValido=!isnan(v.temperature)&&!isnan(v.humidity)&&v.temperature>=-40&&v.temperature<=80&&v.humidity>=0&&v.humidity<=100;if(sensorValido){temperatura=v.temperature;humedad=v.humidity;}control();outputs();}
 if(manualUntil&&(int32_t)(now-manualUntil)>=0)setMode('a',"manual_expirado");
 if(mqttWasConnected&&!mqtt.connected()){mqttWasConnected=false;automatico=true;manualUntil=0;control();outputs();record("auto_respaldo");}
 if(now-lastSample>=SEND_MS){lastSample=now;record("lectura");}
 connectNetwork();sendHistory();sendHttp();delay(5);
}
