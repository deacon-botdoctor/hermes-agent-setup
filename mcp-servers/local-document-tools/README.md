# local-document-tools MCP

Clean-room local document helper surface for the shared client floor. This starter implementation
handles safe local text/HTML extraction and text merging without vendoring PDF/OCR engines. PDF,
OCR, repair, overlay, and rich conversion should be wired as optional adapters around installed
system tools.

## Environment

- `LOCAL_DOCUMENT_TOOLS_MAX_READ_BYTES` defaults to `2000000`
- `LOCAL_DOCUMENT_TOOLS_ROOTS` is an `os.pathsep`-separated allowlist of readable document
  roots. It defaults to the server's current working directory when unset. Files outside these
  resolved roots are rejected before reading.

## Tools

- `document_info(path)` returns file metadata.
- `extract_text(path)` reads text files and converts HTML to text.
- `html_to_text(path)` forces HTML-to-text conversion.
- `document_convert(path, target_format="text", source_format=None)` converts text/HTML inputs
  to text and returns `error_code="unsupported_conversion"` for formats this starter does not
  implement.
- `merge_text_documents(paths, separator)` extracts and joins multiple text/HTML files.

Run with:

```bash
PYTHONPATH=mcp-servers/local-document-tools/src python -m local_document_tools_mcp.server
```
