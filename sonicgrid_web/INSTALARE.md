# sonicgrid_web — login + pagini legale

## 1. Leagă-l de server (2 linii)

```python
from sonicgrid_web import init_web
init_web(app)          # după app = Flask(__name__), înainte de app.run
```

Protejează automat toate rutele. Rămân publice: `/login`, `/termeni`,
`/confidentialitate`, `/despre`. Nodurile (`/ingest/...`, `/api/node/...`)
nu au nevoie de login; dacă setezi `SONICGRID_NODE_KEY`, trebuie să trimită
header-ul `X-Node-Key` (în firmware: `http.addHeader("X-Node-Key", KEY);`).

## 2. Creează contul

```bash
cd ~/sonicgrid
python -m sonicgrid_web add bobo        # parola: minim 10 caractere
python -m sonicgrid_web list
```

Utilizatorii stau în `instance/users.json` (doar hash-uri). Pune `instance/` în `.gitignore`.

## 3. Completează `legal.py`

Toate câmpurile cu `[ ]`: operator, e-mail de contact, DPO, profesor coordonator.
Dacă schimbi termenii, crește `TERMS_VERSION`; toți vor accepta din nou.

## 4. Variabile de mediu (opțional)

| variabilă | ce face |
|---|---|
| `SONICGRID_SECRET_KEY` | cheie de sesiune; altfel se generează în `instance/secret_key` |
| `SONICGRID_NODE_KEY` | cheie comună pentru ESP32 |
| `SONICGRID_HTTPS=1` | cookie `Secure` + HSTS (doar când rulezi pe HTTPS) |
| `SONICGRID_PROXY=1` | când stai în spatele nginx (IP-ul corect în loguri) |

## 5. Producție pe Pi (recomandat)

```bash
pip install gunicorn
gunicorn -w 1 --threads 8 -b 127.0.0.1:5000 sonicgrid_scream.flask_integration:app
```

`-w 1` e intenționat: detectoarele și limitarea încercărilor de login stau în memorie.
În față pui nginx cu HTTPS (certificat self-signed în LAN, sau Let's Encrypt dacă
ai domeniu) și setezi `SONICGRID_PROXY=1 SONICGRID_HTTPS=1`.
Fără HTTPS, parola trece necriptată prin Wi-Fi.
