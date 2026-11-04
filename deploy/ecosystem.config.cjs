// Processos do Sigma no pm2 (mesmo gerenciador do GardenCounties neste servidor).
// O nó fica só em 127.0.0.1: o acesso público passa pelo nginx (HTTPS).
// Não existe faucet: todo SIG nasce da mineração. A VPS não minera nem roda IA, para não disputar
// CPU com o GardenCounties; ela valida os blocos, serve o site/downloads e hospeda sites da rede.
const DATA = "/opt/sigma/data";
// Carteira que recebe o que a VPS ganha hospedando (o deploy_vps.sh passa PAYOUT, já conferido).
// Sem ela, o app cria e guarda uma carteira própria em /opt/sigma/data/host.
const PAYOUT = (process.env.PAYOUT || "").trim();

module.exports = {
  apps: [
    {
      // Bloco com transações sai na hora; sem elas, um bloco vazio a cada 30 s (só para o relógio andar).
      // Limites por conexão de internet (IP; no IPv6, o bloco /64), por janela de 5 min: 256 relatórios de
      // mineração aceitos e 8 recusados (o recusado não barra o válido: CGNAT e famílias com vários PCs),
      // 300 transações assinadas (inclui as da MetaMask) e 500 MB de arquivos novos por hora.
      name: "sigma-node",
      cwd: "/opt/sigma/app",
      script: "python3",
      args: `-m sigma.node run --dir ${DATA}/no --host 127.0.0.1 --port 8700 --producer --public ` +
            `--heartbeat 30 --blob-cap-gb 20 --downloads /opt/sigma/downloads ` +
            `--work-per-ip 256 --bad-work-per-ip 8 --tx-per-ip 300 --blob-mb-per-ip-hour 500`,
      interpreter: "none",
      env: { PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
      // A memória do nó não cresce com a chain (só os últimos 2.000 blocos ficam na RAM): este teto é rede de
      // segurança contra um vazamento, não um reinício de rotina.
      max_memory_restart: "1500M",
    },
    {
      // A máquina do próprio servidor: o mesmo app que as pessoas baixam, sem janela, sem minerar e sem IA.
      name: "sigma-hosting",
      cwd: "/opt/sigma/app",
      script: "python3",
      args: `-m sigma.app --headless --no-mine --no-ai --node http://127.0.0.1:8700 --data-dir ${DATA}/host ` +
            `--disk-gb 5 --name servidor-sigma` + (PAYOUT ? ` --wallet ${PAYOUT}` : ""),
      interpreter: "none",
      env: { PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
      max_memory_restart: "300M",
      kill_timeout: 15000,   // o app desliga com calma (para a máquina e fecha o painel local)
    },
  ],
};
