"""Sigma Code: agente de programação que roda em cima da rede Sigma (no terminal ou na aba Programar do app).

Cada "pensamento" do agente é um pedido de IA pago em SIG, respondido pelo computador de outra pessoa da rede
que esteja com o modelo carregado. Essa máquina vê tudo o que o agente lê e sugere tudo o que ele faz. Por isso:
- nada é gravado nem editado sem você ver (a não ser que você mesmo libere) e todo comando passa por você;
- arquivos de senha e chaves (.env, *.key, *.pem, .ssh/, .git/…) nunca são lidos nem alterados;
- tudo fica dentro da pasta de trabalho.

    python -m sigma.sigmacode --wallet ../eu.key --dir meu-projeto "crie um servidor flask em app.py"
    python -m sigma.sigmacode --wallet ../eu.key --dir meu-projeto --model qwen2.5-coder-7b   # modo interativo

Sem --model, usa um modelo de código que esteja no ar (ou o modelo com mais máquinas).
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading

from . import netsafe
from .client import CONTEXT, DEFAULT_NODE, Client, SigmaError, estimate, fmt
from .crypto import Wallet

SYSTEM = """Você é o Sigma Code, um agente de programação que trabalha na pasta do usuário.
SIGMA_CODE_PROTOCOL
Responda SEMPRE com exatamente UM objeto JSON e nada mais. Opções:
{"tool": "list_dir", "args": {"path": "."}}
{"tool": "read_file", "args": {"path": "arquivo"}}
{"tool": "write_file", "args": {"path": "arquivo", "content": "conteúdo completo"}}
{"tool": "edit_file", "args": {"path": "arquivo", "old": "trecho exato", "new": "substituto"}}
{"tool": "run_command", "args": {"command": "comando de shell"}}
{"final": "resumo curto do que foi feito"}
Caminhos sempre relativos à pasta. Arquivos de senha e chaves (.env, .key, .pem, .ssh, .git) são protegidos.
O usuário confere cada alteração e cada comando antes de acontecer e pode recusar.
Depois de cada ferramenta você recebe uma mensagem TOOL_RESULT. Trabalhe em passos pequenos,
confira o resultado e termine com "final"."""

MAX_OUTPUT = 8000                 # saída de ferramenta (o que vai para a máquina ainda é cortado para caber)
MAX_READ = 200 * 1024             # read_file lê no máximo 200 KB
MAX_EDIT = 2 * 1024 * 1024        # edit_file só em arquivos de até 2 MB
MAX_COMMAND = 2000                # comando maior que isso não dá para conferir com calma
PREVIEW_MAX, PREVIEW_LINES = 4000, 40
CMD_TIMEOUT = 120
REFUSED = "o usuário recusou"
PROTECTED = "arquivo protegido: o Sigma Code não lê nem altera este arquivo"
SURROGATE = ("a resposta da máquina tem caracteres inválidos (um emoji ou símbolo quebrado): nada foi gravado. "
             "Peça para a máquina enviar o texto de novo.")
ONE_COMMAND = "um comando por vez"   # vários comandos numa resposta escondem linhas da prévia; o sh roda todas
WARNING = ("Aviso: quem responde é o computador de outra pessoa da rede. Tudo o que o Sigma Code ler desta pasta "
           "vai para essa máquina. Não use em pastas com senhas, chaves ou documentos pessoais.")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # Windows: comando sem janela de console piscando

# arquivos de segredo: nunca saem do computador (sem diferenciar maiúsculas, em cada parte do caminho)
_SECRET_NAMES = {".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials"}
_SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".git", ".hg", ".svn"}   # .git/hooks roda código: nem ler nem gravar
_SECRET_EXT = (".key", ".pem", ".p12", ".pfx", ".secret")
_SECRET_PREFIX = (".env.", "id_rsa", "id_ed25519")
# caracteres que escondem ou embaralham texto no terminal (ESC, \r, direção do texto, largura zero, quebras de
# linha "invisíveis"): aparecem escapados, para a prévia mostrar exatamente o que vai ser gravado ou executado
_HIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")


_SURROGATE = re.compile("[\ud800-\udfff]")   # metade de par substituto (emoji quebrado): não grava em UTF-8


def _has_surrogate(*values):
    """Algum argumento da IA tem metade de par substituto (JSON \\udXXX solto)? Gravar isso em UTF-8 estouraria,
    e no v2.1 o arquivo já tinha sido esvaziado antes do erro."""
    return any(isinstance(v, str) and _SURROGATE.search(v) for v in values)


def _atomic_write(full, data):
    """Grava `data` (bytes) de forma atômica: arquivo temporário na mesma pasta + os.replace. Como os bytes já
    vêm prontos (o encode falha antes, sem tocar o arquivo), o original nunca fica truncado nem pela metade."""
    folder = os.path.dirname(full) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".sigma-tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, full)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def protected_name(part):
    """Uma parte do caminho é de segredo? O Windows ignora ponto e espaço no fim do nome (".env." abre o .env)."""
    p = str(part).rstrip(" .").lower()
    return p in _SECRET_NAMES or p in _SECRET_DIRS or p.endswith(_SECRET_EXT) or p.startswith(_SECRET_PREFIX)


def _parts(path):
    return [x for x in re.split(r"[\\/]+", path) if x]


# nomes que o Windows abre como dispositivo, não como arquivo (ler "CON" esperaria o teclado para sempre)
_DEVICES = ({"con", "prn", "aux", "nul", "conin$", "conout$"}
            | {p + d for p in ("com", "lpt") for d in "0123456789" + chr(0xB9) + chr(0xB2) + chr(0xB3)})


def _device(part):
    """'CON', 'nul.txt', 'COM1 ': nome reservado do Windows."""
    return os.name == "nt" and str(part).split(".")[0].rstrip(" ").lower() in _DEVICES


def _kill_tree(p):
    """Fecha o processo e todos os que ele abriu: com shell=True, matar só o shell deixaria os filhos rodando."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, creationflags=_NO_WINDOW)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        p.kill()
    except OSError:
        pass


def _inside(path, root):
    """`path` (já resolvido com realpath) está dentro de `root`, ou é ela?"""
    a, b = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:          # outra unidade
        return False


def visible(text):
    """Texto que veio da IA, pronto para mostrar: nada de caractere que esconda o que vem depois, e nenhuma
    metade de par substituto (emoji quebrado) que quebraria o terminal ou a serialização JSON do painel."""
    return _SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}",
                          _HIDDEN.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 256
                                      else f"\\u{ord(m.group()):04x}", str(text)))


def _print_event(ev):
    """Saída no terminal (CLI), quando ninguém passou on_event."""
    kind = ev.get("kind")
    if kind == "step":
        print(f"  · passo {ev['n']}: {fmt(ev.get('cost') or 0)} pagos à máquina {str(ev.get('machine') or '?')[:12]}")
    elif kind == "tool":
        args = visible(json.dumps(ev.get("args"), ensure_ascii=False)[:120])
        print(f"  → {visible(ev.get('tool'))} {args}" + (f" · {ev['output']}" if ev.get("refused") else ""))
    elif kind == "final":
        print(f"\n✔ {visible(ev['text'])}")
    elif kind == "error":
        print(f"\nerro: {visible(ev['text'])}")
    elif kind == "limit":
        print("\n(limite de passos atingido)")


class Agent:
    def __init__(self, client, model, max_tokens=2048, max_steps=15, auto_yes=False, root=".",
                 confirm=None, on_event=None, should_stop=None, auto_files=False, protect=()):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.max_steps = max_steps
        self.auto_yes = auto_yes          # comandos sem perguntar: só a CLI, com --yes
        self.auto_files = auto_files      # gravar/editar sem perguntar: começa desligado
        self.root = os.path.realpath(root)
        self.confirm = confirm            # confirm(tool, args, info) -> bool; None = pergunta no terminal
        self.on_event = on_event          # on_event(evento); None = imprime no terminal
        self.should_stop = should_stop
        self.protect = [os.path.normcase(os.path.realpath(p)) for p in protect or ()]
        # pasta de trabalho dentro de uma pasta de segredo (ex.: ~/.ssh): tudo nela fica protegido
        self._secret_root = any(protected_name(x) for x in _parts(os.path.splitdrive(self.root)[1]))
        self.spent = 0
        self.changed = []                 # arquivos gravados/editados nesta tarefa (caminhos relativos)

    # ------------------------------------------------------------ caminhos
    def _resolve(self, p):
        """Caminho pedido pela IA -> (caminho real, relativo com '/'). Recusa o que sair da pasta de trabalho."""
        if not isinstance(p, str) or not p.strip():
            raise ValueError("caminho vazio")
        # absoluto, outra unidade (C:x), UNC (\\servidor) e fluxo alternativo do NTFS (arquivo:fluxo)
        if "\x00" in p or ":" in p or p.startswith(("/", "\\")) or os.path.isabs(p):
            raise ValueError("caminho fora da pasta de trabalho: use um caminho relativo, dentro dela")
        if any(_device(x) for x in _parts(p)):
            raise ValueError("nome reservado do Windows (CON, NUL, COM1…): escolha outro nome")
        full = os.path.realpath(os.path.join(self.root, p))
        if not _inside(full, self.root):
            raise ValueError("caminho fora da pasta de trabalho")
        try:
            rel = os.path.relpath(full, self.root)
        except ValueError:            # o Windows transformou o nome num dispositivo
            raise ValueError("caminho fora da pasta de trabalho") from None
        return full, ("." if rel == os.curdir else rel.replace(os.sep, "/"))

    def _secret(self, full, *asked):
        """Arquivo de segredo? Confere o caminho pedido e o caminho real (link simbólico, nome curto 8.3)."""
        if self._secret_root:
            return True
        f = os.path.normcase(full)
        if any(f == p or f.startswith(p.rstrip("\\/") + os.sep) for p in self.protect):
            return True
        parts = [x for a in asked if isinstance(a, str) for x in _parts(os.path.normpath(a))]
        if _inside(full, self.root):
            parts += _parts(os.path.relpath(full, self.root))
        return any(protected_name(x) for x in parts)

    def _same_place(self, full, path):
        """Depois da aprovação (que pode demorar), o caminho ainda leva ao mesmo lugar, dentro da pasta e fora
        dos segredos? Uma pasta trocada por um link no meio do caminho não desvia a gravação."""
        try:
            again, _ = self._resolve(path)
        except ValueError:
            return False
        return os.path.normcase(again) == os.path.normcase(full) and not self._secret(again, path)

    # ------------------------------------------------------------ aprovação
    def _ask(self, tool, args, reason, preview, target=None):
        """Pergunta antes de gravar, editar ou executar. `preview` já vem com `visible` aplicado. `target` é o
        alvo real resolvido (caminho), para o cartão mostrar o arquivo certo e não uma chave livre dos args."""
        if len(preview) > PREVIEW_MAX:
            preview = preview[:PREVIEW_MAX - 70] + "\n… (prévia cortada: confira o conteúdo completo antes de aprovar)"
        info = {"reason": reason, "preview": preview, "changed": list(self.changed),
                "target": visible(target) if target is not None else None}
        try:
            return (self.confirm or self._ask_terminal)(tool, args, info) is True
        except Exception:
            return False                  # quem pergunta falhou (ex.: sem terminal): na dúvida, não faz

    @staticmethod
    def _ask_terminal(tool, args, info):
        print(info["preview"])
        path = args.get("path") if isinstance(args, dict) else ""
        if info["reason"] == "command":
            if info["changed"]:
                print("  arquivos alterados nesta tarefa: " + ", ".join(visible(c) for c in info["changed"]))
            q = ("  Executar este comando no seu computador? Ele foi escrito pela IA de outra pessoa da rede. "
                 "Só execute se você entende o que ele faz. [s/N] ")
        elif info["reason"] == "write":
            n = len(args.get("content") or "")
            q = (f"  A máquina que respondeu (o computador de outra pessoa da rede) quer gravar {visible(path)} "
                 f"({n} caracteres). Confira o conteúdo acima. Gravar? [s/N] ")
        else:
            q = f"  A máquina que respondeu quer alterar {visible(path)} (trecho antigo e novo acima). Alterar? [s/N] "
        try:
            return input(q).strip().lower() in ("s", "sim", "y", "yes")
        except (EOFError, OSError, RuntimeError):   # sem terminal: na dúvida, não faz
            return False

    @staticmethod
    def _write_preview(rel, content, exists):
        lines = content.splitlines()
        more = f"\n… (mais {len(lines) - PREVIEW_LINES} linhas)" if len(lines) > PREVIEW_LINES else ""
        what = "substitui o arquivo atual" if exists else "arquivo novo"
        return (f"{visible(rel)} · {len(content)} caracteres · {what}\n"
                + visible("\n".join(lines[:PREVIEW_LINES])) + more)

    @staticmethod
    def _edit_preview(rel, old, new):
        rel, old, new = visible(rel), visible(old), visible(new)
        half = (PREVIEW_MAX - len(rel) - 120) // 2   # o trecho novo sempre aparece, por maior que seja o antigo

        def cut(s):
            return s if len(s) <= half else s[:half] + f"\n… (cortado: {len(s)} caracteres)"
        return f"{rel}\n--- trecho atual ---\n{cut(old)}\n--- trecho novo ---\n{cut(new)}"

    def _changed_add(self, rel):
        if rel not in self.changed:
            self.changed.append(rel)

    def target_of(self, name, args):
        """Alvo real que a ferramenta toca, já resolvido e visível, para o título do cartão e a conversa (nunca
        de uma chave livre dos args). run_command e ferramentas sem caminho não têm alvo."""
        if name not in ("list_dir", "read_file", "write_file", "create_file", "edit_file"):
            return None
        if not isinstance(args, dict):
            return None
        path = args.get("path") or ("." if name == "list_dir" else None)
        try:
            _, rel = self._resolve(path)
        except (ValueError, TypeError):
            return None
        return visible(rel)

    # ------------------------------------------------------------ ferramentas
    def tool(self, name, args):
        """Roda uma ferramenta pedida pela IA -> (saída, recusada)."""
        if not isinstance(args, dict):
            return "erro: 'args' deve ser um objeto JSON", False
        try:
            if name == "list_dir":
                return self._list_dir(args.get("path") or ".")
            if name == "read_file":
                return self._read_file(args.get("path"))
            if name in ("write_file", "create_file"):  # modelos pequenos às vezes inventam "create_file"
                return self._write_file(name, args)
            if name == "edit_file":
                return self._edit_file(name, args)
            if name == "run_command":
                return self._run_command(name, args)
        except FileNotFoundError:
            return "erro: arquivo ou pasta não existe", False
        except UnicodeDecodeError:
            return "erro: o arquivo não está em UTF-8", False
        except OSError as e:          # sem o caminho completo: ele mostraria o nome do usuário do Windows
            return f"erro: {e.strerror or type(e).__name__}", False
        except (ValueError, TypeError) as e:
            return f"erro: {e}", False
        return f"erro: ferramenta desconhecida '{visible(name)[:40]}'", False

    def _list_dir(self, path):
        full, rel = self._resolve(path)
        if self._secret(full, path):
            return PROTECTED, True
        names = []
        for e in sorted(os.listdir(full)):
            f = os.path.join(full, e)
            if self._secret(os.path.realpath(f), e):
                continue                  # arquivos de segredo nem aparecem na lista
            names.append(e + ("/" if os.path.isdir(f) else ""))
        return "\n".join(names) or "(vazio)", False

    def _read_file(self, path):
        full, rel = self._resolve(path)
        if self._secret(full, path):
            return PROTECTED, True
        with open(full, "rb") as f:
            data = f.read(MAX_READ + 1)
        text = data[:MAX_READ].decode("utf-8", "replace").replace("\r\n", "\n")
        if len(data) > MAX_READ:
            text += f"\n… (arquivo cortado: mostrei só os primeiros {MAX_READ // 1024} KB)"
        return text, False

    def _write_file(self, name, args):
        path, content = args.get("path"), args.get("content")
        if not isinstance(content, str):
            return "erro: write_file precisa de 'path' e 'content' (conteúdo completo do arquivo)", False
        if _has_surrogate(path, content):     # recusa antes de perguntar: gravar isso esvaziaria o arquivo
            return SURROGATE, True
        full, rel = self._resolve(path)
        if self._secret(full, path):
            return PROTECTED, True
        if os.path.isdir(full):
            return f"erro: {rel} é uma pasta", False
        data = content.encode("utf-8")        # encode antes de tocar o arquivo; newline="" por serem bytes crus
        if not self.auto_files and not self._ask(name, args, "write",
                                                 self._write_preview(rel, content, os.path.exists(full)), target=rel):
            return REFUSED, True
        if not self._same_place(full, path):
            return "erro: o caminho mudou enquanto o usuário decidia; nada foi gravado", False
        _atomic_write(full, data)             # temporário + os.replace: o arquivo nunca fica truncado
        self._changed_add(rel)
        return f"ok, {len(content)} caracteres escritos em {rel}", False

    def _edit_file(self, name, args):
        path, old, new = args.get("path"), args.get("old"), args.get("new")
        if not (isinstance(old, str) and old and isinstance(new, str)):
            return "erro: edit_file precisa de 'path', 'old' (trecho exato, não vazio) e 'new'", False
        if _has_surrogate(path, old, new):    # recusa antes de perguntar: gravar isso esvaziaria o arquivo
            return SURROGATE, True
        full, rel = self._resolve(path)
        if self._secret(full, path):
            return PROTECTED, True
        if os.path.getsize(full) > MAX_EDIT:
            return "erro: arquivo grande demais para editar (máximo 2 MB)", False
        with open(full, encoding="utf-8", newline="") as f:   # newline="": preserva \r\n/\n como estão no arquivo
            text = f.read()
        # o modelo vê o arquivo com \n (read_file normaliza): casa old/new no fim de linha real do arquivo
        eol = "\r\n" if "\r\n" in text else "\n"
        old_m = old.replace("\r\n", "\n").replace("\n", eol)
        new_m = new.replace("\r\n", "\n").replace("\n", eol)
        if text.count(old_m) != 1:
            return "erro: o trecho 'old' precisa aparecer exatamente uma vez", False
        data = text.replace(old_m, new_m).encode("utf-8")     # encode antes de tocar o arquivo
        if not self.auto_files and not self._ask(name, args, "edit", self._edit_preview(rel, old, new), target=rel):
            return REFUSED, True
        if not self._same_place(full, path):
            return "erro: o caminho mudou enquanto o usuário decidia; nada foi alterado", False
        with open(full, encoding="utf-8", newline="") as f:
            if f.read() != text:          # você mexeu no arquivo enquanto decidia: não passa por cima
                return "erro: o arquivo mudou enquanto o usuário decidia; nada foi alterado", False
        _atomic_write(full, data)         # temporário + os.replace: o arquivo nunca fica truncado
        self._changed_add(rel)
        return "ok, arquivo editado", False

    def _run_command(self, name, args):
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return "erro: run_command precisa de 'command'", False
        if _has_surrogate(cmd):
            return SURROGATE, True
        shown = visible(cmd)
        lines = shown.split("\n")         # visible() não esconde \n: a prévia mostra cada linha
        if len(lines) > 1:                # cmd.exe para na 1ª linha; o sh roda todas: um comando por vez
            marked = " ⏎\n".join(lines)   # mostra as quebras de linha e o número de linhas, nada fica fora da prévia
            return (f"{ONE_COMMAND}: este comando tem {len(lines)} linhas; peça uma de cada vez.\n{marked}", True)
        if len(shown) > MAX_COMMAND:      # a prévia nunca esconde o fim de um comando
            return f"erro: comando longo demais para conferir (máximo {MAX_COMMAND} caracteres)", False
        if not self.auto_yes and not self._ask(name, args, "command", shown):
            return REFUSED, True
        try:
            p = subprocess.Popen(cmd, shell=True, cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace", creationflags=_NO_WINDOW,
                                 start_new_session=os.name != "nt")
        except OSError as e:
            return f"erro: o comando não pôde começar ({e.strerror or type(e).__name__})", False
        parts, size = [], [0]

        def pump():                       # guarda só o começo da saída, mas lê tudo (o comando não trava)
            try:
                for chunk in iter(lambda: p.stdout.read(4096), ""):
                    if size[0] < MAX_OUTPUT:
                        parts.append(chunk)
                        size[0] += len(chunk)
            except (OSError, ValueError):
                pass
            finally:
                p.stdout.close()
        reader = threading.Thread(target=pump, name="sigma-code-saida", daemon=True)
        reader.start()
        try:
            p.wait(timeout=CMD_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(p)                 # o comando e o que ele abriu (ex.: um servidor que nunca termina)
            reader.join(10)
            return f"erro: o comando passou de {CMD_TIMEOUT} s e foi interrompido", False
        reader.join(10)
        return f"exit={p.returncode}\n{''.join(parts)}"[:MAX_OUTPUT], False

    # ------------------------------------------------------------ conversa
    @staticmethod
    def parse(text):
        start = text.find("{")
        if start < 0:
            return {"final": text.strip()}
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            return obj if isinstance(obj, dict) else {"final": text.strip()}
        except json.JSONDecodeError:
            return {"final": text.strip()}

    def _tool_result(self, messages, name, out):
        """Saída da ferramenta cortada para caber no contexto com o protocolo e a tarefa, sobrando ~1/3 para o
        histórico (senão o pedido seguinte nem sairia daqui)."""
        header = f"TOOL_RESULT ({visible(name)[:40]}):\n"
        answer = max(1, min(int(self.max_tokens), CONTEXT // 2))
        room = (CONTEXT - answer - 32 - estimate(messages[:2]) - 8) * 3 - len(header) - 80
        limit = max(200, min(MAX_OUTPUT, room * 2 // 3))
        if len(out) > limit:
            out = out[:limit] + f"\n… (cortado: {len(out)} caracteres no total)"
        return header, out

    def _emit(self, ev):
        (self.on_event or _print_event)(ev)

    def _stopped(self):
        return bool(self.should_stop and self.should_stop())

    def run(self, task):
        """Faz a tarefa e devolve o texto final (None se parou, deu erro ou chegou ao limite de passos)."""
        self.changed, self.spent = [], 0
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": str(task)}]
        for n in range(1, self.max_steps + 1):
            if self._stopped():
                return None
            try:
                text, job = self.client.ask(self.model, messages, self.max_tokens, json_mode=True,
                                            should_stop=self.should_stop)
            except Exception as e:
                if self._stopped():       # pedido cancelado porque você mandou parar
                    return None
                self._emit({"kind": "error", "text": str(e) if isinstance(e, SigmaError)
                            else f"erro inesperado: {type(e).__name__}: {e}"})
                return None
            cost = job.get("cost") or 0
            self.spent += cost
            self._emit({"kind": "step", "n": n, "cost": cost, "machine": job.get("machine")})
            self._emit({"kind": "assistant", "text": text})
            messages.append({"role": "assistant", "content": text})
            action = self.parse(text)
            if "final" in action:
                final = str(action["final"])
                self._emit({"kind": "final", "text": final})
                return final
            name, args = action.get("tool"), action.get("args", {})
            target = self.target_of(name, args)   # alvo resolvido (não uma chave livre dos args), para a conversa
            if self._stopped():           # você mandou parar: o que a IA pediu neste passo não acontece
                self._emit({"kind": "tool", "tool": name, "args": args, "output": REFUSED, "refused": True,
                            "target": target})
                return None
            out, refused = self.tool(name, args)
            header, out = self._tool_result(messages, name, out)
            self._emit({"kind": "tool", "tool": name, "args": args, "output": out, "refused": refused,
                        "target": target})
            messages.append({"role": "user", "content": header + out})
        self._emit({"kind": "limit"})
        return None


def _friendly(e):
    if netsafe.cert_problem(e) and sys.platform == "darwin":
        return ("O Python deste Mac não tem os certificados de segurança. Abra Aplicativos › Python 3.x › "
                "Install Certificates.command e rode o Sigma Code de novo.")
    return str(e)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    netsafe.install_default_opener()
    ap = argparse.ArgumentParser(prog="sigma.sigmacode", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", nargs="?")
    ap.add_argument("--node", default=os.environ.get("SIGMA_NODE", DEFAULT_NODE), help="URL do nó (padrão: rede pública)")
    ap.add_argument("--wallet", default=os.environ.get("SIGMA_WALLET", "wallet.key"),
                    help="arquivo da carteira (precisa ficar fora da pasta de trabalho)")
    ap.add_argument("--model", help="modelo da rede (padrão: um modelo de código que esteja no ar)")
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--max-steps", type=int, default=15)
    ap.add_argument("--dir", default=".", help="pasta do projeto: o Sigma Code só mexe dentro dela")
    ap.add_argument("--yes", action="store_true",
                    help="executa comandos sem perguntar (cuidado: quem escreve os comandos é a IA de outra pessoa)")
    ap.add_argument("--yes-files", action="store_true", help="grava e edita arquivos sem perguntar")
    args = ap.parse_args(argv)
    root = os.path.realpath(args.dir)
    if not os.path.isdir(root):
        sys.exit(f"pasta não encontrada: {args.dir}")
    home = os.path.realpath(os.path.expanduser("~"))
    if os.path.dirname(root) == root or os.path.normcase(root) == os.path.normcase(home):
        sys.exit("escolha a pasta do projeto com --dir: o Sigma Code não trabalha na raiz do disco nem na sua "
                 "pasta de usuário inteira")
    if not os.path.exists(args.wallet):
        sys.exit(f"carteira '{args.wallet}' não encontrada (crie com: python -m sigma.cli wallet)")
    wallet_file = os.path.realpath(args.wallet)
    if _inside(wallet_file, root):
        sys.exit(f"A sua carteira ({args.wallet}) está dentro da pasta de trabalho. Mova o arquivo para fora ou "
                 "escolha outra pasta com --dir: tudo o que o Sigma Code lê vai para a máquina que responde.")
    try:
        wallet = Wallet.load(args.wallet)
    except (ValueError, KeyError, OSError):
        sys.exit(f"arquivo de carteira inválido: {args.wallet}")
    print(WARNING)
    client = Client(args.node, wallet)
    try:
        model = args.model or client.pick_model(prefer="coder")  # a rede decide onde cada modelo roda
        agent = Agent(client, model, args.max_tokens, args.max_steps, args.yes, root,
                      auto_files=args.yes_files, protect=[wallet_file])
        print(f"Sigma Code · modelo {model} · pasta {root} · saldo {fmt(client.balance())}")
        if args.task:
            agent.run(args.task)
            print(f"  total gasto nesta tarefa: {fmt(agent.spent)}")
            return
        while True:
            task = input("\nσ> ").strip()
            if task in ("sair", "exit", "quit"):
                break
            if task:
                agent.run(task)
                print(f"  total gasto nesta tarefa: {fmt(agent.spent)}")
    except (SigmaError, KeyboardInterrupt, EOFError) as e:
        print(f"\n{_friendly(e)}")


if __name__ == "__main__":
    main()
