from chameleon.agents.guardian import _parse_verdict
from chameleon.ask_options import extract_ask_options


def test_extracts_or_choices():
    options = extract_ask_options(
        "Which t-shirt should I add: Sauce Labs Bolt T-Shirt or Sauce Labs Fleece Jacket?"
    )
    assert options == ["Sauce Labs Bolt T-Shirt", "Sauce Labs Fleece Jacket"]


def test_extracts_numbered_list():
    options = extract_ask_options(
        "Which item should I add?\n1. Bolt T-Shirt\n2. Fleece Jacket\n3. Onesie"
    )
    assert options == ["Bolt T-Shirt", "Fleece Jacket", "Onesie"]


def test_confirm_gets_yes_no():
    options = extract_ask_options("Should I submit the application now?")
    assert options == ["Yes", "No"]


def test_freeform_fields_are_not_choices():
    assert extract_ask_options("What zip code should I use?") == []
    assert extract_ask_options("Please provide first name, last name, email, and phone.") == []


def test_guardian_options_are_kept():
    options = extract_ask_options("Which shirt?", ["Bolt T-Shirt", "Fleece Jacket"])
    assert options == ["Bolt T-Shirt", "Fleece Jacket"]


def test_parse_verdict_includes_options():
    ask = _parse_verdict(
        {
            "decision": "ASK",
            "question": "Which t-shirt?",
            "options": ["Bolt T-Shirt", "Fleece Jacket"],
            "reason": "two matches",
        }
    )
    assert ask.options == ["Bolt T-Shirt", "Fleece Jacket"]
