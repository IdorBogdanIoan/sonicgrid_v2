"""
SonicGrid - server de agregare pentru noduri acustice.

Datele vin de la gateway_bridge.py (ESP-NOW -> gateway ESP32 -> UART -> Pi):
  - /api/ingest : heartbeat-uri si citiri de nivel, cate una per nod
  - /api/alert  : alerte confirmate de cel putin doua noduri

Serverul retine ultima valoare de la fiecare nod, salveaza istoricul in SQLite
si expune totul pentru dashboard-ul cu harta.
"""

import json
import math
import os
import sqlite3
import threading
import time
from collections import deque

from flask import Flask, Response, g, jsonify, render_template, request

from sonicgrid_web import doar_admin, init_web

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "sonicgrid.db")
NODES_PATH = os.path.join(BASE_DIR, "nodes.json")

# Un nod care nu a mai trimis nimic de atatea secunde e considerat offline.
# Nodurile trimit heartbeat la 60 s (5 s in modul de test), deci lasam loc
# pentru doua heartbeat-uri pierdute.
OFFLINE_AFTER = 150.0

# Cat timp ramane o alerta "activa" pe dashboard (nodurile ei evidentiate).
# Era 30 s: nodul pulsa rosu mult dupa ce sunetul se oprise. Alerta ramane
# oricum in lista "Ultimele alerte" si in istoric.
ALERT_ACTIVE = 5.0

# Domeniul dBFS pe care il mapam la 0-100%. -60 dBFS = liniste, 0 dBFS = saturare.
DB_FLOOR = -60.0
DB_CEIL = 0.0

# Numele claselor din modelul de pe STM32.
#
# Pe fir, clasele modelului (0..5) sunt deplasate cu 1, ca 0 sa ramana liber
# pentru "nedeterminat": asa trimit nodurile autonome, care nu au clasificator,
# si fara deplasare fiecare eveniment de la ele ar fi etichetat drept tipat.
# Deplasarea se face pe ESP32, in nod_stm.ino (CLS_OFFSET).
CLASS_NAMES = {
    0: "Nedeterminat",
    1: "Tipat",
    2: "Sticla sparta",
    3: "Claxon",
    4: "Alarma",
    5: "Grup de persoane",
    6: "Trafic",
}

# Numele celor sase clase in ORDINEA din proc[], adica ordinea de pe STM32.
# CLASS_NAMES de mai sus e deplasat cu 1 (0 = nedeterminat) fiindca se refera
# la clasa dominanta dintr-o alerta; proc[] nu e deplasat, vine direct din
# model. Trimitem lista la dashboard ca sa nu fie scrisa a treia oara in HTML.
PROC_NAMES = [CLASS_NAMES[k + 1] for k in range(6)]

# Aceleasi clase, dar scurte. Barele din dashboard au o coloana de etichete de
# vreo 70 de pixeli, iar "Grup de persoane" se taie la "Grup de pe...".
# Numele lungi rama n pentru alerte, unde conteaza sa fie explicit.
PROC_SHORT = ["Tipat", "Sticla", "Claxon", "Alarma", "Grup", "Trafic"]

# Peste atatea secunde o citire nu mai e "proaspata" si dashboard-ul o
# estompeaza. Nodurile trimit la 60 s, deci o citire de 70 s e normala, una de
# 200 s nu.
FRESH_FOR = 90.0

# --- Nivelul afisat dupa un eveniment ---
#
# Nodul doarme intre heartbeat-uri, deci dupa un eveniment ultimul nivel primit
# e cel al sunetului puternic, iar urmatorul vine abia peste un minut. Daca il
# afisam ca "nivel curent", vumetrul ramane blocat sus tot minutul ala.
#
# De aceea tinem separat doua lucruri pe fiecare nod:
#   - fondul: nivelul din ultimul heartbeat
#   - evenimentul: nivelul si momentul ultimului eveniment
# si afisam evenimentul ca un varf: sta EVENT_HOLD secunde sus, apoi coboara
# exponential spre fond, cu constanta EVENT_TAU. Dupa ~3 s e practic la fond.
# Cand soseste un heartbeat nou, el devine fondul si varful dispare.
EVENT_HOLD = 0.6
EVENT_TAU = 1.0

# Procentele pe clasa ale evenimentului raman pe ecran cat coboara varful,
# apoi revin la cele din ultimul heartbeat. Altfel "Tipat 92%" ramanea afisat
# ca stare curenta un minut intreg, ca si nivelul.
EVENT_PROC_S = EVENT_HOLD + 3 * EVENT_TAU

# Cat de departe in urma se uita "peak". Inainte era maximul de la pornirea
# nodului, deci dupa primul tipat ramanea acolo pana la restart.
PEAK_WINDOW = 10.0

# Bridge-ul nu spune (inca) daca o citire vine dintr-un heartbeat sau dintr-un
# eveniment. O recunoastem dupa alerta care o insoteste: acelasi nod, acelasi
# nivel, la cel mult atatea secunde distanta. Daca bridge-ul trimite campul
# "tip" ("eveniment" / "heartbeat"), il folosim pe el si ghicitul nu mai e
# necesar.
EVENT_MATCH_S = 3.0

# --- Flux live (SSE) ---
#
# Dashboard-ul primea datele cerand /api/state o data pe secunda, deci o alerta
# putea astepta pana la o secunda doar in browser. Pe /api/stream serverul
# trimite starea imediat ce intra o citire sau o alerta.
#
# Fiecare dashboard deschis tine ocupat un fir al lui waitress cat timp e
# conectat. Implicit waitress are 4 fire, iar firele trebuie sa ramana si
# pentru bridge, deci limitam numarul de fluxuri la 2. Dashboard-urile care nu
# mai incap trec singure pe cereri o data pe secunda, ca inainte.
# Un tab inchis elibereaza locul in cel mult ~10 s (doua ping-uri).
# Daca waitress ruleaza cu --threads=24 (https3.sh), poti ridica limita:
#   SONICGRID_SSE_MAX=12 in mediul serviciului.
SSE_MAX = int(os.environ.get("SONICGRID_SSE_MAX", "2"))
SSE_PING_S = 5.0          # comentariu gol din cand in cand: tine nginx treaz si
                          # descopera clientii plecati
SSE_MAX_AGE_S = 300.0     # dupa atat inchidem fluxul; browserul se reconecteaza
                          # singur, iar firele blocate de clienti morti se elibereaza
SSE_COALESCE_S = 0.05     # citirea si alerta aceluiasi eveniment vin la cateva
                          # ms una de alta: le trimitem intr-un singur mesaj

app = Flask(__name__)

# Login pe tot site-ul. Exceptiile sunt rutele pe care posteaza bridge-ul: ele
# ruleaza pe acelasi Pi si nu au sesiune, deci nu pot trece printr-un formular
# de login. Daca expui serverul in afara retelei locale, pune si o cheie:
# SONICGRID_NODE_KEY in mediu, si bridge-ul o trimite in X-Node-Key.
init_web(app, node_paths=("/api/ingest", "/api/alert"))

_lock = threading.Lock()
_latest = {}                  # node_id -> ultima citire
_alerts = deque(maxlen=200)   # ultimele alerte, cele mai noi la dreapta
_config = {}                  # continutul nodes.json
_pending_ev = {}              # node_id -> (db, ts): alerta venita inaintea citirii ei

# Versiunea starii: creste la fiecare citire, alerta sau reset. Fluxurile SSE
# asteapta pe _cond pana se schimba, apoi trimit starea noua.
_cond = threading.Condition()
_ver = 0
_sse_active = 0


def _anunta():
    """Trezeste fluxurile SSE. Se cheama DUPA ce s-a eliberat _lock."""
    global _ver
    with _cond:
        _ver += 1
        _cond.notify_all()


# --------------------------------------------------------------------------
# Configurare noduri
# --------------------------------------------------------------------------

def load_config():
    global _config
    with open(NODES_PATH, "r", encoding="utf-8") as f:
        _config = json.load(f)
    return _config


def node_config(node_id):
    for n in _config.get("nodes", []):
        if n["id"] == node_id:
            return n
    return None


# --------------------------------------------------------------------------
# Baza de date
# --------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init():
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT    NOT NULL,
            ts      REAL    NOT NULL,
            db      REAL    NOT NULL,
            level   REAL    NOT NULL,
            vf_vf   INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_node_ts ON readings (node_id, ts)")

    # Coloana adaugata ulterior: pe un Pi care are deja istoric, CREATE TABLE
    # de mai sus nu se mai executa, deci coloana trebuie adaugata separat.
    # Fara asta serverul porneste si cade la primul INSERT.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
    if "proc" not in cols:
        conn.execute("ALTER TABLE readings ADD COLUMN proc TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     REAL    NOT NULL,
            cls    INTEGER NOT NULL,
            nodes  TEXT    NOT NULL      -- JSON: [{node_id, conf, db, dt_ms}, ...]
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts (ts)")
    conn.commit()
    conn.close()


_db = None


def db():
    global _db
    if _db is None:
        _db = db_connect()
    return _db


def load_recent_alerts():
    """La pornire, reincarcam ultimele alerte ca sa nu dispara la restart."""
    rows = db().execute(
        "SELECT id, ts, cls, nodes FROM alerts ORDER BY ts DESC LIMIT ?",
        (_alerts.maxlen,),
    ).fetchall()
    for r in reversed(rows):
        _alerts.append(alert_dict(r[0], r[1], r[2], json.loads(r[3])))


# --------------------------------------------------------------------------
# Utilitare
# --------------------------------------------------------------------------

def db_to_level(db_value):
    """Converteste dBFS in procent 0-100."""
    pct = (db_value - DB_FLOOR) / (DB_CEIL - DB_FLOOR) * 100.0
    return max(0.0, min(100.0, pct))


def eveniment_activ(r):
    """True cat timp ultimul eveniment al nodului e mai nou decat fondul lui."""
    if r.get("ev_ts") is None:
        return False
    return r.get("base_ts") is None or r["ev_ts"] > r["base_ts"]


def db_afisat(r, now):
    """Nivelul de aratat acum: fondul, sau varful unui eveniment care coboara spre el.

    Aceeasi formula e si in index.html (dbTinta), unde ruleaza la fiecare cadru.
    Daca o schimbi aici, schimb-o si acolo.
    """
    base = r.get("base_db")
    if not eveniment_activ(r):
        return base if base is not None else r["db"]
    if base is None:
        base = DB_FLOOR                 # nu stim inca fondul: coboram spre liniste
    t = now - r["ev_ts"] - EVENT_HOLD
    if t <= 0:
        return r["ev_db"]
    return base + (r["ev_db"] - base) * math.exp(-t / EVENT_TAU)


def peak_recent(r, now, afisat):
    """Maximul din ultimele PEAK_WINDOW secunde, dar niciodata sub nivelul afisat."""
    vals = [v for t, v in r.get("peaks", ()) if now - t <= PEAK_WINDOW]
    return max(vals + [afisat])


def marcheaza_eveniment(r):
    """Transforma ultima citire a unui nod in eveniment, retroactiv.

    Folosit cand alerta ajunge DUPA citirea ei: citirea a fost luata drept
    heartbeat si a devenit fond, deci punem la loc fondul de dinainte.
    """
    r["ev_db"], r["ev_ts"], r["ev_proc"] = r["db"], r["ts"], r.get("proc")
    r["base_db"], r["base_ts"] = r.get("prev_base_db"), r.get("prev_base_ts")
    r["base_proc"] = r.get("prev_base_proc")
    r["tip"] = "eveniment"


def proc_afisat(r, now):
    """Procentele de aratat acum: ale evenimentului cat timp e proaspat, altfel ale fondului."""
    if eveniment_activ(r) and now - r["ev_ts"] <= EVENT_PROC_S:
        return r.get("ev_proc")
    return r.get("base_proc")


def alert_dict(alert_id, ts, cls, nodes):
    return {
        "id": alert_id,
        "ts": ts,
        "cls": cls,
        "cls_name": CLASS_NAMES.get(cls, f"Clasa {cls}"),
        "nodes": nodes,
        "node_ids": [n["node_id"] for n in nodes],
    }


def proc_list(raw):
    """Normalizeaza procentele pe clasa la exact 6 intregi din 0..100.

    Intoarce None cand nodul nu a trimis nimic, ca sa se vada diferenta intre
    "nodul nu stie" si "toate clasele sunt zero". Primul caz e un nod cu
    firmware vechi, al doilea e liniste adevarata.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    out = []
    for k in range(6):
        try:
            v = int(raw[k]) if k < len(raw) else 0
        except (TypeError, ValueError):
            v = 0
        out.append(max(0, min(100, v)))
    return out


def optional_int(data, key):
    try:
        return int(data[key])
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Rute
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        site=_config.get("site", {}),
        offline_after=OFFLINE_AFTER,
        alert_active=ALERT_ACTIVE,
        class_names=PROC_NAMES,
        class_short=PROC_SHORT,
    )


@app.route("/api/ingest", methods=["POST"])
def ingest():
    """
    Primeste o citire de la un nod.

    Corp asteptat (JSON):
        {"node_id": "n1", "db": -45.7, "batt_mv": 3250, "rssi": -67}
    batt_mv, rssi si vf_vf sunt optionale.

    Accepta si form-encoded, ca sa poti testa rapid din curl fara JSON.
    """
    data = request.get_json(silent=True) or request.form or {}

    node_id = str(data.get("node_id", "")).strip()
    if not node_id:
        return jsonify(ok=False, error="Lipseste node_id."), 400

    try:
        db_value = float(data["db"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="Lipseste db sau nu e numeric."), 400

    vf_vf = optional_int(data, "vf_vf") or 0

    now = time.time()
    level = db_to_level(db_value)

    reading = {
        "node_id": node_id,
        "ts": now,
        "db": round(db_value, 1),
        "level": round(level, 1),
        "vf_vf": vf_vf,
        "batt_mv": optional_int(data, "batt_mv"),
        "rssi": optional_int(data, "rssi"),
        "proc": proc_list(data.get("proc")),
    }

    # "eveniment" sau "heartbeat", daca bridge-ul il trimite. Altfel ghicim
    # dupa alerta care insoteste evenimentul (vezi EVENT_MATCH_S).
    tip = str(data.get("tip", "")).strip().lower()

    with _lock:
        prev = _latest.get(node_id) or {}

        # Starea nodului trece de la o citire la alta: fondul, ultimul
        # eveniment si citirile recente pentru peak.
        for k in ("base_db", "base_ts", "base_proc", "ev_db", "ev_ts", "ev_proc"):
            reading[k] = prev.get(k)
        peaks = prev.get("peaks") or deque()
        peaks.append((now, db_value))
        while peaks and now - peaks[0][0] > PEAK_WINDOW:
            peaks.popleft()
        reading["peaks"] = peaks

        # A venit deja alerta pentru citirea asta? (cele doua pleaca din
        # bridge pe fire separate, deci ordinea nu e garantata)
        pend = _pending_ev.pop(node_id, None)
        potrivit = (pend is not None and now - pend[1] <= EVENT_MATCH_S
                    and abs(pend[0] - db_value) < 0.05)

        if tip == "eveniment" or (tip != "heartbeat" and potrivit):
            reading["ev_db"], reading["ev_ts"] = db_value, now
            reading["ev_proc"] = reading["proc"]
            for k in ("prev_base_db", "prev_base_ts", "prev_base_proc"):
                reading[k] = prev.get(k)
            reading["tip"] = "eveniment"
        else:
            reading["prev_base_db"] = reading["base_db"]
            reading["prev_base_ts"] = reading["base_ts"]
            reading["prev_base_proc"] = reading["base_proc"]
            reading["base_db"], reading["base_ts"] = db_value, now
            reading["base_proc"] = reading["proc"]
            reading["tip"] = "nivel"

        # Peak pe ultimele PEAK_WINDOW secunde, nu de la pornirea nodului.
        reading["peak_db"] = round(max(v for _, v in peaks), 1)
        _latest[node_id] = reading

        db().execute(
            "INSERT INTO readings (node_id, ts, db, level, vf_vf, proc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, now, db_value, level, vf_vf,
             json.dumps(reading["proc"]) if reading["proc"] else None),
        )
        db().commit()

    _anunta()
    return jsonify(ok=True, level=reading["level"])


@app.route("/api/alert", methods=["POST"])
def alert():
    """
    Primeste o alerta confirmata de gateway_bridge.py.

    Corp asteptat (JSON):
        {"cls": 1,
         "nodes": [{"node_id": "n3", "conf": 92, "db": -21, "dt_ms": 0},
                   {"node_id": "n4", "conf": 88, "db": -27, "dt_ms": 160}],
         "ts": 1790000000.0}
    """
    data = request.get_json(silent=True) or {}

    try:
        cls = int(data["cls"])
        nodes = [
            {
                "node_id": str(n["node_id"]),
                "conf": int(n.get("conf", 0)),
                "db": float(n.get("db", 0)),
                "dt_ms": int(n.get("dt_ms", 0)),
                "proc": proc_list(n.get("proc")),
            }
            for n in data["nodes"]
        ]
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="Alerta invalida: trebuie cls si nodes."), 400

    if not nodes:
        return jsonify(ok=False, error="Alerta fara noduri."), 400

    # Ora serverului, nu a bridge-ului: e acelasi Pi, dar asa e sigur coerenta.
    now = time.time()

    with _lock:
        cur = db().execute(
            "INSERT INTO alerts (ts, cls, nodes) VALUES (?, ?, ?)",
            (now, cls, json.dumps(nodes)),
        )
        db().commit()
        a = alert_dict(cur.lastrowid, now, cls, nodes)
        _alerts.append(a)

        # Nivelul din alerta e al unui eveniment, nu fondul nodului. Daca
        # citirea lui a ajuns deja, o reclasificam; daca nu, o asteptam.
        for n in nodes:
            nid = n["node_id"]
            last = _latest.get(nid)
            if last and now - last["ts"] <= EVENT_MATCH_S and abs(last["db"] - n["db"]) < 0.05:
                if last.get("tip") != "eveniment":
                    marcheaza_eveniment(last)
            else:
                _pending_ev[nid] = (n["db"], now)

    _anunta()
    print(f"ALERTA #{a['id']}: {a['cls_name']} - {', '.join(a['node_ids'])}")
    return jsonify(ok=True, id=a["id"])


@app.route("/api/alerts")
def alerts():
    """Ultimele alerte, cele mai noi primele. ?limit=20"""
    try:
        limit = max(1, min(200, int(request.args.get("limit", 20))))
    except ValueError:
        limit = 20

    with _lock:
        out = list(_alerts)[-limit:][::-1]
    return jsonify(alerts=out, server_time=time.time())


@app.route("/api/state")
def state():
    """Starea curenta a tuturor nodurilor, pentru dashboard."""
    return jsonify(stare_curenta())


# Parametrii animatiei, trimisi la dashboard ca sa nu fie scrisi de doua ori.
ANIM = {"event_hold": EVENT_HOLD, "event_tau": EVENT_TAU,
        "event_proc_s": EVENT_PROC_S,
        "db_floor": DB_FLOOR, "db_ceil": DB_CEIL}


def stare_curenta():
    """Tot ce deseneaza dashboard-ul, ca dict. Folosit de /api/state si /api/stream."""
    now = time.time()
    out = []

    with _lock:
        known_ids = {n["id"] for n in _config.get("nodes", [])}
        seen_ids = set(_latest.keys())

        # Nodurile implicate intr-o alerta recenta
        active = [a for a in _alerts if now - a["ts"] <= ALERT_ACTIVE]
        alerting = {nid for a in active for nid in a["node_ids"]}

        for node_id in sorted(known_ids | seen_ids):
            cfg = node_config(node_id)
            last = _latest.get(node_id)

            entry = {
                "id": node_id,
                "name": cfg["name"] if cfg else f"Nod necunoscut ({node_id})",
                "lat": cfg["lat"] if cfg else _config["site"]["lat"],
                "lon": cfg["lon"] if cfg else _config["site"]["lon"],
                "configured": cfg is not None,
                "alert": node_id in alerting,
            }

            if last is None:
                entry.update(online=False, db=None, level=None, vf_vf=None,
                             peak_db=None, age=None, batt_mv=None, rssi=None,
                             proc=None, fresh=False)
            else:
                age = now - last["ts"]
                # db / level = ce se vede acum: fondul, sau varful unui
                # eveniment care coboara spre fond. Dashboard-ul refa calculul
                # la fiecare cadru din base_db, ev_db si ev_age.
                afisat = db_afisat(last, now)
                ev = eveniment_activ(last)
                entry.update(
                    online=age <= OFFLINE_AFTER,
                    db=round(afisat, 1),
                    level=round(db_to_level(afisat), 1),
                    raw_db=last["db"],
                    base_db=last.get("base_db"),
                    ev_db=last["ev_db"] if ev else None,
                    ev_ts=last["ev_ts"] if ev else None,
                    ev_age=round(now - last["ev_ts"], 3) if ev else None,
                    vf_vf=last["vf_vf"],
                    peak_db=round(peak_recent(last, now, afisat), 1),
                    age=round(age, 1),
                    batt_mv=last["batt_mv"],
                    rssi=last["rssi"],
                    proc=proc_afisat(last, now),
                    base_proc=last.get("base_proc"),
                    ev_proc=last.get("ev_proc") if ev else None,
                    # Nodurile dorm intre heartbeat-uri, deci o citire veche de
                    # un minut e normala, nu o defectiune. fresh spune
                    # dashboard-ului cand sa estompeze barele, ca sa nu pari
                    # ca te uiti la ceva live cand te uiti la ceva de acum
                    # doua minute.
                    fresh=age <= FRESH_FOR,
                )

            out.append(entry)

        latest_alert = _alerts[-1] if _alerts else None

    online = [n for n in out if n["online"]]

    # Varful pe clasa peste toate nodurile proaspete: asta deseneaza
    # dashboard-ul in bara de sus. Max si nu media, fiindca un tipat auzit de
    # un singur nod din patru e tot un tipat, iar media l-ar dilua la 25%.
    cu_proc = [n["proc"] for n in out if n.get("fresh") and n.get("proc")]
    proc_max = ([max(v[k] for v in cu_proc) for k in range(6)]
                if cu_proc else None)

    summary = {
        "total": len(out),
        "online": len(online),
        "avg_level": round(sum(n["level"] for n in online) / len(online), 1) if online else None,
        "max_level": round(max(n["level"] for n in online), 1) if online else None,
        "active_alerts": len(active),
        "latest_alert": latest_alert,
        "proc_max": proc_max,
    }

    return dict(nodes=out, summary=summary, server_time=now,
                class_names=PROC_NAMES, class_short=PROC_SHORT,
                fresh_for=FRESH_FOR, anim=ANIM)


@app.route("/api/stream")
def stream():
    """Flux SSE: trimite starea (si ultimele alerte) imediat ce se schimba ceva.

    Fiecare mesaj e un obiect ca cel de la /api/state, plus "alerts" (ultimele
    10, ca la /api/alerts?limit=10). Cand nu se intampla nimic, trimitem doar
    un comentariu la SSE_PING_S secunde.

    503 cand sunt deja SSE_MAX fluxuri deschise: dashboard-ul trece atunci pe
    /api/state o data pe secunda.
    """
    global _sse_active
    with _cond:
        if _sse_active >= SSE_MAX:
            return jsonify(ok=False, error="Prea multe fluxuri live deschise."), 503
        _sse_active += 1

    def mesaj():
        d = stare_curenta()
        with _lock:
            d["alerts"] = list(_alerts)[-10:][::-1]
        return "data: " + json.dumps(d, separators=(",", ":")) + "\n\n"

    def flux():
        # Prima linie spune browserului sa se reconecteze dupa 3 s daca pica.
        yield "retry: 3000\n\n"
        with _cond:
            vazut = _ver
        yield mesaj()
        sfarsit = time.time() + SSE_MAX_AGE_S
        while time.time() < sfarsit:
            with _cond:
                _cond.wait_for(lambda: _ver != vazut, timeout=SSE_PING_S)
                v = _ver
            if v == vazut:
                yield ": ping\n\n"
                continue
            time.sleep(SSE_COALESCE_S)
            with _cond:
                vazut = _ver
            yield mesaj()

    def elibereaza():
        global _sse_active
        with _cond:
            _sse_active -= 1

    r = Response(flux(), mimetype="text/event-stream",
                 headers={"Cache-Control": "no-cache",
                          # nginx: nu tine mesajele in buffer
                          "X-Accel-Buffering": "no"})
    # call_on_close ruleaza si cand clientul pleaca inainte de primul mesaj,
    # spre deosebire de un finally in generator, care atunci nu ruleaza deloc.
    r.call_on_close(elibereaza)
    return r


@app.route("/api/history")
def history():
    """Istoricul unui nod, pentru graficul din panoul lateral."""
    node_id = request.args.get("node", "")
    try:
        minutes = float(request.args.get("minutes", 5))
    except ValueError:
        minutes = 5.0

    since = time.time() - minutes * 60
    with _lock:
        rows = db().execute(
            "SELECT ts, db, level FROM readings "
            "WHERE node_id = ? AND ts >= ? ORDER BY ts ASC",
            (node_id, since),
        ).fetchall()

    return jsonify(points=[{"ts": r[0], "db": r[1], "level": r[2]} for r in rows])


# --------------------------------------------------------------------------
# Istoric: pagina operatorului
# --------------------------------------------------------------------------

# Cat de departe in trecut se poate cere, in zile. Baza creste cu ~1440 de
# randuri pe zi si pe nod; fara limita, o cerere de "tot" ar bloca Pi-ul.
ISTORIC_MAX_ZILE = 90

# Cate randuri scoatem cel mult intr-o pagina de API. Peste atat, interfata
# cere urmatoarea bucata. Exportul CSV nu trece pe aici, el scrie in flux.
PAGINA_MAX = 500


def _interval_cerut():
    """Citeste de/pana din query string si intoarce doua momente absolute.

    Acceptam fie 'ore' (ultimele N ore, comod pentru butoanele rapide), fie
    'de' si 'pana' ca timestamp unix. Lipsa amandurora inseamna ultimele 24 h.
    """
    acum = time.time()
    try:
        ore = float(request.args.get("ore", ""))
        if ore > 0:
            return max(acum - ore * 3600, acum - ISTORIC_MAX_ZILE * 86400), acum
    except ValueError:
        pass

    def ts(nume, implicit):
        try:
            return float(request.args.get(nume, ""))
        except ValueError:
            return implicit

    pana = ts("pana", acum)
    de = ts("de", pana - 24 * 3600)
    de = max(de, acum - ISTORIC_MAX_ZILE * 86400)
    if de > pana:
        de, pana = pana, de
    return de, pana


@app.route("/istoric")
def istoric_pagina():
    return render_template(
        "istoric.html",
        site=_config.get("site", {}),
        class_names=PROC_NAMES,
        class_short=PROC_SHORT,
        noduri=[n["id"] for n in _config.get("nodes", [])],
        istoric_max_zile=ISTORIC_MAX_ZILE,
    )


@app.route("/api/istoric/alerte")
def istoric_alerte():
    """Alertele din interval, cu filtru optional pe clasa si pe nod.

    Filtram nodul in Python, nu in SQL: nodes e o coloana JSON, iar un LIKE pe
    ea ar gasi si un nod al carui nume e prefixul altuia ('n1' in 'n12'). Cu
    cateva sute de randuri pe pagina, costul e neglijabil si rezultatul corect.
    """
    de, pana = _interval_cerut()
    try:
        limita = max(1, min(PAGINA_MAX, int(request.args.get("limita", 200))))
    except ValueError:
        limita = 200
    cls = request.args.get("cls")
    nod = request.args.get("nod")

    sql = "SELECT id, ts, cls, nodes FROM alerts WHERE ts >= ? AND ts <= ?"
    par = [de, pana]
    if cls not in (None, "", "toate"):
        try:
            sql += " AND cls = ?"
            par.append(int(cls))
        except ValueError:
            pass
    sql += " ORDER BY ts DESC LIMIT ?"
    par.append(limita * 4 if nod else limita)

    with _lock:
        randuri = db().execute(sql, par).fetchall()

    out = []
    for rid, ts_, c, noduri_json in randuri:
        noduri = json.loads(noduri_json)
        if nod and not any(n.get("node_id") == nod for n in noduri):
            continue
        out.append(alert_dict(rid, ts_, c, noduri))
        if len(out) >= limita:
            break

    return jsonify(alerte=out, de=de, pana=pana, server_time=time.time())


@app.route("/api/istoric/nivel")
def istoric_nivel():
    """Seria de nivel a unui nod, redusa la cel mult PAGINA_MAX puncte.

    Reducerea se face in SQL cu un pas calculat din numarul de randuri, nu
    citind tot si taind in Python: pe trei luni de istoric ar insemna sute de
    mii de randuri aduse in memorie ca sa afisezi cinci sute.
    """
    nod = request.args.get("nod", "")
    de, pana = _interval_cerut()

    with _lock:
        (n,) = db().execute(
            "SELECT COUNT(*) FROM readings WHERE node_id = ? AND ts BETWEEN ? AND ?",
            (nod, de, pana)).fetchone()
        pas = max(1, n // PAGINA_MAX)
        randuri = db().execute(
            "SELECT ts, db, level, proc FROM ("
            "  SELECT ts, db, level, proc, ROW_NUMBER() OVER (ORDER BY ts) AS rn"
            "  FROM readings WHERE node_id = ? AND ts BETWEEN ? AND ?"
            ") WHERE rn % ? = 0 ORDER BY ts",
            (nod, de, pana, pas)).fetchall()

    puncte = [{"ts": r[0], "db": r[1], "level": r[2],
               "proc": json.loads(r[3]) if r[3] else None} for r in randuri]
    return jsonify(nod=nod, puncte=puncte, total=n, pas=pas, de=de, pana=pana)


@app.route("/api/retea")
def retea():
    """Sanatatea fiecarui nod, calculata din ce a ajuns chiar la server.

    Pierderea de pachete o estimam din cate citiri au sosit in ultima ora fata
    de cate ar fi trebuit la un heartbeat pe minut. Bridge-ul isi numara si el
    pierderile din secventa, mai exact, dar nu le trimite mai departe; cifra de
    aici e a serverului si nu depinde de asta.
    """
    acum = time.time()
    de = acum - 3600
    asteptate = 60                      # un heartbeat pe minut

    with _lock:
        sosite = dict(db().execute(
            "SELECT node_id, COUNT(*) FROM readings WHERE ts >= ? GROUP BY node_id",
            (de,)).fetchall())
        cunoscute = {n["id"] for n in _config.get("nodes", [])}
        out = []
        for nid in sorted(cunoscute | set(_latest) | set(sosite)):
            cfg = node_config(nid)
            last = _latest.get(nid)
            k = sosite.get(nid, 0)
            out.append({
                "id": nid,
                "name": cfg["name"] if cfg else f"Nod necunoscut ({nid})",
                "configured": cfg is not None,
                "online": bool(last) and (acum - last["ts"]) <= OFFLINE_AFTER,
                "age": round(acum - last["ts"], 1) if last else None,
                "db": last["db"] if last else None,
                "rssi": last["rssi"] if last else None,
                "batt_mv": last["batt_mv"] if last else None,
                "citiri_ora": k,
                # Peste 100% inseamna ca nodul e in modul de test, cu heartbeat
                # la 5 s. Nu e o eroare, deci nu o ascundem.
                "livrare_pct": round(100.0 * k / asteptate, 1),
            })

    return jsonify(noduri=out, fereastra_s=3600, asteptate=asteptate,
                   server_time=acum)


@app.route("/api/export.csv")
def export_csv():
    """Istoricul filtrat, ca CSV, scris in flux.

    Generator, nu un sir construit in memorie: trei luni de citiri inseamna
    zeci de megaocteti, iar Pi-ul are un gigaoctet de RAM pe care il imparte cu
    Flask si cu bridge-ul.
    """
    ce = request.args.get("ce", "citiri")
    nod = request.args.get("nod")
    de, pana = _interval_cerut()

    if ce == "alerte":
        antet = ["ora", "ts", "clasa", "clasa_nume", "noduri", "incredere_max"]

        def randuri():
            cur = db().execute(
                "SELECT ts, cls, nodes FROM alerts WHERE ts BETWEEN ? AND ? "
                "ORDER BY ts", (de, pana))
            for ts_, c, noduri_json in cur:
                noduri = json.loads(noduri_json)
                if nod and not any(x.get("node_id") == nod for x in noduri):
                    continue
                yield [
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_)),
                    f"{ts_:.3f}", c, CLASS_NAMES.get(c, f"Clasa {c}"),
                    " ".join(x["node_id"] for x in noduri),
                    max((x.get("conf", 0) for x in noduri), default=0),
                ]
    else:
        antet = ["ora", "ts", "nod", "dbfs", "nivel_pct"] + \
                [f"p_{n.lower()}" for n in PROC_SHORT]

        def randuri():
            sql = ("SELECT ts, node_id, db, level, proc FROM readings "
                   "WHERE ts BETWEEN ? AND ?")
            par = [de, pana]
            if nod:
                sql += " AND node_id = ?"
                par.append(nod)
            sql += " ORDER BY ts"
            for ts_, nid, dbv, lvl, proc in db().execute(sql, par):
                p = json.loads(proc) if proc else [""] * len(PROC_SHORT)
                yield [
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_)),
                    f"{ts_:.3f}", nid, dbv, lvl, *p,
                ]

    def flux():
        yield ",".join(antet) + "\n"
        for r in randuri():
            yield ",".join("" if v is None else str(v) for v in r) + "\n"

    nume = f"sonicgrid_{ce}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(flux(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{nume}"'})


# --------------------------------------------------------------------------
# Administrare
# --------------------------------------------------------------------------

@app.route("/api/utilizatori")
@doar_admin
def utilizatori():
    """Cine are cont, ce rol si cand a intrat ultima oara.

    Nu intoarce niciodata hash-urile de parola: UserStore.list le scoate deja,
    dar daca cineva schimba asta mai tarziu, ar ajunge direct in browser.
    """
    store = app.extensions["sg_users"]
    lista = store.list()
    return jsonify(utilizatori=[
        {"nume": nume,
         "rol": store.get_role(nume),
         "ultim_login": info.get("last_login"),
         "termeni_acceptati": info.get("terms_accepted_at")}
        for nume, info in sorted(lista.items())
    ])


@app.route("/api/sistem")
@doar_admin
def sistem():
    """Marimea bazei, intervalul acoperit si cate randuri sunt. Pentru admin."""
    with _lock:
        (n_cit,) = db().execute("SELECT COUNT(*) FROM readings").fetchone()
        (n_al,) = db().execute("SELECT COUNT(*) FROM alerts").fetchone()
        interval = db().execute(
            "SELECT MIN(ts), MAX(ts) FROM readings").fetchone()
    try:
        octeti = os.path.getsize(DB_PATH)
    except OSError:
        octeti = None
    return jsonify(citiri=n_cit, alerte=n_al,
                   primul=interval[0], ultimul=interval[1],
                   baza_octeti=octeti, baza_cale=DB_PATH)


@app.route("/api/reset", methods=["POST"])
@doar_admin
def reset():
    """Sterge starea in memorie (noduri si alerte). Util intre doua rulari de demo.

    Doar admin: un operator care apasa din greseala ar sterge contextul de sub
    colegii lui, fara ca ei sa afle de ce.
    """
    with _lock:
        _latest.clear()
        _alerts.clear()
        _pending_ev.clear()
    _anunta()
    return jsonify(ok=True)


@app.route("/health")
def health():
    return jsonify(ok=True, nodes_seen=len(_latest), alerts=len(_alerts))


# --------------------------------------------------------------------------

load_config()
db_init()
load_recent_alerts()

if __name__ == "__main__":
    # host 0.0.0.0 = accesibil din reteaua locala, nu doar de pe Pi.
    app.run(host="0.0.0.0", port=5000, debug=False)
