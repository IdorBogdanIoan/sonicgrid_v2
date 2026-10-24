# SonicGrid — server + dashboard

Server de agregare pentru noduri acustice ESP32. Nodurile trimit un POST pe secundă,
serverul le afișează pe hartă și le salvează în SQLite.

```
sonicgrid/
├── app.py                  server Flask
├── nodes.json              numele și coordonatele nodurilor
├── templates/index.html    dashboard
├── sonicgrid.service       autostart
└── esp32_sonicgrid/        firmware pentru plăci
```

## 1. Instalare pe Raspberry Pi

```bash
mkdir -p ~/sonicgrid && cd ~/sonicgrid
# copiază aici app.py, nodes.json, templates/
python3 -m venv venv
source venv/bin/activate
pip install flask waitress
```

## 2. Nume în rețea

Ca să nu depinzi de IP-ul dat de DHCP (se schimbă la fiecare repornire a hotspotului):

```bash
sudo apt install avahi-daemon -y
sudo hostnamectl set-hostname sonicgrid
sudo reboot
```

După repornire, Pi-ul răspunde la `sonicgrid.local`.

## 3. Pornire

Pentru test:

```bash
source venv/bin/activate
python app.py
```

Pentru demo (multi-threaded, rezistă la 4 noduri simultan):

```bash
waitress-serve --host=0.0.0.0 --port=5000 app:app
```

Dashboard: `http://sonicgrid.local:5000` sau `http://IP_PI:5000`

## 4. Autostart

```bash
sudo cp sonicgrid.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sonicgrid
systemctl status sonicgrid
journalctl -u sonicgrid -f      # log live
```

## 5. Configurarea nodurilor

Editează `nodes.json` cu coordonatele reale unde pui plăcile. Ia-le din Google Maps
(click dreapta pe punct → primul rând copiază lat/lon). După editare:

```bash
sudo systemctl restart sonicgrid
```

Un nod care trimite date fără să fie în `nodes.json` apare oricum, plasat în centrul
zonei și marcat „Nod necunoscut" — util ca să nu pierzi date la demo.

## 6. Firmware ESP32

În `esp32_sonicgrid.ino` schimbă pentru fiecare placă:

- `NODE_ID` — trebuie să fie unic (`n1`, `n2`, `n3`, `n4`)
- `WIFI_SSID` / `WIFI_PASS`
- `SERVER_URL` — dacă mDNS nu merge, pune IP-ul direct

## 7. Test fără plăci

Poți verifica dashboard-ul înainte să ai nodurile gata:

```bash
while true; do
  for n in n1 n2 n3 n4; do
    curl -s -X POST http://localhost:5000/api/ingest \
      -d "node_id=$n&db=-$((RANDOM % 50 + 10)).0&vf_vf=$((RANDOM % 5000))" > /dev/null
  done
  sleep 1
done
```

## API

| Rută | Metodă | Descriere |
|---|---|---|
| `/` | GET | dashboard |
| `/api/ingest` | POST | `{"node_id":"n1","db":-45.7,"vf_vf":445}` |
| `/api/state` | GET | starea curentă a tuturor nodurilor |
| `/api/history?node=n1&minutes=5` | GET | istoric pentru grafic |
| `/api/reset` | POST | golește starea între două rulări |
| `/health` | GET | verificare rapidă |

## De reținut înainte de concurs

**Harta are nevoie de internet.** Leaflet și dâlele OpenStreetMap se încarcă din CDN.
Pe hotspot de telefon merge, dar pe un router fără internet dashboard-ul rămâne fără
hartă. Dacă treci pe router dedicat, descarcă Leaflet local:

```bash
mkdir -p static/vendor && cd static/vendor
wget https://unpkg.com/leaflet@1.9.4/dist/leaflet.js
wget https://unpkg.com/leaflet@1.9.4/dist/leaflet.css
wget -r https://unpkg.com/leaflet@1.9.4/dist/images/
```

și schimbă cele două `<link>`/`<script>` din `index.html` către `/static/vendor/`.
Pentru dâle offline ai nevoie de un set pre-descărcat de tile-uri sau de o imagine
statică a zonei ca fundal.

**Baza de date crește.** 4 noduri × 1 citire/secundă ≈ 350.000 de rânduri pe zi.
Nu e o problemă pentru un demo, dar dacă lași sistemul să ruleze zile întregi,
șterge periodic: `sqlite3 sonicgrid.db "DELETE FROM readings WHERE ts < strftime('%s','now') - 86400;"`
