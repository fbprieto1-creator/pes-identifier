"""
PES Embroidery Identifier
Converte .pes em PNG, usa Claude para identificar o bordado e renomeia ambos os arquivos.

Modos:
  claude-code  — usa o CLI `claude` local (sem API key), prompt via stdin
  api          — usa ANTHROPIC_API_KEY diretamente via SDK
"""

import os
import re
import base64
import subprocess
import threading
import time
import tempfile
from io import BytesIO
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import pyembroidery
from PIL import Image, ImageTk, ImageDraw


# ── Renderizador PES → PIL ────────────────────────────────────────────────────

def render_pattern(pattern: pyembroidery.EmbPattern, max_size: int = 600) -> Image.Image:
    stitches = pattern.stitches
    if not stitches:
        return Image.new("RGB", (400, 300), "white")

    xs = [s[0] for s in stitches]
    ys = [s[1] for s in stitches]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    w = max_x - min_x or 1
    h = max_y - min_y or 1
    scale = min(max_size / w, max_size / h, 3.0)
    pad = 40
    img = Image.new("RGB", (int(w * scale) + 2 * pad, int(h * scale) + 2 * pad), "white")
    draw = ImageDraw.Draw(img)

    threads = pattern.threadlist
    tidx = 0
    lw = max(1, int(scale * 0.15))

    def get_color(i: int) -> tuple:
        if threads and i < len(threads):
            c = threads[i].color
            r, g, b = (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF
            return (180, 180, 180) if r > 240 and g > 240 and b > 240 else (r, g, b)
        return (40, 40, 40)

    col = get_color(0)
    px = py = None
    jumping = False

    for s in stitches:
        sx, sy = s[0], s[1]
        cmd_s = s[2] if len(s) > 2 else pyembroidery.STITCH
        x = int((sx - min_x) * scale) + pad
        y = int((sy - min_y) * scale) + pad

        if cmd_s == pyembroidery.STITCH:
            if px is not None and not jumping:
                draw.line([(px, py), (x, y)], fill=col, width=lw)
            jumping = False
            px, py = x, y
        elif cmd_s == pyembroidery.COLOR_CHANGE:
            tidx += 1
            col = get_color(tidx)
            px = py = None
            jumping = False
        elif cmd_s == pyembroidery.JUMP:
            jumping = True
            px, py = x, y
        elif cmd_s == pyembroidery.TRIM:
            px, py = x, y
            jumping = False
        elif cmd_s == pyembroidery.END:
            break

    return img


# ── Utilitários Claude CLI ────────────────────────────────────────────────────

def _get_claude_cmd() -> list[str]:
    import shutil
    found = shutil.which("claude")
    if found:
        return ["cmd", "/c", found] if found.lower().endswith(".cmd") else [found]
    npm_path = Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"
    if npm_path.exists():
        return ["cmd", "/c", str(npm_path)]
    raise FileNotFoundError("Executável 'claude' não encontrado.\nInstale: https://claude.ai/code")


def _full_env() -> dict:
    env = os.environ.copy()
    try:
        import winreg
        machine_path = user_path = ""
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as k:
            machine_path = winreg.QueryValueEx(k, "PATH")[0]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            try:
                user_path = winreg.QueryValueEx(k, "PATH")[0]
            except FileNotFoundError:
                pass
        if machine_path or user_path:
            env["PATH"] = ";".join(filter(None, [user_path, machine_path, env.get("PATH", "")]))
    except Exception:
        pass
    return env


def _claude_available() -> bool:
    try:
        r = subprocess.run(_get_claude_cmd() + ["--version"],
                           capture_output=True, timeout=8, env=_full_env())
        return r.returncode == 0
    except Exception:
        return False


# ── Identificação ─────────────────────────────────────────────────────────────

# Padrões de erro retornados pelo Claude Code CLI
_ERROR_PATTERNS = [
    "session limit", "rate limit", "usage limit", "you've hit",
    "you have hit", "resets in", "too many requests",
    "overloaded", "at capacity", "quota exceeded",
]

# Prompt enviado via stdin — evita o limite de 8191 chars do cmd.exe no Windows
_PROMPT = """\
Use a ferramenta Read para abrir esta imagem de bordado: {path}

Depois de analisar a imagem, responda em portugues com EXATAMENTE este formato (2 linhas):
NOME: nome_do_arquivo
DESCRICAO: descricao breve do bordado

Regras para NOME:
- Letras minusculas sem acento (a-z), numeros (0-9) e underscore (_)
- Sem espacos, sem extensao, entre 5 e 30 caracteres
- Use no maximo 3 palavras separadas por underscore
- Descreva apenas o motivo principal (ex: flor, borboleta, coracao)
- Não usar uma cor como identificador secundario, a menos que seja essencial para diferenciar (ex: flor_azul vs flor_vermelha)



Exemplo de resposta:
NOME: borboleta
DESCRICAO: Bordado de borboleta com detalhes nas asas e corpo central. Ideal para decorar roupas ou acessórios com um toque delicado e natural.

Responda APENAS as 2 linhas acima, nada mais.
"""


def _sanitize_name(raw: str) -> str:
    """Normaliza acentos e caracteres inválidos → nome de arquivo seguro."""
    s = raw.strip().lower().splitlines()[0]
    s = re.sub(r"\.(pes|png|jpg|jpeg)$", "", s, flags=re.IGNORECASE)
    for chars, repl in [
        ("àáâãä", "a"), ("èéêë", "e"), ("ìíîï", "i"),
        ("òóôõö", "o"), ("ùúûü", "u"), ("ç", "c"), ("ñ", "n"),
    ]:
        for c in chars:
            s = s.replace(c, repl)
    s = re.sub(r"[^a-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    # Limita a 3 palavras (tokens separados por _)
    parts = s.split("_")[:3]
    s = "_".join(p for p in parts if p)
    return s[:30] if len(s) >= 3 else ""


def _is_error_response(text: str) -> bool:
    """Detecta mensagens de erro do Claude Code (limite de sessão, rate limit, etc.)."""
    lower = text.lower()
    return any(p in lower for p in _ERROR_PATTERNS)


def _parse_response(text: str) -> tuple[str, str]:
    """
    Extrai (nome, descricao) da resposta do Claude.
    Aceita formato NOME:/DESCRICAO: ou faz melhor esforço.
    """
    name = ""
    desc = ""
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("NOME:"):
            name = _sanitize_name(stripped[5:].strip())
        elif upper.startswith("DESCRICAO:") or upper.startswith("DESCRIÇÃO:"):
            desc = stripped.split(":", 1)[1].strip()

    # Fallback: primeira linha com formato de nome válido
    if not name:
        for line in text.splitlines():
            candidate = _sanitize_name(line.strip())
            if len(candidate) >= 5 and "_" in candidate:
                name = candidate
                break

    if not name:
        raise RuntimeError(f"Não foi possível extrair nome da resposta:\n{text[:300]}")

    return name, desc or text.strip()[:200]


def identify_claude_code(image: Image.Image) -> tuple[str, str]:
    """
    Salva PNG temporário, passa prompt via stdin ao 'claude --print'.
    Usar stdin em vez de -p "..." evita o limite de 8191 chars do cmd.exe.
    Retorna (nome, descricao).
    """
    fd, tmp_str = tempfile.mkstemp(suffix=".png", prefix="pes_ai_")
    os.close(fd)
    tmp_path = Path(tmp_str)
    try:
        image.save(str(tmp_path))
        prompt = _PROMPT.format(path=tmp_path.as_posix())

        cmd = _get_claude_cmd() + [
            "--print",
            "--dangerously-skip-permissions",
            "--allowedTools", "Read",
            "--add-dir", str(tmp_path.parent),
        ]
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            env=_full_env(),
        )
        output = result.stdout.strip()
        if not output:
            raise RuntimeError(result.stderr.strip() or "Claude não retornou resposta")

        if _is_error_response(output) and "NOME:" not in output.upper():
            raise RuntimeError(
                "Limite de sessão do Claude Code atingido.\n"
                "Aguarde alguns minutos e tente novamente, ou use o modo API key.\n\n"
                f"Mensagem: {output[:150]}"
            )

        return _parse_response(output)

    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def identify_openai(image: Image.Image) -> tuple[str, str]:
    """Identifica via OPENAI_API_KEY usando a API da OpenAI."""
    from openai import OpenAI

    buf = BytesIO()
    image.save(buf, format="PNG")
    img_b64 = base64.standard_b64encode(buf.getvalue()).decode()
    client = OpenAI()
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1")
    prompt = (
        "Analise este bordado. Responda em portugues com EXATAMENTE este formato:\n"
        "NOME: nome_sem_extensao_letras_minusculas_underscores\n"
        "DESCRICAO: descricao breve\n\n"
        "Regras para NOME:\n"
        "- Letras minusculas sem acento (a-z), numeros (0-9) e underscore (_)\n"
        "- Sem espacos, sem extensao, entre 5 e 30 caracteres\n"
        "- Use no maximo 3 palavras separadas por underscore\n"
        "- Descreva apenas o motivo principal\n\n"
        "Apenas as 2 linhas, sem mais texto."
    )
    response = client.responses.create(
        model=model,
        max_output_tokens=300,
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{img_b64}",
                    "detail": "high",
                },
            ],
        }],
    )
    return _parse_response(response.output_text)


def identify_openai_local(image: Image.Image) -> tuple[str, str]:
    """Identifica usando Ollama local com um modelo de visao."""
    import json
    import urllib.request

    local_image = image.convert("RGB")
    local_image.thumbnail((256, 256), Image.LANCZOS)
    buf = BytesIO()
    local_image.save(buf, format="PNG", optimize=True)
    img_b64 = base64.standard_b64encode(buf.getvalue()).decode()
    base_url = os.environ.get("OPENAI_LOCAL_BASE_URL", "http://localhost:11434").rstrip("/")
    model = os.environ.get("OPENAI_LOCAL_MODEL", "qwen3-vl:2b-instruct")
    prompt = (
        "Analise este bordado. Responda em portugues com EXATAMENTE este formato, apenas 2 linhas:\n"
        "NOME: nome_do_arquivo\n"
        "DESCRICAO: descricao breve do bordado\n\n"
        "Regras para NOME:\n"
        "- Letras minusculas sem acento (a-z), numeros (0-9) e underscore (_)\n"
        "- Sem espacos, sem extensao, entre 5 e 30 caracteres\n"
        "- Use no maximo 3 palavras separadas por underscore\n"
        "- Descreva apenas o motivo principal\n"
        "- Nao use a palavra bordado no nome\n"
        "- Nao use cores, salvo se forem essenciais para diferenciar\n\n"
        "Apenas as 2 linhas, sem mais texto."
    )
    payload = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": prompt,
            "images": [img_b64],
        }],
        "stream": False,
        "options": {
            "num_ctx": 1024,
            "num_predict": 160,
            "temperature": 0,
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data.get("message", {}).get("content", "")
    if not content:
        content = data.get("response", "")
    return _parse_response(content)


def identify_anthropic_api(image: Image.Image) -> tuple[str, str]:
    """Identifica via ANTHROPIC_API_KEY usando a API da Anthropic."""
    import anthropic

    buf = BytesIO()
    image.save(buf, format="PNG")
    img_b64 = base64.standard_b64encode(buf.getvalue()).decode()
    client = anthropic.Anthropic()
    model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")
    msg = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": img_b64,
            }},
            {"type": "text", "text": (
                "Analise este bordado. Responda em portugues com EXATAMENTE este formato:\n"
                "NOME: nome_sem_extensao_letras_minusculas_underscores\n"
                "DESCRICAO: descricao breve\n"
                "Apenas as 2 linhas, sem mais texto."
            )},
        ]}]
    )
    return _parse_response(msg.content[0].text)


def identify(image: Image.Image, mode: str) -> tuple[str, str]:
    if mode == "openai-local":
        return identify_openai_local(image)
    if mode == "anthropic":
        return identify_anthropic_api(image)
    if mode == "claude-code":
        return identify_claude_code(image)
    return identify_openai(image)


def _ai_mode_available(mode: str) -> bool:
    if mode == "openai-local":
        return True
    if mode == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if mode == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if mode == "claude-code":
        return _claude_available()
    return False


def _ai_mode_name(mode: str) -> str:
    return {
        "openai": "ChatGPT / OpenAI",
        "openai-local": "ChatGPT local / API local",
        "anthropic": "Anthropic API key",
        "claude-code": "Claude Code local",
    }.get(mode, mode)


# ── Aba: Arquivo Único ────────────────────────────────────────────────────────

class SingleFileTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=4)
        self.current_file: Path | None = None
        self.rendered_image: Image.Image | None = None
        self._photo = None
        self._build()

    def _build(self):
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(bar, text="Abrir .PES", command=self._open).pack(side=tk.LEFT, padx=2)
        self.btn_id = ttk.Button(bar, text="Identificar com IA",
                                 command=self._identify, state=tk.DISABLED)
        self.btn_id.pack(side=tk.LEFT, padx=2)
        self.btn_png = ttk.Button(bar, text="Salvar PNG",
                                  command=self._save_png, state=tk.DISABLED)
        self.btn_png.pack(side=tk.LEFT, padx=2)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Label(bar, text="Modo IA:").pack(side=tk.LEFT)
        self.ai_mode = tk.StringVar(value="openai")
        ttk.Radiobutton(bar, text="ChatGPT / OpenAI",
                        variable=self.ai_mode, value="openai").pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(bar, text="ChatGPT local",
                        variable=self.ai_mode, value="openai-local").pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(bar, text="Anthropic API key",
                        variable=self.ai_mode, value="anthropic").pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(bar, text="Claude Code local",
                        variable=self.ai_mode, value="claude-code").pack(side=tk.LEFT, padx=2)
        self.file_lbl = tk.StringVar(value="Nenhum arquivo aberto")
        ttk.Label(bar, textvariable=self.file_lbl, foreground="#555").pack(side=tk.LEFT, padx=10)

        pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        left = ttk.LabelFrame(pane, text="Visualização", padding=4)
        pane.add(left, weight=3)
        self.canvas = tk.Canvas(left, bg="#e8e8e8")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_text(200, 150, text="Abra um arquivo .PES",
                                fill="#aaa", font=("Arial", 11), tags="hint")
        self.canvas.bind("<Configure>", lambda _: self._draw())

        right = ttk.LabelFrame(pane, text="Resultado IA", padding=6)
        pane.add(right, weight=2)
        self.result_box = tk.Text(right, wrap=tk.WORD, font=("Arial", 10),
                                  state=tk.DISABLED, bg="#fafafa", relief=tk.FLAT)
        sb = ttk.Scrollbar(right, command=self.result_box.yview)
        self.result_box.config(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.result_box.pack(fill=tk.BOTH, expand=True)

        rename_bar = ttk.Frame(self)
        rename_bar.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(rename_bar, text="Renomear para:").pack(side=tk.LEFT)
        self.new_name = tk.StringVar()
        ttk.Entry(rename_bar, textvariable=self.new_name, width=38).pack(side=tk.LEFT, padx=4)
        ttk.Label(rename_bar, text=".pes").pack(side=tk.LEFT)
        self.btn_rename = ttk.Button(rename_bar, text="Renomear",
                                     command=self._rename, state=tk.DISABLED)
        self.btn_rename.pack(side=tk.LEFT, padx=6)

        self.status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status, foreground="#444").pack(anchor=tk.W)

    def _open(self):
        path = filedialog.askopenfilename(
            title="Abrir arquivo de bordado",
            filetypes=[("Bordado PES", "*.pes *.PES"), ("Todos", "*.*")]
        )
        if not path:
            return
        self.current_file = Path(path)
        self.file_lbl.set(self.current_file.name)
        self.new_name.set(self.current_file.stem)
        self.status.set("Carregando...")
        self.update()
        try:
            pattern = pyembroidery.read(path)
            self.rendered_image = render_pattern(pattern)
            self._draw()
            self.btn_id.config(state=tk.NORMAL)
            self.btn_png.config(state=tk.NORMAL)
            n = sum(1 for s in pattern.stitches if s[2] == pyembroidery.STITCH)
            self.status.set(f"{self.current_file.name}  —  {n:,} pontos")
        except Exception as exc:
            messagebox.showerror("Erro ao abrir", str(exc))
            self.status.set("Erro")

    def _save_png(self):
        if not self.rendered_image:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG", "*.png")],
            initialfile=self.current_file.stem if self.current_file else "bordado"
        )
        if path:
            self.rendered_image.save(path)
            self.status.set(f"PNG salvo: {Path(path).name}")

    def _identify(self):
        if not self.rendered_image:
            return
        mode = self.ai_mode.get()
        if not _ai_mode_available(mode):
            _warn_no_key(mode)
            return
        self.btn_id.config(state=tk.DISABLED)
        self.status.set(f"Enviando para {_ai_mode_name(mode)}...")
        threading.Thread(target=self._call_ai, args=(mode,), daemon=True).start()

    def _call_ai(self, mode: str):
        try:
            name, desc = identify(self.rendered_image, mode)
            self.after(0, self._show_result, name, desc)
        except Exception as exc:
            self.after(0, lambda: messagebox.showerror("Erro IA", str(exc)))
            self.after(0, lambda: self.btn_id.config(state=tk.NORMAL))

    def _show_result(self, name: str, desc: str):
        self.result_box.config(state=tk.NORMAL)
        self.result_box.delete("1.0", tk.END)
        self.result_box.insert(tk.END, f"Nome sugerido: {name}\n\n{desc}")
        self.result_box.config(state=tk.DISABLED)
        self.new_name.set(name)
        self.btn_id.config(state=tk.NORMAL)
        self.btn_rename.config(state=tk.NORMAL)
        self.status.set("Identificação concluída!")

    def _rename(self):
        if not self.current_file:
            return
        name = re.sub(r"[^\w\-]", "_", self.new_name.get().strip()).lower()
        if not name:
            messagebox.showwarning("Aviso", "Digite um nome válido.")
            return
        new_pes = self.current_file.parent / f"{name}.pes"
        if new_pes == self.current_file:
            return
        if new_pes.exists() and not messagebox.askyesno(
                "Substituir?", f"'{new_pes.name}' já existe. Substituir?"):
            return
        try:
            self.current_file.rename(new_pes)
            self.current_file = new_pes
            self.file_lbl.set(new_pes.name)
            self.status.set(f"Renomeado: {new_pes.name}")
        except Exception as exc:
            messagebox.showerror("Erro ao renomear", str(exc))

    def _draw(self):
        if not self.rendered_image:
            return
        self.canvas.delete("all")
        cw = max(self.canvas.winfo_width(), 200)
        ch = max(self.canvas.winfo_height(), 200)
        display = self.rendered_image.copy()
        display.thumbnail((cw - 10, ch - 10), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(display)
        self.canvas.create_image(cw // 2, ch // 2, image=self._photo, anchor=tk.CENTER)


# ── Aba: Lote Automático ──────────────────────────────────────────────────────

COLS = ("Arquivo original", "Pasta", "Status", "Nome novo")


class BatchTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=4)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._build()

    def _build(self):
        cfg = ttk.LabelFrame(self, text="Configuração", padding=8)
        cfg.pack(fill=tk.X, pady=(0, 6))

        row0 = ttk.Frame(cfg)
        row0.pack(fill=tk.X, pady=2)
        ttk.Label(row0, text="Pasta raiz:", width=14).pack(side=tk.LEFT)
        self.folder_var = tk.StringVar()
        ttk.Entry(row0, textvariable=self.folder_var, width=55).pack(side=tk.LEFT, padx=4)
        ttk.Button(row0, text="Escolher...", command=self._pick).pack(side=tk.LEFT)

        row1 = ttk.Frame(cfg)
        row1.pack(fill=tk.X, pady=4)
        self.opt_png = tk.BooleanVar(value=True)
        self.opt_ai = tk.BooleanVar(value=False)
        self.opt_rename = tk.BooleanVar(value=False)
        self.opt_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="Salvar PNG ao lado do .pes",
                        variable=self.opt_png).pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(row1, text="Identificar com IA",
                        variable=self.opt_ai, command=self._toggle_ai).pack(side=tk.LEFT, padx=6)
        self.chk_rename = ttk.Checkbutton(row1, text="Renomear arquivo",
                                          variable=self.opt_rename, state=tk.DISABLED)
        self.chk_rename.pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(row1, text="Sobrescrever PNG existente",
                        variable=self.opt_overwrite).pack(side=tk.LEFT, padx=6)

        row2 = ttk.Frame(cfg)
        row2.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row2, text="Modo IA:", width=14).pack(side=tk.LEFT)
        self.ai_mode = tk.StringVar(value="openai")
        self.rb_cc = ttk.Radiobutton(
            row2, text="ChatGPT / OpenAI",
            variable=self.ai_mode, value="openai", state=tk.DISABLED)
        self.rb_cc.pack(side=tk.LEFT, padx=4)
        self.rb_openai_local = ttk.Radiobutton(
            row2, text="ChatGPT local",
            variable=self.ai_mode, value="openai-local", state=tk.DISABLED)
        self.rb_openai_local.pack(side=tk.LEFT, padx=4)
        self.rb_api = ttk.Radiobutton(
            row2, text="Anthropic API key",
            variable=self.ai_mode, value="anthropic", state=tk.DISABLED)
        self.rb_api.pack(side=tk.LEFT, padx=4)
        self.rb_local = ttk.Radiobutton(
            row2, text="Claude Code local",
            variable=self.ai_mode, value="claude-code", state=tk.DISABLED)
        self.rb_local.pack(side=tk.LEFT, padx=4)

        ctrl = ttk.Frame(cfg)
        ctrl.pack(fill=tk.X, pady=(4, 0))
        self.btn_start = ttk.Button(ctrl, text="▶  Iniciar processamento", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=2)
        self.btn_stop = ttk.Button(ctrl, text="■  Parar", command=self._stop_proc,
                                   state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=2)
        self.lbl_count = tk.StringVar(value="")
        ttk.Label(ctrl, textvariable=self.lbl_count, foreground="#555").pack(side=tk.LEFT, padx=12)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(0, 4))

        tbl = ttk.LabelFrame(self, text="Resultados", padding=4)
        tbl.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(tbl, columns=COLS, show="headings", height=16)
        for col in COLS:
            self.tree.heading(col, text=col)
        self.tree.column("Arquivo original", width=200)
        self.tree.column("Pasta", width=200)
        self.tree.column("Status", width=80, anchor=tk.CENTER)
        self.tree.column("Nome novo", width=220)
        vsb = ttk.Scrollbar(tbl, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tbl, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.config(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.tag_configure("ok", foreground="#1a7a1a")
        self.tree.tag_configure("erro", foreground="#cc0000")
        self.tree.tag_configure("proc", foreground="#0055cc")

        self.status = tk.StringVar(value="Aguardando...")
        ttk.Label(self, textvariable=self.status, foreground="#444").pack(anchor=tk.W, pady=(2, 0))

    def _pick(self):
        path = filedialog.askdirectory(title="Selecione a pasta raiz")
        if path:
            self.folder_var.set(path)

    def _toggle_ai(self):
        state = tk.NORMAL if self.opt_ai.get() else tk.DISABLED
        self.chk_rename.config(state=state)
        self.rb_cc.config(state=state)
        self.rb_openai_local.config(state=state)
        self.rb_api.config(state=state)
        self.rb_local.config(state=state)
        if not self.opt_ai.get():
            self.opt_rename.set(False)

    def _start(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("Pasta inválida", "Selecione uma pasta válida.")
            return
        if self.opt_ai.get() and not _ai_mode_available(self.ai_mode.get()):
            _warn_no_key(self.ai_mode.get())
            return
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.lbl_count.set("")
        self.progress["value"] = 0
        self._stop.clear()
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        mode = self.ai_mode.get()
        self._thread = threading.Thread(target=self._run, args=(folder, mode), daemon=True)
        self._thread.start()

    def _stop_proc(self):
        self._stop.set()
        self.status.set("Parando após o arquivo atual...")

    def _run(self, folder: str, ai_mode: str):
        root = Path(folder)
        # Coleta arquivos .pes (case-insensitive, sem duplicatas)
        seen: set = set()
        files = []
        for f in sorted(root.rglob("*.pes")) + sorted(root.rglob("*.PES")):
            key = f.resolve()
            if key not in seen:
                seen.add(key)
                files.append(f)

        total = len(files)
        if total == 0:
            self.after(0, lambda: self.status.set("Nenhum arquivo .pes encontrado."))
            self.after(0, self._done)
            return

        self.after(0, lambda: self.progress.config(maximum=total))
        ok = err = 0

        for i, pes_path in enumerate(files):
            if self._stop.is_set():
                break

            rel = pes_path.relative_to(root)
            orig_name = pes_path.name
            self.after(0, self._add_row, orig_name, str(rel.parent))
            self.after(0, lambda v=i + 1: self.progress.config(value=v))
            self.after(0, lambda n=orig_name, d=i+1, t=total:
                       self.status.set(f"[{d}/{t}] {n}"))

            status_txt = "OK"
            tag = "ok"
            display = ""

            try:
                pattern = pyembroidery.read(str(pes_path))
                image = render_pattern(pattern)

                # Salvar PNG ao lado do .pes
                png_path = pes_path.with_suffix(".png")
                if self.opt_png.get():
                    if not png_path.exists() or self.opt_overwrite.get():
                        image.save(str(png_path))

                # Identificar com IA
                new_name = ""
                if self.opt_ai.get():
                    name, desc = identify(image, ai_mode)
                    new_name = name
                    display = name
                    if ai_mode == "openai":
                        time.sleep(0.3)

                # Renomear .pes (e .png se existir)
                if self.opt_rename.get() and new_name:
                    candidate = pes_path.parent / f"{new_name}.pes"
                    # Evita colisão
                    if candidate.exists() and candidate.resolve() != pes_path.resolve():
                        for n in range(2, 200):
                            candidate = pes_path.parent / f"{new_name}_{n}.pes"
                            if not candidate.exists():
                                break
                    pes_path.rename(candidate)
                    display = candidate.name
                    # Renomeia o PNG junto
                    if png_path.exists():
                        try:
                            png_path.rename(candidate.with_suffix(".png"))
                        except OSError:
                            pass
                ok += 1

            except Exception as exc:
                status_txt = "Erro"
                tag = "erro"
                display = str(exc)[:80]
                err += 1

            self.after(0, self._update_row, orig_name, str(rel.parent),
                       status_txt, display, tag)
            self.after(0, lambda o=ok, e=err:
                       self.lbl_count.set(f"OK: {o}  Erros: {e}"))

        self.after(0, self._done, ok, err, total)

    def _add_row(self, name: str, folder: str):
        iid = self.tree.insert("", tk.END, values=(name, folder, "...", ""),
                               tags=("proc",))
        self.tree.see(iid)

    def _update_row(self, name: str, folder: str, status: str, new_name: str, tag: str):
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            if vals[0] == name and vals[1] == folder:
                self.tree.item(iid, values=(name, folder, status, new_name), tags=(tag,))
                return

    def _done(self, ok: int = 0, err: int = 0, total: int = 0):
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        if total:
            self.status.set(f"Concluído — {total} arquivo(s): {ok} OK, {err} erro(s)")
        self._stop.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────

PROCESS_COLS = ("Original", "Pasta", "PNG", "Novo nome", "Descricao", "Status")


class QuickProcessTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=12)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._build()

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header, text="Processar pasta de bordados",
                  font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
        ttk.Label(
            header,
            text="Fluxo direto: converter .PES, gerar PNG, identificar com IA e renomear.",
            foreground="#555",
        ).pack(anchor=tk.W, pady=(2, 0))

        box = ttk.LabelFrame(self, text="Configuracao", padding=10)
        box.pack(fill=tk.X, pady=(0, 8))

        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row, text="Pasta:", width=10).pack(side=tk.LEFT)
        self.folder_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.folder_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(row, text="Escolher", command=self._pick).pack(side=tk.LEFT)
        ttk.Button(row, text="Abrir", command=self._open_folder).pack(side=tk.LEFT, padx=(6, 0))

        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=(0, 8))
        self.opt_png = tk.BooleanVar(value=True)
        self.opt_ai = tk.BooleanVar(value=True)
        self.opt_rename = tk.BooleanVar(value=True)
        self.opt_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Gerar PNG", variable=self.opt_png).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(row, text="Identificar com IA", variable=self.opt_ai,
                        command=self._toggle_ai).pack(side=tk.LEFT, padx=(0, 10))
        self.chk_rename = ttk.Checkbutton(row, text="Renomear .PES e .PNG",
                                          variable=self.opt_rename)
        self.chk_rename.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(row, text="Sobrescrever PNG", variable=self.opt_overwrite).pack(side=tk.LEFT)

        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row, text="IA:", width=10).pack(side=tk.LEFT)
        self.ai_mode = tk.StringVar(value="openai")
        self.rb_cc = ttk.Radiobutton(row, text="ChatGPT / OpenAI",
                                     variable=self.ai_mode, value="openai")
        self.rb_cc.pack(side=tk.LEFT, padx=(0, 10))
        self.rb_openai_local = ttk.Radiobutton(row, text="ChatGPT local",
                                               variable=self.ai_mode, value="openai-local")
        self.rb_openai_local.pack(side=tk.LEFT, padx=(0, 10))
        self.rb_api = ttk.Radiobutton(row, text="Anthropic API key",
                                      variable=self.ai_mode, value="anthropic")
        self.rb_api.pack(side=tk.LEFT, padx=(0, 10))
        self.rb_local = ttk.Radiobutton(row, text="Claude Code local",
                                        variable=self.ai_mode, value="claude-code")
        self.rb_local.pack(side=tk.LEFT)

        row = ttk.Frame(box)
        row.pack(fill=tk.X)
        self.btn_start = ttk.Button(row, text="Executar pasta", command=self._start)
        self.btn_start.pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(row, text="Parar apos arquivo atual",
                                   command=self._stop_proc, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(6, 0))
        self.count_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.count_var, foreground="#555").pack(side=tk.LEFT, padx=12)

        self.status_var = tk.StringVar(value="Escolha uma pasta para comecar.")
        ttk.Label(self, textvariable=self.status_var, foreground="#333",
                  font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 4))
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(0, 8))

        pane = ttk.PanedWindow(self, orient=tk.VERTICAL)
        pane.pack(fill=tk.BOTH, expand=True)

        table_box = ttk.LabelFrame(pane, text="Resultado", padding=4)
        pane.add(table_box, weight=4)
        self.tree = ttk.Treeview(table_box, columns=PROCESS_COLS, show="headings", height=12)
        for col in PROCESS_COLS:
            self.tree.heading(col, text=col)
        self.tree.column("Original", width=135)
        self.tree.column("Pasta", width=130)
        self.tree.column("PNG", width=135)
        self.tree.column("Novo nome", width=150)
        self.tree.column("Descricao", width=430)
        self.tree.column("Status", width=80, anchor=tk.CENTER)
        vsb = ttk.Scrollbar(table_box, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(table_box, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.config(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.tag_configure("ok", foreground="#1a7a1a")
        self.tree.tag_configure("erro", foreground="#cc0000")
        self.tree.tag_configure("proc", foreground="#0055cc")

        log_box = ttk.LabelFrame(pane, text="Log", padding=4)
        pane.add(log_box, weight=2)
        self.log = tk.Text(log_box, height=7, wrap=tk.WORD, font=("Consolas", 9),
                           state=tk.DISABLED, bg="#fbfbfb", relief=tk.FLAT)
        sb = ttk.Scrollbar(log_box, command=self.log.yview)
        self.log.config(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(fill=tk.BOTH, expand=True)

    def _pick(self):
        path = filedialog.askdirectory(title="Selecione a pasta com arquivos .PES")
        if path:
            self.folder_var.set(path)

    def _open_folder(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("Pasta invalida", "Selecione uma pasta valida primeiro.")
            return
        os.startfile(folder)

    def _toggle_ai(self):
        state = tk.NORMAL if self.opt_ai.get() else tk.DISABLED
        self.chk_rename.config(state=state)
        self.rb_cc.config(state=state)
        self.rb_openai_local.config(state=state)
        self.rb_api.config(state=state)
        self.rb_local.config(state=state)
        if not self.opt_ai.get():
            self.opt_rename.set(False)

    def _start(self):
        folder = self.folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("Pasta invalida", "Selecione uma pasta valida.")
            return
        if self.opt_ai.get() and not _ai_mode_available(self.ai_mode.get()):
            _warn_no_key(self.ai_mode.get())
            return

        for row in self.tree.get_children():
            self.tree.delete(row)
        self._clear_log()
        self.count_var.set("")
        self.progress["value"] = 0
        self._stop.clear()
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.status_var.set("Preparando processamento...")
        self._log(f"Pasta: {folder}")

        mode = self.ai_mode.get()
        self._thread = threading.Thread(target=self._run, args=(folder, mode), daemon=True)
        self._thread.start()

    def _stop_proc(self):
        self._stop.set()
        self.status_var.set("Parando apos o arquivo atual...")

    def _run(self, folder: str, ai_mode: str):
        root = Path(folder)
        files = self._collect_pes(root)
        total = len(files)
        if not total:
            self.after(0, lambda: self.status_var.set("Nenhum arquivo .PES encontrado."))
            self.after(0, lambda: self._log("Nenhum arquivo .PES encontrado."))
            self.after(0, self._done)
            return

        self.after(0, lambda: self.progress.config(maximum=total))
        self.after(0, lambda: self._log(f"{total} arquivo(s) encontrado(s)."))
        ok = err = 0

        for index, pes_path in enumerate(files, 1):
            if self._stop.is_set():
                break

            rel_folder = str(pes_path.relative_to(root).parent)
            if rel_folder == ".":
                rel_folder = ""
            original = pes_path.name
            self.after(0, self._add_row, original, rel_folder)
            self.after(0, lambda v=index: self.progress.config(value=v))
            self.after(0, lambda i=index, t=total, n=original:
                       self.status_var.set(f"[{i}/{t}] {n}"))
            self.after(0, lambda n=original: self._log(f"Processando {n}"))

            png_name = ""
            new_name = ""
            desc = ""
            status = "OK"
            tag = "ok"

            try:
                pattern = pyembroidery.read(str(pes_path))
                image = render_pattern(pattern)
                png_path = pes_path.with_suffix(".png")

                if self.opt_png.get():
                    if not png_path.exists() or self.opt_overwrite.get():
                        image.save(str(png_path))
                    png_name = png_path.name

                identified = ""
                if self.opt_ai.get():
                    identified, desc = identify(image, ai_mode)
                    new_name = identified
                    if ai_mode == "openai":
                        time.sleep(0.3)

                if self.opt_rename.get() and identified:
                    candidate = self._unique_path(pes_path.parent / f"{identified}.pes", pes_path)
                    pes_path.rename(candidate)
                    new_name = candidate.name
                    if png_path.exists():
                        new_png = candidate.with_suffix(".png")
                        if new_png.exists() and png_path.resolve() != new_png.resolve():
                            new_png = self._unique_path(new_png, png_path)
                        png_path.rename(new_png)
                        png_name = new_png.name

                ok += 1
            except Exception as exc:
                status = "Erro"
                tag = "erro"
                desc = str(exc)[:180]
                err += 1

            self.after(0, self._update_row, original, rel_folder, png_name, new_name, desc, status, tag)
            self.after(0, lambda n=original, s=status: self._log(f"{n}: {s}"))
            self.after(0, lambda o=ok, e=err: self.count_var.set(f"OK: {o}  Erros: {e}"))

        self.after(0, self._done, ok, err, total)

    def _collect_pes(self, root: Path) -> list[Path]:
        seen = set()
        files = []
        for item in sorted(root.rglob("*.pes")) + sorted(root.rglob("*.PES")):
            resolved = item.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(item)
        return files

    def _unique_path(self, target: Path, current: Path | None = None) -> Path:
        if current is not None and target.exists() and target.resolve() == current.resolve():
            return target
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        for index in range(2, 200):
            candidate = target.with_name(f"{stem}_{index}{suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Nao foi possivel criar nome unico para {target.name}")

    def _add_row(self, original: str, folder: str):
        iid = self.tree.insert("", tk.END, values=(original, folder, "", "", "", "..."),
                               tags=("proc",))
        self.tree.see(iid)

    def _update_row(self, original: str, folder: str, png_name: str, new_name: str,
                    desc: str, status: str, tag: str):
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            if vals[0] == original and vals[1] == folder:
                self.tree.item(iid, values=(original, folder, png_name, new_name, desc, status),
                               tags=(tag,))
                self.tree.see(iid)
                return

    def _done(self, ok: int = 0, err: int = 0, total: int = 0):
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        if total:
            self.status_var.set(f"Concluido - {total} arquivo(s): {ok} OK, {err} erro(s)")
            self._log(f"Concluido. OK: {ok} Erros: {err}")
        self._stop.clear()

    def _clear_log(self):
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)

    def _log(self, text: str):
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)


def _warn_no_key(mode: str = "openai"):
    if mode == "anthropic":
        title = "ANTHROPIC_API_KEY necessaria"
        body = (
            "Configure a variavel de ambiente ANTHROPIC_API_KEY.\n\n"
            "Como configurar no Windows (terminal):\n"
            '  setx ANTHROPIC_API_KEY "sua-chave-aqui"\n\n'
            "Depois reinicie o aplicativo."
        )
    elif mode == "claude-code":
        title = "Claude Code local indisponivel"
        body = "Instale ou entre no Claude Code local para usar este modo."
    elif mode == "openai-local":
        title = "ChatGPT local / API local"
        body = (
            "Abra um servidor local compativel com OpenAI antes de usar este modo.\n\n"
            "Padrao esperado:\n"
            "  OPENAI_LOCAL_BASE_URL=http://localhost:1234/v1\n"
            "  OPENAI_LOCAL_MODEL=local-model\n\n"
            "Use um modelo local com suporte a imagem/visao."
        )
    else:
        title = "OPENAI_API_KEY necessaria"
        body = (
            "Configure a variavel de ambiente OPENAI_API_KEY.\n\n"
            "Como configurar no Windows (terminal):\n"
            '  setx OPENAI_API_KEY "sua-chave-aqui"\n\n'
            "Depois reinicie o aplicativo."
        )

    messagebox.showwarning(title, body)
    return
    messagebox.showwarning(
        "ANTHROPIC_API_KEY necessária",
        "Configure a variável de ambiente ANTHROPIC_API_KEY.\n\n"
        "Como configurar no Windows (terminal):\n"
        '  setx ANTHROPIC_API_KEY "sua-chave-aqui"\n\n'
        "Depois reinicie o aplicativo."
    )


# ── Janela principal ──────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PES Embroidery Identifier")
        self.geometry("1050x740")
        self.minsize(750, 520)

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        nb.add(QuickProcessTab(nb), text="  Processar Pasta  ")
        nb.add(SingleFileTab(nb), text="  Arquivo Único  ")
        nb.add(BatchTab(nb), text="  Lote Automático  ")

        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        cc_ok = _claude_available()
        api_ok = bool(os.environ.get("OPENAI_API_KEY"))
        anthropic_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        local_ok = bool(os.environ.get("OPENAI_LOCAL_MODEL"))
        parts = []
        if cc_ok:
            parts.append("Claude Code: disponível ✓")
        if api_ok:
            parts.append("API Key: configurada ✓")
        if not cc_ok and not api_ok:
            parts.append("Nenhum modo IA disponível — instale Claude Code ou configure ANTHROPIC_API_KEY")
        parts = []
        if api_ok:
            parts.append("OpenAI: API key configurada")
        if anthropic_ok:
            parts.append("Anthropic: API key configurada")
        if local_ok:
            parts.append("ChatGPT local: configurado")
        if cc_ok:
            parts.append("Claude Code local: disponivel")
        if not parts:
            parts.append("Nenhum modo IA disponivel - configure OPENAI_API_KEY ou ANTHROPIC_API_KEY")
        color = "#1a7a1a" if (api_ok or anthropic_ok or cc_ok) else "#cc0000"
        ttk.Label(bar, text="  |  ".join(parts), foreground=color,
                  padding=(8, 2)).pack(side=tk.RIGHT)


if __name__ == "__main__":
    App().mainloop()
