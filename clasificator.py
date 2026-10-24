"""SonicGrid - clasificator pe Raspberry Pi.

Primeste fragmente WAV de la noduri, le trece prin acelasi front-end ca pe
dispozitiv (sg_features.py) si prin modelul cuantizat, apoi scrie TOT intr-un
log: audio pe disc, trasaturi si probabilitati in SQLite si CSV.

    python3 clasificator.py                # asculta pe 0.0.0.0:5001

Ruleaza pe alt port decat app.py (5000) si cu baza de date proprie, ca sa nu
atinga nimic din ce functioneaza deja.

Protocolul e cel al colectorului: POST /upload cu WAV-ul brut in corp,
Content-Type audio/wav, anteturile X-Node si X-Db.

ATENTIE la interpretarea rezultatelor: modelul din 26 septembrie nu separa
clasele pe inregistrarile proprii (vezi RAPORT_25sept.md - la prag 0.95 are
recall 0.06). Logul e util pentru datele brute pe care le aduna, nu pentru
verdictele pe care le da. Cand apare un model mai bun, se schimba un fisier.
"""
import csv
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
from flask import Flask, request, jsonify

import sg_features as F

# ---------------------------------------------------------------- setari
BAZA      = Path(__file__).resolve().parent
FRAGMENTE = BAZA / "fragmente"
MODEL     = BAZA / "evenimente_int8.tflite"
DB        = BAZA / "clasificari.db"
CSV_LOG   = BAZA / "clasificari.csv"
PORT      = 5001
PASTREAZA_AUDIO = True        # False = doar trasaturile, fara WAV-uri pe disc
HOP_FERESTRE    = 25          # 0.5 s intre ferestre de 1 s

CLASE = ["fundal", "voce", "sticla", "alarma", "claxon", "impact"]

FRAGMENTE.mkdir(exist_ok=True)
lacat = threading.Lock()


# ---------------------------------------------------------------- model
def incarca_model():
    """ai-edge-litert e succesorul intretinut al lui tflite-runtime; tflite
    vechi nu merge cu numpy 2.x, de aceea il incercam pe primul."""
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        try:
            from tflite_runtime.interpreter import Interpreter
        except ImportError:
            return None, None, None, "niciun runtime tflite instalat"
    if not MODEL.exists():
        return None, None, None, f"lipseste {MODEL.name}"
    it = Interpreter(str(MODEL))
    it.allocate_tensors()
    return it, it.get_input_details()[0], it.get_output_details()[0], None


interpret, d_in, d_out, eroare_model = incarca_model()
if eroare_model:
    print(f"! model indisponibil ({eroare_model}) - fragmentele se logheaza oricum")
else:
    print(f"model incarcat: intrare {d_in['shape']}, iesire {d_out['shape']}")


def clasifica(x):
    """x = semnal float32 la 16 kHz. Intoarce (prob_max, prob_medie, n_ferestre).

    Modelul are iesiri sigmoid independente, nu softmax: probabilitatile NU
    se aduna la 1 si o fereastra poate fi si 'voce' si 'impact'.
    """
    L = F.logmel(x)
    W = F.ferestre(L, hop=HOP_FERESTRE)
    if interpret is None:
        return None, None, len(W)
    so, zo = d_out["quantization"]
    p = []
    for w in W:
        # trasaturile sunt uint8 [0,255]; modelul cere int8 cu zero_point -128
        q = (F.quant(w).astype(np.int16) - 128).astype(np.int8)
        interpret.set_tensor(d_in["index"], q.reshape(1, F.WIN_FRAMES, F.NMEL, 1))
        interpret.invoke()
        p.append((interpret.get_tensor(d_out["index"])[0].astype(np.float32) - zo) * so)
    P = np.asarray(p)
    return P.max(0), P.mean(0), len(W)


# ---------------------------------------------------------------- log
def deschide_db():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS clasificari (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        moment TEXT NOT NULL,
        nod TEXT NOT NULL,
        dbfs REAL,
        fisier TEXT,
        durata_s REAL,
        ferestre INTEGER,
        clasa_top TEXT,
        prob_top REAL,
        energie_1_4k REAL,
        probabilitati TEXT,
        model TEXT )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_moment ON clasificari(moment)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_nod ON clasificari(nod)")
    c.commit()
    return c


db = deschide_db()
CAP_CSV = (["moment", "nod", "dbfs", "fisier", "durata_s", "ferestre",
            "clasa_top", "prob_top", "energie_1_4k"] + [f"p_{c}" for c in CLASE])
if not CSV_LOG.exists():
    with open(CSV_LOG, "w", newline="") as f:
        csv.writer(f).writerow(CAP_CSV)


def scrie_log(rand, probs):
    with lacat:
        db.execute(
            """INSERT INTO clasificari (moment,nod,dbfs,fisier,durata_s,ferestre,
               clasa_top,prob_top,energie_1_4k,probabilitati,model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (rand["moment"], rand["nod"], rand["dbfs"], rand["fisier"],
             rand["durata_s"], rand["ferestre"], rand["clasa_top"], rand["prob_top"],
             rand["energie_1_4k"], json.dumps(probs), MODEL.name))
        db.commit()
        with open(CSV_LOG, "a", newline="") as f:
            csv.writer(f).writerow([rand[k] for k in CAP_CSV[:9]] +
                                   [probs.get(c) for c in CLASE])


# ---------------------------------------------------------------- web
app = Flask(__name__)


@app.post("/upload")
def upload():
    date = request.get_data()
    if len(date) < 45:
        return jsonify(eroare="corp gol sau prea scurt"), 400

    nod = request.headers.get("X-Node", "necunoscut")
    try:
        dbfs = float(request.headers.get("X-Db", "nan"))
    except ValueError:
        dbfs = float("nan")

    moment = datetime.now()
    nume = f"{nod}_{moment:%Y%m%d_%H%M%S_%f}.wav"
    cale = FRAGMENTE / nume
    cale.write_bytes(date)

    try:
        x = F.citeste_wav(cale)
    except Exception as e:
        cale.unlink(missing_ok=True)
        return jsonify(eroare=f"WAV invalid: {e}"), 400

    mx, _, n = clasifica(x)
    # energie_banda lucreaza pe log-mel (T, 40), nu pe ferestre (N, 50, 40)
    energie = float(F.energie_banda(F.logmel(x)).max())

    if mx is None:
        probs, top, ptop = {}, "fara_model", None
    else:
        probs = {c: round(float(v), 4) for c, v in zip(CLASE, mx)}
        i = int(np.argmax(mx))
        top, ptop = CLASE[i], round(float(mx[i]), 4)

    rand = {"moment": moment.isoformat(timespec="seconds"), "nod": nod, "dbfs": dbfs,
            "fisier": nume if PASTREAZA_AUDIO else "", "durata_s": round(len(x) / F.SR, 2),
            "ferestre": n, "clasa_top": top, "prob_top": ptop,
            "energie_1_4k": round(energie, 4)}
    scrie_log(rand, probs)

    if not PASTREAZA_AUDIO:
        cale.unlink(missing_ok=True)

    print(f"{rand['moment']}  {nod:6s} {dbfs:6.1f} dBFS  {n} ferestre  -> {top} {ptop}")
    return jsonify(**rand, probabilitati=probs), 200


@app.get("/api/ultimele")
def ultimele():
    n = min(int(request.args.get("n", 50)), 500)
    cur = db.execute("""SELECT moment,nod,dbfs,fisier,clasa_top,prob_top,
                        energie_1_4k,probabilitati FROM clasificari
                        ORDER BY id DESC LIMIT ?""", (n,))
    chei = [d[0] for d in cur.description]
    return jsonify([dict(zip(chei, r)) for r in cur.fetchall()])


@app.get("/fragment/<nume>")
def fragment(nume):
    cale = (FRAGMENTE / nume).resolve()
    if cale.parent != FRAGMENTE.resolve() or not cale.exists():
        return "nu exista", 404
    return cale.read_bytes(), 200, {"Content-Type": "audio/wav"}


@app.get("/")
def pagina():
    cur = db.execute("""SELECT moment,nod,dbfs,fisier,clasa_top,prob_top,energie_1_4k
                        FROM clasificari ORDER BY id DESC LIMIT 40""")
    randuri = "".join(
        f"<tr><td>{m}</td><td>{nod}</td><td>{d:.1f}</td>"
        f"<td><b>{c}</b></td><td>{p if p is not None else '-'}</td>"
        f"<td>{e:.3f}</td>"
        f"<td><audio controls preload=none src='/fragment/{f}'></audio></td></tr>"
        for m, nod, d, f, c, p, e in cur.fetchall())
    total = db.execute("SELECT COUNT(*) FROM clasificari").fetchone()[0]
    stare = eroare_model or MODEL.name
    return f"""<!doctype html><meta charset=utf-8>
<meta http-equiv=refresh content=10>
<title>SonicGrid - clasificator</title>
<body style="font-family:system-ui;margin:2rem;max-width:1100px">
<h2>SonicGrid &mdash; clasificator</h2>
<p><b>{total}</b> fragmente clasificate &middot; model: <code>{stare}</code> &middot;
log: <code>{CSV_LOG.name}</code>, <code>{DB.name}</code></p>
<p style="color:#8a6d3b;background:#fcf8e3;padding:.6rem 1rem;border-radius:6px">
Modelul curent nu separa clasele pe inregistrarile proprii. Coloana
<i>clasa</i> e orientativa; valoroase acum sunt audio-ul si trasaturile logate.</p>
<table cellpadding=6 style="border-collapse:collapse;font-size:14px">
<tr style="background:#eee"><th align=left>moment</th><th align=left>nod</th>
<th align=left>dBFS</th><th align=left>clasa</th><th align=left>prob</th>
<th align=left>en.1-4k</th><th align=left>audio</th></tr>{randuri}</table>
</body>"""


if __name__ == "__main__":
    print(f"clasificator pe 0.0.0.0:{PORT}  |  fragmente in {FRAGMENTE}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
