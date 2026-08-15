from chameleon.page_state import restore_js


def test_restore_js_embeds_field_values():
    script = restore_js(
        {
            "url": "https://example.com",
            "fields": [{"id": "email", "name": "email", "value": "jane@x.com", "type": "email"}],
            "localStorage": {},
            "sessionStorage": {"step": "2"},
        }
    )
    assert "jane@x.com" in script
    assert "sessionStorage" in script
    assert "HTMLInputElement" in script
