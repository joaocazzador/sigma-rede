// Processos do Sigma no pm2 (mesmo gerenciador do GardenCounties neste servidor).
// O nó fica só em 127.0.0.1: o acesso público passa pelo nginx (HTTPS).
// Não existe faucet: todo SIG nasce da mineração. A VPS valida os blocos, serve o site/downloads, hospeda sites da
// rede e minera com todos os núcleos, mas só com a sobra: o app roda na classe "idle" do Linux (chrt --idle), que
// só ganha o processador quando nenhum outro programa o quer. O GardenCounties e o nó validador nunca esperam.
const DATA = "/opt/sigma/data";
// Carteira que recebe o que a VPS minera e ganha hospedando (o deploy_vps.sh passa PAYOUT, já conferido).
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
            `--work-per-ip 256 --bad-work-per-ip 8 --tx-per-ip 300 --blob-mb-per-ip-hour 500 ` +
            // v8: relé dos grupos (porta pública TCP): coordenador e trabalhadores atrás de roteador se encontram aqui;
            // o túnel do grupo passa cifrado por dentro, o relé só repassa bytes de membros que provam quem são
            `--relay-port 7790`,
      interpreter: "none",
      env: { PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
      // A memória do nó não cresce com a chain (só os últimos 2.000 blocos ficam na RAM): este teto é rede de
      // segurança contra um vazamento, não um reinício de rotina.
      max_memory_restart: "1500M",
    },
    {
      // A máquina do próprio servidor: o mesmo app que as pessoas baixam, sem janela, no máximo que a VPS aguenta
      // sem tocar no GardenCounties:
      // - minera com 100% dos núcleos e responde IA de texto e computação, tudo na classe idle do Linux (chrt
      //   --idle): os processos do minerador e o llama-server herdam a classe e só usam a sobra do processador;
      // - oom_score_adj 1000: se a memória acabar (a VPS não tem swap), o Linux mata o Sigma primeiro, nunca os apps
      //   do GardenCounties;
      // - memória para IA, imagem/vídeo e computação vêm de deploy/vps-settings.json (sem placa de vídeo e com 2
      //   núcleos, imagem e vídeo estourariam o prazo e tirariam reputação: ficam desligados).
      name: "sigma-hosting",
      cwd: "/opt/sigma/app",
      script: "sh",
      args: ["-c", `echo 1000 > /proc/self/oom_score_adj; exec chrt --idle 0 python3 -m sigma.app --headless ` +
             `--cpu 100 --node http://127.0.0.1:8700 --data-dir ${DATA}/host --disk-gb 60 --name servidor-sigma` +
             (PAYOUT ? ` --wallet ${PAYOUT}` : "")],
      interpreter: "none",
      env: { PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8", SIGMA_NO_AUTO_UPDATE: "1" },
      max_memory_restart: "800M",
      kill_timeout: 15000,   // o app desliga com calma (para a máquina e fecha o painel local)
    },
  ],
};
