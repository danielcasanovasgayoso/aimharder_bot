#!/usr/bin/env python3
"""Tests del bot, sin dependencias y sin salir a internet.

    python3 test_bot.py

La API de AimHarder se simula con un cliente falso. Lo que se prueba es como
reacciona el bot a un {'logout': 1}, a un login rechazado y a un rechazo de
reserva normal, y que nada personal se escape por el endpoint publico.
"""

import contextlib
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

os.environ["TZ"] = "Europe/Madrid"
time.tzset()

sys.path.insert(0, ".")
import aimharder_bot_render as bot

# Configuracion inventada. La de verdad vive en el entorno (AIMHARDER_*), asi
# que no se puede leer de bot.CONFIG, y tampoco debe estar escrita aqui: estos
# valores solo existen para que las cuentas cuadren.
CFG = {
    "email": "a@b.c",
    "password": "pw",
    "box_subdomain": "box",
    "box_id": 1,
    "book_days_before": 7,
    "poll_interval_seconds": 0,
    "retry_window_seconds": 3,
    "targets": [
        {"weekday": 1, "time": "07:00", "name_contains": "Metcon"},     # martes
        {"weekday": 3, "time": "20:00", "name_contains": "Crossfit"},   # jueves
        {"weekday": 5, "time": "10:30", "name_contains": "Gimnastico"}, # sabado
    ],
}

# El horario que devuelve ClienteFalso es una clase a las 18:10, asi que las
# pruebas de reserva usan un objetivo que casa con el.
OBJETIVO = {"weekday": 2, "time": "18:10", "name_contains": "Crossfit"}


class ClienteFalso(bot.AimHarderClient):
    """Responde {'logout': 1} las primeras N veces y luego funciona."""

    def __init__(self, logouts_horario=0, logouts_reserva=0):
        super().__init__("a@b.c", "pw", "box", 1)
        self.logouts_horario = logouts_horario
        self.logouts_reserva = logouts_reserva
        self.logins = 0

    def login(self, verify=True):
        self.logins += 1

    def get_schedule(self, day):
        if self.logouts_horario > 0:
            self.logouts_horario -= 1
            raise bot.SessionExpired("{'logout': 1}")
        return {"bookings": [{"id": 42, "time": "18:10 - 19:10", "className": "Crossfit"}]}

    def book(self, class_id, day):
        if self.logouts_reserva > 0:
            self.logouts_reserva -= 1
            raise bot.SessionExpired("{'logout': 1}")
        return {"bookState": 1, "id": class_id}

    def verificar_sesion(self):
        # Sin esto, mis_reservas saldria a /api/whoami de verdad.
        return {"id": 1, "name": "Falso"}


class RespuestaFalsa(io.BytesIO):
    """Respuesta de red simulada, para no tocar la API real en los tests."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def cliente_con_respuesta(cuerpo):
    cli = bot.AimHarderClient("a@b.c", "pw", "box", 1)
    cli.opener = type("O", (), {"open": lambda self, req, timeout=None: RespuestaFalsa(cuerpo)})()
    cli._request = lambda *a, **k: "ok"
    return cli


def reserva(cliente):
    salida = io.StringIO()
    with contextlib.redirect_stdout(salida):
        bot.book_target(cliente, OBJETIVO, bot.datetime(2026, 8, 19, 18, 10), CFG)
    return salida.getvalue()


def main():
    c = ClienteFalso(logouts_reserva=1)
    out = reserva(c)
    assert "[OK] Reservado" in out and c.logins == 1, out
    print("1 logout al reservar -> re-login y reserva OK")

    c = ClienteFalso(logouts_horario=1)
    out = reserva(c)
    assert "[OK] Reservado" in out and c.logins == 1, out
    print("2 logout leyendo el horario -> re-login y reserva OK")

    c = ClienteFalso(logouts_reserva=99)
    out = reserva(c)
    assert "[FALLO]" in out and c.logins == bot.MAX_RELOGINS_PER_BOOKING, out
    print(f"3 logout persistente -> FALLO tras {c.logins} re-logins, sin bucle")

    c = ClienteFalso()
    out = reserva(c)
    assert "[OK] Reservado" in out and c.logins == 0, out
    print("4 sesion sana -> 0 re-logins")

    cli = bot.AimHarderClient("a", "b", "c", 1)
    cli._request = lambda *a, **k: json.dumps({"logout": 1})
    try:
        cli._api_json("x")
        raise AssertionError("deberia haber lanzado SessionExpired")
    except bot.SessionExpired:
        pass
    cli._request = lambda *a, **k: json.dumps({"bookState": 1})
    assert cli._api_json("x") == {"bookState": 1}
    print("5 _api_json convierte {'logout': 1} en SessionExpired")

    cli = cliente_con_respuesta(json.dumps({"data": {"auth": {"authOK": False}}, "info": "credenciales"}).encode())
    try:
        cli.login()
        raise AssertionError("deberia haber lanzado RuntimeError")
    except RuntimeError as e:
        assert "authOK" in str(e), e
    print("6 authOK=False -> el login falla en el acto, no el dia de la clase")

    cli = cliente_con_respuesta(json.dumps({"data": {"auth": {"authOK": True, "refreshToken": "t" * 50}}}).encode())
    visto = {}
    cli._request = lambda url, **k: visto.setdefault("url", url) or "ok"
    cli.verificar_sesion = lambda: visto.setdefault("verificado", True)
    cli.login()
    assert "/setrefresh?" in visto["url"] and "redirect=" in visto["url"], visto
    assert "token=" in visto["url"] and "fingerprint=" in visto["url"], visto
    assert visto.get("verificado"), "no se verifico la sesion"
    print("7 login OK -> pasa por /setrefresh y verifica la sesion")

    a = bot.AimHarderClient("x@y.z", "p", "b", 1).fingerprint
    b = bot.AimHarderClient("x@y.z", "p", "b", 1).fingerprint
    assert a == b and len(a) == 50, (a, b)
    print("8 la huella es estable entre reinicios (50 hex)")

    # Jueves 13/08/2026 a las 09:00. De los tres targets, la apertura mas
    # cercana es la de esta misma tarde a las 20:00 (clase del jueves 20).
    ahora = bot.datetime(2026, 8, 13, 9, 0)
    apertura, clase, objetivo = bot.proximo_objetivo(CFG, ahora)
    assert objetivo["weekday"] == 3 and objetivo["time"] == "20:00", objetivo
    assert apertura == bot.datetime(2026, 8, 13, 20, 0), apertura
    assert clase == bot.datetime(2026, 8, 20, 20, 0), clase
    otras = [bot.next_opening(t, CFG["book_days_before"], ahora)[0] for t in CFG["targets"]]
    assert apertura == min(otras), (apertura, otras)
    print("9 proximo_objetivo devuelve la apertura mas cercana de los tres")

    clase_falsa = {"id": 42, "className": "Crossfit", "time": "18:10 - 19:10"}
    c = ClienteFalso()
    ok, texto = bot.intentar_reserva(c, clase_falsa, "20260819")
    assert ok and "Crossfit" in texto, texto

    c = ClienteFalso(logouts_reserva=1)
    try:
        bot.intentar_reserva(c, clase_falsa, "20260819")
        raise AssertionError("SessionExpired deberia propagarse")
    except bot.SessionExpired:
        pass

    class Lleno(ClienteFalso):
        def book(self, class_id, day):
            raise RuntimeError("Reserva rechazada: {'bookState': -2}")

    ok, texto = bot.intentar_reserva(Lleno(), clase_falsa, "20260819")
    assert not ok and "bookState" in texto, texto
    print("10 intentar_reserva propaga logout pero devuelve los rechazos normales")

    # --- calendario -------------------------------------------------------
    clase = {"id": 1223249, "className": "Crossfit", "time": "18:10 - 19:10",
             "boxDir": "Carrer Fals 1, Ciutat", "coachName": "Ana"}
    ics = bot.construye_ics([(clase, "20260821")])
    assert ics.count("BEGIN:VEVENT") == 1 and ics.count("END:VEVENT") == 1, ics
    assert "\r\n" in ics and not ics.startswith("\n"), "el .ics va con CRLF"
    # 18:10 CEST son las 16:10 UTC.
    assert "DTSTART:20260821T161000Z" in ics, ics
    assert "DTEND:20260821T171000Z" in ics, ics
    assert "UID:aimharder-1223249-20260821@aimharder-bot" in ics, ics
    assert "SUMMARY:Crossfit" in ics and "Carrer Fals 1\\, Ciutat" in ics, ics
    assert bot.construye_ics([(clase, "20260821")]).split("DTSTAMP")[0] == ics.split("DTSTAMP")[0]
    print("11 construye_ics: un VEVENT, UID estable y horas en UTC")

    suelta = {"id": 7, "className": "Metcon", "time": "09:00", "classLength": 45}
    ics = bot.construye_ics([(suelta, "20260821")])
    assert "DTSTART:20260821T070000Z" in ics and "DTEND:20260821T074500Z" in ics, ics
    rara = {"id": 8, "className": "Rara", "time": "sin hora"}
    assert bot.construye_ics([(rara, "20260821")]).count("BEGIN:VEVENT") == 0
    print("12 sin hora de fin usa classLength; una hora ilegible no rompe el .ics")

    class ConReservas(ClienteFalso):
        def get_schedule(self, day):
            return {"bookings": [
                {"id": 1, "className": "Crossfit", "time": "18:10 - 19:10", "bookState": 1},
                {"id": 2, "className": "Open Box", "time": "18:10 - 19:10", "bookState": None},
            ]}

    reservas = bot.mis_reservas(ConReservas(), atras=0, adelante=3)
    assert len(reservas) == 3, reservas
    assert all(c["id"] == 1 for c, _ in reservas), reservas
    assert len({d for _, d in reservas}) == 3, "un dia distinto por consulta"
    print("13 mis_reservas se queda solo con las de bookState")

    class Contador(ClienteFalso):
        """Cuenta que dias se piden de verdad a la API."""

        def __init__(self):
            super().__init__()
            self.consultas = []

        def get_schedule(self, day):
            self.consultas.append(day)
            return {"bookings": [{"id": 1, "className": "X",
                                  "time": "18:10 - 19:10", "bookState": 1}]}

    bot._CAL_DIAS_CACHE.clear()
    c = Contador()
    assert len(bot.mis_reservas(c, atras=3, adelante=2)) == 5
    assert len(c.consultas) == 5, c.consultas
    assert len(bot.mis_reservas(c, atras=3, adelante=2)) == 5
    # La segunda vez solo se piden hoy y mañana: el pasado ya no se toca.
    assert len(c.consultas) == 7, c.consultas
    print("15 el historial se cachea; un refresco solo consulta el futuro")

    bot._CAL_DIAS_CACHE.clear()
    bot._CAL_DIAS_CACHE["20200101"] = []
    bot.mis_reservas(Contador(), atras=2, adelante=1)
    assert "20200101" not in bot._CAL_DIAS_CACHE, bot._CAL_DIAS_CACHE
    bot._CAL_DIAS_CACHE.clear()
    print("16 los dias fuera de la ventana se olvidan, la cache no crece sola")

    class Caducado(ClienteFalso):
        """Sesion perdida: /api/bookings responde el horario igual, solo que sin
        bookState, y solo /api/whoami delata que no hay sesion."""

        def __init__(self):
            super().__init__()
            self.viva = False

        def login(self, verify=True):
            super().login(verify)
            self.viva = True

        def verificar_sesion(self):
            if not self.viva:
                raise bot.SessionExpired("{'logout': 1}")
            return {"id": 1, "name": "Falso"}

        def get_schedule(self, day):
            clase = {"id": 1, "className": "Crossfit", "time": "18:10 - 19:10"}
            if self.viva:
                clase["bookState"] = 1
            return {"bookings": [clase]}

    bot._CAL_DIAS_CACHE.clear()
    caducado = Caducado()
    try:
        bot.mis_reservas(caducado, atras=2, adelante=1)
        raise AssertionError("un barrido sin sesion no puede darse por bueno")
    except bot.SessionExpired:
        pass
    assert bot._CAL_DIAS_CACHE == {}, bot._CAL_DIAS_CACHE
    print("17 la sesion caida no congela un historial vacio en la cache")

    bot._CAL_CACHE.update(ts=0, texto=None)
    bot._CAL_CLIENTE = caducado
    try:
        ics = bot.calendario_ics()
        assert caducado.logins == 1, caducado.logins
        esperado = bot.CAL_DIAS_ATRAS + bot.CAL_DIAS
        assert ics.count("BEGIN:VEVENT") == esperado, ics.count("BEGIN:VEVENT")
    finally:
        bot._CAL_CLIENTE = None
        bot._CAL_CACHE.update(ts=0, texto=None)
        bot._CAL_DIAS_CACHE.clear()
    print("18 con la sesion caducada el .ics re-loguea en vez de servirse vacio")

    class SinRed(ClienteFalso):
        """Ni sesion confirmada ni re-login posible. URLError a proposito: no
        es SessionExpired ni RuntimeError, y antes se escapaba sin log."""

        def verificar_sesion(self):
            raise urllib.error.URLError("timed out")

        def login(self, verify=True):
            raise RuntimeError("Login rechazado (HTTP 503)")

    bot._CAL_CACHE.update(ts=0, texto=None)
    bot._CAL_CLIENTE = SinRed()
    salida = io.StringIO()
    try:
        with contextlib.redirect_stdout(salida):
            bot.calendario_ics()
        raise AssertionError("un re-login fallido no puede darse por bueno")
    except RuntimeError:
        pass
    finally:
        bot._CAL_CLIENTE = None
        bot._CAL_CACHE.update(ts=0, texto=None)
        bot._CAL_DIAS_CACHE.clear()
    logs = salida.getvalue()
    assert "URLError" in logs, logs
    assert "FALLO" in logs, logs
    print("19 un re-login fallido lo dice en los logs, no se va en silencio")

    servidor = bot.ThreadingHTTPServer(("127.0.0.1", 0), bot.HealthHandler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{servidor.server_address[1]}"
    try:
        assert urllib.request.urlopen(base + "/", timeout=5).status == 200
        for ruta in ("/cal/loquesea.ics", "/otra"):
            try:
                urllib.request.urlopen(base + ruta, timeout=5)
                raise AssertionError(f"{ruta} deberia dar 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404, (ruta, e.code)
        print("20 sin AIMHARDER_CAL_TOKEN el calendario no existe (404), la salud si")

        # El endpoint de salud no lleva token: no puede decir a que clase vas.
        reserva(ClienteFalso())
        cuerpo = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "Estoy vivo" in cuerpo, cuerpo
        for secreto in ("Crossfit", "18:10", "20260819"):
            assert secreto not in cuerpo, (secreto, cuerpo)

        bot.mark_fatal("RuntimeError")
        try:
            urllib.request.urlopen(base + "/", timeout=5)
            raise AssertionError("con un fatal marcado deberia dar 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503, e.code
            assert e.read().decode() == "MUERTO: RuntimeError"
        bot._HEARTBEAT["fatal"] = None
    finally:
        servidor.shutdown()
    print("21 el endpoint publico no publica la agenda ni el detalle del error")

    assert bot._targets_desde_entorno("") == []
    with contextlib.redirect_stdout(io.StringIO()):
        assert bot._targets_desde_entorno("{no json") == []
        assert bot._targets_desde_entorno('{"weekday": 0}') == []
    crudo = json.dumps([
        {"weekday": 1, "time": "07:00", "name_contains": "Metcon"},
        {"weekday": 9, "time": "07:00", "name_contains": "Fuera de rango"},
        {"weekday": 2, "time": "25:00", "name_contains": "Hora imposible"},
        {"weekday": 2, "time": "07:00", "name_contains": ""},
    ])
    with contextlib.redirect_stdout(io.StringIO()):
        salen = bot._targets_desde_entorno(crudo)
    assert salen == [{"weekday": 1, "time": "07:00", "name_contains": "Metcon"}], salen
    print("22 AIMHARDER_TARGETS: se descarta lo invalido sin tumbar el arranque")

    vacio = {"email": "", "password": "", "box_subdomain": "", "box_id": 0, "targets": []}
    assert bot.config_incompleta(vacio) == ["AIMHARDER_EMAIL", "AIMHARDER_PASSWORD",
                                            "AIMHARDER_BOX", "AIMHARDER_BOX_ID",
                                            "AIMHARDER_TARGETS"]
    assert bot.config_incompleta(CFG) == []
    assert bot.config_incompleta(dict(CFG, targets=[])) == ["AIMHARDER_TARGETS"]
    print("23 config_incompleta nombra las variables que faltan, sin valores")

    # --- pausas -----------------------------------------------------------
    def fecha(y, m, d):
        return bot.datetime(y, m, d).date()

    assert bot._pausas_desde_entorno("") == []
    with contextlib.redirect_stdout(io.StringIO()):
        assert bot._pausas_desde_entorno("{no json") == []
        assert bot._pausas_desde_entorno('"2026-09-05"') == []
        salen = bot._pausas_desde_entorno(json.dumps([
            "2026-09-05:2026-09-27",   # rango normal
            "2026-12-24",              # un dia suelto
            "2026-13-01",              # mes imposible
            "2026-09-27:2026-09-05",   # del reves
            "5 de septiembre",         # no es una fecha
        ]))
    assert salen == [(fecha(2026, 9, 5), fecha(2026, 9, 27)),
                     (fecha(2026, 12, 24), fecha(2026, 12, 24))], salen
    print("24 AIMHARDER_PAUSES: rangos y dias sueltos, lo invalido se descarta")

    vacaciones = [(fecha(2026, 9, 5), fecha(2026, 9, 27))]
    assert bot.en_pausa(fecha(2026, 9, 5), vacaciones), "el primer dia entra"
    assert bot.en_pausa(fecha(2026, 9, 27), vacaciones), "el ultimo dia entra"
    assert not bot.en_pausa(fecha(2026, 9, 4), vacaciones)
    assert not bot.en_pausa(fecha(2026, 9, 28), vacaciones)
    assert not bot.en_pausa(fecha(2026, 9, 10), [])
    print("25 en_pausa incluye los dos extremos del rango")

    # Jueves 13/08/2026. Del target de los martes, la primera ocurrencia con la
    # ventana aun cerrada es la clase del 25/08 (apertura el 18/08).
    ahora = bot.datetime(2026, 8, 13, 9, 0)
    martes = CFG["targets"][0]
    apertura, clase = bot.next_opening(martes, 7, ahora)
    assert (apertura, clase) == (bot.datetime(2026, 8, 18, 7, 0),
                                 bot.datetime(2026, 8, 25, 7, 0)), (apertura, clase)

    apertura, clase = bot.next_opening(martes, 7, ahora,
                                       [(fecha(2026, 8, 25), fecha(2026, 8, 25))])
    assert clase == bot.datetime(2026, 9, 1, 7, 0), clase
    print("26 una clase en pausa se salta a la ocurrencia de la semana siguiente")

    # Lo que decide es la fecha de la CLASE, no la de la apertura: la clase del
    # 01/09 se reserva el 25/08, en plena pausa, y aun asi hay que reservarla.
    apertura, clase = bot.next_opening(martes, 7, ahora,
                                       [(fecha(2026, 8, 20), fecha(2026, 8, 26))])
    assert clase == bot.datetime(2026, 9, 1, 7, 0), clase
    assert apertura == bot.datetime(2026, 8, 25, 7, 0), apertura
    assert bot.en_pausa(apertura.date(), [(fecha(2026, 8, 20), fecha(2026, 8, 26))])
    print("27 la vuelta se reserva aunque la apertura caiga dentro de la pausa")

    # Tres semanas fuera: ni el martes 25/08 ni los siguientes hasta el 08/09.
    tres_semanas = dict(CFG, pauses=[(fecha(2026, 8, 20), fecha(2026, 9, 9))])
    apertura, clase, objetivo = bot.proximo_objetivo(tres_semanas, ahora)
    assert clase.date() > fecha(2026, 9, 9), clase
    assert objetivo["weekday"] == 3 and clase == bot.datetime(2026, 9, 10, 20, 0), (objetivo, clase)
    assert bot.proximo_objetivo(CFG, ahora)[1] == bot.datetime(2026, 8, 20, 20, 0)
    print("28 con tres semanas en pausa el siguiente objetivo es el de la vuelta")

    eterna = dict(CFG, pauses=[(fecha(2026, 8, 13), fecha(2099, 1, 1))])
    assert bot.next_opening(martes, 7, ahora, eterna["pauses"]) is None
    assert bot.proximo_objetivo(eterna, ahora) is None
    print("29 una pausa mas larga que el horizonte no cuelga el scheduler")

    class ContadorDias(ClienteFalso):
        """Cuenta que dias se consultan al buscar reservas dentro de la pausa."""

        def __init__(self):
            super().__init__()
            self.consultas = []

        def get_schedule(self, day):
            self.consultas.append(day)
            return {"bookings": [
                {"id": 1, "className": "Crossfit", "time": "18:10 - 19:10", "bookState": 1},
                {"id": 2, "className": "Open Box", "time": "18:10 - 19:10"},
            ]}

    hoy = bot.datetime.now().date()
    manana = hoy + bot.timedelta(days=1)
    c = ContadorDias()
    salida = io.StringIO()
    with contextlib.redirect_stdout(salida):
        bot.avisa_reservas_en_pausa(c, dict(CFG, pauses=[(manana, manana)]))
    out = salida.getvalue()
    assert c.consultas == [manana.strftime("%Y%m%d")], c.consultas
    assert out.count("Ya tenias reservada") == 1 and "Crossfit" in out, out
    assert "Open Box" not in out, out

    c = ContadorDias()
    with contextlib.redirect_stdout(io.StringIO()):
        bot.avisa_reservas_en_pausa(c, CFG)
    assert c.consultas == [], "sin pausas no se consulta nada"
    print("30 avisa de lo ya reservado dentro de la pausa, sin salir de la ventana")

    print("\nTodo en verde.")


if __name__ == "__main__":
    main()
