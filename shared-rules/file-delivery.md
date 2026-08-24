## File delivery

Attach downloadable artifacts with `MEDIA:<absolute-path>`; write ordinary text
inline. Never paste a file path as a substitute for attachment.

Before sending:

1. Confirm the file exists, is the intended artifact, uses a supported media
   extension, and is under the platform size limit.
2. Put it under `~/.hermes/media/` or another operator-allowlisted media root.
3. Use an absolute path. For a Telegram voice note, prefix the media tag with
   `[[audio_as_voice]]`.
4. Verify the attachment actually arrived before claiming delivery.

For lossless image masters such as logos, wordmarks, transparent PNGs, print
exports, or assets whose exact pixels matter, force document delivery instead
of the platform photo path. On Telegram, emit `[[as_document]]` once before the
`MEDIA:<absolute-path>` attachment. Photo delivery may resize, recompress, or
strip transparency and therefore cannot serve as proof of the master file.

For a voice memo produced with `text_to_speech`, the audio is the reply. Return
the generated `MEDIA:` directive without repeating the spoken prose as ordinary
chat text. Use no companion text by default; one short context line is the
maximum when the audio needs a label or qualification.

Do not use an external upload service when the current platform can attach the
file. If the source artifact is outside an allowed root, copy the finished
artifact into `~/.hermes/media/` first.

## Client-presentable document release modes

For PDFs, slide exports, one-pagers, brochures, proposals, reports, and their
preview images, distinguish owner review from final release:

- An owner-review draft may be delivered after basic artifact and render checks
  without semantic visual PASS. Label it exactly `Layout draft only — not
  production-ready`; do not describe or record it as passing final QA.
- A final release requires visual inspection with `vision_analyze` and a receipt
  bound to the SHA-256 of both the artifact and the inspected preview. A naked
  Boolean or an unbound statement that visual QA passed is not evidence.

Use `~/.hermes/bin/client-doc-artifact-qa` for this release decision when it is
installed. Artifact creation, QA, and Telegram delivery proof remain separate
facts.
