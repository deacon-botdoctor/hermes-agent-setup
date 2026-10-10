"""Behavior tests for the Hermes CUA-S1-FORMS scorer.

Seam: plan_form(form_title, elements, entities) -> decisions.
Expected labels come from the published form-fill contract, not from this
adapter's implementation.
"""

from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))


from score import (
    LocalTinyxBackend,
    ax_role_to_train_role,
    default_checkpoint,
    parse_ax_lines,
    plan_form,
    to_computer_use_plan,
)
from cua_s1.schema import Element, Entity

FORM = "Northwind Clinic - New Patient Registration"
ENTITIES = [
    Entity("Tel", "(503) 555-0142"),
    Entity("DOB", "03/14/1987"),
    Entity("Email", "pat@example.com"),
    Entity("Street", "12 Oak Ave"),
]


def backend():
    return LocalTinyxBackend(default_checkpoint(), device="cpu")


def test_phone_field_fills_tel_not_dob():
    elements = [Element("Edit", "Phone number", "", index=1)]
    decisions = plan_form(backend(), FORM, elements, ENTITIES)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "fill"
    assert d.entity_index == 0
    assert ENTITIES[d.entity_index].value == "(503) 555-0142"


def test_already_filled_matching_phone_is_skip():
    elements = [Element("Edit", "Phone number", "(503) 555-0142", index=1)]
    decisions = plan_form(backend(), FORM, elements, ENTITIES)
    assert decisions[0].action == "skip"


def test_email_not_street_on_email_field():
    elements = [Element("Edit", "Email address", "", index=2)]
    decisions = plan_form(backend(), FORM, elements, ENTITIES)
    assert decisions[0].action == "fill"
    assert ENTITIES[decisions[0].entity_index].label == "Email"


def test_submit_button_clicks():
    elements = [Element("Button", "Submit", index=9)]
    decisions = plan_form(backend(), FORM, elements, ENTITIES)
    assert decisions[0].action == "click"


def test_ax_role_map_and_parser():
    assert ax_role_to_train_role("AXTextField") == "Edit"
    assert ax_role_to_train_role("AXCheckBox") == "CheckBox"
    assert ax_role_to_train_role("AXButton") == "Button"
    lines = [
        '#1  AXStaticText \'Northwind Clinic - New Patient Registration\' @ (12, 8, 400, 20)',
        '#2  AXTextField \'Phone number\' value="" @ (80, 80, 200, 32)',
        '#3  AXButton \'Submit\' @ (80, 200, 80, 28)',
    ]
    parsed = parse_ax_lines(lines)
    assert [el.role for el in parsed] == ["Edit", "Button"]
    assert parsed[0].label == "Phone number"
    assert parsed[0].index == 2
    assert parsed[1].index == 3


def test_computer_use_plan_fills_then_omits_submit_by_default():
    elements = [
        Element("Edit", "Phone number", "", index=2),
        Element("Button", "Submit", index=3),
    ]
    decisions = plan_form(backend(), FORM, elements, ENTITIES)
    plan = to_computer_use_plan(decisions, ENTITIES, allow_submit=False)
    assert plan[0]["computer_use"]["action"] == "set_value"
    assert plan[0]["computer_use"]["element"] == 2
    assert plan[0]["computer_use"]["value"] == "(503) 555-0142"
    assert all(step["action"] != "click" for step in plan)

def test_exact_value_skip_is_deterministic():
    from cua_s1.schema import Decision
    e = Element('Edit', 'Email', 'pat@example.com', index=1)
    assert to_computer_use_plan([Decision(e,'fill',0,.99)], [Entity('Email','pat@example.com')]) == []


def test_checkbox_states_and_parser():
    from cua_s1.schema import Decision
    for state in [True, None]:
        e = Element('CheckBox','Accept',index=1,checked=state)
        assert to_computer_use_plan([Decision(e,'check',None,.99)],[]) == []
    e = Element('CheckBox','Accept',index=1,checked=False)
    assert len(to_computer_use_plan([Decision(e,'check',None,.99)],[])) == 1
    assert parse_ax_lines(['#1 AXCheckBox \'Accept\' value="1"'])[0].checked is True
    assert parse_ax_lines(['#1 AXCheckBox \'Accept\' value="0"'])[0].checked is False


def test_single_action_and_fresh_index():
    from cua_s1.schema import Decision
    entities = [Entity('Email','a@example.com'),Entity('Tel','123')]
    ds = [Decision(Element('Edit','Email',index=1),'fill',0,.99),Decision(Element('Edit','Phone',index=2),'fill',1,.99)]
    assert len(to_computer_use_plan(ds,entities)) == 1
    ds = [Decision(Element('Edit','Email','a@example.com',index=8),'fill',0,.99),Decision(Element('Edit','Phone',index=9),'fill',1,.99)]
    assert to_computer_use_plan(ds,entities)[0]['computer_use']['element'] == 9


def test_bad_indexes_and_unknown_state_block_submit():
    import pytest
    from cua_s1.schema import Decision
    for indexes in [[-1],[1,1]]:
        ds=[Decision(Element('Button','Submit',index=i),'click',None,.99) for i in indexes]
        with pytest.raises(ValueError):to_computer_use_plan(ds,[],allow_submit=True)
    ds=[Decision(Element('CheckBox','Accept',index=1),'skip',None,.99),Decision(Element('Button','Submit',index=2),'click',None,.99)]
    assert to_computer_use_plan(ds,[],allow_submit=True) == []

def test_edge_controls_require_handoff():
    from score import ObservedElement, observation_issues
    from cua_s1.schema import Decision
    for element in [ObservedElement('Edit','Email',index=1,enabled=False),ObservedElement('Edit','Email',index=1,read_only=True),ObservedElement('Edit','Email',index=1,enabled='false'),ObservedElement('ComboBox','State',index=1)]:
        assert observation_issues([element])
        assert to_computer_use_plan([Decision(element,'fill',0,.99)],[Entity('Email','x')],allow_submit=True)==[]
    es=[ObservedElement('Edit',' Phone ',index=1),ObservedElement('Edit','phone',index=2)]
    assert observation_issues(es)[0]['reason']=='duplicate_label_requires_context'


def test_ax_preserves_unsupported_controls():
    from score import observation_issues
    es=parse_ax_lines(["#1 AXComboBox 'State'", "#2 AXTextField 'Email' [disabled]", "#3 AXTextField 'Phone' [readonly]"])
    assert es[0].role=='ComboBox'
    assert len(observation_issues(es))==3


def test_linux_accessibility_roles():
    assert ax_role_to_train_role('text') == 'Edit'
    assert ax_role_to_train_role('entry') == 'Edit'
    assert ax_role_to_train_role('push button') == 'Button'
    assert ax_role_to_train_role('check box') == 'CheckBox'
    assert ax_role_to_train_role('combo box') == 'ComboBox'
