#!/usr/bin/env python3
"""
SonicGrid - puntea gateway -> Flask, ruleaza pe Raspberry Pi.

Citeste JSON-urile de la ESP32-gateway (USB serial), arunca duplicatele,
grupeaza evenimentele care vin de la noduri diferite in aceeasi fereastra de timp
si trimite la Flask:
  - heartbeat-urile -> /api/ingest   (formatul de acum)
  - alertele confirmate (>= MIN_NODES noduri) -> /api/alert  (endpoint nou)

pip install pyserial requests
"""

import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import threading

import serial
import requests

PORT      = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyAMA0"   # USB: /dev/ttyUSB0
FLASK     = "http://127.0.0.1:5000"
WINDOW_MS = 800                     # cat de departe in timp pot fi doua detectii ale aceluiasi sunet
MIN_NODES = 1                       # in exterior nodurile sunt la zeci de metri: un nod
                                    # alerteaza singur, al doilea ridica increderea.
                                    # (masurat: un perete atenueaza 26-30 dB, deci in
                                    # interior confirmarea intre camere nu vine niciodata)
RAPORT_S  = 60                      # la cat timp se afiseaza statistica de pierderi
KEEP_MS   = 10_000                  # cat tinem clusterele in memorie

PKT_HEARTBEAT, PKT_EVENT = 1, 2

last_seq = {}                       # node -> ultimul seq vazut (retry-urile vin dublate)
clusters = []                       # [{t, cls, nodes: {node: pkt}, alerted}]

# Log CSV: sursa cifrelor din raport (alarme false/ora, diferenta de nivel
# intre noduri, latenta). Dashboard-ul arata, CSV-ul dovedeste.
LOG = Path(__file__).with_name(f"log_{datetime.now():%Y%m%d_%H%M%S}.csv")
_f = open(LOG, "w", newline="", encoding="utf-8")
_w = csv.writer(_f)
_w.writerow(["ora", "tip", "nod", "seq", "t_eveniment_ms", "age_ms", "dbfs",
             "conf", "rssi", "batt_mv", "noduri_in_cluster"])


# --- pierderi de pachete: seq creste la fiecare pachet, deci golurile se vad ---
_seq_stat = {}      # nod -> [ultimul_seq, primite, pierdute]
_ultim_raport = time.time()


def urmareste(p):
    st = _seq_stat.setdefault(p["node"], [None, 0, 0])
    if st[0] is not None:
        gol = (p["seq"] - st[0] - 1) & 0xFFFF
        if 0 < gol < 1000:          # peste atat inseamna repornire de nod
            st[2] += gol
    st[0] = p["seq"]; st[1] += 1


def raport_pierderi():
    global _ultim_raport
    if time.time() - _ultim_raport < RAPORT_S:
        return
    _ultim_raport = time.time()
    parti = []
    for nod, (_, ok, pierdut) in sorted(_seq_stat.items()):
        tot = ok + pierdut
        parti.append(f"{nod}: {pierdut}/{tot} pierdute ({pierdut/tot*100:.1f}%)" if tot else f"{nod}: -")
    print("  [retea] " + " | ".join(parti))


def scrie(tip, p, t="", n=""):
    _w.writerow([datetime.now().strftime("%H:%M:%S.%f")[:-3], tip, p["node"],
                 p["seq"], t, p.get("age_ms", ""), p["dbfs"], p.get("conf", ""),
                 p.get("rssi", ""), p.get("batt_mv", ""), n])
    _f.flush()


def _post(path, data):
    try:
        r = requests.post(FLASK + path, json=data, timeout=2)
        if r.status_code >= 400:
            print(f"Flask {path} -> HTTP {r.status_code}")
    except requests.RequestException as e:
        print("Flask indisponibil:", e)


def post(path, data):
    """Pe fir separat: citirea de pe serial nu asteapta niciodata dupa HTTP."""
    threading.Thread(target=_post, args=(path, data), daemon=True).start()



def handle_event(p):
    # Momentul detectiei, pe ceasul gateway-ului: a ajuns la rx_ms, s-a intamplat cu age_ms mai devreme.
    t = p["rx_ms"] - p["age_ms"]

    # curatenie
    clusters[:] = [c for c in clusters if p["rx_ms"] - c["t"] < KEEP_MS]

    c = next((c for c in clusters
              if c["cls"] == p["cls"] and abs(t - c["t"]) <= WINDOW_MS), None)
    if c is None:
        c = {"t": t, "cls": p["cls"], "nodes": {}, "alerted": 0}
        clusters.append(c)
    c["nodes"][p["node"]] = p

    print(f"EVT {p['node']} cls={p['cls']} conf={p['conf']} db={p['dbfs']} t={t} "
          f"-> cluster cu {len(c['nodes'])} noduri")
    scrie("eveniment", p, t, len(c["nodes"]))

    # Alerta pleaca de la primul nod, dar se retrimite cand se alatura altul:
    # altfel confirmarea (mai multe noduri = incredere mai mare) nu ajunge niciodata.
    if len(c["nodes"]) >= MIN_NODES and len(c["nodes"]) > c["alerted"]:
        c["alerted"] = len(c["nodes"])
        post("/api/alert", {
            "cls": c["cls"],
            "nodes": [
                {"node_id": n, "conf": q["conf"], "db": q["dbfs"],
                 "dt_ms": (q["rx_ms"] - q["age_ms"]) - c["t"]}   # pentru localizare
                for n, q in c["nodes"].items()
            ],
            "ts": time.time(),
            "confirmat": len(c["nodes"]) >= 2,
        })
        ord_ = sorted(c["nodes"].values(), key=lambda q: -q["dbfs"])
        eticheta = "ALERTA" if len(c["nodes"]) < 2 else "ALERTA CONFIRMATA"
        print(f">>> {eticheta} de", list(c["nodes"]),
              "| cel mai tare:", ord_[0]["node"],
              "|", "  ".join(f"{q['node']}:{q['dbfs']}dB" for q in ord_))


def main():
    ser = serial.Serial(PORT, 115200, timeout=1)
    print(f"Ascult pe {PORT}, scriu in {LOG.name}")
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line.startswith("{"):
            if line:
                print(line)         # mesajele "# gateway MAC..."
            continue
        try:
            p = json.loads(line)
        except json.JSONDecodeError:
            continue

        if last_seq.get(p["node"]) == p["seq"]:
            continue                # duplicat (retry)
        last_seq[p["node"]] = p["seq"]
        urmareste(p)
        raport_pierderi()

        if p["type"] == PKT_HEARTBEAT:
            post("/api/ingest", {"node_id": p["node"], "db": p["dbfs"],
                                 "batt_mv": p["batt_mv"], "rssi": p["rssi"]})
            fond = p["dbfs"] - p.get("conf", 0)
            print(f"  hb {p['node']:>3}  varf {p['dbfs']:>4} dB  fond {fond:>4} dB  rssi {p['rssi']}")
            scrie("heartbeat", p)
        elif p["type"] == PKT_EVENT:
            handle_event(p)


if __name__ == "__main__":
    main()
