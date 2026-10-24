#!/bin/bash
#
# SonicGrid - pune HTTPS in fata aplicatiei, pe Raspberry Pi.
#
#   sudo bash setup_https.sh
#
# Ce face:
#   1. instaleaza nginx
#   2. genereaza un certificat auto-semnat, valabil 2 ani, care include IP-ul
#      curent al Pi-ului si numele sonicgrid.local
#   3. scrie configuratia nginx, cu setarile necesare pentru SSE
#   4. leaga aplicatia doar la 127.0.0.1, ca portul 5000 sa nu mai fie
#      accesibil direct din retea, fara criptare
#   5. porneste tot si verifica
#
# Nu modifica niciun fisier al aplicatiei si nu atinge baza de date.
# Modificarile la serviciu se fac printr-un drop-in systemd, deci unitatea
# originala ramane neschimbata si se poate reveni oricand (vezi final).

set -euo pipefail

SERVICIU="sonicgrid"
PORT_APP="5000"
CERT="/etc/ssl/certs/sonicgrid.crt"
CHEIE="/etc/ssl/private/sonicgrid.key"
SITE="/etc/nginx/sites-available/sonicgrid"
DROPIN="/etc/systemd/system/${SERVICIU}.service.d"

rosu()   { printf '\033[31m%s\033[0m\n' "$*"; }
verde()  { printf '\033[32m%s\033[0m\n' "$*"; }
titlu()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  rosu "Ruleaza cu sudo:  sudo bash setup_https.sh"
  exit 1
fi

# NU folosi "systemctl ... | grep -q": grep iese la prima potrivire, systemctl
# primeste SIGPIPE si, cu pipefail activ, pipeline-ul pare esuat chiar cand
# serviciul exista. systemctl cat raspunde direct, fara pipeline.
if ! systemctl cat "${SERVICIU}.service" >/dev/null 2>&1; then
  rosu "Nu gasesc serviciul ${SERVICIU}.service."
  rosu "Porneste-l intai ca serviciu, apoi reia scriptul."
  exit 1
fi

# IP-ul curent, ca sa intre in certificat. Pe hotspot se schimba intre sesiuni,
# dar certificatul ramane valabil pentru numele sonicgrid.local oricum.
IP=$(hostname -I | awk '{print $1}')
[ -n "$IP" ] || { rosu "Nu am putut determina IP-ul."; exit 1; }
echo "IP detectat: $IP"

# ---------------------------------------------------------------- 1. nginx
titlu "1/5  Instalez nginx"
if command -v nginx >/dev/null 2>&1; then
  echo "nginx e deja instalat."
else
  apt-get update -qq
  apt-get install -y -qq nginx
fi

# ---------------------------------------------------------------- 2. certificat
titlu "2/5  Generez certificatul"
if [ -f "$CERT" ] && [ -f "$CHEIE" ]; then
  echo "Certificatul exista deja. Il pastrez."
  echo "(Sterge $CERT si $CHEIE daca vrei unul nou.)"
else
  # subjectAltName e obligatoriu: browserele moderne ignora complet campul CN.
  # Fara el, certificatul e respins chiar si cu exceptie manuala.
  CFG=$(mktemp)
  cat > "$CFG" <<EOF
[req]
distinguished_name = dn
x509_extensions    = ext
prompt             = no

[dn]
CN = sonicgrid.local
O  = SonicGrid

[ext]
subjectAltName   = DNS:sonicgrid.local, DNS:sonicgrid, DNS:localhost, IP:${IP}, IP:127.0.0.1
basicConstraints = CA:FALSE
keyUsage         = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
EOF
  openssl req -x509 -nodes -days 730 -newkey rsa:2048 \
    -keyout "$CHEIE" -out "$CERT" -config "$CFG" >/dev/null 2>&1
  rm -f "$CFG"
  chmod 600 "$CHEIE"
  verde "Certificat generat, valabil 2 ani, pentru: sonicgrid.local si $IP"
fi

# ---------------------------------------------------------------- 3. nginx conf
titlu "3/5  Configurez nginx"
cat > "$SITE" <<EOF
# SonicGrid - terminare TLS in fata aplicatiei Flask.
server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     ${CERT};
    ssl_certificate_key ${CHEIE};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache   shared:SSL:4m;

    # Fisierele trimise de noduri sunt mici; limita opreste cereri absurde.
    client_max_body_size 4m;

    location / {
        proxy_pass http://127.0.0.1:${PORT_APP};
        proxy_http_version 1.1;

        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection        "";

        # Obligatoriu pentru fluxul SSE. Fara ele nginx aduna raspunsul in
        # tampon si dashboard-ul pare inghetat: alertele apar in rafale, la
        # cateva zeci de secunde, sau deloc.
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding off;
    }
}

server {
    listen 80;
    server_name _;
    return 301 https://\$host\$request_uri;
}
EOF

ln -sf "$SITE" /etc/nginx/sites-enabled/sonicgrid
rm -f /etc/nginx/sites-enabled/default      # altfel raspunde pagina implicita

if ! nginx -t 2>/dev/null; then
  rosu "Configuratia nginx nu e valida:"
  nginx -t || true
  exit 1
fi
verde "Configuratie nginx valida."

# ---------------------------------------------------------------- 4. aplicatia
titlu "4/5  Leg aplicatia la 127.0.0.1"
mkdir -p "$DROPIN"
cat > "${DROPIN}/https.conf" <<EOF
# Adaugat de setup_https.sh. Sterge acest fisier ca sa revii la varianta
# fara HTTPS, apoi: systemctl daemon-reload && systemctl restart ${SERVICIU}
[Service]
Environment=SONICGRID_HTTPS=1
Environment=SONICGRID_PROXY=1
Environment=SONICGRID_HOST=127.0.0.1
EOF
echo "  SONICGRID_HTTPS=1   cookie Secure + HSTS"
echo "  SONICGRID_PROXY=1   IP-ul real al clientului in jurnal"
echo "  SONICGRID_HOST=...  aplicatia nu mai asculta direct in retea"

systemctl daemon-reload
systemctl restart "$SERVICIU"
systemctl enable --now nginx >/dev/null 2>&1 || systemctl restart nginx
sleep 3

# ---------------------------------------------------------------- 5. verificare
titlu "5/5  Verific"
ok=1

if systemctl is-active --quiet "$SERVICIU"; then
  verde "  serviciul $SERVICIU ruleaza"
else
  rosu  "  serviciul $SERVICIU NU ruleaza"; ok=0
fi

if systemctl is-active --quiet nginx; then
  verde "  nginx ruleaza"
else
  rosu  "  nginx NU ruleaza"; ok=0
fi

cod=$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1/login" || echo 000)
if [ "$cod" = "200" ] || [ "$cod" = "401" ] || [ "$cod" = "503" ]; then
  verde "  HTTPS raspunde (cod $cod)"
else
  rosu  "  HTTPS nu raspunde (cod $cod)"; ok=0
fi

red=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1/login" || echo 000)
if [ "$red" = "301" ]; then
  verde "  HTTP redirectioneaza catre HTTPS"
else
  rosu  "  HTTP nu redirectioneaza (cod $red)"; ok=0
fi

antete=$(curl -sk -I "https://127.0.0.1/login" 2>/dev/null || true)

if printf '%s' "$antete" | grep -qi "strict-transport-security"; then
  verde "  antetul HSTS e prezent"
else
  rosu  "  antetul HSTS lipseste"; ok=0
fi

if printf '%s' "$antete" | grep -i "set-cookie" | grep -qi "secure"; then
  verde "  cookie-ul de sesiune e marcat Secure"
else
  echo  "  (cookie-ul apare abia dupa autentificare - normal)"
fi

if curl -s --max-time 3 "http://${IP}:${PORT_APP}/login" >/dev/null 2>&1; then
  rosu  "  ATENTIE: portul ${PORT_APP} inca raspunde direct, necriptat"; ok=0
else
  verde "  portul ${PORT_APP} nu mai e accesibil direct din retea"
fi

echo
if [ "$ok" = "1" ]; then
  verde "Gata. Deschide:  https://${IP}/"
  echo
  echo "Browserul va avertiza ca certificatul nu e de incredere - e normal"
  echo "pentru unul auto-semnat. Accepti exceptia o data si gata."
else
  rosu "Ceva nu a mers. Diagnostic:"
  echo "  journalctl -u ${SERVICIU} -n 30 --no-pager"
  echo "  journalctl -u nginx -n 30 --no-pager"
fi

echo
echo "Ca sa revii la varianta fara HTTPS:"
echo "  sudo rm ${DROPIN}/https.conf"
echo "  sudo rm /etc/nginx/sites-enabled/sonicgrid"
echo "  sudo systemctl daemon-reload && sudo systemctl restart ${SERVICIU} nginx"
