"""SonicGrid web: login, sesiuni securizate si pagini legale pentru serverul Flask.

Folosire (2 linii in serverul tau):

    from sonicgrid_web import init_web
    init_web(app)

Tot ce nu e public cere login. Nodurile ESP32 (/ingest/...) raman pe cheie API
(header X-Node-Key), nu pe sesiune — vezi NODE_ENDPOINTS.
"""
import hmac
import os
import secrets
from datetime import timedelta

from flask import Flask, abort, g, redirect, request, session, url_for

from .auth import bp
from .users import DEFAULT_USERS_PATH, UserStore

# Endpoint-uri accesibile fara login (paginile legale trebuie sa fie publice).
PUBLIC_ENDPOINTS = {"web.login", "web.logout", "web.terms", "web.privacy",
                    "web.about", "static"}
# Prefixe de URL folosite de noduri, autentificate prin cheie API, nu prin sesiune.
NODE_PREFIXES = ("/ingest/", "/api/node/")


def _load_secret(app: Flask) -> str:
    """SECRET_KEY din env sau dintr-un fisier generat o singura data (chmod 600)."""
    key = os.environ.get("SONICGRID_SECRET_KEY")
    if key:
        return key
    path = os.path.join(app.instance_path, "secret_key")
    os.makedirs(app.instance_path, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


def init_web(app: Flask, https: bool = None, node_paths=()) -> None:
    """Configureaza login, CSRF, header-e de securitate si paginile legale.

    node_paths: rute folosite de ESP32 (fara login, doar cheie API optionala).
    """
    node_prefixes = NODE_PREFIXES + tuple(node_paths)
    if https is None:
        https = os.environ.get("SONICGRID_HTTPS", "0") == "1"

    app.config.update(
        SECRET_KEY=_load_secret(app),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=https,           # True doar cand rulezi pe HTTPS
        SESSION_COOKIE_NAME="sg_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    app.extensions["sg_users"] = UserStore(
        os.environ.get("SONICGRID_USERS", DEFAULT_USERS_PATH))
    if os.environ.get("SONICGRID_PROXY", "0") == "1":
        # In spatele nginx: IP-ul real al clientului vine din X-Forwarded-For.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.register_blueprint(bp)
    node_key = os.environ.get("SONICGRID_NODE_KEY")

    @app.before_request
    def _guard():
        path = request.path
        if path.startswith(node_prefixes) and request.method == "POST":
            # Daca ai setat o cheie, nodurile trebuie sa o trimita.
            if node_key and not hmac.compare_digest(
                    request.headers.get("X-Node-Key", ""), node_key):
                abort(401)
            return None
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        user = session.get("user")
        store = app.extensions["sg_users"]
        if not user or not store.exists(user):
            # NU session.clear(): ar sterge si tokenul CSRF al paginii de login
            # (browserul cere /favicon.ico imediat dupa login.html).
            session.pop("user", None)
            if path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
                abort(401)
            return redirect(url_for("web.login", next=path))
        # Daca s-au schimbat termenii, userul trebuie sa-i accepte din nou.
        if not store.accepted_current_terms(user):
            session.pop("user", None)
            return redirect(url_for("web.login", next=path, reaccept=1))
        g.user = user
        return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy",
                                "strict-origin-when-cross-origin")  # OSM cere Referer
        resp.headers.setdefault("Permissions-Policy",
                                "camera=(), microphone=(), geolocation=()")
        if https:
            resp.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000")
        return resp

    @app.context_processor
    def _ctx():
        from .auth import csrf_token
        from .legal import LEGAL
        return {"csrf_token": csrf_token, "legal": LEGAL,
                "current_user": session.get("user")}
