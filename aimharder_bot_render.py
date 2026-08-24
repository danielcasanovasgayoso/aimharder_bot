#!/usr/bin/env python3
"""
Bot AimHarder para despliegue 24/7 en Fly.io.
Sin dependencias externas -- solo libreria estandar de Python.

Arquitectura: un proceso con dos hilos. El principal calcula la proxima
apertura de reserva (7 dias antes de la clase, misma hora), duerme hasta
ese instante y reserva; el secundario sirve el health check. La precision
depende solo de este bucle, no de Fly.

Autenticacion: POST /api/login (JSON) contra aimharder.com. La sesion la
lleva la cookie amhrdrauth sobre .aimharder.com, que por ser de dominio
vale tambien para el subdominio del box. Ver login() y verificar_sesion().

Variables de entorno (secretos de Fly). Ni las credenciales ni el box ni
los horarios estan en el repo: ver CONFIG.
    AIMHARDER_EMAIL         obligatoria
    AIMHARDER_PASSWORD      obligatoria
    AIMHARDER_BOX           obligatoria; subdominio del box
    AIMHARDER_BOX_ID        obligatoria; id numerico del box
    AIMHARDER_TARGETS       obligatoria; JSON con las clases objetivo
    AIMHARDER_PAUSES        opcional; JSON con los dias sin reservas
    AIMHARDER_CAL_TOKEN     opcional; sin ella el calendario no existe
    AIMHARDER_FINGERPRINT   opcional; por defecto se deriva del email

Despliegue: flyctl deploy --remote-only --ha=false

Modo manual (no arranca el scheduler):
    python aimharder_bot_render.py --dia 20260818
    python aimharder_bot_render.py --dia 20260818 --reservar 1223249
    python aimharder_bot_render.py --dia 20260818 --diag-sesion
"""

import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# El login son dos pasos: /api/login crea la sesion y /setrefresh la instala en
# el subdominio del box. Con solo el primero, el box responde {'logout': 1} a
# cualquier peticion.
AIMHARDER_URL = "https://aimharder.com"
LOGIN_API_URL = f"{AIMHARDER_URL}/api/login"
SETREFRESH_URL = f"{AIMHARDER_URL}/setrefresh"
USER_AGENT = "Mozilla/5.0 (compatible; aimharder-autobook/1.0)"

# Nada de esto vive en el codigo: el email es media credencial, y el box y los
# horarios dicen donde esta uno cada semana a una hora exacta. En un repo
# publico eso es un perfil, no una configuracion.


def _entero(nombre):
    """Entero de entorno. Un valor ilegible vale 0, que config_incompleta()
    reporta como variable ausente en vez de reventar durante el import."""
    crudo = os.environ.get(nombre, "").strip()
    if not crudo:
        return 0
    try:
        return int(crudo)
    except ValueError:
        print(f"[CONFIG] {nombre} no es un numero: {crudo!r}", flush=True)
        return 0


def _hora_valida(texto):
    partes = str(texto).split(":")
    if len(partes) != 2 or not all(p.isdigit() for p in partes):
        return False
    hora, minuto = (int(p) for p in partes)
    return 0 <= hora <= 23 and 0 <= minuto <= 59


def _targets_desde_entorno(crudo):
    """AIMHARDER_TARGETS es una lista JSON de clases objetivo:

        [{"weekday": 1, "time": "07:00", "name_contains": "Metcon"}]

    weekday sigue la convencion de Python (0 = lunes ... 6 = domingo), time es
    el inicio de la clase tal y como lo devuelve la API y name_contains se
    compara en minusculas contra className.

    Un target invalido se descarta con un aviso: mejor reservar dos clases de
    tres que ninguna."""
    if not crudo.strip():
        return []
    try:
        datos = json.loads(crudo)
    except json.JSONDecodeError as e:
        print(f"[CONFIG] AIMHARDER_TARGETS no es JSON valido: {e}", flush=True)
        return []
    if not isinstance(datos, list):
        print("[CONFIG] AIMHARDER_TARGETS tiene que ser una lista JSON.", flush=True)
        return []

    validos = []
    for t in datos:
        if (isinstance(t, dict)
                and isinstance(t.get("weekday"), int) and 0 <= t["weekday"] <= 6
                and _hora_valida(t.get("time"))
                and isinstance(t.get("name_contains"), str) and t["name_contains"]):
            validos.append({"weekday": t["weekday"], "time": t["time"],
                            "name_contains": t["name_contains"]})
        else:
            # El valor no se vuelca: basta con saber que uno falla.
            print("[CONFIG] Target descartado, formato invalido "
                  "(weekday 0-6, time HH:MM, name_contains texto).", flush=True)
    return validos


def _rango_de_pausa(texto):
    """Un rango 'AAAA-MM-DD:AAAA-MM-DD' -> (date, date), o None si no vale.
    Una fecha suelta, 'AAAA-MM-DD', es el rango de un solo dia."""
    partes = [p.strip() for p in str(texto).split(":")]
    if len(partes) == 1:
        partes *= 2
    if len(partes) != 2:
        return None
    try:
        inicio, fin = (datetime.strptime(p, "%Y-%m-%d").date() for p in partes)
    except ValueError:
        return None
    return (inicio, fin) if inicio <= fin else None


def _pausas_desde_entorno(crudo):
    """AIMHARDER_PAUSES son los periodos en los que no se reserva nada
    -- vacaciones, un viaje, una lesion --, como lista JSON de rangos con los
    dos extremos incluidos:

        ["2026-09-05:2026-09-27", "2026-12-24"]

    Las fechas son las de LAS CLASES, no las del momento de reservar, y esa es
    la distincion que hace que esto sirva de algo. La reserva se abre 7 dias
    antes: las clases de la primera semana fuera se reservan cuando uno todavia
    esta en casa, y las de la semana de vuelta se reservan estando fuera.
    Apagar el bot se equivoca en los dos extremos -- deja reservada la ida y
    pierde la vuelta --; filtrar por la fecha de la clase, en ninguno.

    Un rango invalido se descarta con un aviso y los demas siguen, igual que
    con los targets. Los ya pasados no estorban: se pueden dejar puestos."""
    if not crudo.strip():
        return []
    try:
        datos = json.loads(crudo)
    except json.JSONDecodeError as e:
        print(f"[CONFIG] AIMHARDER_PAUSES no es JSON valido: {e}", flush=True)
        return []
    if not isinstance(datos, list):
        print("[CONFIG] AIMHARDER_PAUSES tiene que ser una lista JSON.", flush=True)
        return []

    pausas = []
    for entrada in datos:
        rango = _rango_de_pausa(entrada)
        if rango:
            pausas.append(rango)
        else:
            # El valor no se vuelca: una pausa dice cuando no estas en casa.
            print("[CONFIG] Pausa descartada, formato invalido "
                  "('AAAA-MM-DD' o 'AAAA-MM-DD:AAAA-MM-DD', con fin >= inicio).",
                  flush=True)
    return pausas


def en_pausa(dia, pausas):
    """dia es un date, y los dos extremos de cada rango cuentan como pausa."""
    return any(inicio <= dia <= fin for inicio, fin in pausas)


CONFIG = {
    "email": os.environ.get("AIMHARDER_EMAIL", "").strip(),
    "password": os.environ.get("AIMHARDER_PASSWORD", ""),
    "box_subdomain": os.environ.get("AIMHARDER_BOX", "").strip(),
    "box_id": _entero("AIMHARDER_BOX_ID"),
    "book_days_before": 7,
    "poll_interval_seconds": 0.5,
    "retry_window_seconds": 30,
    "targets": _targets_desde_entorno(os.environ.get("AIMHARDER_TARGETS", "")),
    "pauses": _pausas_desde_entorno(os.environ.get("AIMHARDER_PAUSES", "")),
}


# --- Pausas ---------------------------------------------------------------
# Un rango absurdo -- o un dedazo en el ano -- no puede dejar a next_opening
# saltando ocurrencias para siempre: pasado el horizonte se rinde, y el
# scheduler lo dice por los logs en vez de colgarse sin que nadie lo reinicie.
HORIZONTE_SEMANAS = 105        # ~2 anos de ocurrencias semanales
PAUSA_RECHECK_SECONDS = 3600   # cada cuanto se reintenta si no queda ninguna


# --- Sesion ---------------------------------------------------------------
# La cookie caduca en pocas horas y sin vida fija conocida. En vez de
# adivinarla, se renueva la sesion justo antes de cada apertura y se reintenta
# si la API responde logout dentro de la ventana.
PRE_OPENING_LOGIN_SECONDS = 120
MAX_RELOGINS_PER_BOOKING = 2

# --- Calendario -----------------------------------------------------------
# El .ics se genera consultando la API, no de lo que el bot recuerde haber
# reservado: asi salen tambien las reservas hechas desde el movil, las
# cancelaciones desaparecen solas y no hay estado que perder en cada
# despliegue, que se lleva por delante el disco del contenedor.
CAL_TOKEN = os.environ.get("AIMHARDER_CAL_TOKEN", "").strip()
CAL_NOMBRE = "Clases AimHarder"
CAL_DIAS = 8               # la ventana de reserva son 7 dias: con 8 sobra
CAL_DIAS_ATRAS = 90        # historial: a que clases has ido
CAL_TTL_SECONDS = 900      # iOS puede pedir el .ics varias veces seguidas
CAL_DURACION_MIN = 60      # si el horario no trae hora de fin

_CAL_CACHE = {"ts": 0.0, "texto": None}
# Un dia ya vivido no cambia, asi que se guarda y no se vuelve a pedir. Es solo
# una optimizacion: la API devuelve el bookState de dias pasados, asi que un
# reinicio no pierde el historial, solo lo vuelve a consultar.
_CAL_DIAS_CACHE = {}
_CAL_LOCK = threading.Lock()
_CAL_CLIENTE = None

# --- Latido del scheduler -------------------------------------------------
# El servidor HTTP corre en su propio hilo, asi que responder no prueba que el
# bucle de reservas siga vivo. El scheduler marca un latido y el handler sirve
# 503 si se enfria o si hubo un error fatal.
HEARTBEAT_MAX_AGE_SECONDS = 120
# Espera antes de salir tras un error fatal, para que una credencial mala no se
# convierta en un bucle de reinicios de Fly golpeando el login.
FATAL_BACKOFF_SECONDS = 60

_HEARTBEAT = {"ts": time.time(), "status": "arrancando", "fatal": None}
_HEARTBEAT_LOCK = threading.Lock()


def beat(status=None):
    """Marca el latido. OJO: 'status' se sirve por el endpoint publico, asi que
    solo admite fases genericas ('esperando', 'reservando') y nunca la clase,
    la hora ni el dia. El detalle va por print(), a los logs de Fly."""
    with _HEARTBEAT_LOCK:
        _HEARTBEAT["ts"] = time.time()
        if status is not None:
            _HEARTBEAT["status"] = status


def mark_fatal(message):
    """Igual que beat(): el mensaje se sirve publicamente. Los textos de las
    excepciones llevan la URL del box y a veces el usuario, asi que aqui solo
    entra el tipo del error o la lista de variables que faltan."""
    with _HEARTBEAT_LOCK:
        _HEARTBEAT["fatal"] = message


def health_snapshot():
    with _HEARTBEAT_LOCK:
        return _HEARTBEAT["fatal"], _HEARTBEAT["status"], time.time() - _HEARTBEAT["ts"]


class SessionExpired(RuntimeError):
    """La API respondio {'logout': 1}: la cookie de sesion ya no vale.

    Se distingue de un rechazo normal de reserva (plazas agotadas, clase
    cerrada) porque tiene arreglo automatico: volver a hacer login."""


def huella_estable(email):
    """Identificador de dispositivo, 50 hex. Derivarlo del email lo hace
    estable entre reinicios sin necesidad de guardarlo en ningun sitio, para
    no parecer un dispositivo nuevo en cada despliegue."""
    import hashlib

    return os.environ.get("AIMHARDER_FINGERPRINT", "").strip() or \
        hashlib.sha256(email.encode("utf-8")).hexdigest()[:50]


def casa_con(clase, time_hhmm, name_contains):
    """Criterio unico para decidir si una clase del horario es la buscada, y
    fuera del cliente a proposito: el diagnostico de arranque lo aplica sobre
    un horario que ya tiene en la mano, sin volver a pedirlo."""
    return (clase.get("time", "").startswith(time_hhmm)
            and name_contains.lower() in clase.get("className", "").lower())


class AimHarderClient:
    def __init__(self, email, password, box_subdomain, box_id):
        self.fingerprint = huella_estable(email)
        self.email = email
        self.password = password
        self.box_subdomain = box_subdomain
        self.box_id = box_id
        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar)
        )

    def _request(self, url, data=None, method="GET"):
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; aimharder-autobook/1.0)",
            "X-Requested-With": "XMLHttpRequest",
        }
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with self.opener.open(req, timeout=15) as resp:
            return resp.read().decode("utf-8")

    def _api_json(self, url, data=None, method="GET"):
        """Toda respuesta de la API pasa por aqui. AimHarder no devuelve 401:
        con la sesion caducada responde 200 con {'logout': 1}, asi que el unico
        sitio donde detectarlo es el JSON."""
        result = json.loads(self._request(url, data=data, method=method))
        if isinstance(result, dict) and result.get("logout"):
            raise SessionExpired(f"La API respondio logout: {result}")
        return result

    def login(self, verify=True):
        """POST /api/login con JSON, y despues /setrefresh para trasladar la
        sesion al subdominio del box.

        El endpoint responde 200 tanto si autentica como si no, asi que el
        codigo HTTP no dice nada: lo que cuenta es authOK en el JSON, y luego
        que verificar_sesion() encuentre usuario."""
        cuerpo = json.dumps({
            "username": self.email,
            "password": self.password,
            "fingerprint": self.fingerprint,
            "iniframe": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            LOGIN_API_URL, data=cuerpo, method="POST",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                     "Content-Type": "application/json",
                     "X-Requested-With": "XMLHttpRequest"},
        )
        try:
            with self.opener.open(req, timeout=15) as resp:
                datos = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # El motivo del rechazo viene en el cuerpo, no en el codigo.
            detalle = e.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"Login rechazado (HTTP {e.code}): {detalle}") from e

        interior = datos.get("data") if isinstance(datos.get("data"), dict) else {}
        auth = interior.get("auth") if isinstance(interior.get("auth"), dict) else {}
        token = auth.get("refreshToken")
        if not auth.get("authOK") or not token:
            # 'info' trae el motivo (credenciales malas, cuenta bloqueada...).
            raise RuntimeError(f"Login fallido: authOK={auth.get('authOK')!r}, "
                               f"info={datos.get('info')!r}")

        destino = f"https://{self.box_subdomain}.aimharder.com/"
        url = SETREFRESH_URL + "?" + urllib.parse.urlencode(
            {"redirect": destino, "token": token, "fingerprint": self.fingerprint})
        self._request(url)

        if verify:
            self.verificar_sesion()

    def verificar_sesion(self):
        """Confirma que hay sesion de verdad. Tiene que ser /api/whoami:
        /api/bookings responde lo mismo autenticado o no, y daria verde con la
        sesion rota."""
        datos = self._api_json(f"{AIMHARDER_URL}/api/whoami")

        def busca_usuario(nodo, profundidad=0):
            """La forma de la respuesta cambia entre versiones del front, asi
            que se busca el primer dict con id y nombre, bajando tambien por
            las listas, en vez de fijar una ruta concreta."""
            if profundidad > 3:
                return None
            if isinstance(nodo, dict):
                if nodo.get("id") and ("name" in nodo or "mail" in nodo or "email" in nodo):
                    return nodo
                hijos = nodo.values()
            elif isinstance(nodo, list):
                hijos = nodo
            else:
                return None
            for valor in hijos:
                encontrado = busca_usuario(valor, profundidad + 1)
                if encontrado:
                    return encontrado
            return None

        usuario = busca_usuario(datos)
        if usuario is None:
            forma = {k: (sorted(v) if isinstance(v, dict) else type(v).__name__)
                     for k, v in datos.items()}
            raise RuntimeError(f"Login sin sesion: /api/whoami no trae usuario. Forma: {forma}")
        return usuario

    def get_schedule(self, day_yyyymmdd):
        params = urllib.parse.urlencode({"day": day_yyyymmdd, "familyId": "", "box": self.box_id})
        url = f"https://{self.box_subdomain}.aimharder.com/api/bookings?{params}"
        return self._api_json(url)

    def find_class(self, day_yyyymmdd, time_hhmm, name_contains):
        for c in self.get_schedule(day_yyyymmdd).get("bookings", []):
            if casa_con(c, time_hhmm, name_contains):
                return c
        return None

    def book(self, class_id, day_yyyymmdd):
        url = f"https://{self.box_subdomain}.aimharder.com/api/book"
        payload = {
            "id": class_id,
            "day": day_yyyymmdd,
            "familyId": "",
            "insist": "0",
            "box": self.box_id,
        }
        result = self._api_json(url, data=payload, method="POST")
        if not result.get("bookState") and not result.get("id"):
            raise RuntimeError(f"Reserva rechazada: {result}")
        return result


def next_class_datetime(target, after):
    days_ahead = (target["weekday"] - after.weekday()) % 7
    hour, minute = (int(x) for x in target["time"].split(":"))
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= after:
        candidate += timedelta(days=7)
    return candidate


def next_opening(target, book_days_before, after, pausas=()):
    """Primera ocurrencia reservable del target -> (apertura, clase).

    Se salta dos cosas. Las que tienen la ventana ya abierta, que hay que
    reservar a mano: dejarlas devolveria una apertura en el pasado y el bucle
    de reservas giraria sin esperar. Y las que caen en una pausa, mirando la
    fecha de LA CLASE y no la de la apertura -- ver _pausas_desde_entorno().

    Devuelve None si no queda ninguna dentro del horizonte."""
    class_dt = next_class_datetime(target, after)
    for _ in range(HORIZONTE_SEMANAS):
        opening_dt = class_dt - timedelta(days=book_days_before)
        if opening_dt > after and not en_pausa(class_dt.date(), pausas):
            return opening_dt, class_dt
        class_dt += timedelta(days=7)
    return None


def sleep_until(dt, status=None):
    while True:
        remaining = (dt - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        beat(status)
        time.sleep(min(30, remaining))


def cliente_desde_config(config=CONFIG):
    return AimHarderClient(config["email"], config["password"],
                           config["box_subdomain"], config["box_id"])


def proximo_objetivo(config, ahora):
    """La apertura mas cercana de todos los targets -> (apertura, clase, target),
    o None si ninguno tiene ocurrencia reservable dentro del horizonte."""
    pausas = config.get("pauses") or ()
    candidatos = [ocurrencia + (t,) for t in config["targets"]
                  if (ocurrencia := next_opening(t, config["book_days_before"],
                                                 ahora, pausas))]
    return min(candidatos, key=lambda c: c[0]) if candidatos else None


def imprimir_horario(bookings, prefijo):
    for c in bookings:
        plazas = f"  [{c.get('ocupation', '?')}/{c['limit']}]" if c.get("limit") else ""
        estado = "  YA RESERVADA" if c.get("bookState") else ""
        print(f"{prefijo}   id={c.get('id', '?'):<10} {c.get('time', '?'):<16} "
              f"{c.get('className', '?')}{plazas}{estado}", flush=True)


def intentar_reserva(client, clase, day_str):
    """Un intento de reserva, sin reintentos -> (ok, texto).

    SessionExpired se propaga a proposito: es el unico fallo con arreglo
    automatico. El resto de rechazos -- clase llena, sin bono, fuera de plazo --
    son definitivos y vuelven como (False, motivo)."""
    try:
        resultado = client.book(clase["id"], day_str)
    except SessionExpired:
        raise
    except RuntimeError as e:
        return False, str(e)
    return True, f"{clase.get('className')} {clase.get('time')} del {day_str} -> {resultado}"


def refresh_session(client, motivo, verify=True):
    beat("refrescando login")
    client.login(verify=verify)
    print(f"[SESION] Login refrescado ({motivo}).", flush=True)


def book_target(client, target, class_dt, config):
    day_str = class_dt.strftime("%Y%m%d")
    deadline = time.time() + config.get("retry_window_seconds", 30)
    beat("reservando")
    relogins = 0
    found = None

    def recuperar_sesion(motivo):
        """Rehace el login tras un logout. Devuelve False si hay que rendirse
        con esta reserva: reintentos agotados o login fallido. Nunca propaga,
        porque una reserva perdida no debe tumbar el scheduler."""
        nonlocal relogins
        if relogins >= MAX_RELOGINS_PER_BOOKING:
            print(f"[FALLO] {target} el {day_str}: sesion caducada tras {relogins} re-logins.", flush=True)
            return False
        relogins += 1
        try:
            # verify=False: dentro de la ventana cada peticion cuenta, y la
            # siguiente ya delata si el login no ha servido.
            refresh_session(client, motivo, verify=False)
        except Exception as e:
            print(f"[FALLO] {target} el {day_str}: no se pudo rehacer el login: {e!r}", flush=True)
            return False
        return True

    while time.time() < deadline:
        try:
            found = client.find_class(day_str, target["time"], target["name_contains"])
        except SessionExpired:
            if not recuperar_sesion("logout leyendo el horario"):
                return
            continue
        if found:
            break
        beat()
        time.sleep(config.get("poll_interval_seconds", 0.5))

    if not found:
        print(f"[FALLO] No se encontro clase para {target} el {day_str}", flush=True)
        return

    while True:
        try:
            ok, texto = intentar_reserva(client, found, day_str)
        except SessionExpired:
            if not recuperar_sesion("logout al reservar"):
                return
            continue
        etiqueta = "[OK] Reservado:" if ok else f"[FALLO] {target} el {day_str}:"
        print(f"{etiqueta} {texto}", flush=True)
        return


def startup_diagnostic(client, config):
    """Una sola lectura al arrancar: confirma que las horas y los nombres de
    los targets casan con lo que devuelve la API. No reserva nada."""
    ahora = datetime.now()
    for inicio, fin in config.get("pauses") or ():
        marca = " -- ya pasada" if fin < ahora.date() else ""
        print(f"[DIAG] Pausa del {inicio} al {fin}, incluidos: ninguna clase de "
              f"esos dias se reserva{marca}.", flush=True)

    siguiente = proximo_objetivo(config, ahora)
    if siguiente is None:
        print(f"[DIAG] AVISO: ningun objetivo tiene apertura en las proximas "
              f"{HORIZONTE_SEMANAS} semanas. Revisa AIMHARDER_PAUSES.", flush=True)
        return
    _, class_dt, target = siguiente
    day_str = class_dt.strftime("%Y%m%d")

    print(f"[DIAG] Hora local del contenedor: {ahora:%Y-%m-%d %H:%M:%S} "
          f"{ahora.astimezone().tzname()} (TZ={os.environ.get('TZ', 'no definida')})", flush=True)
    try:
        data = client.get_schedule(day_str)
    except Exception as e:
        print(f"[DIAG] FALLO al leer el horario de {day_str}: {e!r}", flush=True)
        return

    bookings = data.get("bookings", [])
    print(f"[DIAG] Horario de {day_str}: {len(bookings)} clases devueltas.", flush=True)
    if not bookings:
        print(f"[DIAG] Respuesta cruda (500 chars): {json.dumps(data)[:500]}", flush=True)
        return
    imprimir_horario(bookings, "[DIAG]")

    match = next((c for c in bookings
                  if casa_con(c, target["time"], target["name_contains"])), None)
    if match:
        print(f"[DIAG] OK: '{target['name_contains']}' a las {target['time']} "
              f"encontrada (id={match.get('id')}).", flush=True)
    else:
        print(f"[DIAG] AVISO: ninguna clase casa con '{target['name_contains']}' a las "
              f"{target['time']}. Revisa AIMHARDER_TARGETS.", flush=True)


def avisa_reservas_en_pausa(client, config):
    """Avisa por los logs de las clases ya reservadas que caen en una pausa.

    Poner la pausa con menos de book_days_before dias de margen llega tarde
    para lo que el bot ya reservo: esas reservas siguen en pie. El bot no las
    anula -- cancelar no es cosa de un bot que reserva, y una pausa mal escrita
    borraria clases de verdad --, asi que se nombran y se anulan desde la app.

    Solo se consultan los dias en pausa que caen dentro de la ventana de
    reserva: fuera de vacaciones esto no cuesta ni una peticion."""
    pausas = config.get("pauses") or ()
    if not pausas:
        return
    hoy = datetime.now()
    revisados = avisadas = 0
    for delta in range(config["book_days_before"] + 1):
        dia = hoy + timedelta(days=delta)
        if not en_pausa(dia.date(), pausas):
            continue
        day_str = dia.strftime("%Y%m%d")
        revisados += 1
        try:
            clases = client.get_schedule(day_str).get("bookings", [])
        except Exception as e:
            print(f"[PAUSA] No se pudo revisar {day_str}: {e!r}", flush=True)
            continue
        for clase in (c for c in clases if c.get("bookState")):
            avisadas += 1
            print(f"[PAUSA] Ya tenias reservada {clase.get('className')} "
                  f"{clase.get('time')} del {day_str}, que cae en pausa. "
                  "El bot no cancela nada: anulala desde la app.", flush=True)
    if revisados and not avisadas:
        print(f"[PAUSA] {revisados} dias en pausa dentro de la ventana de "
              "reserva, sin nada reservado en ellos.", flush=True)


def config_incompleta(config):
    """Variables de entorno obligatorias que faltan. Devuelve los nombres, que
    se pueden publicar; los valores no."""
    falta = [nombre for nombre, valor in (
        ("AIMHARDER_EMAIL", config["email"]),
        ("AIMHARDER_PASSWORD", config["password"]),
        ("AIMHARDER_BOX", config["box_subdomain"]),
        ("AIMHARDER_BOX_ID", config["box_id"]),
    ) if not valor]
    if not config["targets"]:
        # Sin targets no hay nada que esperar, y proximo_objetivo() haria un
        # min() sobre lista vacia.
        falta.append("AIMHARDER_TARGETS")
    return falta


def scheduler_loop(config):
    falta = config_incompleta(config)
    if falta:
        # Reiniciar no traeria el secreto que falta: quedarse sirviendo 503 es
        # la senal correcta, y ademas la unica que se ve desde fuera.
        aviso = f"Faltan variables de entorno: {', '.join(falta)}."
        mark_fatal(aviso)
        print(f"{aviso} El bucle no arranca.", flush=True)
        return

    client = cliente_desde_config(config)
    beat("login inicial")
    client.login()
    print("[DIAG] login() verificado contra la API.", flush=True)
    startup_diagnostic(client, config)
    avisa_reservas_en_pausa(client, config)

    while True:
        # La fase que se sirve por el endpoint publico es la misma con pausa y
        # sin ella. Un "de vacaciones" ahi diria a cualquiera que pase por la
        # URL que no estas en casa, que es exactamente lo que no se publica.
        espera = "esperando a la proxima apertura"
        siguiente = proximo_objetivo(config, datetime.now())
        if siguiente is None:
            # Con todos los targets en pausa mas alla del horizonte no hay nada
            # que esperar, pero tampoco es un error: los secretos cambian en
            # caliente cuando Fly reinicia, asi que se vuelve a mirar.
            print(f"[PAUSA] Ningun objetivo tiene apertura en las proximas "
                  f"{HORIZONTE_SEMANAS} semanas. Revisa AIMHARDER_PAUSES. "
                  f"Se reintenta en {PAUSA_RECHECK_SECONDS // 60} min.", flush=True)
            sleep_until(datetime.now() + timedelta(seconds=PAUSA_RECHECK_SECONDS), espera)
            continue
        opening_dt, class_dt, target = siguiente

        # Con la zona horaria explicita: el visor de logs de Fly marca cada
        # linea en UTC, y en verano eso son 2 h menos que la hora del bot.
        print(f"Proxima apertura: {target} -> {opening_dt:%Y-%m-%d %H:%M} "
              f"{datetime.now().astimezone().tzname()}", flush=True)
        # Fecha y clase se quedan en los logs. Ver beat().

        # Despertar antes de la apertura para llegar a la ventana con una
        # sesion recien hecha: entre dos aperturas pueden pasar dias.
        sleep_until(opening_dt - timedelta(seconds=PRE_OPENING_LOGIN_SECONDS), espera)
        try:
            refresh_session(client, f"apertura de {opening_dt:%H:%M}")
        except Exception as e:
            # Puede ser un corte de red pasajero: se intenta reservar igual, y
            # book_target reintenta si la API responde logout.
            print(f"[AVISO] No se pudo refrescar el login antes de la apertura: {e!r}", flush=True)

        sleep_until(opening_dt, espera)
        book_target(client, target, class_dt, config)
        time.sleep(2)


def _olvida_dias_viejos(hoy, atras):
    """La cache solo guarda la ventana, para que no crezca un dia cada dia."""
    limite = (hoy - timedelta(days=atras)).strftime("%Y%m%d")
    for dia in [d for d in _CAL_DIAS_CACHE if d < limite]:
        del _CAL_DIAS_CACHE[dia]


def mis_reservas(client, atras=CAL_DIAS_ATRAS, adelante=CAL_DIAS):
    """Clases reservadas entre 'atras' dias antes de hoy y 'adelante' despues,
    como [(clase, AAAAMMDD)]. Reservada = con bookState.

    Los dias pasados salen de _CAL_DIAS_CACHE, asi que un refresco normal son
    'adelante' consultas y no las ~100 de la ventana entera.

    El barrido no se fia de /api/bookings para saber si la sesion sigue viva:
    con la cookie caducada responde el horario igual, solo que sin bookState en
    ninguna clase, que aqui es indistinguible de 'no tienes nada reservado'. Por
    eso se confirma con /api/whoami antes de dar el barrido por bueno."""
    hoy = datetime.now()
    reservas, congelables = [], {}
    for delta in range(-atras, adelante):
        dia = (hoy + timedelta(days=delta)).strftime("%Y%m%d")
        pasado = delta < 0
        clases = _CAL_DIAS_CACHE.get(dia) if pasado else None
        if clases is None:
            clases = [c for c in client.get_schedule(dia).get("bookings", [])
                      if c.get("bookState")]
            if pasado:
                # Aun no a la cache: si la sesion se cayo a mitad del barrido,
                # esto son dias vacios y congelarlos los perderia para siempre.
                congelables[dia] = clases
        reservas += [(clase, dia) for clase in clases]

    try:
        client.verificar_sesion()
    except SessionExpired:
        raise
    except RuntimeError as e:
        # verificar_sesion() distingue mal login de sesion caida; para quien
        # llama las dos son lo mismo, un barrido que hay que repetir tras entrar.
        raise SessionExpired(f"/api/whoami no confirma la sesion: {e}") from e

    _CAL_DIAS_CACHE.update(congelables)
    _olvida_dias_viejos(hoy, atras)
    return reservas


def _escapa_ics(texto):
    """RFC 5545: en los campos de texto hay que escapar \\ ; , y los saltos."""
    return (str(texto or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _pliega(linea):
    """RFC 5545: las lineas largas se parten y continuan con un espacio
    delante. El corte va por caracteres, no por bytes, para no partir un
    acento en dos."""
    if len(linea) <= 72:
        return linea
    trozos, resto = [linea[:72]], linea[72:]
    while resto:
        trozos.append(" " + resto[:71])
        resto = resto[71:]
    return "\r\n".join(trozos)


def _horas_de_clase(clase, dia):
    """El horario da la hora como '18:10 - 19:10'. Devuelve inicio y fin en
    hora local; si no hay hora de fin, se usa classLength."""
    partes = [p.strip() for p in str(clase.get("time", "")).split("-")]
    inicio = datetime.strptime(f"{dia} {partes[0]}", "%Y%m%d %H:%M")
    if len(partes) > 1 and partes[1]:
        try:
            fin = datetime.strptime(f"{dia} {partes[1]}", "%Y%m%d %H:%M")
            if fin > inicio:
                return inicio, fin
        except ValueError:
            pass
    try:
        minutos = int(clase.get("classLength") or CAL_DURACION_MIN)
    except (TypeError, ValueError):
        minutos = CAL_DURACION_MIN
    return inicio, inicio + timedelta(minutes=minutos)


def construye_ics(reservas, nombre=CAL_NOMBRE):
    """Calendario ICS a partir de [(clase, AAAAMMDD)].

    Las horas van en UTC ('...Z') convertidas desde la local, que evita tener
    que emitir un bloque VTIMEZONE. El contenedor corre con TZ=Europe/Madrid,
    asi que astimezone() sobre una fecha naive acierta."""
    sello = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lineas = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//aimharder-bot//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escapa_ics(nombre)}",
    ]
    for clase, dia in reservas:
        try:
            inicio, fin = _horas_de_clase(clase, dia)
        except (ValueError, TypeError, IndexError):
            # Una hora ilegible se salta: no vale cargarse el calendario entero
            # por una clase.
            continue
        lineas += [
            "BEGIN:VEVENT",
            # UID estable: iOS actualiza el evento en vez de duplicarlo.
            f"UID:aimharder-{clase.get('id')}-{dia}@aimharder-bot",
            f"DTSTAMP:{sello}",
            f"DTSTART:{inicio.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
            f"DTEND:{fin.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{_escapa_ics(clase.get('className') or 'Clase')}",
        ]
        sitio = clase.get("boxDir") or clase.get("boxName")
        if sitio:
            lineas.append(f"LOCATION:{_escapa_ics(sitio)}")
        if clase.get("coachName"):
            lineas.append(f"DESCRIPTION:{_escapa_ics('Coach: ' + clase['coachName'])}")
        lineas.append("END:VEVENT")
    lineas.append("END:VCALENDAR")
    return "\r\n".join(_pliega(l) for l in lineas) + "\r\n"


def _cliente_calendario():
    """Cliente del calendario con la sesion ya comprobada.

    La cookie dura pocas horas y el calendario no reserva nada, asi que nada la
    renovaba: el scheduler refresca la suya antes de cada apertura, pero esta se
    quedaba caducada para siempre. Y como el .ics solo consulta /api/bookings,
    que no devuelve logout, el fallo no se veia -- el calendario seguia dando
    200 con el historial congelado en cache y nada de hoy en adelante.

    Comprobar aqui ademas evita barrer ~100 dias para tirarlos: mis_reservas
    tambien confirma la sesion al final, pero eso es para el caso raro de que se
    caiga a mitad del barrido."""
    global _CAL_CLIENTE

    if _CAL_CLIENTE is None:
        _CAL_CLIENTE = cliente_desde_config()
        _CAL_CLIENTE.login()
        return _CAL_CLIENTE
    try:
        _CAL_CLIENTE.verificar_sesion()
    except (SessionExpired, RuntimeError) as e:
        print(f"[CAL] Sesion caducada ({type(e).__name__}); re-login.", flush=True)
        _CAL_CLIENTE.login()
    return _CAL_CLIENTE


def calendario_ics():
    """Texto del .ics, cacheado CAL_TTL_SECONDS.

    Usa cliente propio, no el del scheduler: el CookieJar no es seguro entre
    hilos, y un re-login lanzado desde el hilo HTTP en plena ventana de reserva
    dejaria al scheduler sin sesion. El lock ademas evita que varias peticiones
    simultaneas de iOS disparen ocho consultas cada una."""
    with _CAL_LOCK:
        if _CAL_CACHE["texto"] is not None and time.time() - _CAL_CACHE["ts"] < CAL_TTL_SECONDS:
            return _CAL_CACHE["texto"]

        cliente = _cliente_calendario()
        try:
            reservas = mis_reservas(cliente)
        except SessionExpired:
            cliente.login()
            reservas = mis_reservas(cliente)

        texto = construye_ics(reservas)
        _CAL_CACHE.update(ts=time.time(), texto=texto)
        return texto


def precalienta_calendario():
    """Rellena la cache del historial al arrancar, en segundo plano, para que
    la primera peticion del iPhone no tenga que encadenar ~100 consultas y
    agotar su paciencia. No es critico: si falla, esa peticion lo reintenta."""
    inicio = time.time()
    try:
        calendario_ics()
    except Exception as e:
        print(f"[AVISO] No se pudo precalentar el calendario: {e!r}", flush=True)
        return
    print(f"[CAL] Calendario listo: {len(_CAL_DIAS_CACHE)} dias de historial "
          f"en cache ({time.time() - inicio:.0f}s).", flush=True)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        ruta = urllib.parse.urlsplit(self.path).path
        if ruta == "/":
            code, cuerpo = self._salud()
            self._responde(code, cuerpo)
        elif CAL_TOKEN and ruta == f"/cal/{CAL_TOKEN}.ics":
            try:
                cuerpo = calendario_ics()
            except Exception as e:
                self._responde(503, f"No se pudo generar el calendario: {e!r}")
                return
            self._responde(200, cuerpo, "text/calendar; charset=utf-8")
        else:
            # Sin AIMHARDER_CAL_TOKEN el calendario no existe, ni siquiera
            # para decir que el token es incorrecto.
            self._responde(404, "No hay nada aqui.")

    def _salud(self):
        fatal, status, age = health_snapshot()
        if fatal is not None:
            return 503, f"MUERTO: {fatal}"
        if age > HEARTBEAT_MAX_AGE_SECONDS:
            return 503, (f"SIN LATIDO: {age:.0f}s sin senal del scheduler "
                         f"(limite {HEARTBEAT_MAX_AGE_SECONDS}s). Ultimo estado: {status}")
        return 200, f"Estoy vivo | {status} | ultimo latido hace {age:.0f}s"

    def _responde(self, code, cuerpo, tipo="text/plain; charset=utf-8"):
        payload = cuerpo.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    # Con hilos a proposito: construir el calendario son hasta ocho peticiones a
    # AimHarder, que en un servidor de un solo hilo bloquearian el health check
    # mas alla del timeout de 5s de fly.toml.
    ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


def diag_sesion(day_yyyymmdd):
    """Estado de la sesion, para cuando algo huela mal. Solo lecturas, y sin
    imprimir valores de cookie: son credenciales."""
    client = cliente_desde_config()
    client.login(verify=False)
    usuario = client.verificar_sesion()
    print(f"[SES] Usuario: id={usuario.get('id')} nombre={usuario.get('name')} "
          f"centro={usuario.get('centre')}", flush=True)

    print("[SES] Cookies:", flush=True)
    for c in client.cookiejar:
        print(f"[SES]   {c.name:<12} dominio={c.domain}", flush=True)
    dominios = {c.domain for c in client.cookiejar if c.name == "amhrdrauth"}
    print(f"[SES] amhrdrauth sobre {dominios or 'NINGUN dominio'} "
          f"-- tiene que ser '.aimharder.com' para que valga en el box.", flush=True)

    clases = client.get_schedule(day_yyyymmdd).get("bookings", [])
    reservadas = [c for c in clases if c.get("bookState")]
    print(f"[SES] Horario de {day_yyyymmdd}: {len(clases)} clases, "
          f"{len(reservadas)} ya reservadas por ti.", flush=True)
    return 0


def cli(argv):
    """Modo manual, para clases fuera de los targets o para probar el flujo
    completo sin esperar a una apertura. Sin --reservar solo lee."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Consulta el horario de un dia y, opcionalmente, reserva una clase.",
    )
    parser.add_argument("--dia", required=True, metavar="YYYYMMDD",
                        help="dia a consultar, p.ej. 20260818")
    parser.add_argument("--reservar", metavar="ID",
                        help="id de clase a reservar; sin esto solo se lista el horario")
    parser.add_argument("--diag-sesion", action="store_true",
                        help="diagnostica donde se rompe la sesion; no reserva nada")
    args = parser.parse_args(argv)

    # El modo manual no usa targets: basta con poder entrar y hablar con el box.
    falta = [v for v in config_incompleta(CONFIG) if v != "AIMHARDER_TARGETS"]
    if falta:
        print(f"Faltan variables de entorno: {', '.join(falta)}.", flush=True)
        return 1

    if args.diag_sesion:
        return diag_sesion(args.dia)

    client = cliente_desde_config()
    client.login()
    print(f"[CLI] Login verificado. Hora local: {datetime.now():%Y-%m-%d %H:%M:%S} "
          f"{datetime.now().astimezone().tzname()}", flush=True)

    data = client.get_schedule(args.dia)
    bookings = data.get("bookings", [])
    print(f"[CLI] Horario de {args.dia}: {len(bookings)} clases.", flush=True)
    imprimir_horario(bookings, "[CLI]")

    if not args.reservar:
        print("[CLI] Solo lectura. Anade --reservar <id> para reservar una de estas.", flush=True)
        return 0

    elegida = next((c for c in bookings if str(c.get("id")) == str(args.reservar)), None)
    if elegida is None:
        print(f"[CLI] El id {args.reservar} no esta en el horario de {args.dia}. No se reserva nada.", flush=True)
        return 1

    print(f"[CLI] Reservando {elegida.get('className')} {elegida.get('time')} del {args.dia}...", flush=True)
    try:
        ok, texto = intentar_reserva(client, elegida, args.dia)
    except SessionExpired as e:
        print(f"[CLI] FALLO: sesion caducada ({e}).", flush=True)
        return 1
    if not ok:
        print(f"[CLI] FALLO: {texto}", flush=True)
        return 1
    print(f"[CLI] OK -> {texto}", flush=True)
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        # Modo manual: ni scheduler ni servidor de health check.
        raise SystemExit(cli(sys.argv[1:]))

    threading.Thread(target=start_health_server, daemon=True).start()
    if CAL_TOKEN:
        threading.Thread(target=precalienta_calendario, daemon=True).start()
    try:
        scheduler_loop(CONFIG)
    except BaseException as e:
        # Una excepcion no controlada (403 en login, red caida, JSON corrupto)
        # se marca, se sirve como 503 un rato para que se vea desde fuera, y
        # sale con codigo != 0 para que Fly reinicie la maquina.
        import traceback

        # Solo el tipo: el texto de un HTTPError lleva la URL del box, y el de
        # un login rechazado puede llevar el usuario. El traceback completo va
        # a los logs, que no son publicos.
        mark_fatal(type(e).__name__)
        traceback.print_exc()
        print(f"[FATAL] Scheduler caido. Saliendo en {FATAL_BACKOFF_SECONDS}s para que Fly reinicie.", flush=True)
        time.sleep(FATAL_BACKOFF_SECONDS)
        raise SystemExit(1)

    # scheduler_loop solo retorna sin excepcion si falta configuracion. El
    # proceso sigue vivo sirviendo 503: reiniciar no lo arreglaria.
    print("[FATAL] El scheduler termino. Sirviendo 503 indefinidamente.", flush=True)
    while True:
        time.sleep(3600)
