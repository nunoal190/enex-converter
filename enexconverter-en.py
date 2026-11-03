"""Convert Evernote exports to local HTML notebooks. Requires Python 3.8+."""

from xml.parsers import expat
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


EXTENSIONS = {
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
for format_name, extension in [
    ("wordprocessingml.document", ".docx"), ("spreadsheetml.sheet", ".xlsx"),
    ("presentationml.presentation", ".pptx"),
]:
    EXTENSIONS["application/vnd.openxmlformats-officedocument." + format_name] = extension

RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]) *($|\.)", re.I)
CSS = """
body { max-width: 56rem; margin: 2rem auto; padding: 0 1rem;
       font: 1rem/1.55 system-ui, sans-serif; color: #202124; overflow-wrap: anywhere; }
table { border-collapse: collapse; } td, th { border: 1px solid #aaa; padding: .35rem; }
img { max-width: 100%; height: auto; } pre { white-space: pre-wrap; }
"""


def safe_name(text, pattern):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text).strip().rstrip(". ")
    return ("_" if RESERVED.match(text) else "") + (text or pattern)


class Filenames:
    """Reserve filenames case-insensitively on all platforms."""

    def __init__(self, folder):
        self.limit = 220
        if os.name == "nt":
            self.limit = min(220, 258 - len(str(folder.absolute()).encode("utf-16-le")) // 2)
        self.used = set()
        self.next_number = {}

    def reserve(self, base, extension):
        key = (base.casefold(), extension.casefold())
        number = self.next_number.get(key, 1)
        while True:
            end = (f" ({number})" if number > 1 else "") + extension
            # Leave room for the numeric suffix on Windows and Linux.
            if self.limit <= len(end.encode("utf-8")):
                raise OSError("Output folder path is too long. Choose a shorter path.")
            start = base.encode("utf-8")[:self.limit - len(end.encode("utf-8"))]
            name = start.decode("utf-8", "ignore").rstrip(". ") + end
            number += 1
            if name.casefold() not in self.used:
                self.used.add(name.casefold())
                self.next_number[key] = number
                return name


def attachment_url(name):
    # Encode the filename so spaces, %, # and Unicode work in relative URLs.
    unreserved = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "_attachments/" + "".join(chr(b) if b in unreserved else f"%{b:02X}"
                                for b in name.encode("utf-8"))


def extract_resource(resource, folder, names):
    text = resource.findtext("data")
    if text is None:
        raise ValueError("missing data element")
    if resource.find("data").get("encoding", "base64").lower() != "base64":
        raise ValueError("unknown resource encoding")
    mime = resource.findtext("mime", "").split(";")[0].strip().lower()
    original = safe_name(resource.findtext("resource-attributes/file-name", ""), "attachment")
    base, ext = os.path.splitext(original)
    if len(ext.encode("utf-8")) > 32:
        base, ext = original, ""
    if len(original.encode("utf-8")) > min(120, names.limit):
        suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
        base = f"attachment-{suffix}"
    name = names.reserve(base, ext or EXTENSIONS.get(mime, ".bin"))
    destination = folder / name
    digest = hashlib.md5()  # ENML identifies attachments by MD5.
    with destination.open("xb") as output:
        try:
            # iterparse buffers each data element. Decode it in chunks to avoid
            # another full-size copy and accept MIME line breaks.
            remainder = b""
            finished = False
            for pos in range(0, len(text), 256 * 1024):
                block = remainder + b"".join(text[pos:pos + 256 * 1024].encode("ascii").split())
                n = len(block) // 4 * 4
                if finished and block:
                    raise ValueError("unexpected data after Base64 padding")
                if n:
                    data = base64.b64decode(block[:n], validate=True)
                    finished = block[n - 1] == ord("=")
                    output.write(data)
                    digest.update(data)
                remainder = block[n:]
            if remainder:
                raise ValueError("incomplete Base64 data")
        except ValueError:
            output.close()
            destination.unlink()
            raise
    return digest.hexdigest(), (name, mime)


def set_link(element, name):
    element.tag = "a"
    element.attrib = {"href": attachment_url(name), "target": "_blank", "rel": "noopener"}
    element.text = "📎 Attachment: " + name


def parse_enml(content):
    try:
        return ET.fromstring(content)
    except ET.ParseError as error:
        if "undefined entity" not in str(error):
            raise
    # The external ENML DTD can declare XHTML entities such as &nbsp;.
    # Resolve them locally while preserving CDATA, comments and processing instructions.
    pattern = r"<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>|&([A-Za-z][A-Za-z0-9]*);"

    def entity(match):
        codepoint = name2codepoint.get(match[1])
        return f"&#{codepoint};" if codepoint is not None else match[0]

    return ET.fromstring(re.sub(pattern, entity, content, flags=re.S))


def render_tasks(body, note):
    def field(task, *names):
        for name in names:
            value = task.get(name) or task.findtext(name)
            if value and value.strip():
                return value.strip()
        return ""

    records = {}
    tasks = note.findall(".//task") + note.findall(".//en-task")
    for task in tasks:
        key = field(task, "taskId", "taskGuid", "guid", "id")
        if key:
            records[key] = task

    def checkbox(row, task, description):
        state = field(task, "taskStatus", "status", "checked", "completed").lower()
        control = ET.SubElement(row, "input", type="checkbox", disabled="disabled")
        if state in ("true", "1", "completed", "done"):
            control.set("checked", "checked")
            control.tail = " "
            ET.SubElement(row, "s").text = description
        else:
            control.tail = " " + description

    # Match the warning as a pair so an unrelated heading is retained.
    parents = {child: parent for parent in body.iter() for child in parent}
    cleared = set()
    blocks = list(body.iter())
    for i, block in enumerate(blocks):
        for attr in ("text", "tail"):
            value = getattr(block, attr) or ""
            if value.strip().casefold().startswith(("este bloco é um espaço reservado para tarefas",
                                                     "this block is a placeholder for tasks")):
                setattr(block, attr, "")
                cleared.add(block)
                for previous in reversed(blocks[:i + 1]):
                    heading = (previous.text or "").strip().casefold()
                    if heading in ("conteúdo não suportado", "unsupported content"):
                        previous.text = ""
                        cleared.add(previous)
                        break
                    if previous is not block and heading:
                        break

    for node in cleared:
        while node in parents and not len(node) and not (node.text or "").strip() and not (node.tail or "").strip():
            parent = parents[node]
            if node in parent:
                parent.remove(node)
            node = parent

    used = set()
    for parent in list(body.iter()):
        for index, child in enumerate(list(parent)):
            tag = child.tag
            if tag not in ("en-task", "en-todo"):
                continue
            key = field(child, "taskId", "taskGuid", "guid", "id")
            task = records.get(key, child)
            used.add(task)
            description = field(task, "title", "taskTitle", "description", "text") or (task.text or "").strip()
            inline = tag == "en-todo" and not description
            row = ET.Element("span" if parent.tag == "p" or (inline and (len(parent) > 1 or (parent.text or "").strip())) else "p")
            checkbox(row, task, (child.tail or "").lstrip() if inline else description)
            row.tail = None if inline else child.tail
            parent.remove(child)
            parent.insert(index, row)

    for task in tasks:
        if task not in used:
            description = field(task, "title", "taskTitle", "description", "text") or (task.text or "").strip()
            if description:
                checkbox(ET.SubElement(body, "p"), task, description)


def markdown_text(value):
    return re.sub(r"([\\\[\]*_])", r"\\\1", re.sub(r"\s+", " ", value or ""))


def write_embedded_markdown(output, title, body, images, directory):
    # XML cannot contain NUL. Use temporary tokens to stream Base64 after formatting.
    sources = {attachment_url(name): (name, mime) for name, mime in images.items()}
    embedded = []
    for image in body.iter("img"):
        source = sources.get(image.get("src"))
        if source:
            image.set("src", f"\x00{len(embedded)}\x00")
            embedded.append(source)
    text = markdown_body(body).strip()
    output.write("# " + markdown_text(title) + "\n\n")
    start = 0
    for token in re.finditer(r"\x00(\d+)\x00", text):
        output.write(text[start:token.start()])
        name, mime = embedded[int(token[1])]
        output.write(f"data:{mime};base64,")
        with (directory / name).open("rb") as image:
            # Multiples of three produce one continuous Base64 stream.
            for block in iter(lambda: image.read(3 * 65536), b""):
                output.write(base64.b64encode(block).decode("ascii"))
        start = token.end()
    output.write(text[start:] + "\n")


def markdown_body(node):
    tag = node.tag
    if tag in ("style", "script"):
        return ""
    if tag in ("pre", "code"):
        raw = "".join(node.itertext())
        fence = "`" * max(3 if tag == "pre" else 1,
                             1 + max((len(m) for m in re.findall(r"`+", raw)), default=0))
        if tag == "pre":
            return f"\n\n{fence}\n{raw}\n{fence}\n\n"
        return f"{fence} {raw} {fence}"
    if tag == "table":
        rows = []
        for row in node.iter("tr"):
            cells = [re.sub(r"\s+", " ", markdown_body(cell)).strip().replace("|", r"\|")
                     for cell in row if cell.tag in ("td", "th")]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(map(len, rows))
        rows.insert(1, ["---"] * width)
        return "\n\n" + "\n".join("| " + " | ".join(row + [""] * (width - len(row))) + " |"
                                   for row in rows) + "\n\n"
    if tag in ("ul", "ol"):
        lines = []
        for i, item in enumerate((child for child in node if child.tag == "li"), 1):
            marker = "- " if tag == "ul" else f"{i}. "
            value = markdown_body(item).strip().replace("\n", "\n" + " " * len(marker))
            lines.append(marker + value[2:] if value.startswith(("- [x]", "- [ ]")) else marker + value)
        return "\n\n" + "\n".join(lines) + "\n\n"

    value = markdown_text(node.text) + "".join(markdown_body(child) + markdown_text(child.tail) for child in node)
    if tag == "img":
        url = node.get("src", "")
        return f"![{markdown_text(node.get('alt', ''))}]({url})"
    if tag == "a" and node.get("href"):
        url = node.get("href").replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        label = re.sub(r"^📎 (?:Anexo|Attachment): ", "📎 ", value)
        return f"[{label or url}]({url})"
    if tag == "input" and node.get("type") == "checkbox":
        return "- [x]" if "checked" in node.attrib else "- [ ]"
    if tag == "br":
        return "  \n"
    if tag in ("s", "del", "strike"):
        return "~~" + value + "~~"
    if tag in ("strong", "b") or re.search(r"font-weight:\s*(bold|[7-9]00)", node.get("style", ""), re.I):
        value = "**" + value.strip() + "**"
    if tag in ("em", "i"):
        return "*" + value + "*"
    if re.fullmatch(r"h[1-6]", tag):
        return "\n\n" + "#" * int(tag[1]) + " " + value.strip() + "\n\n"
    if tag in ("div", "p", "blockquote"):
        value = value.strip()
        if not value:
            return ""
        if tag == "blockquote":
            value = "> " + value.replace("\n", "\n> ")
        return "\n\n" + value + "\n\n"
    return value


def write_note(note, resources, folder, names, warn, markdown=False):
    title = note.findtext("title", "").strip() or "Untitled"
    content = note.findtext("content", "").strip()
    try:
        # Parsing strips the prolog and DOCTYPE without fetching the remote DTD.
        body_content = parse_enml(content)
        if body_content.tag != "en-note":
            raise ValueError("expected an en-note root element")
    except (ET.ParseError, ValueError) as error:
        warn(f"Invalid ENML ({error}). Original content included as plain text.")
        body_content = ET.Element("div")
        ET.SubElement(body_content, "pre").text = content
    body_content.tag = "div"
    used = set()
    by_hash = dict(resources)
    images = {}
    for media in body_content.iter("en-media"):
        key = media.get("hash", "").strip().lower()
        kind = media.get("type", "").lower()
        tail = media.tail
        media.clear()
        media.tail = tail  # Preserve text immediately following the attachment.
        if key not in by_hash:
            media.tag = "span"
            media.text = f"[Missing attachment: {key or 'no hash'}]"
            warn(media.text)
            continue
        name, mime = by_hash[key]
        used.add(name)
        if (mime or kind).startswith("image/"):
            images[name] = mime or kind
            media.tag = "img"
            media.attrib = {"src": attachment_url(name), "alt": name,
                            "style": "max-width:100%; height:auto;"}
        else:
            set_link(media, name)
    for _, (name, mime) in resources:
        if name not in used:
            if markdown and mime.startswith("image/"):
                images[name] = mime
                ET.SubElement(body_content, "img", src=attachment_url(name), alt=name)
                continue
            set_link(ET.SubElement(ET.SubElement(body_content, "p"), "a"), name)
    render_tasks(body_content, note)
    if markdown:
        path = folder / names.reserve(safe_name(title, "Untitled"), ".md")
        with path.open("x", encoding="utf-8") as output:
            write_embedded_markdown(output, title, body_content, images, folder / "_attachments")
        for image_name in images:
            (folder / "_attachments" / image_name).unlink()
        return path.name


    root = ET.Element("html")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "meta", charset="utf-8")
    ET.SubElement(head, "meta", name="viewport", content="width=device-width, initial-scale=1")
    ET.SubElement(head, "meta", {"http-equiv": "Content-Security-Policy",
                  "content": "default-src 'none'; img-src 'self' file: data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"})
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "style").text = CSS
    body = ET.SubElement(root, "body")
    ET.SubElement(body, "h1").text = title
    body.append(body_content)
    path = folder / names.reserve(safe_name(title, "Untitled"), ".html")
    with path.open("xb") as output:
        output.write(b"<!doctype html>\n")
        ET.ElementTree(root).write(output, encoding="utf-8", method="html")
    return path.name


def console(kind, message, value=0):
    if kind not in ("note", "attachment", "start", "end"):
        print(message, file=sys.stderr if kind in ("warning", "error") else sys.stdout, flush=True)


class RecoveryReader:
    """Read from the original file with an optional virtual root and one byte edit."""
    def __init__(self, stream, prefix=b"", edit=None):
        self.stream, self.prefix, self.edit = stream, prefix, edit

    def read(self, size):
        start = self.stream.tell()
        data = self.stream.read(size)
        if self.edit and start <= self.edit[0] < start + len(data):
            pos, replacement = self.edit
            index = pos - start
            data = data[:index] + replacement + data[index + 1:]
        result = self.prefix + data
        self.prefix = b""
        return result


def diagnose(stream, start, prefix, edit):
    stream.seek(start)
    reader = RecoveryReader(stream, prefix, edit)
    parser = expat.ParserCreate()
    stack = []
    note_start = None

    def opened(tag, attrs):
        nonlocal note_start
        if stack == ["en-export"] and tag == "note":
            note_start = start + parser.CurrentByteIndex - len(prefix)
            if edit and note_start > edit[0]:
                note_start -= len(edit[1]) - 1
        stack.append(tag)

    def closed(tag):
        nonlocal note_start
        stack.pop()
        if stack == ["en-export"]:
            note_start = None

    parser.StartElementHandler = opened
    parser.EndElementHandler = closed
    try:
        while True:
            chunk = reader.read(65536)
            parser.Parse(chunk, not chunk)
            if not chunk:
                raise ValueError("XML diagnostic failed")
    except expat.ExpatError as error:
        pos = start + parser.ErrorByteIndex - len(prefix)
        if edit and pos > edit[0]:
            pos -= len(edit[1]) - 1
        return note_start, pos, stack[-1] if stack else "?", error.code


def repair_candidate(stream, pos):
    begin = max(0, pos - 128)
    stream.seek(begin)
    sample = stream.read(pos - begin + 64)
    index = pos - begin
    byte = sample[index:index + 1]
    if byte and byte[0] < 32 and byte not in (b"\t", b"\r", b"\n"):
        return (pos, b""), f"U+{byte[0]:04X}"
    # Expat often points just after the ampersand (e.g. the space in 'A & B').
    match = re.search(rb"&[A-Za-z0-9_.:-]*$", sample[:index])
    if byte == b"&" or match:
        amp = index if byte == b"&" else match.start()
        return (begin + amp, b"&amp;"), "& → &amp;"
    return None, f"byte 0x{byte.hex()}"


def valid_repaired_note(stream, start, edit):
    class NoteEnd(Exception):
        pass

    parser = expat.ParserCreate()
    depth = 0
    def opened(tag, attrs):
        nonlocal depth
        if depth == 1 and tag == "note":
            raise ValueError("nested note")
        depth += 1

    def closed(tag):
        nonlocal depth
        depth -= 1
        if not depth:
            raise NoteEnd()

    parser.StartElementHandler, parser.EndElementHandler = opened, closed
    stream.seek(start)
    reader = RecoveryReader(stream, edit=edit)
    try:
        while True:
            chunk = reader.read(65536)
            parser.Parse(chunk, not chunk)
            if not chunk:
                return False
    except NoteEnd:
        return True
    except (expat.ExpatError, ValueError):
        return False


def recoverable_events(stream, recover, directory, stats):
    prefix, edit = b"", None
    while True:
        start = stream.tell()
        try:
            yield from ET.iterparse(RecoveryReader(stream, prefix, edit), events=("start", "end"))
            return
        except ET.ParseError as error:
            first, pos, expected, code = diagnose(stream, start, prefix, edit)
            if first is None or pos < first:
                raise
            candidate, detail = repair_candidate(stream, pos)
            if code == expat.errors.codes[expat.errors.XML_ERROR_TAG_MISMATCH]:
                stream.seek(max(0, pos - 2))
                tag = re.match(rb"</([\w:.-]+)", stream.read(128))
                detail = f"expected </{expected}>, found </{tag[1].decode('ascii') if tag else '?'}>"
            if code == expat.errors.codes[expat.errors.XML_ERROR_INVALID_TOKEN] and candidate and valid_repaired_note(stream, first, candidate):
                recover(f"{error}; byte {pos}; {detail}; REPAIRED")
                stats[0] += 1
                stream.seek(first)
                prefix, edit = b"<en-export>", candidate
                continue

            # Preserve the original bytes. Resume only at a standalone ENEX boundary.
            stream.seek(pos)
            at_start = False
            while True:
                boundary = stream.tell()
                chunk = stream.readline(65536)
                if not chunk or (at_start and chunk.strip() in (b"<note>", b"</en-export>")):
                    break
                at_start = chunk.endswith(b"\n")
            directory.mkdir(exist_ok=True)
            fragment = directory / f"note-{first}.xml.fragment"
            stream.seek(first)
            with fragment.open("xb") as output:
                while stream.tell() < boundary:
                    output.write(stream.read(min(65536, boundary - stream.tell())))
            recover(f"{error}; byte {pos}; {detail}; FRAGMENT {directory.name}/{fragment.name}")
            stats[1] += 1
            if not chunk:
                raise
            stream.seek(boundary)
            prefix, edit = b"<en-export>", None


def convert(source, output_folder=None, emit=console, markdown=False):
    folder = source.parent if output_folder is None else output_folder
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / source.stem
    partial = folder / (source.stem + ".enex-partial")
    if destination.exists():
        raise FileExistsError(f"output folder already exists: {destination}")
    partial.mkdir()  # Keep partial output recoverable after an interruption.
    attachments = partial / "_attachments"
    attachments.mkdir()
    pages, files = Filenames(partial), Filenames(attachments)
    notes = attachment_count = warnings = 0
    resources = []

    stats = [0, 0]  # Repaired notes, archived fragments

    def warn(msg):
        nonlocal warnings
        warnings += 1
        emit("warning", f"{source.name}, note {notes + stats[1] + 1}: {msg}")

    with source.open("rb") as source_file:
        size = max(1, os.fstat(source_file.fileno()).st_size)
        stack = []
        def recover(error):
            nonlocal attachment_count
            title = stack[1].findtext("title") if len(stack) > 1 else None
            warn(f"{title or '?'} — {error}")
            for _, (name, _) in resources:
                (attachments / name).unlink()
            attachment_count -= len(resources)
            resources.clear()
            stack.clear()

        for event, elem in recoverable_events(source_file, recover, partial / "_recovery", stats):
            if event == "start":
                if not stack and elem.tag != "en-export":
                    raise ValueError("expected an en-export root element")
                stack.append(elem)
                continue
            if len(stack) == 3 and stack[-2].tag == "note" and elem.tag == "resource":
                try:
                    resource = extract_resource(elem, attachments, files)
                    resources.append(resource)
                    attachment_count += 1
                    emit("attachment", f"  Attachment: {resource[1][0]}")
                except ValueError as error:
                    warn(f"skipped resource ({error})")
                elem.clear()
                stack[-2].remove(elem)
            elif len(stack) == 2:
                if elem.tag == "note":
                    page = write_note(elem, resources, partial, pages, warn, markdown)
                    resources.clear()
                    notes += 1
                    # The parser reads ahead. Reserve the final step for publishing the folder.
                    emit("note", f"Note {notes}: {page}", min(0.99, source_file.tell() / size))
                    if notes == 1 or notes % 50 == 0:
                        emit("count", f"{source.name}: {notes} notes, {attachment_count} attachments")
                elem.clear()
                stack[0].remove(elem)
            stack.pop()
    emit("report", f"Repaired notes: {stats[0]}; fragments needing recovery: {stats[1]}.")
    partial.rename(destination)
    emit("report", f"{destination}: {notes} notes, {attachment_count} attachments, {warnings} warnings.")
    return notes, attachment_count, warnings


def list_inputs(paths):
    inputs = {}
    for path in paths:
        path = Path(path)
        if path.is_dir():
            found = sorted(p for p in path.iterdir() if p.suffix.lower() == ".enex" and p.is_file())
        elif path.is_file() and path.suffix.lower() == ".enex":
            found = [path]
        else:
            found = []
        if not found:
            raise ValueError(f"No .enex files found at {path}")
        for file in found:
            inputs.setdefault(file.resolve(), file)
    return list(inputs.values())


def convert_batch(inputs, destination=None, emit=console, markdown=False):
    notes = attachments = warnings = failures = 0
    for index, source_file in enumerate(inputs, 1):
        emit("start", f"[{index}/{len(inputs)}] v1.5 · Converting {source_file}")
        try:
            n, a, v = convert(source_file, destination, emit, markdown)
            notes += n
            attachments += a
            warnings += v
        except (OSError, ET.ParseError, ValueError, LookupError) as error:
            failures += 1
            emit("error", f"{source_file}: {error}")
        emit("end", "", index)
    emit("report", f"Final totals: {notes} notes, {attachments} attachments, {warnings} warnings. "
           f"Notebooks completed: {len(inputs) - failures}/{len(inputs)}.")
    return int(bool(warnings or failures))


class ConversionWindow:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from tkinter.scrolledtext import ScrolledText

        self.root = root
        self.dialogs = filedialog
        self.messages = messagebox
        self.paths = ()
        self.running = False
        self.events = queue.Queue(maxsize=2048)
        self.selection = tk.StringVar(value="No input selected")
        self.destination = tk.StringVar()
        self.status = tk.StringVar(value="Select files or an input folder.")
        root.title("Evernote · ENEX to HTML Converter · v1.5")
        root.minsize(620, 440)
        root.protocol("WM_DELETE_WINDOW", self.close)
        panel = ttk.Frame(root, padding=16)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(9, weight=1)

        ttk.Label(panel, text="Input").grid(row=0, column=0, sticky="w")
        buttons = ttk.Frame(panel)
        buttons.grid(row=1, column=0, sticky="w", pady=4)
        files = ttk.Button(buttons, text="Select .enex files…", command=self.select_files)
        files.pack(side="left")
        folder = ttk.Button(buttons, text="Select folder…", command=self.select_folder)
        folder.pack(side="left", padx=6)
        ttk.Label(panel, textvariable=self.selection, wraplength=580).grid(row=2, column=0, sticky="w")

        ttk.Label(panel, text="Output folder (leave blank to save beside each source file)").grid(
            row=3, column=0, sticky="w", pady=(12, 2))
        row = ttk.Frame(panel)
        row.grid(row=4, column=0, sticky="ew")
        field = ttk.Entry(row, textvariable=self.destination)
        field.pack(side="left", fill="x", expand=True)
        browse = ttk.Button(row, text="Browse…", command=self.select_destination)
        browse.pack(side="left", padx=(6, 0))
        self.start_button = ttk.Button(panel, text="Start Conversion", command=self.begin, state="disabled")
        self.start_button.grid(row=5, column=0, sticky="ew", pady=12)
        self.markdown = tk.BooleanVar(value=False)
        format_box = ttk.Checkbutton(panel, text="Markdown (.md)", variable=self.markdown)
        format_box.grid(row=10, column=0, sticky="w")
        self.controls = (format_box, files, folder, field, browse, self.start_button)
        self.progress = ttk.Progressbar(panel, mode="determinate")
        self.progress.grid(row=6, column=0, sticky="ew")
        ttk.Label(panel, textvariable=self.status).grid(row=7, column=0, sticky="w", pady=4)
        ttk.Label(panel, text="Conversion log").grid(row=8, column=0, sticky="w")
        self.log = ScrolledText(panel, width=84, height=15, wrap="word", state="disabled")
        self.log.grid(row=9, column=0, sticky="nsew")

    def select_files(self):
        paths = self.dialogs.askopenfilenames(parent=self.root, title="Select Evernote exports",
                                                   filetypes=[("Evernote exports", ("*.enex", "*.ENEX")),
                                                              ("All files", "*")])
        if paths:
            self.paths = paths
            self.selection.set(paths[0] if len(paths) == 1 else f"{len(paths)} files selected")
            self.start_button.state(["!disabled"])

    def select_folder(self):
        folder = self.dialogs.askdirectory(parent=self.root, title="Input folder", mustexist=True)
        if folder:
            self.paths = (folder,)
            self.selection.set(folder)
            self.start_button.state(["!disabled"])

    def select_destination(self):
        folder = self.dialogs.askdirectory(parent=self.root, title="Output folder")
        if folder:
            self.destination.set(folder)

    def begin(self):
        if self.running or not self.paths:
            return
        destination = self.destination.get().strip()
        self.running = True
        self.total = self.processed = self.notes = self.attachments = 0
        self.progress.configure(value=0, maximum=1)
        self.status.set("Preparing conversion…")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        for control in self.controls:
            control.state(["disabled"])
        threading.Thread(target=self.run_worker, args=(self.paths, Path(destination) if destination else None, self.markdown.get()),
                         name="enex-convert").start()
        self.root.after(40, self.refresh)

    def run_worker(self, paths, destination, markdown=False):
        # Only the main thread accesses Tk. The worker sends events through the queue.
        def emit(kind, message, value=0):
            self.events.put((kind, message, value))

        try:
            inputs = list_inputs(paths)
            emit("batch", "", len(inputs))
            result = convert_batch(inputs, destination, emit, markdown)
            status = "Completed with warnings or errors" if result else "Completed"
        except Exception as error:
            emit("error", str(error))
            status = "Conversion interrupted"
        emit("finished", status)

    def refresh(self):
        # Limit each update so the window stays responsive during large conversions.
        lines = []
        status = "Converting"
        for _ in range(256):
            try:
                kind, message, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "batch":
                self.total = value
                self.progress.configure(maximum=value)
            elif kind == "end":
                self.processed = value
                self.progress.configure(value=value)
            elif kind == "note":
                self.notes += 1
                self.progress.configure(value=self.processed + value)
            elif kind == "attachment":
                self.attachments += 1
            elif kind == "finished":
                self.running = False
                status = message
                for control in self.controls:
                    control.state(["!disabled"])
            if message and kind not in ("count", "finished"):
                prefix = {"warning": "Warning: ", "error": "Error: "}.get(kind, "")
                lines.append(prefix + message)
        if lines:
            self.log.configure(state="normal")
            self.log.insert("end", "\n".join(lines) + "\n")
            excess = int(self.log.index("end-1c").split(".")[0]) - 3001
            if excess > 0:
                self.log.delete("1.0", f"{excess + 1}.0")
            self.log.see("end")
            self.log.configure(state="disabled")
        if self.total or not self.running:
            self.status.set(f"{status} · {self.processed}/{self.total} files · "
                            f"{self.notes} notes · {self.attachments} attachments")
        if self.running:
            self.root.after(40, self.refresh)

    def close(self):
        if self.running:
            self.messages.showinfo("Conversion in progress", "Please wait for the conversion to finish before closing the window.",
                                    parent=self.root)
        else:
            self.root.destroy()


def main(args):
    usage = ("Usage: python enexconverter-en.py [--markdown] [file.enex | folder] ...\n"
           "With no arguments, opens the GUI. Use . to convert the current folder from the command line.")
    if not args:
        try:
            import tkinter as tk
        except ImportError:
            print("The GUI requires Python with Tcl/Tk installed.", file=sys.stderr)
            return 2
        try:
            root = tk.Tk()
        except tk.TclError as error:
            print(f"Could not open the GUI: {error}", file=sys.stderr)
            return 2
        ConversionWindow(root)
        root.mainloop()
        return 0
    if args in (["-h"], ["--help"]):
        print(usage)
        return 0
    markdown = "--markdown" in args
    if markdown:
        args = [arg for arg in args if arg != "--markdown"] or ["."]
    try:
        inputs = list_inputs(args)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    return convert_batch(inputs, markdown=markdown)


if __name__ == "__main__":
    # Legacy console encodings may encounter Unicode filenames.
    if sys.stdout is not None:  # pythonw.exe has no console.
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nConversion interrupted. Partial results have been preserved.", file=sys.stderr)
        sys.exit(130)
