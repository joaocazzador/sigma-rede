"""Fórum da comunidade Sigma.

O login é feito com a carteira: o servidor manda uma mensagem com um código
aleatório, a pessoa assina com a MetaMask (ou com a carteira do navegador) e o
servidor confere quem assinou. Nenhuma senha é guardada aqui.

As conversas ficam fora da chain, num SQLite local do nó (não custam SIG).
"""
import re
import secrets
import sqlite3
import threading
import time

from .crypto import keccak256, norm_address, recover_pub, address_of

CATEGORIES = {
    "causa": "A causa: mobilização e ideias",
    "geral": "Conversa geral",
    "ajuda": "Ajuda e dúvidas",
    "provedores": "Máquinas (cedendo o computador)",   # a chave fica: tópicos antigos continuam nela
    "modelos": "Modelos de IA e padrinhos",
    "sites": "Sites na rede",
    "desenvolvimento": "Desenvolvimento do Sigma",
}
SESSION_DAYS = 30
CHALLENGE_TTL = 300
MAX_CHALLENGES = 10_000    # desafios de login guardados ao mesmo tempo (o mais antigo sai): memória limitada
POST_INTERVAL = 8          # segundos mínimos entre mensagens de uma mesma conta
HOST_BADGE = "PC na rede"  # quem cede o computador para a rede ("provedor" no Brasil é a operadora de internet)
NAME_RE = re.compile(r"^[\w.\- ]{3,24}$", re.UNICODE)


def hosts(st):
    """Carteiras que recebem por uma máquina online: o selo "PC na rede" (quem cede o computador para a rede).
    A chave da própria máquina ninguém vê nem usa para entrar no fórum, então o selo vai para o payout."""
    return {m.get("payout") for m in st.machines.values() if m.get("online")} - {None}


class ForumError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def personal_sign_address(message: str, signature: str) -> str:
    """Endereço que assinou `message` com personal_sign (EIP-191), como a MetaMask faz."""
    sig = bytes.fromhex(signature.removeprefix("0x"))
    if len(sig) != 65:
        raise ForumError("assinatura inválida")
    data = message.encode("utf-8")
    z = keccak256(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)
    v = sig[64] - 27 if sig[64] >= 27 else sig[64]
    try:
        return address_of(recover_pub(z, int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big"), v))
    except ValueError:
        raise ForumError("assinatura inválida")


class Forum:
    def __init__(self, path, chain_state):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.state = chain_state          # função que devolve o estado atual da chain
        self.challenges = {}              # endereço -> (mensagem, expira), na ordem em que foram pedidos
        self.clock = time.time            # os testes trocam
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS users (address TEXT PRIMARY KEY, name TEXT UNIQUE, created REAL);
                CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, address TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS threads (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT,
                    title TEXT, author TEXT, created REAL, last_post REAL, replies INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER,
                    author TEXT, body TEXT, created REAL);
                CREATE INDEX IF NOT EXISTS posts_thread ON posts(thread_id);
            """)

    # ------------------------------------------------------------ login
    def challenge(self, address):
        try:
            address = norm_address(address)
        except ValueError:
            raise ForumError("endereço inválido")
        now = self.clock()
        msg = ("Entrar no fórum da rede Sigma\n\n"
               f"Endereço: 0x{address[3:]}\n"
               f"Código: {secrets.token_hex(12)}\n"
               f"Emitido em: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(now))}\n\n"
               "Assinar esta mensagem não custa nada e não autoriza nenhuma transação.")
        with self.lock:
            ch = self.challenges
            ch.pop(address, None)          # o novo vai para o fim: a ordem do dicionário é a da expiração
            while ch:                      # do mais antigo: sai o que venceu e, com MAX_CHALLENGES, o que sobra
                first = next(iter(ch))
                if ch[first][1] > now and len(ch) < MAX_CHALLENGES:
                    break
                del ch[first]
            ch[address] = (msg, now + CHALLENGE_TTL)
        return msg

    def login(self, address, signature):
        try:
            address = norm_address(address)
        except ValueError:
            raise ForumError("endereço inválido")
        with self.lock:
            msg, expires = self.challenges.pop(address, (None, 0))
        if not msg or self.clock() > expires:
            raise ForumError("o código expirou; tente entrar de novo")
        if personal_sign_address(msg, signature) != address:
            raise ForumError("a assinatura não é desta carteira", 401)
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO users (address, name, created) VALUES (?, NULL, ?)",
                            (address, time.time()))
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token, address, time.time() + SESSION_DAYS * 86400))
        return token, self.profile(address)

    def logout(self, token):
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def who(self, token):
        if not token:
            return None
        with self.lock:
            row = self.db.execute("SELECT address FROM sessions WHERE token = ? AND expires > ?",
                                  (token, time.time())).fetchone()
        return row["address"] if row else None

    def require(self, token):
        addr = self.who(token)
        if not addr:
            raise ForumError("entre com sua carteira para participar", 401)
        return addr

    def set_name(self, address, name):
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ForumError("nome: 3 a 24 letras, números, espaço, ponto ou hífen")
        try:
            with self.lock, self.db:
                self.db.execute("UPDATE users SET name = ? WHERE address = ?", (name, address))
        except sqlite3.IntegrityError:
            raise ForumError("esse nome já está em uso")
        return self.profile(address)

    def profile(self, address):
        with self.lock:
            row = self.db.execute("SELECT name, created FROM users WHERE address = ?", (address,)).fetchone()
            posts = self.db.execute("SELECT COUNT(*) FROM posts WHERE author = ?", (address,)).fetchone()[0]
        st = self.state()
        badges = []
        if address in hosts(st):
            badges.append(HOST_BADGE)
        if any(m["sponsor"] == address for m in st.models.values()):
            badges.append("padrinho")
        if any(s["owner"] == address for s in st.sites.values()):
            badges.append("dono de site")
        return {"address": address, "name": row["name"] if row else None, "since": row["created"] if row else None,
                "posts": posts, "balance": st.balances.get(address, 0), "badges": badges}

    # ----------------------------------------------------------- conversas
    def _check_rate(self, address):
        with self.lock:
            last = self.db.execute("SELECT MAX(created) FROM posts WHERE author = ?", (address,)).fetchone()[0]
        if last and time.time() - last < POST_INTERVAL:
            raise ForumError(f"espere alguns segundos entre mensagens")

    @staticmethod
    def _body(body):
        body = (body or "").strip()
        if not 2 <= len(body) <= 5000:
            raise ForumError("a mensagem deve ter entre 2 e 5000 caracteres")
        return body

    def new_thread(self, address, category, title, body):
        if category not in CATEGORIES:
            raise ForumError("categoria inválida")
        title = (title or "").strip()
        if not 4 <= len(title) <= 120:
            raise ForumError("o título deve ter entre 4 e 120 caracteres")
        body = self._body(body)
        self._check_rate(address)
        now = time.time()
        with self.lock, self.db:
            cur = self.db.execute("INSERT INTO threads (category, title, author, created, last_post) VALUES (?,?,?,?,?)",
                                  (category, title, address, now, now))
            self.db.execute("INSERT INTO posts (thread_id, author, body, created) VALUES (?,?,?,?)",
                            (cur.lastrowid, address, body, now))
        return cur.lastrowid

    def reply(self, address, thread_id, body):
        body = self._body(body)
        self._check_rate(address)
        now = time.time()
        with self.lock, self.db:
            if not self.db.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone():
                raise ForumError("tópico não existe", 404)
            self.db.execute("INSERT INTO posts (thread_id, author, body, created) VALUES (?,?,?,?)",
                            (thread_id, address, body, now))
            self.db.execute("UPDATE threads SET last_post = ?, replies = replies + 1 WHERE id = ?", (now, thread_id))

    def _names(self, addresses):
        if not addresses:
            return {}
        with self.lock:
            rows = self.db.execute(f"SELECT address, name FROM users WHERE address IN ({','.join('?' * len(addresses))})",
                                   list(addresses)).fetchall()
        return {r["address"]: r["name"] for r in rows}

    def threads(self, category=None, limit=50):
        q = "SELECT * FROM threads" + (" WHERE category = ?" if category else "") + " ORDER BY last_post DESC LIMIT ?"
        with self.lock:
            rows = [dict(r) for r in self.db.execute(q, ([category] if category else []) + [limit]).fetchall()]
        names = self._names({r["author"] for r in rows})
        for r in rows:
            r["author_name"] = names.get(r["author"])
        return rows

    def thread(self, thread_id):
        with self.lock:
            t = self.db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if not t:
                raise ForumError("tópico não existe", 404)
            posts = [dict(r) for r in self.db.execute(
                "SELECT * FROM posts WHERE thread_id = ? ORDER BY id", (thread_id,)).fetchall()]
        authors = {p["author"] for p in posts}
        names = self._names(authors)
        st = self.state()
        online = hosts(st)
        badges = {a: [b for b, ok in ((HOST_BADGE, a in online),
                                       ("padrinho", any(m["sponsor"] == a for m in st.models.values()))) if ok]
                  for a in authors}
        for p in posts:
            p["author_name"] = names.get(p["author"])
            p["badges"] = badges[p["author"]]
        return {"thread": dict(t), "posts": posts}

    def stats(self):
        with self.lock:
            return {k: self.db.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0] for k in ("users", "threads", "posts")}
