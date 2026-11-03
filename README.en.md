# Evernote to HTML and Markdown · v1.5

[Português](README.md)

A free, local converter for Evernote exports (.enex). It creates one folder per notebook and one file per note, with images, attachments and tasks. Use the graphical interface or command line to convert notebooks, including partial recovery of damaged XML.

## Quick start

Download `enexconverter-en.py`. You need Python 3.8 or later. There are no packages to install with pip. The GUI also requires Tkinter and Tcl/Tk. Run `python -m tkinter` to check that a test window opens. If your system uses `python3`, substitute it in the examples below.

```sh
# Open the GUI
python enexconverter-en.py

# Convert two notebooks to HTML
python enexconverter-en.py "Work.enex" "Personal notes.enex"

# Convert notebooks in the current folder to Markdown
python enexconverter-en.py --markdown .

# Show help
python enexconverter-en.py --help
```

In the GUI, select one or more files or a folder, select **Markdown (.md)** if desired, then click **Start Conversion**. Leave the destination blank to save each notebook beside its source. The conversion runs in the background with a progress bar, counters and activity log. The CLI accepts a mix of files and folders. Folder searches are non-recursive.

## What gets exported

| Content | Output |
|---|---|
| HTML (default) | A UTF-8 page with a title, tables, responsive images and relative links to files in `_attachments/`. Open it in a browser. |
| Markdown | UTF-8 text with headings, lists, tables, bold, italics, quotations, code and links. Images from the ENEX are embedded as Base64 data URIs with their MIME type. PDFs and other attachments stay in `_attachments/`. |
| Tasks | Recognizes checkboxes and supported task records, preserves pending or completed status, and removes the known Evernote task placeholder warning. Task descriptions cannot be recovered from a placeholder alone. |
| Resources | Decodes Base64 attachments, matches them to the note using Evernote’s MD5 identifiers, and uses the original filename or an extension inferred from the MIME type. Missing references are flagged. |
| Filenames and large notebooks | Makes filenames Windows-compatible, shortens long names and adds numeric suffixes for duplicates. Reads XML incrementally and releases each processed note and resource. The largest individual resource affects memory use. |

For example, `Work.enex` produces `Work/Meeting.md` and may save a PDF as `Work/_attachments/budget.pdf`. To sync to Google Drive or another service, copy the entire folder. Embedded image rendering and relative link support depend on the viewer. Version 1.5 does not decrypt Evernote-protected text. Keep the original ENEX.

## Recovery and logs

- Output is written to `<notebook>.enex-partial` and renamed when finalized. Existing destinations are protected. After an interruption, choose a new destination or move the partial folder before retrying.
- If a note’s ENML cannot be parsed, the converter tries to preserve its content as text. For invalid notebook XML, it attempts to escape a stray `&` or remove a prohibited control character. It accepts the repair only after validating the note. The original export is preserved.
- If a note cannot be repaired, the converter tries to save the original fragment in `_recovery/` and resume at the next identifiable note. Warnings give the error position and, when available, the note title, mismatched tags and fragment path.
- **“Notebooks completed: 1/1”** means the output folder was finalized. Also check warnings, repaired notes and fragments needing recovery. An exported note can still contain a missing attachment reference.
- CLI exit codes are `0` for success without warnings, `1` for warnings or conversion failures, and `2` for input or GUI startup errors.
