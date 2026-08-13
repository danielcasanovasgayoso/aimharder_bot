# aimharder_bot

Bot que reserva automáticamente clases en [AimHarder](https://aimharder.com)
en cuanto se abre la ventana de reserva, desplegado 24/7 en [Fly.io](https://fly.io).

Sin dependencias externas: solo librería estándar de Python.

## Cómo funciona

AimHarder abre la reserva de cada clase exactamente **7 días antes**, a la misma
hora. El bot calcula ese instante para cada clase objetivo, duerme hasta él, y
en cuanto llega hace polling cada 0,5 s durante 30 s hasta que la clase aparece
en el horario, momento en el que la reserva.

Un proceso, dos hilos:

| Hilo | Función |
|---|---|
| Principal | Bucle de reservas (`scheduler_loop`) |
| Secundario | Servidor HTTP de health check en `$PORT` |

El hilo principal marca un **latido** cada 30 s. El servidor HTTP lo consulta,
de modo que el health check refleja si el scheduler está realmente vivo y no
solo si el socket responde. Ver [Health check](#health-check).

## Configuración

**En el repo no hay nada tuyo.** Ni credenciales, ni el box, ni tus horarios:
todo entra por variables de entorno, que en Fly son secretos. Ver
[Privacidad](#privacidad).

| Variable | Obligatoria | Qué es |
|---|---|---|
| `AIMHARDER_EMAIL` | sí | Usuario de AimHarder |
| `AIMHARDER_PASSWORD` | sí | Contraseña |
| `AIMHARDER_BOX` | sí | Subdominio del box, el de `<box>.aimharder.com` |
| `AIMHARDER_BOX_ID` | sí | Id numérico del box (parámetro `box` de la API) |
| `AIMHARDER_TARGETS` | sí | Clases objetivo, en JSON |
| `AIMHARDER_CAL_TOKEN` | no | Sin ella el calendario no existe. Ver [Calendario](#calendario-en-el-iphone) |
| `AIMHARDER_FINGERPRINT` | no | Por defecto se deriva del email |

`AIMHARDER_TARGETS` es una lista JSON:

```json
[
  {"weekday": 1, "time": "07:00", "name_contains": "Metcon"},
  {"weekday": 3, "time": "20:00", "name_contains": "Crossfit"}
]
```

`weekday` sigue la convención de Python: 0 = lunes … 6 = domingo. `time` debe
coincidir con el inicio de la clase tal y como lo devuelve la API, y
`name_contains` se compara en minúsculas contra `className`.

Un target con formato inválido se descarta con un aviso en los logs y los demás
siguen funcionando: es mejor reservar dos clases de tres que ninguna. Si falta
alguna variable obligatoria, el scheduler no arranca y el health check sirve
`503` diciendo **qué variables** faltan (los nombres, nunca los valores).

Si no sabes el `box_id`, sale de la propia web: abre el horario de tu box con las
herramientas de desarrollador del navegador y mira el parámetro `box=` de la
llamada a `/api/bookings`.

## Despliegue

Requisitos: [flyctl](https://fly.io/docs/flyctl/install/) y una cuenta de Fly.

```sh
flyctl auth login

# La contraseña se pide por stdin: no queda en el historial del shell.
read -rs "PW?AimHarder password: " && flyctl secrets set AIMHARDER_PASSWORD="$PW" && unset PW

# El resto de la configuración, también como secretos: el box y los horarios
# dicen dónde estás cada semana, y `flyctl secrets` no los muestra luego.
flyctl secrets set \
  AIMHARDER_EMAIL='tu@email.com' \
  AIMHARDER_BOX='tubox' \
  AIMHARDER_BOX_ID='12345' \
  AIMHARDER_TARGETS='[{"weekday": 1, "time": "07:00", "name_contains": "Metcon"}]'

flyctl deploy --remote-only --ha=false
```

Las comillas simples alrededor del JSON son necesarias: sin ellas el shell se
come las dobles y `AIMHARDER_TARGETS` llega como algo que no es JSON.

`--ha=false` es **obligatorio**. Fly despliega por defecto 2 máquinas para apps
con `http_service`; dos schedulers dispararían reservas duplicadas contra
AimHarder en el mismo instante.

### Detalles de `fly.toml`

| Opción | Valor | Motivo |
|---|---|---|
| `primary_region` | `cdg` | Fly **deprecó Madrid (`mad`)**. París es la región disponible más cercana a España. |
| `auto_stop_machines` | `off` | El bot duerme días entre aperturas sin recibir tráfico. Con el valor por defecto (`stop`) Fly pararía la máquina y no despertaría a reservar. |
| `auto_start_machines` | `false` | Complementa lo anterior: la máquina no depende del tráfico para estar viva. |
| `TZ` | `Europe/Madrid` | Los contenedores corren en UTC. Sin esto, `datetime.now()` calcularía las aperturas con 1–2 h de desfase y las fallaría todas. |

`flyctl platform regions` ya no lista `mad`: no es que `flyctl launch` ignorara
el fichero, es que Madrid ya no existe como región. `cdg` es la elección
deliberada, no un descarte. La sesión de AimHarder no va ligada a la IP, así que
correr desde París no cambia nada de cara al box.

No hace falta UptimeRobot ni ningún pinger externo: a diferencia de Render, Fly
no duerme la máquina por inactividad mientras `auto_stop_machines` esté en `off`.

## Health check

`GET /` devuelve el estado real del scheduler:

| Situación | Respuesta |
|---|---|
| Scheduler latiendo con normalidad | `200 Estoy vivo \| <fase> \| ultimo latido hace Ns` |
| Sin latido durante más de 120 s | `503 SIN LATIDO: ...` |
| Excepción no controlada (403, red, JSON inválido) | `503 MUERTO: <tipo de excepción>` |
| Falta configuración | `503 MUERTO: Faltan variables de entorno: ...` |

**Este endpoint es público y no lleva token**, así que sólo dice cosas que no
importa que lea cualquiera. `<fase>` es una palabra genérica — `esperando a la
proxima apertura`, `refrescando login`, `reservando` — y nunca la clase, la hora
ni el día: eso publicaría tu agenda a quien pase por la URL. Del mismo modo, de
un error sale el tipo (`HTTPError`) y no su texto, que suele llevar la URL del
box y a veces el usuario. El detalle completo va a `flyctl logs`, que sí es
privado.

Ante una excepción no controlada el proceso espera 60 s y sale con código
distinto de 0, para que Fly reinicie la máquina y reintente el login. La espera
evita que una credencial incorrecta se convierta en un bucle de reintentos
contra el endpoint de login de AimHarder.

Si falta configuración el proceso **no** sale: reiniciar no arreglaría un
secreto ausente, así que se queda sirviendo 503 con la lista de variables que
faltan.

## Operación

```sh
flyctl logs                  # logs en vivo
flyctl status                # estado de la máquina y del health check
flyctl secrets set AIMHARDER_PASSWORD=...   # rotar contraseña (redespliega)
curl https://<tu-app>.fly.dev/              # estado del scheduler
```

Al arrancar, el bot ejecuta un diagnóstico de una sola lectura que confirma que
el login funcionó y vuelca el horario del próximo día objetivo:

```
[DIAG] login() verificado contra la API.
[DIAG] Hora local del contenedor: 2026-08-12 13:06:45 (TZ=Europe/Madrid)
[DIAG] Horario de 20260819: 13 clases devueltas.
[DIAG]   07:00 - 08:00  Metcon
[DIAG] OK: 'Metcon' a las 07:00 encontrada (id=646157).
Proxima apertura: {'weekday': 1, ...} -> 2026-08-12 07:00:00
```

Esto sale por `flyctl logs`, que es privado, no por el endpoint HTTP.

> El visor de logs de Fly marca cada línea en **UTC**, mientras que las fechas
> que imprime el bot van en `Europe/Madrid`. En verano son 2 h de diferencia:
> una apertura de las 07:00 aparece a las `05:00` en la columna de la izquierda.
> No es un desfase del scheduler.

Si aparece `[DIAG] AVISO: ninguna clase casa con ...`, la hora o el nombre en
`AIMHARDER_TARGETS` no coinciden con el horario real del box.

## Sesión

El login **no** es el formulario de `login.aimharder.com`. Ese endpoint acepta
el POST y responde `200` con la home comercial de AimHarder, sin autenticar. El
front (React) hace:

1. `POST https://aimharder.com/api/login` con JSON
   `{username, password, fingerprint, iniframe}`. Responde
   `data.auth.authOK` + `refreshToken`, y deja la cookie **`amhrdrauth` sobre
   `.aimharder.com`** — con punto, así que vale también para
   `<box>.aimharder.com`.
2. `GET https://aimharder.com/setrefresh?redirect=…&token=…&fingerprint=…`,
   que traslada la sesión al subdominio de destino.

El `fingerprint` son 50 caracteres hex que el front guarda en `localStorage`.
Aquí se derivan del email (`huella_estable`), para que sea el mismo en cada
reinicio y el bot no parezca un dispositivo nuevo en cada despliegue.

Que el algoritmo sea público (`sha256(email)[:50]`) no lo hace adivinable
mientras el email no lo sea, y por eso el email no está en el repo. Si aun así
prefieres que no dependa de él, fíjalo a mano con `AIMHARDER_FINGERPRINT`:

```sh
flyctl secrets set AIMHARDER_FINGERPRINT=$(python3 -c "import secrets; print(secrets.token_hex(25))")
```

### Cómo se verifica

`GET /api/whoami` devuelve `{"data": [{id, name, centre, …}]}` con sesión, y no
lo hace sin ella. **`/api/bookings` no sirve para verificar**: es público y
devuelve exactamente lo mismo autenticado o no, que es justo por qué el
diagnóstico de arranque salía en verde con la sesión rota.

### Renovación

| Momento | Qué hace |
|---|---|
| Al arrancar | `login()` verificado contra `whoami`. Una credencial mala falla en el arranque, no el día de la reserva. |
| 2 min antes de cada apertura | Se rehace el login, así que la sesión que entra en la ventana es siempre nueva. Si falla, se avisa y se intenta reservar igual. |
| Durante la ventana | Un `{'logout': 1}` al leer el horario o al reservar dispara re-login y reintento (máximo 2 por reserva, para no golpear el endpoint de login en bucle). |

Un `[FALLO] ... Reserva rechazada:` **sin** `logout` es un rechazo real del box
(plazas agotadas, clase cerrada, sin bono): reintentar no lo arregla.

## Modo manual

Fuera del scheduler, para clases que no están en `AIMHARDER_TARGETS` o para
probar el flujo sin esperar a una apertura. No necesita `AIMHARDER_TARGETS`,
sólo las credenciales y el box. En Fly, con los secretos ya en la máquina:

```sh
flyctl ssh console -C "python /app/aimharder_bot_render.py --dia 20260818"
```

| Comando | Qué hace |
|---|---|
| `--dia 20260818` | Lista el horario de ese día con id, ocupación y si ya la tienes reservada. Solo lectura. |
| `--dia 20260818 --reservar 1223249` | Reserva esa clase. Comprueba antes que el id está en el horario del día. |
| `--dia 20260818 --diag-sesion` | Usuario, cookies y sobre qué dominio está `amhrdrauth`. Solo lectura. |

## Calendario en el iPhone

El bot sirve un `.ics` con tus clases reservadas en su propio servidor HTTP, y
el iPhone se suscribe de forma nativa. Sin apps, sin credenciales de Apple.

Se genera **consultando la API en cada refresco**, no de lo que el bot recuerde
haber reservado. Por eso incluye también las reservas que hagas desde el móvil,
las cancelaciones desaparecen solas, y no hay estado que se pierda al desplegar.

### Activarlo

El calendario está desactivado mientras no haya token: la ruta devuelve 404.
Una suscripción tiene que ser accesible sin autenticación para que Apple la lea,
así que el secreto va en la propia URL.

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

```sh
flyctl secrets set AIMHARDER_CAL_TOKEN=<el-valor-generado>
```

En el iPhone: **Ajustes → Calendario → Cuentas → Añadir cuenta → Otra → Añadir
calendario suscrito**, y pegar:

```
https://<tu-app>.fly.dev/cal/<token>.ics
```

### Detalles

| | |
|---|---|
| Cobertura | 90 días de historial (`CAL_DIAS_ATRAS`) y 8 vista (`CAL_DIAS`), que cubre toda la ventana de reserva |
| Caché | 15 min para el `.ics` (`CAL_TTL_SECONDS`); los días pasados se guardan aparte y no se vuelven a consultar |
| Arranque | Al arrancar se precalienta el historial en segundo plano, para que la primera petición del iPhone no tenga que hacer ~100 consultas seguidas |
| Horas | En UTC dentro del `.ics`; el iPhone las muestra en tu zona |
| Sesión | Cliente propio, separado del scheduler, para no tocar su sesión desde el hilo HTTP |

El historial se congela: un día ya vivido no puede cambiar, así que se consulta
una vez y se guarda. Es solo una optimización — la API devuelve el `bookState`
de días pasados, así que un despliegue no pierde el historial, solo lo vuelve a
pedir. Las clases de más de 90 días desaparecen del calendario.

**iOS refresca los calendarios suscritos cuando quiere** (Ajustes → Calendario →
Cuentas → Obtener datos), como mínimo cada 15 min y a veces más lento. Una
reserva recién hecha no aparece al instante: puede tardar de minutos a una hora.
Para verla ya, tirar hacia abajo en la app Calendario.

## Privacidad

Este repo es público, y el riesgo de un bot de reservas no es que alguien te
robe el código: es que una configuración de gimnasio dice **dónde estás, a qué
hora y qué días**. Eso es un patrón semanal de una persona concreta, y por
complemento también dice cuándo no estás en casa. De ahí estas reglas.

**Fuera del repo, en variables de entorno:**

| | Por qué |
|---|---|
| Email | Es media credencial de AimHarder; con el box también da un phishing dirigido creíble |
| Contraseña | Evidente |
| Box y `box_id` | Es tu ubicación física |
| Horarios (`AIMHARDER_TARGETS`) | Es tu rutina semanal, a la hora exacta |

**Fuera de las respuestas HTTP públicas:** el health check sirve una fase
genérica y el tipo de una excepción, nunca la clase, la fecha ni el texto del
error. Ver [Health check](#health-check).

**Público a propósito:** el hostname `<app>.fly.dev` (está en `fly.toml` porque
`flyctl` lo necesita). No pasa nada: `/` ya no cuenta nada y `/cal` no existe sin
el token.

**El calendario es la excepción.** Un `.ics` suscrito tiene que ser legible sin
autenticación para que iOS lo lea, así que la URL *es* la credencial, y el
contenido incluye la dirección del box y tus clases. Trátala como una
contraseña: no la pegues en un issue, en una captura ni en un chat. Para
revocarla basta con `flyctl secrets set AIMHARDER_CAL_TOKEN=<otro valor>`, que
deja la anterior en 404 al instante.

**Los tests no usan la configuración real.** `test_bot.py` trae sus propios
valores inventados en vez de leer `CONFIG`, para que el horario de verdad no
entre por la puerta de atrás.

## Limitaciones conocidas

- El diagnóstico de arranque solo valida **la próxima** clase objetivo, no las
  demás. Las otras se verifican cuando les llega el turno.
- Si la ventana de reserva de una clase ya se abrió cuando arranca el bot, esa
  clase se salta y se pasa a la ocurrencia de la semana siguiente. Hay que
  reservarla a mano.
- Al arrancar, el precalentado del calendario lanza ~100 consultas a AimHarder
  durante unos 40 s. Evita desplegar en los minutos previos a una apertura: el
  bot reservaría igual, pero estaría compitiendo consigo mismo por la API.
- Si el scheduler se **cuelga** en lugar de morir, el endpoint pasa a 503 pero
  nada reinicia la máquina automáticamente. Para recibir un aviso en ese caso,
  apuntar un monitor externo a la URL pública.
