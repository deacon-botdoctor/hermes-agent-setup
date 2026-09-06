---
name: email-card-qa
description: "Use when drafting or sending email. Lint the card/body first."
---

# Email card QA

Fail-closed lint for email drafts, chat cards, and send bodies. A writing reminder is not enough.

## When to Use

- Drafting or showing an email
- A From/To/Subject card in chat
- Sending mail via Gmail/Composio/gog
- Email formatting or spacing looks wrong

## Hard Rule

Do not show or send until this exits 0:

```bash
python3 ~/.hermes/plugins/telegram-email-card-qa/telegram_email_card_lint.py --text '...exact card...'
# MIME body before send:
python3 ~/.hermes/plugins/telegram-email-card-qa/telegram_email_card_lint.py --mime --text '...body...'
```

Non-zero = fix, re-run, then deliver. Never ship a failing card with a caveat.

If plugin `telegram-email-card-qa` is enabled, Gmail send/draft tools and
terminal `gog send`/`gmail send` are blocked when the MIME body fails.
Assembled chat cards with a To/From/Subject header block are repaired or
held. Streamed Telegram text can still appear before that repair.

## Card format

```
To: name@domain
Cc: name@domain
From: name@domain
Subject: one line

Hello,
...
```

- Single newlines between labeled fields. No blank line between To/Cc/From/Subject.
- One blank line between header block and body, and between paragraphs.
- No trailing spaces. No `<email@domain>`. No tables. No `###`.

## Adversarial pass

Attack the text as the channel will render it:

1. Strip `<...>` tags (HTML parse_mode eats unknown tags and leaves a hole).
2. Treat two trailing spaces as a hard-break that inserts an extra gap.
3. Fail if a To/Cc/From address vanishes after the strip.
4. Fail if labeled fields are separated by a blank line.

Do not eyeball this. Run the script.

## Proof

```bash
python3 -m pytest bin/test/test_telegram_email_card_qa.py
```
