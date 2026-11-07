"""Bounded inspection of visible page text and interactive controls."""

from __future__ import annotations

import re
from typing import Any

from app.browser.snapshot import (
    MAX_SNAPSHOT_URL_LENGTH,
    InteractiveElementSnapshot,
    PageSnapshot,
)


MAX_VISIBLE_TEXT = 5_000
MAX_ELEMENTS = 100
MAX_ATTRIBUTE_LENGTH = 160


class PageInspectionError(RuntimeError):
    """Raised when a page cannot be inspected, for example during navigation."""


_INSPECT_PAGE = f"""() => {{
  const textLimit = {MAX_VISIBLE_TEXT};
  const elementLimit = {MAX_ELEMENTS};
  const attributeLimit = {MAX_ATTRIBUTE_LENGTH};

  const compact = (value, limit = attributeLimit) => {{
    const source = value == null ? '' : String(value);
    let output = '';
    let pendingSpace = false;
    for (let index = 0; index < source.length; index++) {{
      const character = source[index];
      if (/\\s/.test(character)) {{
        pendingSpace = output.length > 0;
        continue;
      }}
      if (pendingSpace && output.length < limit) output += ' ';
      pendingSpace = false;
      if (output.length >= limit) break;
      output += character;
    }}
    return output;
  }};

  const visibleElement = element => {{
    if (!element || element.closest('[aria-hidden="true"]')) return false;
    for (let current = element; current && current !== document.documentElement; current = current.parentElement) {{
      if (current.hidden) return false;
      const style = window.getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
    }}
    return element.getClientRects().length > 0;
  }};

  const visibleText = root => {{
    if (!root) return {{ text: '', truncated: false }};
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let output = '';
    let pendingSpace = false;
    let truncated = false;
    let node;
    while ((node = walker.nextNode())) {{
      let parent = node.parentElement;
      let rendered = true;
      while (parent && parent !== document.documentElement) {{
        if (parent.hidden) {{ rendered = false; break; }}
        const style = window.getComputedStyle(parent);
        if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') {{
          rendered = false;
          break;
        }}
        parent = parent.parentElement;
      }}
      if (!rendered) continue;
      const source = node.nodeValue || '';
      for (let index = 0; index < source.length; index++) {{
        const character = source[index];
        if (/\\s/.test(character)) {{
          pendingSpace = output.length > 0;
          continue;
        }}
        if (pendingSpace && output.length < textLimit) output += ' ';
        pendingSpace = false;
        if (output.length >= textLimit) {{ truncated = true; break; }}
        output += character;
      }}
      if (truncated) break;
      // Keep separate text nodes from becoming a single merged word.
      pendingSpace = output.length > 0;
    }}
    return {{ text: output.trim(), truncated }};
  }};

  const accessibleName = element => {{
    const labelledBy = (element.getAttribute('aria-labelledby') || '').trim();
    if (labelledBy) {{
      const referenced = labelledBy.split(/\\s+/)
        .map(id => document.getElementById(id))
        .filter(Boolean)
        .map(target => compact(target.innerText || target.textContent || ''))
        .filter(Boolean);
      if (referenced.length) return compact(referenced.join(' '));
    }}
    const ariaLabel = compact(element.getAttribute('aria-label'));
    if (ariaLabel) return ariaLabel;
    const labels = element.labels ? Array.from(element.labels)
      .map(label => compact(label.innerText || label.textContent || ''))
      .filter(Boolean) : [];
    if (labels.length) return compact(labels.join(' '));
    if (element.matches('button, a[href], [role="button"], [role="link"]')) {{
      return compact(element.innerText || element.textContent || '');
    }}
    return compact(element.getAttribute('title') || element.getAttribute('alt') || '');
  }};

  const result = [];
  const seen = new Set();
  let elementsTruncated = false;
  const add = (element, kind, inferredRole) => {{
    if (seen.has(element) || !visibleElement(element)) return false;
    if (result.length >= elementLimit) {{ elementsTruncated = true; return false; }}
    seen.add(element);
    const labels = element.labels ? Array.from(element.labels)
      .map(label => compact(label.innerText || label.textContent || ''))
      .filter(Boolean) : [];
    result.push({{
      kind,
      role: compact(element.getAttribute('role') || inferredRole),
      accessible_name: accessibleName(element),
      label: compact(labels.join(' ')) || null,
      aria_label: compact(element.getAttribute('aria-label')) || null,
      placeholder: compact(element.getAttribute('placeholder')) || null,
      name: compact(element.getAttribute('name')) || null,
      test_id: compact(element.getAttribute('data-testid')) || null,
      text: kind === 'button' || kind === 'link' ? compact(element.innerText || element.textContent || '') || null : null,
      input_type: kind === 'input' ? compact(element.getAttribute('type') || 'text') || 'text' : null
    }});
    return true;
  }};

  const fillControlPriority = element => {{
    let score = element.form ? 2 : 0;
    if (element.type === 'search') score += 8;
    if (element.labels && element.labels.length) score += 5;
    if (element.getAttribute('placeholder')) score += 5;
    if (element.getAttribute('aria-label') || element.getAttribute('aria-labelledby')) score += 4;
    if (element.getAttribute('name') || element.getAttribute('data-testid')) score += 3;
    if (element.disabled || element.readOnly) score -= 10;
    if (['checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'hidden'].includes(element.type)) score -= 20;
    return score;
  }};
  const inputControls = Array.from(
    document.querySelectorAll('input:not([type="hidden" i])')
  ).sort((left, right) => fillControlPriority(right) - fillControlPriority(left));
  const groups = [
    {{ elements: inputControls, kind: 'input', role: 'textbox', quota: 40 }},
    {{ selector: 'select', kind: 'select', role: 'combobox', quota: 10 }},
    {{ selector: 'textarea', kind: 'textarea', role: 'textbox', quota: 10 }},
    {{ selector: '[role="textbox"], [role="combobox"], [contenteditable="true"]', kind: 'custom', role: 'textbox', quota: 15 }},
    {{ selector: 'button, [role="button"]', kind: 'button', role: 'button', quota: 8 }},
    {{ selector: 'a[href], [role="link"]', kind: 'link', role: 'link', quota: 7 }}
  ];
  for (const group of groups) {{
    let taken = 0;
    const candidates = group.elements || document.querySelectorAll(group.selector);
    for (const element of candidates) {{
      if (seen.has(element) || !visibleElement(element)) continue;
      if (taken >= group.quota) {{ elementsTruncated = true; break; }}
      const role = element.getAttribute('role') ||
        (group.kind === 'input' ? (element.type === 'checkbox' ? 'checkbox' :
          element.type === 'radio' ? 'radio' : element.type === 'search' ? 'searchbox' : 'textbox') :
        group.kind === 'select' ? (element.multiple ? 'listbox' : 'combobox') : group.role);
      if (add(element, group.kind, role)) taken++;
    }}
  }}

  if (result.length < elementLimit) {{
    const allInteractive = 'button, [role="button"], a[href], [role="link"], ' +
      'input:not([type="hidden" i]), select, textarea, ' +
      '[role="textbox"], [role="combobox"], [contenteditable="true"]';
    for (const element of document.querySelectorAll(allInteractive)) {{
      if (seen.has(element) || !visibleElement(element)) continue;
      const role = element.getAttribute('role') || '';
      const kind = element.matches('button, [role="button"]') ? 'button' :
        element.matches('a[href], [role="link"]') ? 'link' :
        element.matches('select') ? 'select' : element.matches('textarea') ? 'textarea' :
        element.matches('input') ? 'input' : 'custom';
      const inferred = role || (kind === 'button' ? 'button' : kind === 'link' ? 'link' :
        kind === 'select' ? (element.multiple ? 'listbox' : 'combobox') :
        kind === 'textarea' ? 'textbox' : kind === 'input' && element.type === 'checkbox' ? 'checkbox' :
        kind === 'input' && element.type === 'radio' ? 'radio' :
        kind === 'input' && element.type === 'search' ? 'searchbox' : 'textbox');
      if (add(element, kind, inferred) && result.length >= elementLimit) {{
        elementsTruncated = true;
        break;
      }}
    }}
  }}

  const textResult = visibleText(document.body);
  return {{
    title: compact(document.title),
    visible_text: textResult.text,
    visible_text_truncated: textResult.truncated,
    elements: result,
    elements_truncated: elementsTruncated
  }};
}}"""


def _normalize(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


async def inspect_page(page: Any) -> PageSnapshot:
    """Extract a bounded, value-free snapshot from a Playwright page object."""

    page_url = str(page.url)
    if len(page_url) > MAX_SNAPSHOT_URL_LENGTH:
        raise PageInspectionError(
            f"Page URL exceeds the supported snapshot limit of {MAX_SNAPSHOT_URL_LENGTH} characters"
        )
    try:
        data = await page.evaluate(_INSPECT_PAGE)
    except Exception as exc:
        raise PageInspectionError("Could not inspect the current page") from exc
    if not isinstance(data, dict):
        data = {}
    raw_elements = data.get("elements")
    if not isinstance(raw_elements, list):
        raw_elements = []

    elements: list[InteractiveElementSnapshot] = []
    for item in raw_elements[:MAX_ELEMENTS]:
        if not isinstance(item, dict):
            continue
        safe_item = {
            key: _normalize(item.get(key), MAX_ATTRIBUTE_LENGTH) or None
            for key in (
                "role", "accessible_name", "label", "aria_label", "placeholder",
                "name", "test_id", "text", "input_type",
            )
        }
        kind = item.get("kind")
        if kind not in {"button", "link", "input", "select", "textarea", "custom"}:
            continue
        elements.append(InteractiveElementSnapshot(kind=kind, **safe_item))

    visible_text = _normalize(data.get("visible_text"), MAX_VISIBLE_TEXT)
    text_truncated = bool(data.get("visible_text_truncated")) or len(
        str(data.get("visible_text") or "")
    ) > MAX_VISIBLE_TEXT
    elements_truncated = bool(data.get("elements_truncated")) or len(raw_elements) > MAX_ELEMENTS
    return PageSnapshot(
        url=page_url,
        title=_normalize(data.get("title"), MAX_ATTRIBUTE_LENGTH),
        visible_text=visible_text,
        visible_text_truncated=text_truncated,
        elements=elements,
        elements_truncated=elements_truncated,
        truncated=text_truncated or elements_truncated,
    )
