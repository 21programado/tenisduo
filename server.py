import math
import os
import random
import socket
import sys
import threading
import webbrowser

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room


def recurso(ruta):
    """Ruta de un archivo (templates, static): funciona con `python server.py` y dentro del .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, ruta)


app = Flask(__name__, template_folder=recurso("templates"), static_folder=recurso("static"))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------- Ajustes (tocá acá para cambiar la sensación del juego) ----------
ANCHO, ALTO = 600, 400
PALETA_W, PALETA_H, RADIO, MARGEN = 12, 80, 8, 20
MODO = "dobles"                   # "dobles": uno al fondo y otro adelantado, ambos recorren toda la altura
                                  # "zonas": cada jugador defiende una mitad (arriba/abajo) de su lado
SEPARACION = 70                   # modo dobles: cuánto más adelante está la paleta de adelante (px)
ZONA_H = ALTO / 2                 # modo zonas: altura que defiende cada jugador
NECESARIOS = 4                    # jugadores para empezar (poné 2 para probar con dos celulares)
PUNTOS_PARA_GANAR = 10
VELOCIDADES = {"normal": 4.5, "rapido": 6.0}  # px por frame (a 60 fps)
ACELERACION = 1.06   # la bola acelera un 6 % en cada golpe de paleta
VEL_MAX = 2.4        # tope: velocidad base x 2.4
ANGULO_BORDE = math.radians(30)   # cuánto curva el golpe según dónde pega (centro = 0, punta = máx.)
SALIDA_MAX = math.radians(65)     # ángulo máximo de salida respecto de la horizontal
PAUSA_INICIO, PAUSA_GOL, PAUSA_REANUDAR = 5.0, 1.0, 3.0  # segundos de cuenta regresiva

# Roles: id -> (equipo, puesto). Equipo 1 = azul (izquierda), equipo 2 = rojo (derecha).
# Puesto 0 = fondo (o mitad de arriba en modo zonas), puesto 1 = adelante (o mitad de abajo).
ROLES = {1: (1, 0), 2: (1, 1), 3: (2, 0), 4: (2, 1)}
if MODO == "dobles":
    NOMBRES = {1: "Azul · fondo", 2: "Azul · adelante", 3: "Rojo · fondo", 4: "Rojo · adelante"}
else:
    NOMBRES = {1: "Azul · arriba", 2: "Azul · abajo", 3: "Rojo · arriba", 4: "Rojo · abajo"}
ORDEN = [1, 3, 2, 4]  # orden en que se asignan los celulares: así los equipos se llenan parejo

jugadores = {}  # {sid: rol}


def y_centro(r):
    """Posición (borde superior) de la paleta centrada en su zona (o en toda la cancha en dobles)."""
    if MODO == "dobles":
        return (ALTO - PALETA_H) / 2
    return ROLES[r][1] * ZONA_H + (ZONA_H - PALETA_H) / 2


def x_paleta(r):
    """Centro horizontal de la paleta. En dobles, la de adelante está SEPARACION px más cerca de la red."""
    equipo, puesto = ROLES[r]
    adelanto = puesto * SEPARACION if MODO == "dobles" else 0
    if equipo == 1:
        return MARGEN + PALETA_W / 2 + adelanto
    return ANCHO - MARGEN - PALETA_W / 2 - adelanto


estado = {
    "modo": MODO, "nombres": NOMBRES,
    "x1": x_paleta(1), "x2": x_paleta(2), "x3": x_paleta(3), "x4": x_paleta(4),
    "y1": y_centro(1), "y2": y_centro(2), "y3": y_centro(3), "y4": y_centro(4),
    "bola_x": ANCHO / 2, "bola_y": ALTO / 2, "vx": 0.0, "vy": 0.0,
    "puntos_p1": 0, "puntos_p2": 0,
    "jugando": False, "ganador": None,
    "pausa": 0.0, "racha": 0, "velocidad": "normal",
    "pausado": False, "completa": False, "listos": [], "conectados": [],
}


def ip_local():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


URL_MANDO = f"http://{ip_local()}:5000"


def emit_marcador(anota=0, a=None):
    socketio.emit("marcador", {
        "p1": estado["puntos_p1"], "p2": estado["puntos_p2"],
        "ganador": estado["ganador"], "jugando": estado["jugando"], "anota": anota,
        "completa": estado["completa"], "listos": estado["listos"], "pausado": estado["pausado"],
    }, to=a)


def sacar(direccion=None, pausa=PAUSA_GOL):
    base = VELOCIDADES[estado["velocidad"]]
    ang = math.radians(random.uniform(-30, 30))
    d = direccion or random.choice([-1, 1])
    estado.update(bola_x=ANCHO / 2, bola_y=ALTO / 2,
                  vx=d * base * math.cos(ang), vy=base * math.sin(ang),
                  pausa=pausa, racha=0)


def reiniciar_partida():
    estado.update(puntos_p1=0, puntos_p2=0, ganador=None, pausado=False, listos=[])
    for r in ROLES:
        estado[f"y{r}"] = y_centro(r)
    sacar(pausa=PAUSA_INICIO)
    estado["jugando"] = (len(jugadores) == NECESARIOS)


def preparar_sala():
    """Están todos conectados: la partida espera a que cada uno toque «Estoy listo»."""
    reiniciar_partida()
    estado["jugando"] = False


def punto(equipo):
    estado[f"puntos_p{equipo}"] += 1
    if estado[f"puntos_p{equipo}"] >= PUNTOS_PARA_GANAR:
        estado["jugando"] = False
        estado["ganador"] = equipo
        socketio.emit("sonido", {"tipo": "victoria"})
    else:
        socketio.emit("sonido", {"tipo": "gol"})
        sacar(direccion=-1 if equipo == 2 else 1)  # saca hacia el equipo que recibió el punto
    emit_marcador(anota=equipo)


def choque_paleta(r):
    """Paleta vertical con grosor. El ángulo de salida depende de cómo llegó la bola y de en qué
    parte de la paleta pegó: centro = devolución recta, punta = devolución cruzada."""
    e = estado
    equipo = ROLES[r][0]
    sentido = 1 if equipo == 1 else -1  # hacia dónde sale la bola
    hl = PALETA_H / 2
    cx = x_paleta(r)
    cy = e[f"y{r}"] + hl
    px, py = e["bola_x"] - cx, e["bola_y"] - cy
    d = px * sentido                 # distancia frente a la paleta
    fuera = max(0.0, abs(py) - hl)   # cuánto se pasa de la punta
    if d < 0 or math.hypot(fuera, d) > RADIO + PALETA_W / 2:
        return
    if e["vx"] * sentido >= 0:       # la bola ya se está alejando
        return
    b = max(-1.0, min(1.0, py / hl)) * ANGULO_BORDE  # pegar arriba/abajo curva la normal
    mx, my = sentido * math.cos(b), math.sin(b)
    vn = e["vx"] * mx + e["vy"] * my
    rx, ry = e["vx"] - 2 * vn * mx, e["vy"] - 2 * vn * my  # reflexión tipo espejo
    ang = math.atan2(ry, sentido * rx)
    if abs(ang) > SALIDA_MAX:  # nunca sale casi vertical ni hacia atrás
        ang = math.copysign(SALIDA_MAX, ang)
    v = min(math.hypot(e["vx"], e["vy"]) * ACELERACION, VELOCIDADES[e["velocidad"]] * VEL_MAX)
    e["vx"], e["vy"] = sentido * v * math.cos(ang), v * math.sin(ang)
    e["bola_x"] += sentido * (RADIO + PALETA_W / 2 - d)  # saca la bola de la paleta
    e["racha"] += 1
    socketio.emit("sonido", {"tipo": "paleta", "racha": e["racha"], "lado": r})


def paso(dt):
    e = estado
    if e["pausa"] > 0:
        antes = math.ceil(e["pausa"])
        e["pausa"] = max(0.0, e["pausa"] - dt / 60)
        if e["pausa"] == 0:
            socketio.emit("sonido", {"tipo": "saque"})
        elif math.ceil(e["pausa"]) != antes:
            socketio.emit("sonido", {"tipo": "cuenta"})
        return

    pasos = max(1, math.ceil(max(abs(e["vx"]), abs(e["vy"])) * dt / 6))  # subpasos de ≤ 6 px: no atraviesa la paleta
    for _ in range(pasos):
        e["bola_x"] += e["vx"] * dt / pasos
        e["bola_y"] += e["vy"] * dt / pasos
        if e["bola_y"] < RADIO:
            e["bola_y"], e["vy"] = RADIO, abs(e["vy"])
            socketio.emit("sonido", {"tipo": "pared"})
        elif e["bola_y"] > ALTO - RADIO:
            e["bola_y"], e["vy"] = ALTO - RADIO, -abs(e["vy"])
            socketio.emit("sonido", {"tipo": "pared"})
        for r in ROLES:
            choque_paleta(r)
        if e["bola_x"] < -RADIO:
            return punto(2)
        if e["bola_x"] > ANCHO + RADIO:
            return punto(1)


def bucle_juego():
    import time
    previo = time.perf_counter()
    while True:
        ahora = time.perf_counter()
        dt = min((ahora - previo) * 60, 3)  # en "frames" de 1/60 s; compensa los tirones
        previo = ahora
        if estado["jugando"] and not estado["pausado"]:
            paso(dt)
        socketio.emit("actualizar_tv", estado, to="tv")  # solo a la TV, no a los celulares
        socketio.sleep(1 / 60)


@app.route("/")
def mando():
    return render_template("mando.html")


@app.route("/tv")
def tv():
    return render_template("tv.html")


@socketio.on("connect")
def conectar():
    if request.args.get("tipo") == "tv":
        join_room("tv")
        emit("actualizar_tv", estado)
        emit("info", {"url": URL_MANDO})
        emit_marcador(a=request.sid)
        return

    rol = next((r for r in ORDEN if r not in jugadores.values()), 0)
    if rol:
        jugadores[request.sid] = rol
    emit("rol", {"jugador": rol, "equipo": ROLES[rol][0] if rol else 0, "nombre": NOMBRES.get(rol, "")})
    emit_marcador(a=request.sid)
    if not rol:
        return  # un quinto celular no molesta la partida en curso

    estado["conectados"] = sorted(jugadores.values())
    if len(jugadores) == NECESARIOS:
        preparar_sala()
        estado["completa"] = True
        socketio.emit("estado_sala", {"msg": "Están todos: ¡cada uno toca «Estoy listo»!"})
        emit_marcador()
    else:
        socketio.emit("estado_sala", {"msg": f"Esperando jugadores ({len(jugadores)}/{NECESARIOS})"})


@socketio.on("disconnect")
def desconectar():
    if request.sid in jugadores:
        del jugadores[request.sid]
        estado.update(jugando=False, completa=False, pausado=False, listos=[],
                      conectados=sorted(jugadores.values()))
        socketio.emit("estado_sala", {"msg": f"Se fue un jugador. Esperando ({len(jugadores)}/{NECESARIOS})"})
        emit_marcador()


@socketio.on("mover")
def mover(data):
    r = jugadores.get(request.sid)
    try:
        p = max(0.0, min(1.0, float(data["porcentaje"])))
    except (KeyError, TypeError, ValueError):
        return
    if r in ROLES:
        if MODO == "dobles":
            estado[f"y{r}"] = p * (ALTO - PALETA_H)
        else:
            estado[f"y{r}"] = ROLES[r][1] * ZONA_H + p * (ZONA_H - PALETA_H)


@socketio.on("revancha")
def revancha():
    if len(jugadores) == NECESARIOS and estado["ganador"]:  # solo cuando terminó la partida
        reiniciar_partida()
        socketio.emit("estado_sala", {"msg": "¡Revancha!"})
        emit_marcador()


@socketio.on("listo")
def listo():
    rol = jugadores.get(request.sid)
    if not rol or estado["jugando"] or len(jugadores) != NECESARIOS:
        return
    if rol not in estado["listos"]:
        estado["listos"].append(rol)
    if len(estado["listos"]) == NECESARIOS:  # empieza recién cuando todos están listos
        reiniciar_partida()
        socketio.emit("estado_sala", {"msg": f"¡A jugar! Primero a {PUNTOS_PARA_GANAR}"})
    else:
        socketio.emit("estado_sala", {"msg": f"Listos {len(estado['listos'])}/{NECESARIOS}. Esperando al resto…"})
    emit_marcador()


@socketio.on("pausar")
def pausar():
    if not estado["jugando"] or estado["ganador"]:
        return
    estado["pausado"] = not estado["pausado"]
    if not estado["pausado"]:
        estado["pausa"] = max(estado["pausa"], PAUSA_REANUDAR)  # cuenta regresiva para retomar
    socketio.emit("pausa", {"pausado": estado["pausado"]})


@socketio.on("velocidad")
def velocidad(data):
    nivel = (data or {}).get("nivel")
    if nivel in VELOCIDADES:
        estado["velocidad"] = nivel  # se aplica desde el próximo saque


if __name__ == "__main__":
    try:
        print(f"\n  TV:      {URL_MANDO}/tv\n  Celular: {URL_MANDO}\n")
        print("  Para cerrar el juego, cerrá esta ventana.\n")
        socketio.start_background_task(bucle_juego)
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000/tv")).start()
        try:
            socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
        except TypeError:  # versiones viejas de Flask-SocketIO no conocen ese parámetro
            socketio.run(app, host="0.0.0.0", port=5000)
    except Exception as err:  # que la ventana no desaparezca sin explicar qué pasó
        print(f"\n  No se pudo iniciar el servidor: {err}")
        print("  (¿Ya hay otra copia del juego abierta usando el puerto 5000?)")
        input("\n  Presioná Enter para cerrar...")
