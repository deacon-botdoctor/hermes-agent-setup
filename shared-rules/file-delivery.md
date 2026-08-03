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

For a voice memo produced with `text_to_speech`, the audio is the reply. Return
the generated `MEDIA:` directive without repeating the spoken prose as ordinary
chat text. Use no companion text by default; one short context line is the
maximum when the audio needs a label or qualification.

Do not use an external upload service when the current platform can attach the
file. If the source artifact is outside an allowed root, copy the finished
artifact into `~/.hermes/media/` first.
