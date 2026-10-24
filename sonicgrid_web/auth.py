"""Rute: /login, /logout, /termeni, /confidentialitate, /despre."""
import hmac
import logging
import secrets
import threading
import time
from collections import defaultdict, deque

from flask import (Blueprint, abort, current_app, redirect, render_template,
                   request, session, url_for)

bp = Blueprint("web", __name__, template_folder="templates")
log = logging.getLogger("sonicgrid.auth")

# Limitare incercari: max 5 esecuri / 15 min per IP si per utilizator.
MAX_FAILS, WINDOW = 5, 15 * 60
_fails = defaultdict(deque)
_fails_lock = threading.Lock()


def _blocked(key: str) -> bool:
    now = time.time()
    with _fails_lock:
        q = _fails[key]
        while q and now - q[0] > WINDOW:
            q.popleft()
        return len(q) >= MAX_FAILS


def _fail(*keys):
    now = time.time()
    with _fails_lock:
        for k in keys:
            _fails[k].append(now)


def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def _csrf_ok() -> bool:
    sent = request.form.get("_csrf", "")
    return bool(sent) and hmac.compare_digest(sent, session.get("_csrf", ""))


def _check_csrf():
    if not _csrf_ok():
        abort(400, "Token CSRF invalid. Reincarca pagina.")


def _safe_next(target: str) -> str:
    # Doar cai relative din acelasi site (fara open redirect).
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


@bp.route("/login", methods=["GET", "POST"])
def login():
    store = current_app.extensions["sg_users"]
    nxt = _safe_next(request.values.get("next", "/"))
    error = None
    if request.method == "POST" and not _csrf_ok():
        # Pagina veche (server repornit) sau cookie blocat: nu mai dam 400 sec,
        # reafisam formularul cu token nou si un mesaj clar.
        cookie = current_app.config["SESSION_COOKIE_NAME"]
        log.warning("csrf esuat ip=%s cookie_primit=%s", request.remote_addr,
                    cookie in request.cookies)
        if cookie in request.cookies:
            error = "Sesiunea a expirat. Mai încearcă o dată."
        else:
            error = ("Browserul nu a trimis cookie-ul de sesiune. Permite cookie-urile "
                     "pentru acest site sau deschide http://localhost:5000.")
        session.pop("_csrf", None)
    elif request.method == "POST":
        user = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        accepted = request.form.get("accept") == "on"
        ip = request.remote_addr or "?"
        if _blocked("ip:" + ip) or _blocked("u:" + user):
            error = "Prea multe încercări eșuate. Încearcă din nou peste 15 minute."
        elif not accepted:
            error = "Trebuie să accepți Termenii și Politica de confidențialitate."
        elif store.verify(user, pw):
            session.clear()                         # previne session fixation
            session.permanent = True
            session["user"] = user
            store.record_acceptance(user)
            store.record_login(user)
            log.info("login ok user=%s ip=%s", user, ip)
            return redirect(nxt)
        else:
            _fail("ip:" + ip, "u:" + user)
            log.warning("login esuat user=%s ip=%s", user, ip)
            error = "Utilizator sau parolă greșită."
    return render_template("web/login.html", error=error, next=nxt,
                           reaccept=request.args.get("reaccept")), (401 if error else 200)


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    if request.method == "GET":
        return render_template("web/logout.html")
    if _csrf_ok():
        session.clear()
    return redirect(url_for("web.login"))


@bp.route("/termeni")
def terms():
    return render_template("web/terms.html")


@bp.route("/confidentialitate")
def privacy():
    return render_template("web/privacy.html")


@bp.route("/despre")
def about():
    return render_template("web/about.html")
