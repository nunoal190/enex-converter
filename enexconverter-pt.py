"""Converte exportações Evernote em cadernos HTML locais. Requer Python 3.8+."""

import base64
import hashlib
import os
import queue
import re
import sys
import threading
import xml.etree.ElementTree as ET
from html.entities import name2codepoint
from pathlib import Path


EXTENSOES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/tiff": ".tif", "image/bmp": ".bmp",
    "image/svg+xml": ".svg", "image/heic": ".heic", "image/avif": ".avif",
    "application/pdf": ".pdf", "application/zip": ".zip",
    "application/msword": ".doc", "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/wav": ".wav",
    "audio/amr": ".amr", "audio/ogg": ".ogg", "video/mp4": ".mp4",
    "text/plain": ".txt", "text/html": ".html", "text/csv": ".csv",
}
for formato, extensao in [
    ("wordprocessingml.document", ".docx"), ("spreadsheetml.sheet", ".xlsx"),
    ("presentationml.presentation", ".pptx"),
]:
    EXTENSOES["application/vnd.openxmlformats-officedocument." + formato] = extensao

RESERVADO = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]) *($|\.)", re.I)
CSS = """
body { max-width: 56rem; margin: 2rem auto; padding: 0 1rem;
       font: 1rem/1.55 system-ui, sans-serif; color: #202124; overflow-wrap: anywhere; }
table { border-collapse: collapse; } td, th { border: 1px solid #aaa; padding: .35rem; }
img { max-width: 100%; height: auto; } pre { white-space: pre-wrap; }
"""


def seguro(texto, padrao):
    texto = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", texto).strip().rstrip(". ")
    return ("_" if RESERVADO.match(texto) else "") + (texto or padrao)


class Nomes:
    """Reserva nomes sem distinguir maiúsculas, mesmo num disco Linux."""

    def __init__(self, pasta):
        self.limit = 220
        if os.name == "nt":
            self.limit = min(220, 258 - len(str(pasta.absolute()).encode("utf-16-le")) // 2)
        self.usados = set()
        self.proximo = {}

    def reservar(self, base, extensao):
        chave = (base.casefold(), extensao.casefold())
        numero = self.proximo.get(chave, 1)
        while True:
            fim = (f" ({numero})" if numero > 1 else "") + extensao
            # Limite conservador por componente, também válido em UTF-16.
            if self.limit <= len(fim.encode("utf-8")):
                raise OSError("Pasta de destino demasiado longa. Escolha um caminho mais curto.")
            inicio = base.encode("utf-8")[:self.limit - len(fim.encode("utf-8"))]
            nome = inicio.decode("utf-8", "ignore").rstrip(". ") + fim
            numero += 1
            if nome.casefold() not in self.usados:
                self.usados.add(nome.casefold())
                self.proximo[chave] = numero
                return nome


def url_anexo(nome):
    # Codificar cada segmento mantém espaços, %, # e Unicode válidos em URLs.
    livres = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "_anexos/" + "".join(chr(b) if b in livres else f"%{b:02X}"
                                for b in nome.encode("utf-8"))


def extrair(recurso, pasta, nomes):
    texto = recurso.findtext("data")
    if texto is None:
        raise ValueError("falta o elemento data")
    if recurso.find("data").get("encoding", "base64").lower() != "base64":
        raise ValueError("codificação de recurso desconhecida")
    mime = recurso.findtext("mime", "").split(";")[0].strip().lower()
    original = seguro(recurso.findtext("resource-attributes/file-name", ""), "anexo")
    base, ext = os.path.splitext(original)
    if len(ext.encode("utf-8")) > 32:
        base, ext = original, ""
    if len(original.encode("utf-8")) > min(120, nomes.limit):
        suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
        base = f"anexo-{suffix}"
    nome = nomes.reservar(base, ext or EXTENSOES.get(mime, ".bin"))
    destino = pasta / nome
    digest = hashlib.md5()  # Identificador de conteúdo usado pelo ENML.
    with destino.open("xb") as saida:
        try:
            # iterparse materializa o texto de um <data>. Descodificar em blocos
            # evita uma segunda cópia integral e admite quebras de linha MIME.
            resto = b""
            terminou = False
            for pos in range(0, len(texto), 256 * 1024):
                bloco = resto + b"".join(texto[pos:pos + 256 * 1024].encode("ascii").split())
                n = len(bloco) // 4 * 4
                if terminou and bloco:
                    raise ValueError("dados após o fim do Base64")
                if n:
                    dados = base64.b64decode(bloco[:n], validate=True)
                    terminou = bloco[n - 1] == ord("=")
                    saida.write(dados)
                    digest.update(dados)
                resto = bloco[n:]
            if resto:
                raise ValueError("Base64 incompleto")
        except ValueError:
            saida.close()
            destino.unlink()
            raise
    return digest.hexdigest(), (nome, mime)


def ligacao(elemento, nome):
    elemento.tag = "a"
    elemento.attrib = {"href": url_anexo(nome), "target": "_blank", "rel": "noopener"}
    elemento.text = "📎 Anexo: " + nome


def ler_enml(conteudo):
    try:
        return ET.fromstring(conteudo)
    except ET.ParseError as erro:
        if "undefined entity" not in str(erro):
            raise
    # O DTD externo do ENML pode declarar entidades XHTML como &nbsp;.
    # Resolver localmente, preservando CDATA, comentários e instruções XML.
    padrao = r"<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>|&([A-Za-z][A-Za-z0-9]*);"

    def entidade(match):
        codigo = name2codepoint.get(match[1])
        return f"&#{codigo};" if codigo is not None else match[0]

    return ET.fromstring(re.sub(padrao, entidade, conteudo, flags=re.S))


def escrever_nota(nota, recursos, pasta, nomes, avisar):
    titulo = nota.findtext("title", "").strip() or "Sem título"
    conteudo = nota.findtext("content", "").strip()
    try:
        # O parser descarta o prólogo e o DOCTYPE sem consultar o DTD remoto.
        corpo = ler_enml(conteudo)
        if corpo.tag != "en-note":
            raise ValueError("raiz ENML diferente de en-note")
    except (ET.ParseError, ValueError) as erro:
        avisar(f"ENML inválido ({erro}). Conteúdo original incluído como texto.")
        corpo = ET.Element("div")
        ET.SubElement(corpo, "pre").text = conteudo
    corpo.tag = "div"
    usados = set()
    por_hash = dict(recursos)
    for media in corpo.iter("en-media"):
        chave = media.get("hash", "").strip().lower()
        tipo = media.get("type", "").lower()
        cauda = media.tail
        media.clear()
        media.tail = cauda  # Texto imediatamente a seguir ao anexo.
        if chave not in por_hash:
            media.tag = "span"
            media.text = f"[Anexo em falta: {chave or 'sem hash'}]"
            avisar(media.text)
            continue
        nome, mime = por_hash[chave]
        usados.add(nome)
        if (mime or tipo).startswith("image/"):
            media.tag = "img"
            media.attrib = {"src": url_anexo(nome), "alt": nome,
                            "style": "max-width:100%; height:auto;"}
        else:
            ligacao(media, nome)
    for _, (nome, _) in recursos:
        if nome not in usados:
            ligacao(ET.SubElement(ET.SubElement(corpo, "p"), "a"), nome)
    for caixa in corpo.iter("en-todo"):
        marcada = caixa.get("checked", "").lower() == "true"
        caixa.tag = "input"
        caixa.attrib = {"type": "checkbox", "disabled": "disabled"}
        if marcada:
            caixa.set("checked", "checked")

    raiz = ET.Element("html")
    head = ET.SubElement(raiz, "head")
    ET.SubElement(head, "meta", charset="utf-8")
    ET.SubElement(head, "meta", name="viewport", content="width=device-width, initial-scale=1")
    ET.SubElement(head, "meta", {"http-equiv": "Content-Security-Policy",
                  "content": "default-src 'none'; img-src 'self' file: data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"})
    ET.SubElement(head, "title").text = titulo
    ET.SubElement(head, "style").text = CSS
    body = ET.SubElement(raiz, "body")
    ET.SubElement(body, "h1").text = titulo
    body.append(corpo)
    caminho = pasta / nomes.reservar(seguro(titulo, "Sem título"), ".html")
    with caminho.open("xb") as saida:
        saida.write(b"<!doctype html>\n")
        ET.ElementTree(raiz).write(saida, encoding="utf-8", method="html")
    return caminho.name


def consola(tipo, mensagem, valor=0):
    if tipo not in ("nota", "anexo", "inicio", "fim"):
        print(mensagem, file=sys.stderr if tipo in ("aviso", "erro") else sys.stdout, flush=True)


def converter(origem, pasta_destino=None, emitir=consola):
    pasta = origem.parent if pasta_destino is None else pasta_destino
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / origem.stem
    parcial = pasta / (origem.stem + ".enex-partial")
    if destino.exists():
        raise FileExistsError(f"a pasta de destino já existe: {destino}")
    parcial.mkdir()  # Uma interrupção deixa o trabalho recuperável nesta pasta.
    anexos = parcial / "_anexos"
    anexos.mkdir()
    paginas, ficheiros = Nomes(parcial), Nomes(anexos)
    notas = total_anexos = avisos = 0
    recursos = []

    def avisar(msg):
        nonlocal avisos
        avisos += 1
        emitir("aviso", f"{origem.name}, nota {notas + 1}: {msg}")

    with origem.open("rb") as entrada:
        tamanho = max(1, os.fstat(entrada.fileno()).st_size)
        pilha = []
        for evento, elem in ET.iterparse(entrada, events=("start", "end")):
            if evento == "start":
                if not pilha and elem.tag != "en-export":
                    raise ValueError("a raiz do ficheiro não é en-export")
                pilha.append(elem)
                continue
            if len(pilha) == 3 and pilha[-2].tag == "note" and elem.tag == "resource":
                try:
                    recurso = extrair(elem, anexos, ficheiros)
                    recursos.append(recurso)
                    total_anexos += 1
                    emitir("anexo", f"  Anexo: {recurso[1][0]}")
                except ValueError as erro:
                    avisar(f"recurso ignorado ({erro})")
                elem.clear()
                pilha[-2].remove(elem)
            elif len(pilha) == 2:
                if elem.tag == "note":
                    pagina = escrever_nota(elem, recursos, parcial, paginas, avisar)
                    recursos.clear()
                    notas += 1
                    # O parser lê por blocos. Reservar o último passo para a publicação.
                    emitir("nota", f"Nota {notas}: {pagina}", min(0.99, entrada.tell() / tamanho))
                    if notas == 1 or notas % 50 == 0:
                        emitir("contagem", f"{origem.name}: {notas} notas, {total_anexos} anexos")
                elem.clear()
                pilha[0].remove(elem)
            pilha.pop()
    parcial.rename(destino)
    emitir("relatorio", f"{destino}: {notas} notas, {total_anexos} anexos, {avisos} avisos.")
    return notas, total_anexos, avisos


def listar_entradas(caminhos):
    entradas = {}
    for caminho in caminhos:
        caminho = Path(caminho)
        if caminho.is_dir():
            encontrados = sorted(p for p in caminho.iterdir() if p.suffix.lower() == ".enex" and p.is_file())
        elif caminho.is_file() and caminho.suffix.lower() == ".enex":
            encontrados = [caminho]
        else:
            encontrados = []
        if not encontrados:
            raise ValueError(f"Nenhum ficheiro .enex encontrado em {caminho}")
        for ficheiro in encontrados:
            entradas.setdefault(ficheiro.resolve(), ficheiro)
    return list(entradas.values())


def converter_lote(entradas, destino=None, emitir=consola):
    notas = anexos = avisos = falhas = 0
    for indice, entrada in enumerate(entradas, 1):
        emitir("inicio", f"[{indice}/{len(entradas)}] v1.2 · A converter {entrada}")
        try:
            n, a, v = converter(entrada, destino, emitir)
            notas += n
            anexos += a
            avisos += v
        except (OSError, ET.ParseError, ValueError, LookupError) as erro:
            falhas += 1
            emitir("erro", f"{entrada}: {erro}")
        emitir("fim", "", indice)
    emitir("relatorio", f"Total finalizado: {notas} notas, {anexos} anexos, {avisos} avisos. "
           f"Cadernos concluídos: {len(entradas) - falhas}/{len(entradas)}.")
    return int(bool(avisos or falhas))


class JanelaConversao:
    def __init__(self, raiz):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from tkinter.scrolledtext import ScrolledText

        self.raiz = raiz
        self.dialogos = filedialog
        self.mensagens = messagebox
        self.caminhos = ()
        self.ativa = False
        self.eventos = queue.Queue(maxsize=2048)
        self.selecao = tk.StringVar(value="Nenhuma origem selecionada")
        self.destino = tk.StringVar()
        self.estado = tk.StringVar(value="Selecione os ficheiros ou a pasta de entrada.")
        raiz.title("Evernote · Conversor ENEX para HTML · v1.2")
        raiz.minsize(620, 440)
        raiz.protocol("WM_DELETE_WINDOW", self.fechar)
        painel = ttk.Frame(raiz, padding=16)
        painel.pack(fill="both", expand=True)
        painel.columnconfigure(0, weight=1)
        painel.rowconfigure(9, weight=1)

        ttk.Label(painel, text="Origem").grid(row=0, column=0, sticky="w")
        botoes = ttk.Frame(painel)
        botoes.grid(row=1, column=0, sticky="w", pady=4)
        ficheiros = ttk.Button(botoes, text="Selecionar ficheiros .enex…", command=self.escolher_ficheiros)
        ficheiros.pack(side="left")
        pasta = ttk.Button(botoes, text="Selecionar pasta…", command=self.escolher_pasta)
        pasta.pack(side="left", padx=6)
        ttk.Label(painel, textvariable=self.selecao, wraplength=580).grid(row=2, column=0, sticky="w")

        ttk.Label(painel, text="Destino (em branco para guardar junto a cada original)").grid(
            row=3, column=0, sticky="w", pady=(12, 2))
        linha = ttk.Frame(painel)
        linha.grid(row=4, column=0, sticky="ew")
        campo = ttk.Entry(linha, textvariable=self.destino)
        campo.pack(side="left", fill="x", expand=True)
        procurar = ttk.Button(linha, text="Escolher…", command=self.escolher_destino)
        procurar.pack(side="left", padx=(6, 0))
        self.iniciar_btn = ttk.Button(painel, text="Iniciar Conversão", command=self.iniciar, state="disabled")
        self.iniciar_btn.grid(row=5, column=0, sticky="ew", pady=12)
        self.controlos = (ficheiros, pasta, campo, procurar, self.iniciar_btn)
        self.barra = ttk.Progressbar(painel, mode="determinate")
        self.barra.grid(row=6, column=0, sticky="ew")
        ttk.Label(painel, textvariable=self.estado).grid(row=7, column=0, sticky="w", pady=4)
        ttk.Label(painel, text="Registo de conversão").grid(row=8, column=0, sticky="w")
        self.log = ScrolledText(painel, width=84, height=15, wrap="word", state="disabled")
        self.log.grid(row=9, column=0, sticky="nsew")

    def escolher_ficheiros(self):
        caminhos = self.dialogos.askopenfilenames(parent=self.raiz, title="Selecionar exportações Evernote",
                                                   filetypes=[("Exportações Evernote", ("*.enex", "*.ENEX")),
                                                              ("Todos os ficheiros", "*")])
        if caminhos:
            self.caminhos = caminhos
            self.selecao.set(caminhos[0] if len(caminhos) == 1 else f"{len(caminhos)} ficheiros selecionados")
            self.iniciar_btn.state(["!disabled"])

    def escolher_pasta(self):
        pasta = self.dialogos.askdirectory(parent=self.raiz, title="Pasta de entrada", mustexist=True)
        if pasta:
            self.caminhos = (pasta,)
            self.selecao.set(pasta)
            self.iniciar_btn.state(["!disabled"])

    def escolher_destino(self):
        pasta = self.dialogos.askdirectory(parent=self.raiz, title="Pasta de destino")
        if pasta:
            self.destino.set(pasta)

    def iniciar(self):
        if self.ativa or not self.caminhos:
            return
        destino = self.destino.get().strip()
        self.ativa = True
        self.total = self.processados = self.notas = self.anexos = 0
        self.barra.configure(value=0, maximum=1)
        self.estado.set("A preparar a conversão…")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        for controlo in self.controlos:
            controlo.state(["disabled"])
        threading.Thread(target=self.trabalhar, args=(self.caminhos, Path(destino) if destino else None),
                         name="conversor-enex").start()
        self.raiz.after(40, self.atualizar)

    def trabalhar(self, caminhos, destino):
        # A thread só comunica pela fila. Toda a interação com Tcl/Tk fica na thread principal.
        def emitir(tipo, mensagem, valor=0):
            self.eventos.put((tipo, mensagem, valor))

        try:
            entradas = listar_entradas(caminhos)
            emitir("lote", "", len(entradas))
            resultado = converter_lote(entradas, destino, emitir)
            estado = "Concluído com avisos ou erros" if resultado else "Concluído"
        except Exception as erro:
            emitir("erro", str(erro))
            estado = "Conversão interrompida"
        emitir("terminou", estado)

    def atualizar(self):
        # Limitar o trabalho por ciclo mantém a janela responsiva mesmo com milhares de notas.
        linhas = []
        estado = "A converter"
        for _ in range(256):
            try:
                tipo, mensagem, valor = self.eventos.get_nowait()
            except queue.Empty:
                break
            if tipo == "lote":
                self.total = valor
                self.barra.configure(maximum=valor)
            elif tipo == "fim":
                self.processados = valor
                self.barra.configure(value=valor)
            elif tipo == "nota":
                self.notas += 1
                self.barra.configure(value=self.processados + valor)
            elif tipo == "anexo":
                self.anexos += 1
            elif tipo == "terminou":
                self.ativa = False
                estado = mensagem
                for controlo in self.controlos:
                    controlo.state(["!disabled"])
            if mensagem and tipo not in ("contagem", "terminou"):
                prefixo = {"aviso": "Aviso: ", "erro": "Erro: "}.get(tipo, "")
                linhas.append(prefixo + mensagem)
        if linhas:
            self.log.configure(state="normal")
            self.log.insert("end", "\n".join(linhas) + "\n")
            excesso = int(self.log.index("end-1c").split(".")[0]) - 3001
            if excesso > 0:
                self.log.delete("1.0", f"{excesso + 1}.0")
            self.log.see("end")
            self.log.configure(state="disabled")
        if self.total or not self.ativa:
            self.estado.set(f"{estado} · {self.processados}/{self.total} ficheiros · "
                            f"{self.notas} notas · {self.anexos} anexos")
        if self.ativa:
            self.raiz.after(40, self.atualizar)

    def fechar(self):
        if self.ativa:
            self.mensagens.showinfo("Conversão em curso", "Aguarde pelo fim da conversão para fechar a janela.",
                                    parent=self.raiz)
        else:
            self.raiz.destroy()


def main(args):
    uso = ("Uso: python enex_para_html.py [ficheiro.enex | pasta] ...\n"
           "Sem argumentos: abre a interface gráfica. Use . para converter a pasta atual na consola.")
    if not args:
        try:
            import tkinter as tk
        except ImportError:
            print("A interface gráfica requer uma instalação de Python com Tcl/Tk.", file=sys.stderr)
            return 2
        try:
            raiz = tk.Tk()
        except tk.TclError as erro:
            print(f"Não foi possível abrir a interface gráfica: {erro}", file=sys.stderr)
            return 2
        JanelaConversao(raiz)
        raiz.mainloop()
        return 0
    if args in (["-h"], ["--help"]):
        print(uso)
        return 0
    try:
        entradas = listar_entradas(args)
    except (OSError, ValueError) as erro:
        print(erro, file=sys.stderr)
        return 2
    return converter_lote(entradas)


if __name__ == "__main__":
    # Consolas e logs com páginas de código antigas podem receber nomes Unicode.
    if sys.stdout is not None:  # pythonw.exe não tem consola.
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nConversão interrompida. Resultados parciais preservados.", file=sys.stderr)
        sys.exit(130)
