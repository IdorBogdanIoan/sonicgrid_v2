"""
SonicGrid - autentificare, roluri si administrare utilizatori.

Doua roluri:
  admin    - vede dashboard-ul SI gestioneaza utilizatorii (/admin)
  operator - vede doar dashboard-ul

Utilizatorii stau in aceeasi baza de date SQLite ca restul aplicatiei
(sonicgrid.db), in tabelul `users`. Parolele sunt hash-uite cu scrypt prin
werkzeug - in baza de date nu exista nicio parola in clar.

Fiecare actiune importanta (login reusit, login esuat, creare/stergere user,
schimbare de rol) se scrie in tabelul `audit`.

LEGARE IN app.py (doua linii):

    from sg_auth import init_auth
    init_auth(app, DB_PATH, node_paths=("/api/ingest", "/api/alert"))

ADMINISTRARE DIN LINIA DE COMANDA (pe Pi):

    python3 sg_auth.py add bobo --role admin
    python3 sg_auth.py add dispecer1 --role operator
    python3 sg_auth.py passwd bobo
    python3 sg_auth.py role dispecer1 admin
    python3 sg_auth.py del dispecer1
    python3 sg_auth.py list
"""

import argparse
import functools
import json
import getpass
import hmac
import ipaddress
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from legal import LEGAL, campuri_necompletate

log = logging.getLogger("sonicgrid.auth")

ROLES = ("admin", "operator")
MIN_PASSWORD_LEN = 10
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")

# Hash calculat pe un user inexistent: verificarea dureaza la fel de mult ca
# pentru unul real, deci atacatorul nu poate afla din timpul de raspuns care
# utilizatori exista.
_DUMMY_HASH = generate_password_hash("sonicgrid-utilizator-inexistent")

# Limitare incercari de login: 5 esecuri / 15 minute, separat pe IP si pe user.
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60
_fails = defaultdict(deque)
_fails_lock = threading.Lock()


# ---------------------------------------------------------------- baza de date

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'operator',
    created_at    REAL    NOT NULL,
    last_login    REAL,
    terms_version TEXT,
    terms_at      REAL,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    disabled      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    actor  TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    ip     TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts);
"""


class UserStore:
    """Utilizatori in SQLite. Toate metodele isi deschid propria conexiune:
    Flask raspunde pe mai multe fire, iar o conexiune sqlite3 nu se poate
    plimba intre ele in siguranta."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Baze create cu o versiune mai veche a schemei: adaugam coloanele
            # lipsa, fara sa pierdem utilizatorii existenti.
            existente = {r[1] for r in c.execute("PRAGMA table_info(users)")}
            for coloana, tip in (("terms_version", "TEXT"), ("terms_at", "REAL")):
                if coloana not in existente:
                    c.execute(f"ALTER TABLE users ADD COLUMN {coloana} {tip}")

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        return c

    # ---- citire

    def get(self, username: str):
        with self._conn() as c:
            return c.execute("SELECT * FROM users WHERE username = ?",
                             (username,)).fetchone()

    def exists(self, username: str) -> bool:
        return self.get(username) is not None

    def role(self, username: str):
        u = self.get(username)
        return u["role"] if u else None

    def is_active(self, username: str) -> bool:
        u = self.get(username)
        return bool(u) and not u["disabled"]

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def count_admins(self, exclude: str = None) -> int:
        q = "SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0"
        p = ()
        if exclude:
            q += " AND username != ?"
            p = (exclude,)
        with self._conn() as c:
            return c.execute(q, p).fetchone()[0]

    def list(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT username, role, created_at, last_login, disabled "
                "FROM users ORDER BY role, username")]

    # ---- scriere

    def verify(self, username: str, password: str) -> bool:
        u = self.get(username)
        h = u["password_hash"] if u else _DUMMY_HASH
        ok = check_password_hash(h, password)
        return bool(u) and not u["disabled"] and ok

    def create(self, username: str, password: str, role: str = "operator"):
        if not USERNAME_RE.match(username):
            raise ValueError("Numele poate avea 3-32 caractere: litere, cifre, . _ -")
        if role not in ROLES:
            raise ValueError(f"Rol necunoscut: {role}. Permise: {', '.join(ROLES)}")
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"Parola trebuie sa aiba minim {MIN_PASSWORD_LEN} caractere.")
        with self._lock, self._conn() as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise ValueError("Exista deja un utilizator cu numele asta.")
            c.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), role, time.time()))

    def set_password(self, username: str, password: str):
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"Parola trebuie sa aiba minim {MIN_PASSWORD_LEN} caractere.")
        with self._lock, self._conn() as c:
            n = c.execute("UPDATE users SET password_hash=? WHERE username=?",
                          (generate_password_hash(password), username)).rowcount
        if not n:
            raise ValueError("Utilizatorul nu exista.")

    def set_role(self, username: str, role: str):
        if role not in ROLES:
            raise ValueError(f"Rol necunoscut: {role}")
        # Nu lasam sistemul fara niciun admin.
        if self.role(username) == "admin" and role != "admin" \
                and self.count_admins(exclude=username) == 0:
            raise ValueError("Nu poti scoate ultimul administrator.")
        with self._lock, self._conn() as c:
            n = c.execute("UPDATE users SET role=? WHERE username=?",
                          (role, username)).rowcount
        if not n:
            raise ValueError("Utilizatorul nu exista.")

    def delete(self, username: str):
        if self.role(username) == "admin" and self.count_admins(exclude=username) == 0:
            raise ValueError("Nu poti sterge ultimul administrator.")
        with self._lock, self._conn() as c:
            n = c.execute("DELETE FROM users WHERE username=?", (username,)).rowcount
        if not n:
            raise ValueError("Utilizatorul nu exista.")

    def record_login(self, username: str):
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET last_login=?, failed_logins=0 WHERE username=?",
                      (time.time(), username))

    def record_fail(self, username: str):
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET failed_logins=failed_logins+1 "
                      "WHERE username=?", (username,))

    def a_acceptat_termenii(self, username: str) -> bool:
        u = self.get(username)
        return bool(u) and u["terms_version"] == LEGAL["TERMS_VERSION"]

    def inregistreaza_acceptarea(self, username: str):
        """Dovada consimtamantului: ce versiune a acceptat si cand."""
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET terms_version=?, terms_at=? WHERE username=?",
                      (LEGAL["TERMS_VERSION"], time.time(), username))

    def exporta(self, username: str) -> dict:
        """Toate datele despre un utilizator, pentru dreptul de acces (art. 15)
        si de portabilitate (art. 20). Hash-ul parolei nu se exporta: nu e o
        informatie utila persoanei si publicarea lui ar fi un risc inutil."""
        u = self.get(username)
        if not u:
            return {}
        d = {k: u[k] for k in u.keys() if k != "password_hash"}
        with self._conn() as c:
            d["jurnal_acces"] = [dict(r) for r in c.execute(
                "SELECT ts, action, detail, ip FROM audit WHERE actor=? "
                "ORDER BY ts DESC", (username,))]
        return d

    def sterge_jurnal_vechi(self, zile: int) -> int:
        limita = time.time() - zile * 86400
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM audit WHERE ts < ?", (limita,)).rowcount

    # ---- audit

    def audit(self, action: str, actor=None, detail=None, ip=None):
        try:
            with self._conn() as c:
                c.execute("INSERT INTO audit (ts, actor, action, detail, ip) "
                          "VALUES (?, ?, ?, ?, ?)",
                          (time.time(), actor, action, detail, ip))
        except sqlite3.Error as e:          # jurnalul nu trebuie sa pice aplicatia
            log.warning("audit esuat: %s", e)

    def audit_tail(self, limit: int = 50):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT ts, actor, action, detail, ip FROM audit "
                "ORDER BY ts DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- CSRF

def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def csrf_ok() -> bool:
    sent = request.form.get("_csrf", "") or request.headers.get("X-CSRF-Token", "")
    return bool(sent) and hmac.compare_digest(sent, session.get("_csrf", ""))


def require_csrf():
    if not csrf_ok():
        abort(400, "Token CSRF invalid. Reincarca pagina.")


# ---------------------------------------------------------------- rate limit

def _blocked(key: str) -> bool:
    now = time.time()
    with _fails_lock:
        q = _fails[key]
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()
        return len(q) >= MAX_FAILS


def _note_fail(*keys):
    now = time.time()
    with _fails_lock:
        for k in keys:
            _fails[k].append(now)


# ---------------------------------------------------------------- decoratori

def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if getattr(g, "role", None) != "admin":
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="Doar administratorii."), 403
            abort(403)
        return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- rute

bp = Blueprint("auth", __name__)


def _safe_next(target: str) -> str:
    """Doar cai relative din acelasi site - altfel e open redirect."""
    if target and target.startswith("/") and not target.startswith("//") \
            and "\\" not in target:
        return target
    return "/"


@bp.route("/login", methods=["GET", "POST"])
def login():
    store = current_app.extensions["sg_users"]
    nxt = _safe_next(request.values.get("next", "/"))
    error = None

    if store.count() == 0:
        return render_template("login.html", error=None, next=nxt,
                               setup=True), 503

    if request.method == "POST":
        if not csrf_ok():
            # Pagina veche dupa un restart, sau cookie-uri blocate in browser.
            cookie = current_app.config["SESSION_COOKIE_NAME"]
            if cookie in request.cookies:
                error = "Sesiunea a expirat. Mai incearca o data."
            else:
                error = ("Browserul nu a trimis cookie-ul de sesiune. "
                         "Permite cookie-urile pentru acest site.")
            session.pop("_csrf", None)
        else:
            user = request.form.get("username", "").strip()
            pw = request.form.get("password", "")
            ip = request.remote_addr or "?"
            if _blocked("ip:" + ip) or _blocked("u:" + user):
                error = "Prea multe incercari esuate. Incearca peste 15 minute."
                store.audit("login_blocat", actor=user, ip=ip)
            elif not request.form.get("accept"):
                error = ("Trebuie sa iei la cunostinta Termenii si Politica de "
                         "confidentialitate.")
            elif store.verify(user, pw):
                session.clear()                   # previne session fixation
                session.permanent = True
                session["user"] = user
                session["role"] = store.role(user)
                store.inregistreaza_acceptarea(user)
                store.record_login(user)
                store.audit("login", actor=user, ip=ip)
                log.info("login ok user=%s rol=%s ip=%s", user, session["role"], ip)
                return redirect(nxt)
            else:
                _note_fail("ip:" + ip, "u:" + user)
                if store.exists(user):
                    store.record_fail(user)
                store.audit("login_esuat", actor=user, ip=ip)
                log.warning("login esuat user=%s ip=%s", user, ip)
                error = "Utilizator sau parola gresita."

    return render_template("login.html", error=error, next=nxt, setup=False), \
        (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    require_csrf()
    user = session.get("user")
    current_app.extensions["sg_users"].audit("logout", actor=user,
                                             ip=request.remote_addr)
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    store = current_app.extensions["sg_users"]
    ip = request.remote_addr
    mesaj = eroare = None

    if request.method == "POST":
        require_csrf()
        act = request.form.get("actiune", "")
        tinta = request.form.get("username", "").strip()
        try:
            if act == "creeaza":
                rol = request.form.get("role", "operator")
                pw = request.form.get("password", "")
                if pw != request.form.get("password2", ""):
                    raise ValueError("Parolele nu coincid.")
                store.create(tinta, pw, rol)
                store.audit("user_creat", actor=g.user, detail=f"{tinta} ({rol})", ip=ip)
                mesaj = f"Utilizatorul {tinta} a fost creat ca {rol}."
            elif act == "parola":
                pw = request.form.get("password", "")
                if pw != request.form.get("password2", ""):
                    raise ValueError("Parolele nu coincid.")
                store.set_password(tinta, pw)
                store.audit("parola_schimbata", actor=g.user, detail=tinta, ip=ip)
                mesaj = f"Parola pentru {tinta} a fost schimbata."
            elif act == "rol":
                rol = request.form.get("role", "operator")
                store.set_role(tinta, rol)
                store.audit("rol_schimbat", actor=g.user, detail=f"{tinta} -> {rol}", ip=ip)
                mesaj = f"{tinta} este acum {rol}."
            elif act == "sterge":
                if tinta == g.user:
                    raise ValueError("Nu te poti sterge pe tine.")
                store.delete(tinta)
                store.audit("user_sters", actor=g.user, detail=tinta, ip=ip)
                mesaj = f"Utilizatorul {tinta} a fost sters."
            else:
                raise ValueError("Actiune necunoscuta.")
        except ValueError as e:
            eroare = str(e)

    return render_template("admin.html", utilizatori=store.list(),
                           audit=store.audit_tail(40), roluri=ROLES,
                           mesaj=mesaj, eroare=eroare)


# ---------------------------------------------------------------- pagini legale

def _legal_ctx(pagina):
    return {"L": LEGAL, "pagina": pagina, "necompletate": campuri_necompletate()}


@bp.route("/confidentialitate")
def confidentialitate():
    return render_template("confidentialitate.html", **_legal_ctx("conf"))


@bp.route("/termeni")
def termeni():
    return render_template("termeni.html", **_legal_ctx("termeni"))


@bp.route("/cookieuri")
def cookieuri():
    return render_template("cookieuri.html", **_legal_ctx("cookie"))


@bp.route("/despre")
def despre():
    return render_template("despre.html", **_legal_ctx("despre"))


@bp.route("/panou")
@admin_required
def panou():
    """Panoul de informare pentru montat la fata locului (art. 13 GDPR).

    Se completeaza singur din legal.py, iar adresa paginii de confidentialitate
    o afla din cererea curenta - deci e mereu corecta, oriunde ar rula serverul.
    """
    return render_template(
        "panou.html",
        L=LEGAL,
        url_conf=url_for("auth.confidentialitate", _external=True))


@bp.route("/contul-meu", methods=["GET", "POST"])
def contul_meu():
    """Dreptul de acces (art. 15) si de stergere (art. 17), exercitate direct
    de utilizator, fara sa fie nevoie sa ceara cuiva."""
    store = current_app.extensions["sg_users"]
    eroare = None

    if request.method == "POST":
        require_csrf()
        act = request.form.get("actiune")
        if act == "export":
            date = store.exporta(g.user)
            store.audit("export_date", actor=g.user, ip=request.remote_addr)
            return current_app.response_class(
                json.dumps(date, indent=2, ensure_ascii=False),
                mimetype="application/json",
                headers={"Content-Disposition":
                         f'attachment; filename="sonicgrid-{g.user}.json"'})
        if act == "sterge":
            if request.form.get("confirm") != g.user:
                eroare = "Scrie exact numele tau de utilizator ca sa confirmi."
            else:
                try:
                    store.delete(g.user)
                    store.audit("cont_autosters", actor=g.user, ip=request.remote_addr)
                    session.clear()
                    return redirect(url_for("auth.login"))
                except ValueError as e:
                    eroare = str(e)

    return render_template("contul_meu.html", eroare=eroare,
                           utilizator=store.get(g.user), **_legal_ctx("cont"))


# ---------------------------------------------------------------- initializare

def _load_secret(app) -> str:
    """Cheia de sesiune: din env, altfel dintr-un fisier generat o data, 0600.

    Daca s-ar genera la fiecare pornire, toti utilizatorii ar fi deconectati
    la fiecare restart al serviciului."""
    key = os.environ.get("SONICGRID_SECRET_KEY")
    if key:
        return key
    path = os.path.join(os.path.dirname(os.path.abspath(app.root_path)),
                        "instance", "secret_key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


def init_auth(app, db_path: str, node_paths=(), https: bool = None,
              tile_host: str = "https://tile.openstreetmap.org"):
    """Pune login, roluri, CSRF si header-e de securitate peste aplicatie.

    node_paths: rutele pe care posteaza bridge-ul; nu cer sesiune, dar pot cere
                cheia din SONICGRID_NODE_KEY.
    """
    if https is None:
        https = os.environ.get("SONICGRID_HTTPS", "0") == "1"

    app.config.update(
        SECRET_KEY=_load_secret(app),
        SESSION_COOKIE_HTTPONLY=True,       # JavaScript nu poate citi cookie-ul
        SESSION_COOKIE_SAMESITE="Lax",      # nu pleaca la cereri din alt site
        SESSION_COOKIE_SECURE=https,        # doar pe HTTPS, cand e activ
        SESSION_COOKIE_NAME="sg_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    store = UserStore(db_path)
    app.extensions["sg_users"] = store
    app.register_blueprint(bp)

    if os.environ.get("SONICGRID_PROXY", "0") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    node_key = os.environ.get("SONICGRID_NODE_KEY")
    node_paths = tuple(node_paths)
    # Paginile legale TREBUIE sa fie accesibile fara cont: altfel cineva din
    # zona monitorizata nu ar avea cum sa afle ce se prelucreaza despre el.
    public = {"auth.login", "static", "auth.confidentialitate", "auth.termeni",
              "auth.cookieuri", "auth.despre"}

    @app.before_request
    def _guard():
        # 1. Rutele nodurilor: fara sesiune, eventual cu cheie API.
        if request.path.startswith(node_paths) and request.method == "POST":
            if node_key and not hmac.compare_digest(
                    request.headers.get("X-Node-Key", ""), node_key):
                abort(401)
            return None

        if request.endpoint in public:
            return None

        # 2. Restul cere sesiune valida.
        user = session.get("user")
        if not user or not store.is_active(user):
            session.pop("user", None)
            session.pop("role", None)
            if request.path.startswith("/api/") or \
                    request.accept_mimetypes.best == "application/json":
                return jsonify(ok=False, error="Autentificare necesara."), 401
            return redirect(url_for("auth.login", next=request.path))

        # Rolul se reciteste din DB la fiecare cerere: daca un admin ii scade
        # rolul cuiva, schimbarea se aplica imediat, nu dupa ce expira sesiunea.
        g.user = user
        g.role = store.role(user)
        session["role"] = g.role
        return None

    @app.after_request
    def _headers(resp):
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy",
                     "camera=(), microphone=(), geolocation=(), interest-cohort=()")
        # Toate resursele sunt servite local; singura exceptie sunt tile-urile
        # de harta, care pot veni de la OSM cand exista internet.
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; "
                     "img-src 'self' data: " + tile_host + "; "
                     "style-src 'self' 'unsafe-inline'; "
                     "script-src 'self' 'unsafe-inline'; "
                     "connect-src 'self'; "
                     "frame-ancestors 'none'; "
                     "base-uri 'none'; "
                     "form-action 'self'")
        if https:
            h.setdefault("Strict-Transport-Security",
                         "max-age=31536000; includeSubDomains")
        return resp

    @app.template_filter("timp")
    def _timp(ts):
        """Ora locala a Pi-ului, pentru tabelele din /admin."""
        if not ts:
            return "\u2014"
        return datetime.fromtimestamp(float(ts)).strftime("%d.%m %H:%M:%S")

    @app.template_global("camp")
    def _camp(valoare):
        """Marcheaza vizibil campurile ramase necompletate in legal.py, ca sa nu
        ajunga din greseala intr-un document aratat cuiva din afara."""
        from markupsafe import Markup, escape
        s = str(valoare)
        if s.strip().startswith("["):
            return Markup('<span class="lipsa">%s</span>' % escape(s))
        return escape(s)

    @app.context_processor
    def _ctx():
        return {"csrf_token": csrf_token, "L": LEGAL,
                "current_user": session.get("user"),
                "current_role": session.get("role")}

    return store


# ---------------------------------------------------------------- CLI

def _cli(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="Utilizatori SonicGrid")
    p.add_argument("--db", default=os.path.join(here, "sonicgrid.db"))
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="adauga un utilizator")
    a.add_argument("username")
    a.add_argument("--role", choices=ROLES, default="operator")

    for name, h in (("passwd", "schimba parola"), ("del", "sterge utilizatorul")):
        s = sub.add_parser(name, help=h)
        s.add_argument("username")

    r = sub.add_parser("role", help="schimba rolul")
    r.add_argument("username")
    r.add_argument("role", choices=ROLES)

    sub.add_parser("list", help="listeaza utilizatorii")
    sub.add_parser("audit", help="ultimele actiuni inregistrate")

    args = p.parse_args(argv)
    store = UserStore(args.db)

    def cere_parola():
        p1 = getpass.getpass("Parola (minim %d caractere): " % MIN_PASSWORD_LEN)
        if p1 != getpass.getpass("Din nou: "):
            print("Parolele nu coincid.")
            sys.exit(1)
        return p1

    try:
        if args.cmd == "list":
            users = store.list()
            if not users:
                print("Niciun utilizator. Creeaza primul administrator:")
                print("  python3 sg_auth.py add <nume> --role admin")
                return 0
            print(f"{'utilizator':<20} {'rol':<10} {'ultimul login':<20} stare")
            for u in users:
                ll = (datetime.fromtimestamp(u["last_login"], timezone.utc)
                      .strftime("%Y-%m-%d %H:%M") if u["last_login"] else "niciodata")
                print(f"{u['username']:<20} {u['role']:<10} {ll:<20} "
                      f"{'dezactivat' if u['disabled'] else 'activ'}")
            return 0

        if args.cmd == "audit":
            for e in store.audit_tail(40):
                t = datetime.fromtimestamp(e["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                print(f"{t}  {e['actor'] or '-':<16} {e['action']:<20} "
                      f"{e['detail'] or '':<28} {e['ip'] or ''}")
            return 0

        if args.cmd == "add":
            store.create(args.username, cere_parola(), args.role)
            store.audit("user_creat", actor="cli", detail=f"{args.username} ({args.role})")
            print(f"Creat: {args.username} ({args.role})")
        elif args.cmd == "passwd":
            store.set_password(args.username, cere_parola())
            store.audit("parola_schimbata", actor="cli", detail=args.username)
            print("Parola schimbata.")
        elif args.cmd == "role":
            store.set_role(args.username, args.role)
            store.audit("rol_schimbat", actor="cli", detail=f"{args.username} -> {args.role}")
            print(f"{args.username} este acum {args.role}.")
        elif args.cmd == "del":
            store.delete(args.username)
            store.audit("user_sters", actor="cli", detail=args.username)
            print("Sters.")
    except ValueError as e:
        print("Eroare:", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
