"""Utilizatori in fisier JSON, parole hash-uite (scrypt/pbkdf2 via werkzeug).

Gestionare din linia de comanda, pe Pi:
    python -m sonicgrid_web add bobo admin
    python -m sonicgrid_web add gigel          (implicit: operator)
    python -m sonicgrid_web passwd bobo
    python -m sonicgrid_web rol gigel admin
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

# Rolurile. Ordinea conteaza: primul e cel implicit.
#
#   operator  vede starea retelei, alertele si istoricul
#   admin     in plus: utilizatorii, exportul brut, stergerea datelor
#
# Un utilizator fara camp "rol" in users.json e operator. Asta inseamna ca
# fisierele existente, scrise inainte de rolurile astea, raman valide si NU
# capata din greseala drepturi de admin.
ROLURI = ("operator", "admin")
ROL_IMPLICIT = ROLURI[0]
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
        # Refuzam stergerea ultimului admin: altfel ramai incuiat afara din
        # propriul sistem si singura iesire e editarea users.json cu mana.
        if self.get_role(username) == "admin" and self.count_admins() <= 1:
            raise ValueError("Nu poti sterge ultimul administrator.")
        with self._lock:
            data = self._load()
            if data.pop(username, None) is None:
                return False
            self._save(data)
            return True

    def get_role(self, username: str) -> str:
        """Rolul utilizatorului. Orice valoare necunoscuta devine operator.

        Fail-closed intentionat: daca cineva scrie manual in users.json un rol
        gresit, sau il sterge, utilizatorul pierde drepturi, nu capata.
        """
        rol = self._load().get(username, {}).get("rol", ROL_IMPLICIT)
        return rol if rol in ROLURI else ROL_IMPLICIT

    def set_role(self, username: str, rol: str) -> None:
        if rol not in ROLURI:
            raise ValueError(f"Rol necunoscut: {rol}. Alege din {', '.join(ROLURI)}.")
        with self._lock:
            data = self._load()
            if username not in data:
                raise ValueError(f"Utilizatorul {username} nu exista.")
            data[username]["rol"] = rol
            self._save(data)

    def count_admins(self) -> int:
        return sum(1 for u in self._load()
                   if self.get_role(u) == "admin")

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
    if len(argv) < 1 or argv[0] not in {"add", "passwd", "del", "list", "rol"}:
        print(__doc__)
        return 1
    cmd = argv[0]

    if cmd == "list":
        for name, info in store.list().items():
            rol = info.get("rol", ROL_IMPLICIT)
            ultim = info.get("last_login", "niciodata")
            print(f"{name:<16} {rol:<9} ultim login: {ultim}")
        return 0

    if len(argv) < 2:
        print("Lipseste numele utilizatorului.")
        return 1
    name = argv[1]

    if cmd == "del":
        try:
            print("sters" if store.delete(name) else "nu exista")
        except ValueError as e:
            print(e)
            return 1
        return 0

    if cmd == "rol":
        if len(argv) < 3:
            print(f"Lipseste rolul. Alege din: {', '.join(ROLURI)}")
            return 1
        try:
            store.set_role(name, argv[2])
        except ValueError as e:
            print(e)
            return 1
        print(f"{name} este acum {argv[2]}")
        return 0

    if cmd == "add" and store.exists(name):
        print("Exista deja. Foloseste 'passwd'.")
        return 1

    # add accepta rolul ca al treilea argument; passwd nu il atinge.
    rol = None
    if cmd == "add":
        rol = argv[2] if len(argv) > 2 else ROL_IMPLICIT
        if rol not in ROLURI:
            print(f"Rol necunoscut: {rol}. Alege din {', '.join(ROLURI)}.")
            return 1

    p1 = getpass.getpass("Parola: ")
    p2 = getpass.getpass("Din nou: ")
    if p1 != p2:
        print("Parolele nu coincid.")
        return 1
    try:
        store.set_password(name, p1)
        if rol is not None:
            store.set_role(name, rol)
    except ValueError as e:
        print(e)
        return 1
    print(f"OK ({path})" + (f", rol {rol}" if rol else ""))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
