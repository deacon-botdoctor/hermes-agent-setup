# Bot Doctor Telegram

This optional plugin sends the first actual task-specific model commentary immediately,
then edits that same message with later commentary. It uses no extra model call,
reasoning stream, tool-name guesses, or generic opening.

Enable `botdoctor-telegram` through the native plugin manager. Keep Telegram
`streaming` and `interim_assistant_messages` off, and set
`display.platforms.telegram.long_running_notifications: false` so the native
heartbeat does not create a second progress message. Other platforms are unchanged.

The plugin observes persisted commentary before tools even with native interim
display disabled. It uses the admitted foreground turn, its native delivery adapter,
thread metadata, session/turn identity and run-ownership check. Stopped, held,
scheduled, muted and delegated turns do not publish progress. Updates serialize
per turn; an uncertain first send is not retried as a second bubble.

The final answer stays on the native delivery path. The plugin registers its message with native temporary-message cleanup, which
owns the existing `cleanup_progress` preference and failure policy. The native
post-delivery callback releases only plugin bookkeeping. Disabling the plugin restores
ordinary native behavior; re-enable native long-running notifications if desired.

This package owns progress presentation. It does not activate another profile
or change model, authentication, approval or login controls.
