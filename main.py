import asyncio
import aiohttp
import pymelcloud
import os
import json
import traceback
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# --- CONFIGURACIÓN ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "estado_melcloud.json")
LAST_ACTION_FILE = os.path.join(BASE_DIR, "ultima_accion.json")
TZ_SPAIN = ZoneInfo("Europe/Madrid")

# PARÁMETROS DE CONTROL ACTUALIZADOS
UMBRAL_SEGURIDAD_FRIO = -2.0  # Bajado de 5.0 a -2.0 para que apoyen a la caldera en frío
UMBRAL_BUEN_TIEMPO_INVIERNO = 19.0 
UMBRAL_CORTE_VERANO = 22.0 
DURACION_BLOQUEO_MANUAL = 3600

# CONFIGURACIÓN DE TEMPERATURAS PARA EQUILIBRAR ARRIBA/ABAJO
# Bajamos un poco arriba para que el calor no se acumule allí y el salón trabaje más
TEMP_OBJETIVOS = {
    "Salón": 22.5,      # Abajo: Consigna alta para ayudar a la caldera y evitar estratificación
    "Dormitorio": 21.0, # Arriba: Consigna moderada para no "robar" calor de abajo
    "Jimena": 21.0,     # Arriba
    "Elisa": 21.0       # Arriba
}
DEFAULT_TEMP_AUTO = 21.0 

# CREDENCIALES
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
LAT, LON = 41.6596, -4.7454

def guardar_resumen_csv(temp_ext, devices):
    path_csv = os.path.join(BASE_DIR, "historico_calefaccion.csv")
    file_exists = os.path.exists(path_csv)
    
    with open(path_csv, "a", encoding="utf-8") as f:
        if not file_exists:
            f.write("fecha,temp_ext,salon_on,dorm_on,jimena_on,elisa_on\n")
        
        # Creamos una lista de estados (1 si está ON, 0 si está OFF)
        estados = {d.name: (1 if d.power else 0) for d in devices}
        linea = f"{datetime.now(TZ_SPAIN).strftime('%Y-%m-%d %H:%M')},{temp_ext},"
        linea += f"{estados.get('Salón',0)},{estados.get('Dormitorio',0)},{estados.get('Jimena',0)},{estados.get('Elisa',0)}\n"
        f.write(linea)

def log(msg):
    ts = datetime.now(TZ_SPAIN).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def load_json(path, default):
    if not os.path.exists(path):
        return default.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"❌ ERROR: Fallo leyendo {path}: {e}")
        return default.copy()

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        log(f"❌ ERROR: Fallo guardando {path}: {e}")

async def enviar_telegram(session, msg, temp=None):
    if temp is not None:
        msg = f"{msg}\n\n🌡️ <b>Temp. exterior:</b> {temp}°C"
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                log("✅ TELEGRAM: Enviado correctamente.")
    except Exception as e:
        log(f"⚠️ TELEGRAM EXCEPTION: {e}")

async def check_telegram_commands(session, registro, estados_previos, temp_ext, devices, es_invierno, en_horario):
    if not BOT_TOKEN: return
    last_id = registro.get("last_telegram_update_id", 0)
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"offset": last_id + 1, "timeout": 5}
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                for result in data.get("result", []):
                    registro["last_telegram_update_id"] = result["update_id"]
                    msg_text = result.get("message", {}).get("text", "").lower()
                    if "/reset" in msg_text:
                        for dev_name in estados_previos: estados_previos[dev_name]["bloqueo_hasta"] = 0
                        await enviar_telegram(session, "✅ Bloqueos reseteados.", temp_ext)
                    if "/stop" in msg_text:
                        registro["stop_mode"] = True
                        await enviar_telegram(session, "🛑 Modo STOP activado.", temp_ext)
                    if "/start" in msg_text:
                        registro["stop_mode"] = False
                        await enviar_telegram(session, "▶️ Modo START activado.", temp_ext)
                    if "/info" in msg_text:
                        txt = f"📊 <b>ESTADO</b>\nHorario: {'SI' if en_horario else 'NO'}\nSTOP: {registro.get('stop_mode')}\n"
                        for d in devices: txt += f"• {d.name}: {'ON' if d.power else 'OFF'} ({d.target_temperature}°C)\n"
                        await enviar_telegram(session, txt, temp_ext)
    except Exception as e: log(f"⚠️ Error Telegram: {e}")

async def main():
    log("🔊 === INICIO CICLO V8.3 (FUNCIONALIDAD COMPLETA + LOGS + HISTÓRICO) ===")
    async with aiohttp.ClientSession() as session:
        registro = load_json(LAST_ACTION_FILE, {})
        estados_previos = load_json(STATE_FILE, {})
        
        if "last_telegram_update_id" not in registro: registro["last_telegram_update_id"] = 0
        if "stop_mode" not in registro: registro["stop_mode"] = False

        ahora = datetime.now(TZ_SPAIN)
        ahora_ts = time.time()
        minutos = ahora.hour * 60 + ahora.minute
        es_finde = ahora.weekday() >= 5
        es_invierno = ahora.month >= 10 or ahora.month <= 5
        # Mantenemos tu horario de 6:30 a 23:00
        en_horario = (390 <= minutos < 1380)

        # --- TEMPERATURA EXTERIOR ---
        try:
            url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&current_weather=true"
            async with session.get(url) as r:
                data = await r.json()
                temp_ext = float(data["current_weather"]["temperature"])
                log(f"📡 Temp Exterior: {temp_ext}°C")
        except Exception as e:
            log(f"❌ ERROR CLIMA: {e}"); return

        # --- AVISO MANUAL CALDERA ---
        if es_invierno and temp_ext <= 1.0:
            log("⚠️ AVISO: Temp baja detectada. Sugiriendo caldera a 73°C.")
            await enviar_telegram(session, "⚠️ <b>ALERTA:</b> Menos de 1°C exterior. Sube la caldera a <b>73°C</b> manualmente.", temp_ext)

        # --- MELCLOUD ---
        token = await pymelcloud.login(EMAIL, PASSWORD, session)
        devices = (await pymelcloud.get_devices(token, session)).get("ata", [])
        await asyncio.gather(*(d.update() for d in devices))

        await check_telegram_commands(session, registro, estados_previos, temp_ext, devices, es_invierno, en_horario)

        for dev in devices:
            temp_objetivo_actual = TEMP_OBJETIVOS.get(dev.name, DEFAULT_TEMP_AUTO)
            # Recuperamos memoria
            mem = estados_previos.get(dev.name, {"power": dev.power, "target_temperature": dev.target_temperature, "bloqueo_hasta": 0})

            log(f"💠 {dev.name}: Estado={dev.power}, Temp={dev.target_temperature}°C | Obj={temp_objetivo_actual}°C")

            # Gestión de Bloqueo Manual
            if (mem["power"] != dev.power or mem["target_temperature"] != dev.target_temperature):
                if ahora_ts > mem["bloqueo_hasta"]:
                    log(f"    ✋ MANUAL DETECTADO en {dev.name}")
                    await enviar_telegram(session, f"✋ <b>CAMBIO MANUAL:</b> {dev.name} a {dev.target_temperature}°C", temp_ext)
                    mem["bloqueo_hasta"] = ahora_ts + DURACION_BLOQUEO_MANUAL
            
            # Actualizamos memoria con el estado actual del dispositivo antes de decidir
            mem["power"], mem["target_temperature"] = dev.power, dev.target_temperature
            estados_previos[dev.name] = mem

            if mem["bloqueo_hasta"] > ahora_ts:
                log(f"    ⏳ {dev.name} en bloqueo manual.")
                continue

            # --- MOTOR DE DECISIÓN ---
            accion_tomada = None

            if temp_ext < UMBRAL_SEGURIDAD_FRIO:
                if dev.power:
                    await dev.set({"power": False})
                    mem["power"] = False
                    accion_tomada = f"❄️ <b>SEGURIDAD:</b> Apagado {dev.name} por frío extremo"
            
            elif (not es_invierno) and (temp_ext < UMBRAL_CORTE_VERANO):
                if dev.power:
                    await dev.set({"power": False})
                    mem["power"] = False
                    accion_tomada = f"☀️ <b>FRESCO:</b> Apagado {dev.name}"

            elif registro.get("stop_mode"):
                if dev.power:
                    await dev.set({"power": False})
                    mem["power"] = False
                    accion_tomada = f"🛑 <b>STOP:</b> Apagado {dev.name}"

            else:
                if es_invierno:
                    if not en_horario:
                        if dev.power:
                            await dev.set({"power": False})
                            mem["power"] = False
                            accion_tomada = f"🕒 <b>NOCHE:</b> Apagado {dev.name}"
                    elif en_horario and temp_ext <= UMBRAL_BUEN_TIEMPO_INVIERNO:
                        # Si está apagado o la temperatura no es la correcta, actuamos
                        if not dev.power or dev.target_temperature != temp_objetivo_actual:
                            log(f"    🔥 Ajustando {dev.name} a {temp_objetivo_actual}°C")
                            await dev.set({"power": True, "target_temperature": temp_objetivo_actual, "operation_mode": "heat"})
                            mem["power"], mem["target_temperature"] = True, temp_objetivo_actual
                            accion_tomada = f"🔥 <b>APOYO:</b> {dev.name} a {temp_objetivo_actual}°C"

            if accion_tomada:
                await enviar_telegram(session, accion_tomada, temp_ext)

        # --- GUARDADO FINAL ---
        save_json(STATE_FILE, estados_previos)
        save_json(LAST_ACTION_FILE, registro)
        guardar_resumen_csv(temp_ext, devices)
        log("🏁 === FIN CICLO V8.3 ===")
        
if __name__ == "__main__":
    async def run_safe():
        try: await main()
        except Exception: log(f"❌ FATAL ERROR:\n{traceback.format_exc()}")
    asyncio.run(run_safe())
