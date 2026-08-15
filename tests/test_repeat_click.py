from chameleon.profiles import ChecklistItem
from chameleon.repeat_click import complete_after_repeat_click, is_repeat_click


def test_repeat_same_target():
    history = [
        {
            "tool": "browser_click",
            "arguments": {"element": "Add to cart", "target": "e54"},
            "reason": "add Bolt T-Shirt",
        }
    ]
    assert is_repeat_click(
        "browser_click",
        {"element": "Add to cart", "target": "e54"},
        "click again to be sure",
        history,
    )


def test_repeat_add_then_remove_on_cart_subgoal():
    subgoal = ChecklistItem(goal="add the chosen item to the cart", risk="none")
    history = [
        {
            "tool": "browser_click",
            "arguments": {"element": "Add to cart", "target": "e54"},
            "reason": "Click Add to cart for Sauce Labs Bolt T-Shirt",
        }
    ]
    assert is_repeat_click(
        "browser_click",
        {"element": "Remove", "target": "e99"},
        "button shows Add to cart so click it",
        history,
        subgoal,
    )
    assert complete_after_repeat_click(
        subgoal,
        {"element": "Remove", "target": "e99"},
        "undo?",
    )


def test_repeat_add_with_new_ref_still_skipped():
    subgoal = ChecklistItem(goal="add the chosen item to the cart", risk="none")
    history = [
        {
            "tool": "browser_click",
            "arguments": {"element": "Add to cart", "target": "e54"},
            "reason": "Click Add to cart for Sauce Labs Bolt T-Shirt",
        }
    ]
    assert is_repeat_click(
        "browser_click",
        {"element": "Add to cart", "target": "e60"},
        "The button shows Add to cart so clicking it will add the item",
        history,
        subgoal,
    )


def test_different_clicks_are_not_repeats():
    history = [
        {
            "tool": "browser_click",
            "arguments": {"element": "Login", "target": "e15"},
            "reason": "login",
        }
    ]
    assert not is_repeat_click(
        "browser_click",
        {"element": "Add to cart", "target": "e54"},
        "add",
        history,
    )
