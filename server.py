#!/usr/bin/env python3
"""
SonicGrid - server central (Raspberry Pi)
=========================================
Primeste date de la nodurile ESP32, ruleaza recunoasterea de cuvinte pe
fragmentele audio trimise, si impinge totul in dashboard prin SSE.

INSTALARE
    pip install flask
    # optional, pentru detectia cuvintelor:
    pip install vosk
    # model romanesc mic (~45 MB):
    #   wget https://alphacephei.com/vosk/models/vosk-model-small-ro-0.22.zip
    #   unzip vosk-model-small-ro-0.22.zip -d models/

RULARE
    python3 server.py
    # dashboard: http://<ip-raspberry>:8000

ENDPOINTS pentru ESP32
    POST /api/event   JSON: {"id":"nod01","level":72,"db":-18.5,"scream":true}
    POST /api/audio   raw WAV 16 kHz mono, header X-Node-Id: nod01
"""

import json
import queue
import threading
import time
import wave
import io
import os
from flask import Flask, request, jsonify, Response, send_file

app = Flask(__name__)

# Login + termeni + confidentialitate. ESP32 posteaza fara login pe rutele de mai jos.
from sonicgrid_web import init_web
init_web(app, node_paths=("/api/event", "/api/audio"))

# ---------------------------------------------------------------- CONFIGURARE
# Pozitiile fizice ale nodurilor. Completeaza cu coordonatele reale.
NODES = {
    "nod01": {"name": "Intrare principala", "lat": 47.7905, "lng": 22.8840},
    "nod02": {"name": "Scena",              "lat": 47.7898, "lng": 22.8862},
    "nod03": {"name": "Zona bar",           "lat": 47.7893, "lng": 22.8845},
    "nod04": {"name": "Iesire est",         "lat": 47.7901, "lng": 22.8871},
}

KEYWORDS = ["ajutor", "help", "salvati", "salvare", "socoare", "aiutor"]
VOSK_MODEL_PATH = "models/vosk-model-small-ro-0.22"
KEYWORD_COOLDOWN = 8      # secunde intre doua alerte de la acelasi nod

# ---------------------------------------------------------------- STARE
state = {nid: {**info, "id": nid, "level": 0, "db": -90,
               "scream": False, "keyword": None, "ts": 0}
         for nid, info in NODES.items()}

subscribers = []          # cozi SSE
lock = threading.Lock()
last_alert = {}           # nod -> timestamp

# ---------------------------------------------------------------- VOSK
recognizer = None
try:
    from vosk import Model, KaldiRecognizer
    if os.path.isdir(VOSK_MODEL_PATH):
        _model = Model(VOSK_MODEL_PATH)
        print(f"[vosk] model incarcat din {VOSK_MODEL_PATH}")
    else:
        _model = None
        print(f"[vosk] lipseste modelul din {VOSK_MODEL_PATH} - detectia de cuvinte e oprita")
except ImportError:
    _model = None
    print("[vosk] biblioteca vosk nu e instalata - detectia de cuvinte e oprita")


def detect_keyword(wav_bytes: bytes):
    """Returneaza cuvantul gasit, sau None. Asteapta WAV 16 kHz mono 16-bit."""
    if _model is None:
        return None
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                return None
            rec = KaldiRecognizer(_model, wf.getframerate(),
                                  json.dumps(KEYWORDS + ["[unk]"]))
            rec.SetWords(False)
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                rec.AcceptWaveform(data)
            text = json.loads(rec.FinalResult()).get("text", "").lower()
    except Exception as exc:
        print(f"[vosk] eroare: {exc}")
        return None

    for kw in KEYWORDS:
        if kw in text:
            return kw
    return None


# ---------------------------------------------------------------- RETENTIE
# Politica de confidentialitate promite stergerea fragmentelor dupa N zile.
from sonicgrid_web.legal import LEGAL
AUDIO_RETENTION_SEC = LEGAL["retention_audio_days"] * 86400


def _cleanup_alerts():
    while True:
        try:
            now = time.time()
            if os.path.isdir("alerte"):
                for f in os.listdir("alerte"):
                    p = os.path.join("alerte", f)
                    if os.path.isfile(p) and now - os.path.getmtime(p) > AUDIO_RETENTION_SEC:
                        os.remove(p)
                        print(f"[retentie] sters {p}")
        except Exception as exc:
            print(f"[retentie] eroare: {exc}")
        time.sleep(3600)


threading.Thread(target=_cleanup_alerts, daemon=True).start()


# ---------------------------------------------------------------- SSE
def broadcast(payload: dict):
    msg = f"data: {json.dumps(payload)}\n\n"
    with lock:
        dead = []
        for q in subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            subscribers.remove(q)


@app.route("/stream")
def stream():
    q = queue.Queue(maxsize=50)
    with lock:
        subscribers.append(q)

    def gen():
        snapshot = {"type": "snapshot", "nodes": list(state.values())}
        yield f"data: {json.dumps(snapshot)}\n\n"
        try:
            while True:
                try:
                    yield q.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with lock:
                if q in subscribers:
                    subscribers.remove(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- API NODURI
@app.route("/api/event", methods=["POST"])
def api_event():
    d = request.get_json(force=True, silent=True) or {}
    nid = d.get("id")
    if not nid:
        return jsonify(error="lipseste id"), 400

    if nid not in state:      # nod nou, necunoscut in configuratie
        state[nid] = {"id": nid, "name": nid, "lat": 0, "lng": 0,
                      "level": 0, "db": -90, "scream": False,
                      "keyword": None, "ts": 0}

    n = state[nid]
    n["level"] = int(d.get("level", n["level"]))
    n["db"] = round(float(d.get("db", n["db"])), 1)
    n["scream"] = bool(d.get("scream", False))
    n["keyword"] = None
    n["ts"] = time.time()

    broadcast({k: n[k] for k in
               ("id", "name", "lat", "lng", "level", "db", "scream", "keyword")})
    return jsonify(ok=True)


@app.route("/api/audio", methods=["POST"])
def api_audio():
    """ESP32 trimite un fragment WAV cand a detectat ceva suspect."""
    nid = request.headers.get("X-Node-Id", "necunoscut")
    wav_bytes = request.get_data()
    if len(wav_bytes) < 100:
        return jsonify(error="fisier prea mic"), 400

    kw = detect_keyword(wav_bytes)
    print(f"[audio] {nid}: {len(wav_bytes)} octeti -> {kw or 'niciun cuvant'}")

    if kw and time.time() - last_alert.get(nid, 0) > KEYWORD_COOLDOWN:
        last_alert[nid] = time.time()
        n = state.setdefault(nid, {"id": nid, "name": nid, "lat": 0, "lng": 0,
                                   "level": 0, "db": -90, "scream": False,
                                   "keyword": None, "ts": 0})
        n["keyword"] = kw
        n["ts"] = time.time()
        broadcast({k: n[k] for k in
                   ("id", "name", "lat", "lng", "level", "db", "scream", "keyword")})

        # arhivam fragmentul pentru analiza ulterioara
        os.makedirs("alerte", exist_ok=True)
        fname = f"alerte/{nid}_{int(time.time())}_{kw}.wav"
        with open(fname, "wb") as f:
            f.write(wav_bytes)

    return jsonify(ok=True, keyword=kw)


@app.route("/api/nodes")
def api_nodes():
    return jsonify(list(state.values()))


@app.route("/")
def index():
    return send_file("dashboard.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Dashboard: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
