"""Fail-closed email-card spacing QA on Telegram delivery and Gmail send."""

from .telegram_email_card_lint import gate_response, gate_send_tool

__all__ = ["register"]


def on_transform_llm_output(response_text: str = "", **kwargs):
    return gate_response(response_text or "")


def on_pre_tool_call(tool_name: str = "", args=None, **kwargs):
    return gate_send_tool(tool_name or "", args if isinstance(args, dict) else {})


def register(ctx) -> None:
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
