#!/bin/bash
#
# SonicGrid - HTTPS in fata aplicatiei. Versiune corectata.
#
#   sudo bash https3.sh
#
# Fata de incercarea anterioara:
#   - nginx se reporneste intotdeauna; "enable --now" nu face nimic daca apt
#     l-a pornit deja la instalare, deci configuratia nu era citita niciodata
#   - gazda aplicatiei se schimba in ExecStart, nu prin variabila de mediu:
#     serviciul ruleaza waitress-serve --host=..., care ignora SONICGRID_HOST
#   - waitress primeste mai multe fire: fiecare dashboard deschis ocupa un fir
#     cat tine conexiunea SSE, iar implicitul de 4 blocheaza serverul

set -euo pipefail

SERVICIU="sonicgrid"
PORT_APP="5000"
FIRE="24"
CERT="/etc/ssl/certs/sonicgrid.crt"
CHEIE="/etc/ssl/private/sonicgrid.key"
SITE="/etc/nginx/sites-available/sonicgrid"
DROPIN="/etc/systemd/system/${SERVICIU}.service.d"

rosu()  { printf '\033[31m%s\033[0m\n' "$*"; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
titlu() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { rosu "Ruleaza cu sudo: sudo bash https3.sh"; exit 1; }

if ! systemctl cat "${SERVICIU}.service" >/dev/null 2>&1; then
  rosu "Nu gasesc ${SERVICIU}.service."; exit 1
fi

IP=$(hostname -I | awk '{print $1}')
[ -n "$IP" ] || { rosu "Nu am putut determina IP-ul."; exit 1; }
echo "IP detectat: $IP"

# ---------------------------------------------------------------- 1. nginx
titlu "1/5  nginx"
if command -v nginx >/dev/null 2>&1; then
  echo "deja instalat"
else
  apt-get update -qq && apt-get install -y -qq nginx
fi

# ---------------------------------------------------------------- 2. certificat
titlu "2/5  Certificat"
if [ -f "$CERT" ] && [ -f "$CHEIE" ]; then
  echo "exista deja (sterge $CERT si $CHEIE pentru unul nou)"
else
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
  verde "generat pentru sonicgrid.local si $IP"
fi

# ---------------------------------------------------------------- 3. nginx conf
titlu "3/5  Configuratie nginx"
cat > "$SITE" <<EOF
server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     ${CERT};
    ssl_certificate_key ${CHEIE};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:4m;

    client_max_body_size 4m;

    location / {
        proxy_pass http://127.0.0.1:${PORT_APP};
        proxy_http_version 1.1;

        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection        "";

        # Obligatoriu pentru SSE: fara ele nginx tamponeaza fluxul si alertele
        # apar in rafale sau deloc.
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
rm -f /etc/nginx/sites-enabled/default

if ! nginx -t 2>/dev/null; then
  rosu "Configuratie nginx invalida:"; nginx -t || true; exit 1
fi
verde "configuratie valida"

# ---------------------------------------------------------------- 4. aplicatia
titlu "4/5  Aplicatia asculta doar local"

# Serviciul poate rula python, gunicorn sau waitress. Luam linia ExecStart
# originala din unitate si o rescriem, in loc sa presupunem ceva.
UNITATE=$(systemctl show -P FragmentPath "${SERVICIU}.service")
EXEC=$(grep -m1 '^ExecStart=' "$UNITATE" | sed 's/^ExecStart=//')
[ -n "$EXEC" ] || { rosu "Nu am gasit ExecStart in $UNITATE"; exit 1; }
echo "  inainte: $EXEC"

NOU="$EXEC"
NOU=${NOU//--host=0.0.0.0/--host=127.0.0.1}
NOU=${NOU//-b 0.0.0.0:/-b 127.0.0.1:}
NOU=${NOU//--bind=0.0.0.0:/--bind=127.0.0.1:}

# waitress porneste cu 4 fire. Fiecare dashboard deschis ocupa un fir cat tine
# conexiunea SSE, deci 4 dashboard-uri ar bloca inclusiv pachetele de la noduri.
if [[ "$NOU" == *waitress-serve* && "$NOU" != *--threads* ]]; then
  NOU=${NOU/waitress-serve/waitress-serve --threads=${FIRE}}
fi

if [ "$NOU" = "$EXEC" ]; then
  if [[ "$EXEC" == *app.py* ]]; then
    echo "  linia ramane la fel - aplicatia citeste SONICGRID_HOST din mediu"
  else
    rosu "  ATENTIE: nu am stiut cum sa rescriu linia de pornire."
    rosu "  Fa-o manual ca sa asculte pe 127.0.0.1, altfel portul ${PORT_APP}"
    rosu "  ramane accesibil necriptat din retea. Verificarea de la final o sa"
    rosu "  iti spuna daca mai e deschis."
  fi
else
  echo "  dupa:    $NOU"
fi

mkdir -p "$DROPIN"
cat > "${DROPIN}/https.conf" <<EOF
# Adaugat de https3.sh. Sterge acest fisier si ruleaza
#   systemctl daemon-reload && systemctl restart ${SERVICIU}
# ca sa revii exact la configuratia dinainte.
[Service]
Environment=SONICGRID_HTTPS=1
Environment=SONICGRID_PROXY=1
ExecStart=
ExecStart=${NOU}
EOF

systemctl daemon-reload
systemctl restart "$SERVICIU"

# nginx: restart NECONDITIONAT. Daca apt l-a pornit deja la instalare,
# "enable --now" nu face nimic si configuratia noastra nu e citita niciodata.
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx
sleep 3

# ---------------------------------------------------------------- 5. verificare
titlu "5/5  Verificare"
ok=1

for s in "$SERVICIU" nginx; do
  if systemctl is-active --quiet "$s"; then verde "  $s ruleaza"
  else rosu "  $s NU ruleaza"; ok=0; fi
done

if ss -tln | grep -q ':443 '; then
  verde "  nginx asculta pe 443"
else
  rosu  "  nimic nu asculta pe 443"; ok=0
fi

cod=$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1/login" 2>/dev/null || true)
case "$cod" in
  200|401|503) verde "  HTTPS raspunde (cod $cod)" ;;
  *)           rosu  "  HTTPS nu raspunde (cod ${cod:-000})"; ok=0 ;;
esac

red=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1/login" 2>/dev/null || true)
if [ "$red" = "301" ]; then verde "  HTTP redirectioneaza catre HTTPS"
else rosu "  HTTP nu redirectioneaza (cod ${red:-000})"; ok=0; fi

antete=$(curl -sk -I "https://127.0.0.1/login" 2>/dev/null || true)
if printf '%s' "$antete" | grep -qi "strict-transport-security"; then
  verde "  antetul HSTS e prezent"
else
  rosu  "  antetul HSTS lipseste"; ok=0
fi

if ss -tln | grep -q "0.0.0.0:${PORT_APP} "; then
  rosu  "  ATENTIE: portul ${PORT_APP} asculta inca in toata reteaua, necriptat"; ok=0
else
  verde "  portul ${PORT_APP} e legat doar la 127.0.0.1"
fi

echo
if [ "$ok" = "1" ]; then
  verde "Gata. Deschide:  https://${IP}/"
  echo "Browserul avertizeaza ca certificatul nu e de incredere - normal pentru"
  echo "unul auto-semnat. Accepti exceptia o data."
else
  rosu "Ceva tot nu merge:"
  echo "  journalctl -u ${SERVICIU} -n 30 --no-pager"
  echo "  journalctl -u nginx -n 30 --no-pager"
  echo "  ss -tlnp | grep -E ':80 |:443 |:${PORT_APP} '"
fi

echo
echo "Revenire:"
echo "  sudo rm ${DROPIN}/https.conf /etc/nginx/sites-enabled/sonicgrid"
echo "  sudo systemctl daemon-reload && sudo systemctl restart ${SERVICIU} nginx"
