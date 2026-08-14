"""
Turns a declarative Locator (from the artifact) into a live Playwright
locator on the current page, trying the primary strategy first and then
each fallback in order. This is the single place that knows how each
LocatorStrategy maps to Playwright API calls - keeping it isolated means
adding a new strategy (e.g. for a desktop/accessibility-tree surface later)
touches one file, not the whole replay engine.

Returns (playwright_locator, strategy_used) so evidence can record exactly
which strategy resolved the element - useful both for debugging and for the
"confidence" signal mentioned as a stretch goal (an artifact that's
constantly falling through to its 3rd fallback is a drift signal).
"""
from __future__ import annotations
from agent.schemas import Locator, ElementTarget, LocatorStrategy


class LocatorResolutionError(Exception):
    def __init__(self, target: ElementTarget, attempts: list[str]):
        self.target = target
        self.attempts = attempts
        super().__init__(
            f"Could not resolve element after trying {len(attempts)} "
            f"strategies: {attempts}"
        )


def _frame_for(page, frame_path: list[str]):
    ctx = page
    for name in frame_path:
        ctx = ctx.frame_locator(name)
    return ctx


def _locate(ctx, loc: Locator):
    if loc.strategy == LocatorStrategy.TEST_ID:
        return ctx.get_by_test_id(loc.value)
    if loc.strategy == LocatorStrategy.ROLE_NAME:
        # value encoded as role=<role>[name='<name>'] by dom_observer; parse it back out
        role = loc.value.split("role=", 1)[1].split("[", 1)[0]
        name = loc.value.split("name=", 1)[1].rstrip("]").strip("'")
        return ctx.get_by_role(role, name=name)
    if loc.strategy == LocatorStrategy.FORM_FIELD_NAME:
        return ctx.locator(f'[name="{loc.value}"]')
    if loc.strategy == LocatorStrategy.LABEL_TEXT:
        # nearest-preceding-<td> pattern used by the mock legacy app: find the
        # <td> containing this text, then the first input/select/textarea/button
        # in the following sibling <td>.
        xpath = (
            f"xpath=//td[normalize-space(text())={_xpath_literal(loc.value)}]"
            f"/following-sibling::td[1]//*[self::input or self::select or self::textarea or self::button]"
        )
        return ctx.locator(xpath).first
    if loc.strategy == LocatorStrategy.EXACT_TEXT:
        return ctx.get_by_text(loc.value, exact=True)
    if loc.strategy == LocatorStrategy.CSS:
        return ctx.locator(loc.value)
    if loc.strategy == LocatorStrategy.XPATH:
        return ctx.locator(f"xpath={loc.value}")
    raise ValueError(f"unhandled locator strategy: {loc.strategy}")


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat('" + "', \"'\", '".join(parts) + "')"


def resolve(page, target: ElementTarget, timeout_ms: int = 8000):
    """Try primary then fallbacks; return the first locator that resolves to
    exactly one visible, attached element within timeout_ms (split across
    attempts). Raises LocatorResolutionError if none succeed."""
    ctx = _frame_for(page, target.frame_path)
    attempts: list[str] = []
    candidates = [target.primary, *target.fallbacks]
    per_attempt_timeout = max(500, timeout_ms // max(1, len(candidates)))

    for loc in candidates:
        attempts.append(f"{loc.strategy.value}:{loc.value}")
        try:
            locator = _locate(ctx, loc)
            locator.first.wait_for(state="visible", timeout=per_attempt_timeout)
            return locator.first, loc.strategy.value
        except Exception:
            continue

    raise LocatorResolutionError(target, attempts)
