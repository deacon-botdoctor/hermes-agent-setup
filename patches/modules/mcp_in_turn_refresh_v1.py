#!/usr/bin/env python3
"""Refresh an agent's MCP tool snapshot after an in-turn activation."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_MCP_IN_TURN_REFRESH_v1"
ANCHOR = """        finally:
            self._executing_tools = False
"""
REPLACEMENT = f"""        finally:
            self._executing_tools = False
            # [{MARKER}] Safe boundary: all tool results are complete and the
            # next model request has not been assembled yet. A control tool may
            # have registered a cold MCP backend during this batch, so publish
            # the new schemas to this agent before its next API iteration.
            try:
                from tools.registry import registry as _live_tool_registry

                if _live_tool_registry._generation != getattr(
                    self, "_tool_snapshot_generation", -1
                ):
                    from tools.mcp_tool import refresh_agent_mcp_tools

                    _added_mcp_tools = refresh_agent_mcp_tools(
                        self, quiet_mode=True
                    )
                    if _added_mcp_tools:
                        logger.info(
                            "in-turn MCP refresh exposed %d tool(s)",
                            len(_added_mcp_tools),
                        )
            except Exception:
                logger.debug("in-turn MCP refresh skipped", exc_info=True)
"""


def patch_run_agent_text(source: str) -> str:
    if MARKER in source:
        return source
    if source.count(ANCHOR) != 1:
        raise RuntimeError("tool-execution finally anchor drift")
    return source.replace(ANCHOR, REPLACEMENT, 1)


def patch_mcp_in_turn_refresh_v1(hermes_dir: Path) -> bool:
    target = Path(hermes_dir) / "run_agent.py"
    original = target.read_text(encoding="utf-8")
    patched = patch_run_agent_text(original)
    if patched == original:
        return False
    target.write_text(patched, encoding="utf-8")
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_mcp_in_turn_refresh_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
