---
name: email-card-qa
description: "Use when drafting or sending email. Lint the card/body first."
---

# Email card QA

Repair email-card presentation without removing the answer. Validate send bodies before invoking a send or draft tool.

## When to Use

- Drafting or showing an email
- A From/To/Subject card in chat
- Sending mail via Gmail/Composio/gog
- Email formatting or spacing looks wrong

## Hard Rule

Use the card linter to find presentation defects. Fix them without replacing substantive content with a warning:

```bash
python3 ~/.hermes/plugins/telegram-email-card-qa/telegram_email_card_lint.py --text '...exact card...'
# MIME body before send:
python3 ~/.hermes/plugins/telegram-email-card-qa/telegram_email_card_lint.py --mime --text '...body...'
```

Before sending or creating a draft, fix MIME body defects and rerun the MIME check until it exits 0. Do not invent missing recipients, senders, subjects, or body text. Ask for missing information when the requested action requires it.

If plugin `telegram-email-card-qa` is enabled, Gmail send/draft tools and
terminal `gog send`/`gmail send` are blocked when the MIME body fails.
Assembled chat cards receive safe spacing and address-format repairs. A remaining presentation defect must not discard the answer or imply that an email was sent. Streamed Telegram text can appear before that repair.

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
- From is required on every To/Subject send card. Missing From cannot be auto-fixed.
- Reply-to-thread is a send. The send tool must be the mailbox named on From.

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
