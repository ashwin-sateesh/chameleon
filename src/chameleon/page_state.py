"""Serialize and restore in-page form progress (not covered by cookie storage_state)."""

from __future__ import annotations

import json
from typing import Any

DUMP_JS = """(() => {
  const fields = [];
  const seen = new Set();
  const labelFor = (el) => {
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return (lab.innerText || "").trim().slice(0, 120);
    }
    const parent = el.closest("label");
    return parent ? (parent.innerText || "").trim().slice(0, 120) : "";
  };
  const push = (el) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    const tag = (el.tagName || "").toLowerCase();
    const type = (el.type || tag).toLowerCase();
    if (type === "password" || type === "file" || type === "hidden" || type === "submit" || type === "button") return;
    if (el.disabled) return;
    fields.push({
      tag,
      type,
      name: el.name || "",
      id: el.id || "",
      value: (type === "checkbox" || type === "radio") ? "" : String(el.value || ""),
      checked: !!(el.checked),
      placeholder: el.placeholder || "",
      label: labelFor(el),
    });
  };
  document.querySelectorAll("input, textarea, select").forEach(push);
  document.querySelectorAll("[contenteditable='true']").forEach((el) => {
    if (seen.has(el)) return;
    seen.add(el);
    fields.push({
      tag: "contenteditable",
      type: "contenteditable",
      name: "",
      id: el.id || "",
      value: (el.innerText || "").slice(0, 20000),
      checked: false,
      placeholder: "",
      label: "",
    });
  });
  const bag = (store) => {
    const out = {};
    try {
      for (let i = 0; i < store.length; i++) {
        const key = store.key(i);
        if (key) out[key] = store.getItem(key);
      }
    } catch (err) {}
    return out;
  };
  return {
    url: location.href,
    title: document.title,
    scrollX: window.scrollX || 0,
    scrollY: window.scrollY || 0,
    fields,
    localStorage: bag(window.localStorage),
    sessionStorage: bag(window.sessionStorage),
  };
})()"""


def restore_js(page_state: dict[str, Any]) -> str:
    payload = json.dumps(page_state, ensure_ascii=False)
    return f"""(() => {{
  const s = {payload};
  const setValue = (el, value) => {{
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }};
  const find = (f) => {{
    if (f.id) {{
      const byId = document.getElementById(f.id);
      if (byId) return byId;
    }}
    if (f.name) {{
      const byName = document.querySelector(`[name="${{CSS.escape(f.name)}}"]`);
      if (byName) return byName;
    }}
    if (f.placeholder) {{
      const nodes = document.querySelectorAll("input[placeholder], textarea[placeholder]");
      for (const n of nodes) {{
        if ((n.placeholder || "") === f.placeholder) return n;
      }}
    }}
    return null;
  }};
  try {{
    for (const [k, v] of Object.entries(s.localStorage || {{}})) localStorage.setItem(k, v);
    for (const [k, v] of Object.entries(s.sessionStorage || {{}})) sessionStorage.setItem(k, v);
  }} catch (err) {{}}
  for (const f of s.fields || []) {{
    if (f.type === "contenteditable") {{
      const el = f.id ? document.getElementById(f.id) : null;
      if (el) {{
        el.innerText = f.value || "";
        el.dispatchEvent(new Event("input", {{ bubbles: true }}));
      }}
      continue;
    }}
    const el = find(f);
    if (!el) continue;
    const type = (el.type || "").toLowerCase();
    if (type === "checkbox" || type === "radio") {{
      el.checked = !!f.checked;
      el.dispatchEvent(new Event("change", {{ bubbles: true }}));
      continue;
    }}
    if (f.value) setValue(el, f.value);
  }}
  try {{ window.scrollTo(s.scrollX || 0, s.scrollY || 0); }} catch (err) {{}}
  return true;
}})()"""
