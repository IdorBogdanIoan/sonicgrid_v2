"""Test cap-coada: pornesc serverul si verific login, roluri, ingest, alerta, SSE."""
import json, os, re, subprocess, sys, threading, time, urllib.error, urllib.request
from http.cookiejar import CookieJar

AICI = os.path.dirname(os.path.abspath(__file__))

# Testul NU atinge baza reala: isi face una temporara, cu utilizatorii lui,
# si o sterge la final. Altfel ar scrie date false peste masuratorile tale
# si ar avea nevoie de parola ta adevarata.
import tempfile
_tmp = tempfile.mkdtemp(prefix="sonicgrid-test-")
DB = os.path.join(_tmp, "test.db")

sys.path.insert(0, AICI)
from sg_auth import UserStore
_store = UserStore(DB)
_store.create("bobo", "parola-test-123", "admin")
_store.create("dispecer1", "operator-test-99", "operator")
BASE = "http://127.0.0.1:5055"
ok = fail = 0


def verifica(nume, conditie, detaliu=""):
    global ok, fail
    if conditie:
        ok += 1
        print(f"  OK   {nume}")
    else:
        fail += 1
        print(f"  ESEC {nume} {detaliu}")


def client():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()),
        urllib.request.HTTPRedirectHandler())


def cere(op, cale, data=None, headers=None, metoda=None):
    url = BASE + cale
    body = None
    h = {"Accept": "text/html"}
    if isinstance(data, dict) and (headers or {}).get("Content-Type") != "application/json":
        body = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in data.items()).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
        h["Accept"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method=metoda)
    try:
        r = op.open(req, timeout=10)
        return r.status, r.read().decode("utf-8", "replace"), r.url
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), url


def csrf(html):
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    return m.group(1) if m else None


def autentifica(user, parola):
    op = client()
    st, html, _ = cere(op, "/login")
    tok = csrf(html)
    st, html, url = cere(op, "/login", {"username": user, "password": parola,
                                        "_csrf": tok, "accept": "on"})
    return op, st, url, html


# ---------------------------------------------------------------- pornire
srv = subprocess.Popen([sys.executable, "app.py"], cwd=AICI,
                       env={**os.environ, "PORT": "5055", "SONICGRID_DB": DB},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(3)
if srv.poll() is not None:
    print("Serverul nu a pornit:\n", srv.stdout.read())
    sys.exit(1)

try:
    print("\n--- 1. Fara autentificare ---")
    an = client()
    st, html, url = cere(an, "/")
    verifica("dashboard-ul redirectioneaza la /login", "/login" in url, f"(url={url})")

    st, body, _ = cere(an, "/api/state", headers={"Accept": "application/json"})
    verifica("/api/state da 401 fara sesiune", st == 401, f"(status={st})")

    st, body, _ = cere(an, "/admin")
    verifica("/admin cere login", "/login" in _ or st in (302, 401), f"(status={st})")

    print("\n--- 2. Nodurile posteaza fara sesiune ---")
    st, body, _ = cere(an, "/api/ingest", {"node_id": "n1", "db": -42.5, "rssi": -50})
    verifica("/api/ingest accepta fara login", st == 200, f"(status={st} body={body[:80]})")

    # /api/alert cere JSON: lista de noduri nu se poate trimite form-encoded.
    st, body, _ = cere(an, "/api/alert",
                       json.loads(json.dumps(
                           {"cls": 1,
                            "nodes": [{"node_id": "n1", "conf": 90, "db": -20},
                                      {"node_id": "n2", "conf": 85, "db": -24}]})),
                       headers={"Content-Type": "application/json"})
    verifica("/api/alert accepta fara login", st == 200, f"(status={st} body={body[:80]})")

    print("\n--- 3. Login gresit ---")
    op, st, url, html = autentifica("bobo", "parola-gresita")
    verifica("parola gresita e respinsa", st == 401, f"(status={st})")

    print("\n--- 4. Login admin ---")
    adm, st, url, html = autentifica("bobo", "parola-test-123")
    verifica("admin intra", st == 200 and "/login" not in url, f"(url={url})")
    st, body, _ = cere(adm, "/api/state", headers={"Accept": "application/json"})
    verifica("admin vede /api/state", st == 200, f"(status={st})")
    d = json.loads(body)
    verifica("n1 apare in stare", any(n["id"] == "n1" for n in d["nodes"]))
    verifica("alerta a fost inregistrata", d["summary"]["active_alerts"] >= 1,
             f"(alerte={d['summary']['active_alerts']})")
    st, html, _ = cere(adm, "/admin")
    verifica("admin deschide /admin", st == 200 and "Administrare" in html, f"(status={st})")

    print("\n--- 5. Login operator ---")
    opr, st, url, html = autentifica("dispecer1", "operator-test-99")
    verifica("operatorul intra", st == 200 and "/login" not in url, f"(url={url})")
    st, body, _ = cere(opr, "/api/state", headers={"Accept": "application/json"})
    verifica("operatorul vede dashboard-ul", st == 200, f"(status={st})")
    st, html, _ = cere(opr, "/admin")
    verifica("operatorul NU intra pe /admin", st == 403, f"(status={st})")
    st, body, _ = cere(opr, "/api/reset", {}, headers={"Accept": "application/json"})
    verifica("operatorul NU poate da reset", st == 403, f"(status={st})")

    print("\n--- 6. CSRF ---")
    o2 = client()
    cere(o2, "/login")
    st, html, url = cere(o2, "/login", {"username": "bobo", "password": "parola-test-123",
                                        "_csrf": "token-fals"})
    verifica("login cu CSRF gresit e respins", st == 401 and "/login" in url, f"(status={st})")

    print("\n--- 7. Header-e de securitate ---")
    req = urllib.request.Request(BASE + "/login")
    with urllib.request.urlopen(req, timeout=5) as r:
        h = {k.lower(): v for k, v in r.headers.items()}
    for cheie, asteptat in (("x-frame-options", "DENY"),
                            ("x-content-type-options", "nosniff"),
                            ("content-security-policy", "default-src 'self'")):
        verifica(f"header {cheie}", asteptat in h.get(cheie, ""), f"(primit={h.get(cheie)})")
    verifica("cookie HttpOnly", "HttpOnly" in h.get("set-cookie", ""),
             f"(cookie={h.get('set-cookie','')[:60]})")

    print("\n--- 8. Leaflet servit local ---")
    st, body, _ = cere(adm, "/static/vendor/leaflet.js")
    verifica("leaflet.js vine de pe server", st == 200 and len(body) > 100000,
             f"(status={st} octeti={len(body)})")
    st, html, _ = cere(adm, "/")
    verifica("dashboard-ul nu mai cere unpkg", "unpkg.com" not in html)

    print("\n--- 9. SSE ---")
    primite = []

    def asculta():
        try:
            req = urllib.request.Request(BASE + "/stream")
            r = adm.open(req, timeout=12)
            for linie in r:
                linie = linie.decode().strip()
                if linie.startswith("data: "):
                    primite.append(json.loads(linie[6:]))
                    if len(primite) >= 2:
                        break
        except Exception as e:
            primite.append({"eroare": str(e)})

    th = threading.Thread(target=asculta, daemon=True)
    th.start()
    time.sleep(1.5)
    t0 = time.time()
    cere(an, "/api/ingest", {"node_id": "n3", "db": -30.0})
    th.join(timeout=8)
    latenta = (time.time() - t0) * 1000

    verifica("SSE trimite snapshot la conectare",
             bool(primite) and primite[0].get("type") == "snapshot",
             f"(primit={primite[:1]})")
    verifica("SSE impinge imediat dupa ingest",
             len(primite) >= 2 and primite[1].get("type") == "reading",
             f"(primit={[p.get('type') for p in primite]})")
    if len(primite) >= 2:
        print(f"       latenta masurata: {latenta:.0f} ms (polling-ul vechi: pana la 1000 ms)")

    print("\n--- 10. Pagini legale, accesibile FARA cont ---")
    anon = client()
    for cale, cuvant in (("/confidentialitate", "Politica de confiden"),
                         ("/termeni", "Termeni de utilizare"),
                         ("/cookieuri", "Politica de cookie"),
                         ("/despre", "Despre")):
        st, html, _ = cere(anon, cale)
        verifica(f"{cale} publica", st == 200 and cuvant in html, f"(status={st})")

    print("\n--- 11. Consimtamant ---")
    o3 = client()
    _, html, _ = cere(o3, "/login")
    tok = csrf(html)
    st, html, url = cere(o3, "/login", {"username": "bobo", "password": "parola-test-123",
                                        "_csrf": tok})          # fara bifa
    verifica("login fara acceptarea termenilor e respins",
             st == 401 and "cunostinta" in html, f"(status={st})")

    st, html, _ = cere(adm, "/contul-meu")
    verifica("pagina Contul meu se deschide", st == 200 and "Dreptul la" in html,
             f"(status={st})")

    print("\n--- 12. Export date (art. 15 si 20) ---")
    _, html, _ = cere(adm, "/contul-meu")
    tok = csrf(html)
    st, body, _ = cere(adm, "/contul-meu", {"actiune": "export", "_csrf": tok})
    try:
        d = json.loads(body)
    except Exception:
        d = {}
    verifica("exportul da JSON valid", st == 200 and "username" in d, f"(status={st})")
    verifica("exportul NU contine hash-ul parolei", "password_hash" not in d)
    verifica("exportul contine jurnalul de acces", "jurnal_acces" in d)

    print("\n--- 13. Retentie reala ---")
    import sqlite3
    c = sqlite3.connect(DB)
    vechi = time.time() - 400 * 86400
    c.execute("INSERT INTO readings (node_id, ts, db, level, vf_vf) VALUES (?,?,?,?,?)",
              ("n9", vechi, -50, 20, 0))
    c.execute("INSERT INTO alerts (ts, cls, nodes) VALUES (?,?,?)",
              (vechi, 1, '[{"node_id":"n9","conf":1,"db":-50,"dt_ms":0}]'))
    c.commit()
    inainte_r = c.execute("SELECT COUNT(*) FROM readings WHERE ts < ?",
                          (time.time()-365*86400,)).fetchone()[0]
    c.close()
    verifica("am inserat date vechi de test", inainte_r >= 1)

    os.environ["SONICGRID_DB"] = DB
    import app as _app
    sterse = _app.curata_date_vechi()
    c = sqlite3.connect(DB)
    dupa_r = c.execute("SELECT COUNT(*) FROM readings WHERE ts < ?",
                       (time.time()-365*86400,)).fetchone()[0]
    dupa_a = c.execute("SELECT COUNT(*) FROM alerts WHERE ts < ?",
                       (time.time()-365*86400,)).fetchone()[0]
    c.close()
    verifica("masuratorile vechi au fost sterse", dupa_r == 0, f"(ramase={dupa_r})")
    verifica("alertele vechi au fost sterse", dupa_a == 0, f"(ramase={dupa_a})")

finally:
    srv.terminate()
    import shutil
    shutil.rmtree(_tmp, ignore_errors=True)
    try:
        srv.wait(timeout=5)
    except subprocess.TimeoutExpired:
        srv.kill()

print(f"\n==== {ok} reusite, {fail} esecuri ====")
sys.exit(1 if fail else 0)
