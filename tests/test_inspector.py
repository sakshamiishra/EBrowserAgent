from __future__ import annotations

import unittest

from app.browser.inspector import (
    MAX_ATTRIBUTE_LENGTH,
    MAX_ELEMENTS,
    MAX_SNAPSHOT_URL_LENGTH,
    MAX_VISIBLE_TEXT,
    PageInspectionError,
    _INSPECT_PAGE,
    inspect_page,
)


class FakePage:
    def __init__(
        self,
        payload: dict[str, object] | None = None,
        error: Exception | None = None,
        url: str = "https://example.com/",
    ) -> None:
        self.payload = payload or {}
        self.error = error
        self.script = ""
        self.url = url

    async def evaluate(self, script: str) -> dict[str, object]:
        self.script = script
        if self.error:
            raise self.error
        return self.payload


class InspectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_python_snapshot_normalizes_text_and_bounds_attributes(self) -> None:
        page = FakePage({
            "title": "  Example\n  Page ",
            "visible_text": " Hello\n\tworld ",
            "elements": [{
                "kind": "button", "text": " Save\n changes ",
                "aria_label": "x" * (MAX_ATTRIBUTE_LENGTH + 10),
                "accessible_name": " Save changes ",
            }],
        })
        snapshot = await inspect_page(page)
        self.assertEqual(snapshot.title, "Example Page")
        self.assertEqual(snapshot.visible_text, "Hello world")
        self.assertEqual(snapshot.elements[0].text, "Save changes")
        self.assertEqual(snapshot.elements[0].accessible_name, "Save changes")
        self.assertEqual(len(snapshot.elements[0].aria_label or ""), MAX_ATTRIBUTE_LENGTH)

    async def test_text_is_truncated_and_flag_preserved(self) -> None:
        # The mock validates Python-side output only; it does not execute the
        # page script or demonstrate the browser-side pre-transfer cap.
        page = FakePage({
            "title": "Example",
            "visible_text": "x" * (MAX_VISIBLE_TEXT + 100),
            "visible_text_truncated": True,
            "elements": [],
        })
        snapshot = await inspect_page(page)
        self.assertEqual(len(snapshot.visible_text), MAX_VISIBLE_TEXT)
        self.assertTrue(snapshot.visible_text_truncated)
        self.assertTrue(snapshot.truncated)

    async def test_embedded_script_caps_text_before_returning_and_reports_truncation(self) -> None:
        # Source-level contract check only: no JavaScript runtime or DOM is used.
        self.assertIn("const textLimit = 5000", _INSPECT_PAGE)
        self.assertIn("document.createTreeWalker(root, NodeFilter.SHOW_TEXT)", _INSPECT_PAGE)
        self.assertIn("visible_text_truncated: textResult.truncated", _INSPECT_PAGE)
        self.assertNotIn("document.body.innerText", _INSPECT_PAGE)

    async def test_mixed_element_categories_and_custom_controls_survive_python_mapping(self) -> None:
        kinds = ["button", "link", "input", "select", "textarea", "custom"]
        page = FakePage({
            "title": "Controls",
            "visible_text": "Controls",
            "elements": [
                {"kind": kind, "role": "textbox" if kind == "custom" else kind}
                for kind in kinds
            ],
        })
        snapshot = await inspect_page(page)
        self.assertEqual([element.kind for element in snapshot.elements], kinds)
        # The mocked payload is not evidence that the JavaScript DOM selectors work.
        self.assertIn("[role=\"textbox\"], [role=\"combobox\"]", _INSPECT_PAGE)

    async def test_search_inputs_use_their_native_searchbox_role(self) -> None:
        self.assertIn("element.type === 'search' ? 'searchbox' : 'textbox'", _INSPECT_PAGE)
        self.assertIn("kind === 'input' && element.type === 'search' ? 'searchbox'", _INSPECT_PAGE)

    async def test_fillable_controls_are_prioritized_before_navigation_controls(self) -> None:
        # Source-level contract: crowded navigation groups cannot consume the cap first.
        input_group = _INSPECT_PAGE.index("const inputControls = Array.from(")
        input_collection = _INSPECT_PAGE.index("document.querySelectorAll('input:not([type=\"hidden\" i])')")
        first_navigation_group = _INSPECT_PAGE.index("selector: 'button, [role=\"button\"]'")
        self.assertLess(input_group, first_navigation_group)
        self.assertLess(input_collection, first_navigation_group)
        self.assertIn("quota: 40", _INSPECT_PAGE)
        self.assertIn("element.getAttribute('placeholder')", _INSPECT_PAGE)
        self.assertIn("element.getAttribute('aria-label')", _INSPECT_PAGE)
        self.assertIn("element.labels.length", _INSPECT_PAGE)

    async def test_hidden_ancestor_and_labelledby_handling_are_in_embedded_script(self) -> None:
        # Static checks make the limits of this non-browser test explicit.
        self.assertIn("element.closest('[aria-hidden=\"true\"]')", _INSPECT_PAGE)
        self.assertIn("getAttribute('aria-labelledby')", _INSPECT_PAGE)
        self.assertIn("document.getElementById(id)", _INSPECT_PAGE)

    async def test_password_metadata_is_exposed_but_values_are_excluded(self) -> None:
        page = FakePage({
            "title": "Login",
            "visible_text": "Email Password",
            "elements": [
                {"kind": "input", "input_type": "email", "name": "email", "value": "person@example.com"},
                {"kind": "input", "input_type": "PASSWORD", "name": "password", "label": "Password", "value": "secret"},
            ],
        })
        snapshot = await inspect_page(page)
        encoded = snapshot.model_dump_json()
        self.assertNotIn("person@example.com", encoded)
        self.assertNotIn("secret", encoded)
        self.assertEqual(len(snapshot.elements), 2)
        password = snapshot.elements[1]
        self.assertEqual(password.input_type, "PASSWORD")
        self.assertEqual(password.label, "Password")
        self.assertNotIn("value", type(password).model_fields)
        self.assertIn("input:not([type=\"hidden\" i])", _INSPECT_PAGE)
        self.assertNotIn("element.value", _INSPECT_PAGE)

    async def test_element_and_attribute_limits_are_enforced(self) -> None:
        page = FakePage({
            "title": "Example",
            "visible_text": "short",
            "elements": [
                {"kind": "button", "name": str(index), "text": "a" * 300}
                for index in range(MAX_ELEMENTS + 10)
            ],
            "elements_truncated": True,
        })
        snapshot = await inspect_page(page)
        self.assertEqual(len(snapshot.elements), MAX_ELEMENTS)
        self.assertTrue(snapshot.elements_truncated)
        self.assertTrue(snapshot.truncated)
        self.assertTrue(all(len(element.text or "") <= MAX_ATTRIBUTE_LENGTH for element in snapshot.elements))

    async def test_evaluation_failure_becomes_inspector_error(self) -> None:
        page = FakePage(error=RuntimeError("execution context was destroyed"))
        with self.assertRaisesRegex(PageInspectionError, "Could not inspect"):
            await inspect_page(page)

    async def test_snapshot_accepts_url_at_configured_limit(self) -> None:
        exact_limit_url = "https://example.com/" + "x" * (
            MAX_SNAPSHOT_URL_LENGTH - len("https://example.com/")
        )
        self.assertEqual(len(exact_limit_url), MAX_SNAPSHOT_URL_LENGTH)
        page = FakePage(
            {"title": "Long URL", "visible_text": "ok", "elements": []},
            url=exact_limit_url,
        )
        snapshot = await inspect_page(page)
        self.assertEqual(len(snapshot.url), MAX_SNAPSHOT_URL_LENGTH)

    async def test_oversized_snapshot_url_raises_clear_inspection_error_before_evaluate(self) -> None:
        oversized_url = "https://example.com/" + "x" * (
            MAX_SNAPSHOT_URL_LENGTH + 1 - len("https://example.com/")
        )
        self.assertEqual(len(oversized_url), MAX_SNAPSHOT_URL_LENGTH + 1)
        page = FakePage(url=oversized_url)
        with self.assertRaisesRegex(PageInspectionError, "Page URL exceeds the supported snapshot limit"):
            await inspect_page(page)
        self.assertEqual(page.script, "")


if __name__ == "__main__":
    unittest.main()
