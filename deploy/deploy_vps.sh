#!/usr/bin/env bash
# Publica (ou atualiza) a rede Sigma na VPS do GardenCounties.
#
#   bash deploy/deploy_vps.sh              atualiza o código e reinicia o Sigma (chain, chaves, fórum e blobs ficam)
#   RESET=1 bash deploy/deploy_vps.sh      recria a chain do zero, sem nenhum SIG pré-minerado. Nada é apagado:
#                                          os dados antigos vão para /opt/sigma/data-old-<data> e o fórum é copiado
#   PAYOUT=sig… bash deploy/deploy_vps.sh  o que o servidor ganhar hospedando vai para essa carteira (fica gravada
#                                          em /opt/sigma/payout e vale para os próximos deploys)
#
# A v2.3 muda as regras da chain (emissão contínua, aluguel pela demanda, votações): a primeira publicação dela precisa
# de RESET=1. Sem ele, o script para antes de mexer em qualquer coisa se a chain da VPS for de uma versão anterior.
#
# Se existir dist/ (python packaging/build_pyz.py e python packaging/build_windows.py), os instaladores vão
# para /opt/sigma/downloads e a página /baixar passa a oferecê-los.
set -euo pipefail

VPS_HOST="${VPS_HOST:-root@195.35.29.232}"
VPS_KEY="${VPS_KEY:-$HOME/.ssh/garden_vps}"
DOMAIN="${DOMAIN:-sigma.195-35-29-232.sslip.io}"
RESET="${RESET:-0}"
PAYOUT="${PAYOUT:-}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PKG="${TMPDIR:-/tmp}/sigma-deploy.tar.gz"
ADDR_RE='^(0[xX][0-9a-fA-F]{40}|sig[0-9a-fA-F]{40})$'

[ "$RESET" = "1" ] || RESET=0
if [ -n "$PAYOUT" ] && ! [[ "$PAYOUT" =~ $ADDR_RE ]]; then
  echo "ERRO: PAYOUT deve ser um endereço sig… ou 0x… (40 caracteres hexadecimais depois do prefixo)"; exit 1
fi
if [ "$RESET" = "1" ]; then
  echo "==> RESET=1: a chain da VPS recomeça do zero, sem SIG pré-minerado (dados antigos guardados, fórum mantido)"
fi

# O pacote leva a pasta sigma/ inteira (o nó, o app da hospedagem e os módulos que eles importam, como o
# sigma/netsafe.py das conexões de saída). Confere antes que nada essencial falta na cópia local.
for f in sigma/__init__.py sigma/node.py sigma/chain.py sigma/pow.py sigma/netsafe.py sigma/rpc.py sigma/evm.py \
         sigma/forum.py sigma/app.py sigma/web/index.html deploy/ecosystem.config.cjs deploy/nginx-sigma.conf; do
  if [ ! -f "$HERE/$f" ]; then
    echo "ERRO: falta $f na pasta do projeto; nada foi enviado"; exit 1
  fi
done

echo "==> Empacotando o Sigma..."
tar -czf "$PKG" -C "$HERE" --exclude='__pycache__' sigma deploy README.md

DOWNLOADS=()
for f in "$HERE"/dist/*; do
  if [ -f "$f" ]; then DOWNLOADS+=("$f"); fi
done

echo "==> Enviando para $VPS_HOST..."
ssh -i "$VPS_KEY" "$VPS_HOST" "mkdir -p /opt/sigma/app /opt/sigma/data /opt/sigma/downloads/.envio"
scp -i "$VPS_KEY" "$PKG" "$VPS_HOST:/opt/sigma/deploy.tar.gz"
rm -f "$PKG"
if [ ${#DOWNLOADS[@]} -gt 0 ]; then
  # primeiro numa pasta escondida (o nó não serve nomes começando com ponto): ninguém baixa pela metade
  echo "==> Enviando ${#DOWNLOADS[@]} arquivo(s) de dist/ para /opt/sigma/downloads..."
  scp -i "$VPS_KEY" "${DOWNLOADS[@]}" "$VPS_HOST:/opt/sigma/downloads/.envio/"
else
  echo "==> Sem dist/: os downloads da VPS ficam como estão (gere com packaging/build_pyz.py e build_windows.py)"
fi

ssh -i "$VPS_KEY" "$VPS_HOST" DOMAIN="$DOMAIN" RESET="$RESET" PAYOUT="$PAYOUT" bash -s << 'REMOTE'
set -euo pipefail
cd /opt/sigma
ADDR_RE='^(0[xX][0-9a-fA-F]{40}|sig[0-9a-fA-F]{40})$'

# 1) Chain de uma versão com outras regras? O nó novo não roda nela: só com RESET=1. Conferido antes de mexer
#    em qualquer coisa. A v1 tinha SIG fora da mineração (alloc); a v2.0 não tinha o preço de hospedagem da rede
#    no genesis (params.hosting_per_mb_window); até a v2.2 a emissão saía de 12 em 12 h (sem
#    params.ram_per_mb_window, que a v2.3 grava junto com a emissão contínua e as votações).
if [ "$RESET" != "1" ] && [ -f data/no/genesis.json ] && \
   python3 -c 'import json, sys; g = json.load(open(sys.argv[1])); sys.exit(0 if g.get("alloc") or "ram_per_mb_window" not in (g.get("params") or {}) else 1)' data/no/genesis.json; then
  rm -f deploy.tar.gz
  rm -rf downloads/.envio
  echo "ERRO: a chain da VPS é de uma versão anterior do Sigma (as regras mudaram na v2.3)."
  echo "      Rode de novo com RESET=1 para recriar a rede (o fórum é preservado). Nada foi alterado."
  exit 1
fi

# 2) Código novo: primeiro numa pasta à parte, conferido com o python3 da VPS (versão e imports); só então
#    entra no lugar do antigo. Um pacote quebrado nunca tira do ar o que está rodando.
rm -rf app.new && mkdir -p app app.new
tar -xzf deploy.tar.gz -C app.new && rm deploy.tar.gz
if ! (cd app.new && python3 -c 'import sys; assert sys.version_info >= (3, 11), "o Sigma precisa do Python 3.11 ou mais novo"; import sigma.node, sigma.netsafe, sigma.app'); then
  rm -rf app.new downloads/.envio
  echo "ERRO: o código novo não abre com o python3 desta VPS (mensagem acima); nada foi alterado"; exit 1
fi
rm -rf app/sigma app/deploy
mv app.new/sigma app.new/deploy app/
mv -f app.new/README.md app/
rm -rf app.new
cd app

# 3) nginx: só na primeira vez (depois o certbot cuida do arquivo); só recarrega se a configuração inteira
#    continuar válida
if [ ! -f /etc/nginx/sites-available/sigma ]; then
  sed "s/__DOMAIN__/$DOMAIN/" deploy/nginx-sigma.conf > /etc/nginx/sites-available/sigma
  ln -sf /etc/nginx/sites-available/sigma /etc/nginx/sites-enabled/sigma
  if ! nginx -t; then   # nunca deixar uma config quebrada que derrubaria os outros sites no próximo reload
    rm -f /etc/nginx/sites-enabled/sigma /etc/nginx/sites-available/sigma
    echo "ERRO: configuração do nginx inválida; nada foi alterado"; exit 1
  fi
  systemctl reload nginx
  certbot --nginx -d "$DOMAIN" --non-interactive --redirect || echo "AVISO: certbot falhou; o site segue em HTTP"
fi

# 4) Para só os processos do Sigma (o pm2 também roda o GardenCounties). Recria em vez de recarregar:
#    o reload do pm2 manteria os argumentos antigos (ex.: o --faucet que não existe mais).
for p in sigma-node sigma-hosting; do pm2 delete "$p" > /dev/null 2>&1 || true; done

# 5) RESET=1: rede nova sem nenhum SIG pré-minerado. Nada é apagado: a pasta antiga vira data-old-<data>
if [ "$RESET" = "1" ] && [ -d /opt/sigma/data ]; then
  OLD="/opt/sigma/data-old-$(date +%Y%m%d-%H%M%S)"
  mv /opt/sigma/data "$OLD"
  mkdir -p /opt/sigma/data/no
  if ls "$OLD"/no/forum.db* > /dev/null 2>&1; then
    cp -p "$OLD"/no/forum.db* /opt/sigma/data/no/
    echo "==> RESET: dados antigos em $OLD; o fórum foi copiado para a rede nova"
  else
    echo "==> RESET: dados antigos em $OLD (não havia fórum para copiar)"
  fi
fi
mkdir -p /opt/sigma/data/no /opt/sigma/data/host

# 6) Genesis com os parâmetros da rede principal (época de 12 h, janela de 5 min), só se ainda não existe.
#    Não há faucet nem alloc: todo SIG nasce da mineração.
if [ ! -f /opt/sigma/data/no/genesis.json ]; then
  python3 -m sigma.node init --dir /opt/sigma/data/no
fi
chmod 600 /opt/sigma/data/no/*.key

# 7) Instaladores novos: cada arquivo entra de uma vez (mv)
if [ -d /opt/sigma/downloads/.envio ]; then
  find /opt/sigma/downloads/.envio -maxdepth 1 -type f -exec chmod 644 {} \; -exec mv -f {} /opt/sigma/downloads/ \;
  rmdir /opt/sigma/downloads/.envio 2> /dev/null || true
fi
if [ -f /opt/sigma/downloads/SHA256SUMS.txt ]; then
  (cd /opt/sigma/downloads && sha256sum -c --quiet SHA256SUMS.txt) || \
    echo "AVISO: o SHA256SUMS.txt não confere com os arquivos em /opt/sigma/downloads"
fi

# 8) A máquina da VPS hospeda sites, mas não faz computação: tarefas pesadas disputariam a CPU com o
#    GardenCounties (minerar e IA já ficam de fora pela linha de comando)
python3 - /opt/sigma/data/host/config.json << 'PY' || echo "AVISO: não consegui ajustar o config.json da hospedagem"
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
except (OSError, ValueError):
    cfg = {}
if not isinstance(cfg, dict):
    cfg = {}
settings = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}
settings["compute_enabled"] = False
cfg["settings"] = settings
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
PY

# 9) Carteira que recebe o que a VPS ganha: a do PAYOUT (gravada para os próximos deploys) ou a do próprio app
if [ -n "$PAYOUT" ]; then
  echo "$PAYOUT" > /opt/sigma/payout
elif [ -f /opt/sigma/payout ]; then
  PAYOUT="$(tr -d '[:space:]' < /opt/sigma/payout)"
  if ! [[ "$PAYOUT" =~ $ADDR_RE ]]; then
    echo "AVISO: /opt/sigma/payout não tem um endereço válido; ignorado"
    PAYOUT=""
  fi
fi
export PAYOUT

# 10) Nó primeiro; quando ele responder, a máquina de hospedagem
pm2 start deploy/ecosystem.config.cjs --only sigma-node --update-env
for i in $(seq 1 90); do curl -sf http://127.0.0.1:8700/status > /dev/null && break; sleep 1; done
if ! curl -sf http://127.0.0.1:8700/status > /dev/null; then
  echo "ERRO: o nó não respondeu em 90 s; veja: pm2 logs sigma-node"; exit 1
fi
pm2 start deploy/ecosystem.config.cjs --only sigma-hosting --update-env
pm2 save
sleep 5
pm2 jlist | python3 -c "import json,sys; [print(' ', p['name'], p['pm2_env']['status']) for p in json.load(sys.stdin) if p['name'].startswith('sigma')]"
python3 - << 'PY' || true
import json, urllib.request
get = lambda path: json.load(urllib.request.urlopen("http://127.0.0.1:8700" + path, timeout=5))
s, m = get("/status"), get("/mining")
print(f"  altura {s['height']} · época {m['epoch']} · impresso {m['minted'] / 1e6:,.2f} de {m['cap'] / 1e6:,.0f} SIG")
PY
if [ -n "$PAYOUT" ]; then
  echo "  o que a VPS ganhar hospedando vai para $PAYOUT"
else
  python3 -c 'import json; print("  carteira da própria VPS:", json.load(open("/opt/sigma/data/host/config.json"))["wallet"]["address"], "(para usar outra: PAYOUT=sig… bash deploy/deploy_vps.sh)")' 2> /dev/null || true
fi
ls -la /opt/sigma/downloads | sed 's/^/  /'
REMOTE

echo "==> Pronto: https://$DOMAIN  (downloads em https://$DOMAIN/baixar)"
