"""Utilizatori in fisier JSON, parole hash-uite (scrypt/pbkdf2 via werkzeug).

Gestionare din linia de comanda, pe Pi:
    python -m sonicgrid_web add bobo
    python -m sonicgrid_web passwd bobo
    python -m sonicgrid_web del bobo
    python -m sonicgrid_web list
"""
import getpass
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from .legal import LEGAL

# Hash pentru useri inexistenti: verificarea dureaza la fel, deci nu poti ghici
# din timpul de raspuns care useri exista.
_DUMMY_HASH = generate_password_hash("sonicgrid-dummy")
MIN_PASSWORD_LEN = 10
DEFAULT_USERS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance", "users.json")


class UserStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data: dict) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)          # scriere atomica

    def exists(self, username: str) -> bool:
        return username in self._load()

    def verify(self, username: str, password: str) -> bool:
        user = self._load().get(username)
        h = user["password"] if user else _DUMMY_HASH
        ok = check_password_hash(h, password)
        return bool(user) and ok

    def set_password(self, username: str, password: str) -> None:
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"Parola trebuie sa aiba minim {MIN_PASSWORD_LEN} caractere.")
        with self._lock:
            data = self._load()
            data.setdefault(username, {})["password"] = generate_password_hash(password)
            self._save(data)

    def delete(self, username: str) -> bool:
        with self._lock:
            data = self._load()
            if data.pop(username, None) is None:
                return False
            self._save(data)
            return True

    def accepted_current_terms(self, username: str) -> bool:
        u = self._load().get(username, {})
        return u.get("terms_version") == LEGAL["TERMS_VERSION"]

    def record_acceptance(self, username: str) -> None:
        """Dovada consimtamantului: ce versiune a acceptat si cand."""
        with self._lock:
            data = self._load()
            u = data.setdefault(username, {})
            u["terms_version"] = LEGAL["TERMS_VERSION"]
            u["terms_accepted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save(data)

    def record_login(self, username: str) -> None:
        with self._lock:
            data = self._load()
            if username in data:
                data[username]["last_login"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._save(data)

    def list(self) -> dict:
        return {k: {kk: vv for kk, vv in v.items() if kk != "password"}
                for k, v in self._load().items()}


def _cli(argv):
    path = os.environ.get("SONICGRID_USERS", DEFAULT_USERS_PATH)
    store = UserStore(path)
    if len(argv) < 1 or argv[0] not in {"add", "passwd", "del", "list"}:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "list":
        for name, info in store.list().items():
            print(name, info)
        return 0
    if len(argv) < 2:
        print("Lipseste numele utilizatorului.")
        return 1
    name = argv[1]
    if cmd == "del":
        print("sters" if store.delete(name) else "nu exista")
        return 0
    if cmd == "add" and store.exists(name):
        print("Exista deja. Foloseste 'passwd'.")
        return 1
    p1 = getpass.getpass("Parola: ")
    p2 = getpass.getpass("Din nou: ")
    if p1 != p2:
        print("Parolele nu coincid.")
        return 1
    try:
        store.set_password(name, p1)
    except ValueError as e:
        print(e)
        return 1
    print(f"OK ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
