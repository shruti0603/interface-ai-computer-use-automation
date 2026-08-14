"""
Observation layer for the agent loop.

Deliberately DOM/accessibility-grounded rather than screenshot+coordinate
based: coordinates are cheap to discover with but brittle to replay (they
break on any layout shift, window size change, or zoom level) and they give
the LLM no semantic handle to reason with. Since the target environment is
explicitly "often no clean DOM, no test IDs", we don't assume selectors
exist - we *generate* candidate locators ourselves from whatever structure
is available (name attributes, associated label text, table position,
visible text), ranked by expected stability. This ranking is exactly what
becomes each Step's primary + fallback locators in the artifact
(see schemas.Locator / ElementTarget).

This module has no LLM calls in it - it only turns a live page into a
plain-text/JSON snapshot the LLM (or a human reviewer) can read.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any

# Injected into the page and evaluated once per observation. Kept as plain
# JS (not a Playwright locator built element-by-element) because we need a
# single fast pass over the whole interactive surface, including elements
# with no id/name/testid at all.
_EXTRACT_JS = r"""
() => {
  function visible(el) {
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }

  function nearestLabelText(el) {
    // Common legacy pattern: <tr><td>Label</td><td><input ...></td></tr>
    const td = el.closest('td');
    if (td) {
      const tr = td.closest('tr');
      if (tr) {
        const cells = Array.from(tr.children);
        const idx = cells.indexOf(td);
        if (idx > 0) {
          const labelCell = cells[idx - 1];
          const txt = (labelCell.innerText || '').trim();
          if (txt) return txt;
        }
      }
    }
    // <label for="id"> or wrapping <label>
    if (el.id) {
      const lbl = document.querySelector(`label[for="${el.id}"]`);
      if (lbl) return (lbl.innerText || '').trim();
    }
    const wrapLabel = el.closest('label');
    if (wrapLabel) return (wrapLabel.innerText || '').trim();
    return null;
  }

  const interactiveSelector = 'input, button, select, textarea, a[href]';
  const readableSelector = 'th, td, h1, h2, h3, p, label, span';
  const interactiveNodes = Array.from(document.querySelectorAll(interactiveSelector));
  const readableNodes = Array.from(document.querySelectorAll(readableSelector)).filter(el => {
    if (el.matches(interactiveSelector)) return false;
    if (el.querySelector(interactiveSelector)) return false;
    const txt = (el.innerText || '').trim();
    return txt && txt.length <= 240;
  });
  const nodes = [...interactiveNodes, ...readableNodes];
  const out = [];
  let i = 0;
  for (const el of nodes) {
    if (!visible(el)) continue;
    i += 1;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const isInteractive = el.matches(interactiveSelector);
    const role = el.getAttribute('role') || (isInteractive ? (tag === 'a' ? 'link' : (tag === 'button' || type === 'submit' ? 'button' : tag)) : 'text');
    const text = (el.innerText || el.value || '').trim().slice(0, 120);
    out.push({
      ref: 'e' + i,
      tag: tag,
      type: type || null,
      role: role,
      name_attr: el.getAttribute('name'),
      id_attr: el.getAttribute('id'),
      test_id: el.getAttribute('data-testid') || el.getAttribute('data-qa'),
      aria_label: el.getAttribute('aria-label'),
      placeholder: el.getAttribute('placeholder'),
      href: el.getAttribute('href'),
      text: text,
      label_text: nearestLabelText(el),
      interactive: isInteractive,
      options: tag === 'select'
        ? Array.from(el.options).map(o => ({value: o.value, text: o.text}))
        : null,
    });
  }
  return out;
}
"""


@dataclass
class ObservedElement:
    ref: str
    tag: str
    type: str | None
    role: str
    name_attr: str | None
    id_attr: str | None
    test_id: str | None
    aria_label: str | None
    placeholder: str | None
    href: str | None
    text: str
    label_text: str | None
    interactive: bool = True
    options: list[dict] | None = None

    def to_prompt_line(self) -> str:
        kind = "interactive" if self.interactive else "readable"
        bits = [f"[{self.ref}] <{self.tag}", f"kind={kind}"]
        if self.type:
            bits.append(f"type={self.type}")
        bits.append(f"role={self.role}>")
        label = self.label_text or self.aria_label or self.placeholder
        if label:
            bits.append(f'label="{label}"')
        if self.text:
            bits.append(f'text="{self.text}"')
        if self.name_attr:
            bits.append(f'name="{self.name_attr}"')
        if self.options:
            opts = ", ".join(o["text"] for o in self.options if o["text"])
            bits.append(f"options=[{opts}]")
        return " ".join(bits)


@dataclass
class PageObservation:
    url: str
    title: str
    elements: list[ObservedElement]
    banner_text: str  # best-effort extraction of any red/error/notice-styled text on the page
    visible_text: str  # bounded visible body text so the model can reason about read-only data

    def to_prompt(self) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.banner_text:
            lines.append(f"NOTABLE PAGE TEXT: {self.banner_text}")
        if self.visible_text:
            lines.append("VISIBLE PAGE TEXT:")
            lines.append(self.visible_text)
        lines.append("PAGE ELEMENTS (interactive + readable):")
        for el in self.elements:
            lines.append("  " + el.to_prompt_line())
        return "\n".join(lines)


_VISIBLE_TEXT_JS = r"""
() => {
  const text = (document.body && document.body.innerText ? document.body.innerText : '').trim();
  return text.slice(0, 6000);
}
"""


_BANNER_JS = r"""
() => {
  const sel = '[style*="color: red"], [style*="color:red"], font[color="red"], .error, .warning, td[bgcolor="#ffe5e5"], td[bgcolor="#fff6d5"], td[bgcolor="#e6ffe6"]';
  const nodes = Array.from(document.querySelectorAll(sel));
  return nodes.map(n => (n.innerText || '').trim()).filter(Boolean).slice(0, 3).join(' | ');
}
"""


def observe(page) -> PageObservation:
    """Take a Playwright Page and return a structured, LLM- and
    human-readable observation. No side effects on the page."""
    raw_elements = page.evaluate(_EXTRACT_JS)
    elements = [ObservedElement(**e) for e in raw_elements]
    banner = page.evaluate(_BANNER_JS)
    visible_text = page.evaluate(_VISIBLE_TEXT_JS)
    return PageObservation(
        url=page.url,
        title=page.title(),
        elements=elements,
        banner_text=banner,
        visible_text=visible_text,
    )


# ---------------------------------------------------------------------------
# Locator generation - turns an ObservedElement into a ranked Locator/fallback
# set for the artifact. This is the logic that gets exercised at "record
# time"; replay just plays the resulting Locators back.
# ---------------------------------------------------------------------------

def build_element_target(el: ObservedElement) -> dict:
    """Returns a dict shaped like schemas.ElementTarget (kept as dict here to
    avoid a circular import; agent_loop.py wraps it in the pydantic model)."""
    candidates: list[dict] = []

    # Read-only values (table cells, headings, spans) should resolve by their
    # visible text. Form-label strategies intentionally locate a *control* in
    # the neighboring cell, so they are inappropriate for data extraction.
    if not el.interactive:
        if el.text:
            candidates.append({"strategy": "exact_text", "value": el.text,
                                "description": f"readable text '{el.text}'"})
        if el.id_attr:
            candidates.append({"strategy": "css", "value": f"#{el.id_attr}",
                                "description": "CSS id fallback for readable element"})
    else:
        if el.test_id:
            candidates.append({"strategy": "test_id", "value": el.test_id,
                                "description": f"data-testid={el.test_id}"})
        if el.aria_label:
            candidates.append({"strategy": "role_name", "value": f"role={el.role}[name={el.aria_label!r}]",
                                "description": f"{el.role} labelled '{el.aria_label}'"})
        if el.name_attr:
            candidates.append({"strategy": "form_field_name", "value": el.name_attr,
                                "description": f"form field name='{el.name_attr}'"})
        if el.label_text:
            candidates.append({"strategy": "label_text", "value": el.label_text,
                                "description": f"control in the row/label for '{el.label_text}'"})
        if el.text:
            candidates.append({"strategy": "exact_text", "value": el.text,
                                "description": f"{el.role} with visible text '{el.text}'"})
        if el.id_attr:
            candidates.append({"strategy": "css", "value": f"#{el.id_attr}",
                                "description": f"CSS id selector (fallback only)"})

    if not candidates:
        # last resort - should be rare given the selector list we scan
        candidates.append({"strategy": "css", "value": f"{el.tag}",
                            "description": "untargeted tag selector - low confidence"})

    return {
        "primary": candidates[0],
        "fallbacks": candidates[1:],
        "frame_path": [],
    }
