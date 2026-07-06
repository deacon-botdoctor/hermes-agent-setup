# local-document-tools MCP

Clean-room local document helper surface for the shared client floor. This starter implementation
handles safe local text/HTML extraction and text merging without vendoring PDF/OCR engines. PDF,
OCR, repair, overlay, and rich conversion should be wired as optional adapters around installed
system tools.

## Environment

- `LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES` defaults to `2000000`

## Tools

- `document_info(path)` returns file metadata.
- `extract_text(path)` reads text files and converts HTML to text.
- `html_to_text(path)` forces HTML-to-text conversion.
- `merge_text_documents(paths, separator)` extracts and joins multiple text/HTML files.

Run with:

```bash
PYTHONPATH=mcp-servers/local-document-tools/src python -m local_document_tools_mcp.server
```
