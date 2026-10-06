"""Local CUA-S1-FORMS scorer for Hermes desktop form fills.

Scores each form element against document entities in one forward pass.
Emits a plan of set_value/click steps. Does not send input itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

import torch

from cua_s1.model import ChoiceExample, load_checkpoint
from cua_s1.planner import Planner, filter_elements, order_decisions
from cua_s1.schema import Decision, Element, Entity, decode, render_context, render_options

DEFAULT_CHECKPOINT = Path.home() / "models" / "cua-s1-forms" / "cua-s1-forms.safetensors"
TASK = "fill the form from the document, then submit"

_AX_LINE = re.compile(
    r"^#(?P<index>\d+)\s+(?P<role>\S+)\s+(?:'(?P<label>[^']*)'|\"(?P<label2>[^\"]*)\")?"
    r"(?:\s+value=\"(?P<value>[^\"]*)\")?"
    r"(?:\s+value='(?P<value2>[^']*)')?",
    re.IGNORECASE,
)

_ROLE_MAP = {
    "edit": "Edit",
    "text": "Edit",
    "entry": "Edit",
    "pushbutton": "Button",
    "textfield": "Edit",
    "axtextfield": "Edit",
    "axcombobox": "ComboBox",
    "combobox": "ComboBox",
    "checkbox": "CheckBox",
    "axcheckbox": "CheckBox",
    "button": "Button",
    "axbutton": "Button",
}


@dataclass
class ObservedElement(Element):
    enabled: bool | None = True
    read_only: bool | None = False


def observation_issues(elements: Sequence[Element]) -> list[dict[str, Any]]:
    """Return explicit handoff reasons before any model call or action plan."""
    issues = []
    labels = {}
    for e in elements:
        key = (e.role, " ".join(e.label.casefold().split()))
        labels.setdefault(key, []).append(e.index)
        reason = None
        if getattr(e, "enabled", True) is not True:
            reason = "disabled_or_unknown_enabled_state"
        elif getattr(e, "read_only", False) is not False:
            reason = "read_only_or_unknown_editability"
        elif e.role == "ComboBox":
            reason = "dropdown_requires_browser_select"
        if reason:
            issues.append({"index": e.index, "reason": reason})
    for (role, label), indexes in labels.items():
        if len(indexes) > 1 and role in {"Edit", "ComboBox", "CheckBox"}:
            issues.append({"indexes": indexes, "reason": "duplicate_label_requires_context"})
    return issues


def default_checkpoint() -> Path:
    return DEFAULT_CHECKPOINT


def ax_role_to_train_role(role: str) -> str:
    key = role.replace("_", "").replace(" ", "").casefold()
    return _ROLE_MAP.get(key, role)


def _checked(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    if isinstance(value, str):
        if value.lower() in {"1", "true", "checked"}:
            return True
        if value.lower() in {"0", "false", "unchecked"}:
            return False
    return None


def parse_ax_lines(lines: Iterable[str]) -> list[Element]:
    """Parse SOM/AX index lines into scorer elements."""
    elements: list[Element] = []
    for raw in lines:
        line = raw.strip()
        if not line.startswith("#"):
            continue
        match = _AX_LINE.match(line)
        if not match:
            continue
        role = ax_role_to_train_role(match.group("role"))
        if role not in {"Edit", "CheckBox", "Button", "ComboBox"}:
            continue
        label = match.group("label") or match.group("label2") or ""
        value = match.group("value")
        if value is None:
            value = match.group("value2") or ""
        elements.append(
            ObservedElement(
                role=role,
                label=label,
                value=value,
                index=int(match.group("index")),
                checked=_checked(value) if role == "CheckBox" else None,
                enabled=not bool(re.search(r"\bdisabled\b", line[match.end():], re.I)),
                read_only=bool(re.search(r"\bread[-_ ]?only\b", line[match.end():], re.I)),
            )
        )
    return elements


class LocalTinyxBackend:
    """One-pass option scorer over a local safetensors checkpoint."""

    def __init__(self, checkpoint: str | Path, device: str = "cpu") -> None:
        path = Path(checkpoint).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"checkpoint not found: {path}")
        self.model, self.collator, self.config = load_checkpoint(path, device)
        self.device = torch.device(device)

    def plan(
        self,
        form_title: str,
        elements: list[Element],
        entities: list[Entity],
    ) -> list[Decision]:
        if not elements:
            return []
        options = tuple(render_options(entities))
        examples = [
            ChoiceExample(render_context(form_title, element), options, 0)
            for element in elements
        ]
        batch = {
            key: value.to(self.device) if torch.is_tensor(value) else value
            for key, value in self.collator(examples).items()
        }
        with torch.no_grad():
            logits = self.model(batch)
            probs = torch.softmax(logits.float(), dim=-1).cpu()
        decisions: list[Decision] = []
        for element, row in zip(elements, probs):
            live = row[: len(options)]
            index = int(live.argmax().item())
            action, entity_index = decode(index, entities)
            decisions.append(
                Decision(
                    element=element,
                    action=action,
                    entity_index=entity_index,
                    probability=float(live[index].item()),
                    distribution=[float(p) for p in live.tolist()],
                )
            )
        return decisions


def plan_form(
    backend: LocalTinyxBackend,
    form_title: str,
    elements: Sequence[Element],
    entities: Sequence[Entity],
) -> list[Decision]:
    planner = Planner(backend)
    return planner.plan(form_title, list(elements), list(entities))


def to_computer_use_plan(
    decisions: Sequence[Decision],
    entities: Sequence[Entity],
    *,
    allow_submit: bool = False,
    min_confidence: float = 0.5,
) -> list[dict[str, Any]]:
    """Map scored decisions to Hermes desktop-tool calls. Submit is opt-in."""
    # Numeric indexes are valid only for this observation. Return one action;
    # the caller must recapture and replan before requesting the next action.
    indexes = [d.element.index for d in decisions]
    if any(type(i) is not int or i < 0 for i in indexes) or len(set(indexes)) != len(indexes):
        raise ValueError("Action targets need unique nonnegative observation indexes")
    if observation_issues([d.element for d in decisions]):
        return []
    unresolved = any(
        (d.element.role == "CheckBox" and d.element.checked is None)
        or (d.action != "skip" and d.probability < min_confidence)
        for d in decisions
    )
    ordered = order_decisions(list(decisions), min_confidence,
                              allow_submit=allow_submit and not unresolved)
    plan: list[dict[str, Any]] = []
    for decision in ordered:
        element = decision.element.index
        if decision.action == "fill":
            assert decision.entity_index is not None
            value = entities[decision.entity_index].value
            if decision.element.role != "Edit":
                continue
            if decision.element.value == value:
                continue
            plan.append(
                {
                    "action": "fill",
                    "confidence": round(decision.probability, 4),
                    "label": decision.element.label,
                    "computer_use": {
                        "action": "set_value",
                        "element": element,
                        "value": value,
                    },
                }
            )
        elif decision.action in {"check", "click"}:
            if decision.action == "check" and (
                decision.element.role != "CheckBox" or decision.element.checked is not False
            ):
                continue
            plan.append(
                {
                    "action": decision.action,
                    "confidence": round(decision.probability, 4),
                    "label": decision.element.label,
                    "computer_use": {"action": "click", "element": element},
                }
            )
    return plan[:1]


def _entities_from_mapping(payload: dict[str, str] | list[dict[str, str]]) -> list[Entity]:
    if isinstance(payload, dict):
        return [Entity(str(label), str(value)) for label, value in payload.items()]
    return [Entity(item["label"], item["value"]) for item in payload]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--form", required=True)
    parser.add_argument("--entities-json", required=True)
    parser.add_argument("--elements-json", help="JSON list of {role,label,value,index}")
    parser.add_argument("--ax-file", help="SOM/AX capture text")
    parser.add_argument("--allow-submit", action="store_true")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    args = parser.parse_args(argv)

    entities = _entities_from_mapping(json.loads(Path(args.entities_json).read_text()))
    if args.elements_json:
        raw = json.loads(Path(args.elements_json).read_text())
        elements = [
            ObservedElement(
                role=ax_role_to_train_role(item["role"]),
                label=item.get("label", ""),
                value=item.get("value", ""),
                index=int(item.get("index", -1)),
                checked=_checked(item.get("checked")),
                enabled=item.get("enabled", True),
                read_only=item.get("read_only", False),
            )
            for item in raw
        ]
    elif args.ax_file:
        elements = parse_ax_lines(Path(args.ax_file).read_text().splitlines())
    else:
        parser.error("provide --elements-json or --ax-file")

    elements = filter_elements(elements)
    issues = observation_issues(elements)
    decisions = []
    if not issues:
        backend = LocalTinyxBackend(args.checkpoint, device=args.device)
        decisions = plan_form(backend, args.form, elements, entities)
    plan = to_computer_use_plan(
        decisions,
        entities,
        allow_submit=args.allow_submit,
        min_confidence=args.min_confidence,
    )
    json.dump(
        {
            "form": args.form,
            "review_required": bool(issues),
            "handoff_reasons": issues,
            "task": TASK,
            "recapture_and_replan_after_action": True,
            "elements_scored": len(elements),
            "decisions": [
                {
                    "index": d.element.index,
                    "role": d.element.role,
                    "label": d.element.label,
                    "action": d.action,
                    "confidence": round(d.probability, 4),
                    "entity": (
                        {
                            "label": entities[d.entity_index].label,
                            "value": entities[d.entity_index].value,
                        }
                        if d.entity_index is not None
                        else None
                    ),
                }
                for d in decisions
            ],
            "computer_use_plan": plan,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
