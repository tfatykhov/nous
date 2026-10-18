"""Runs browser_toolset_20260801 member calls against a BrowserSession.

`execute(name, input)` returns the tool_result content blocks for one member
call (text, image and browser_state only). It raises BrowserActionError for a
call that failed (the loop turns it into an is_error result) and
ConfirmationRequired when the call would take a consequential step; the loop
then stops the whole task.

Element references: Playwright's AI snapshot tags elements `[ref=e5]` and
resolves them through the `aria-ref=e5` selector. The model sees them as
`[ref_5]`. Playwright keeps a node's ref stable across snapshots, so refs stay
valid until the page re-renders the node.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nous.browser.session import VIEWPORT, BrowserSession

logger = logging.getLogger(__name__)

# Off by default in the toolset; this executor never implements them.
DISABLED_MEMBERS = frozenset({"javascript_exec", "file_upload", "read_console", "read_network"})
# Results that carry no appended browser_state: tab members return exactly one, zoom renders none.
NO_STATE_MEMBERS = frozenset({"new_tab", "list_tabs", "switch_tab", "close_tab", "zoom"})

READ_PAGE_LIMIT = 50_000
PAGE_TEXT_LIMIT = 50_000
FIND_LIMIT = 20
_FIND_FIELD_WORDS = frozenset({"search", "box", "field", "input"})
ACTION_TIMEOUT_MS = 10_000
FINAL_SCREENSHOT_TIMEOUT_MS = 3_000
NAV_TIMEOUT_MS = 30_000
SCROLL_NOTCH_PX = 100

_PW_REF = re.compile(r"\[ref=([A-Za-z0-9]+)\]")
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "listbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
        "treeitem",
    }
)
# Narrow on purpose: a false positive ends the task, so only high-signal labels trip it.
# The model-level request_confirmation tool covers the judgment cases.
_CONSEQUENTIAL = re.compile(
    r"\b(buy|purchase|pay|checkout|check out|place (?:your |my )?order|confirm (?:order|purchase|payment|booking)"
    r"|book now|subscribe|donate|transfer|sign in|log ?in|sign up|register|send|post|publish|delete|remove account"
    r"|(?:i )?(?:accept|agree)(?: to)?(?: the)? (?:terms|conditions|tos|eula|privacy))\b",
    re.IGNORECASE,
)
_KEY_ALIASES = {
    "return": "Enter",
    "enter": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "space": "Space",
    "insert": "Insert",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "pageup": "PageUp",
    "page_up": "PageUp",
    "pagedown": "PageDown",
    "page_down": "PageDown",
    "home": "Home",
    "end": "End",
    "ctrl": "Control",
    "control": "Control",
    "shift": "Shift",
    "alt": "Alt",
    "option": "Alt",
    "cmd": "Meta",
    "command": "Meta",
    "meta": "Meta",
    "super": "Meta",
    "win": "Meta",
}

# Describes the element a call would act on, for the confirmation guard. `deepAt` and `deepActive` follow
# open shadow roots and same-origin frames; a cross-origin frame is reported as `frame` (uninspectable).
_ELEMENT_INFO_JS = """
const SEL = 'a,button,input,select,textarea,label,[role=button],[role=link],[role=menuitem],[role=checkbox],'
  + '[role=option],[role=switch],[role=tab]';
function isFrame(e) { return e.tagName === 'IFRAME' || e.tagName === 'FRAME'; }
function frameDoc(e) { try { return e.contentDocument; } catch (_) { return null; } }
function deepAt(doc, x, y) {
  let e = doc.elementFromPoint(x, y);
  while (e) {
    if (e.shadowRoot) {
      const inner = e.shadowRoot.elementFromPoint(x, y);
      if (inner && inner !== e) { e = inner; continue; }
    }
    if (isFrame(e)) {
      const d = frameDoc(e);
      if (!d) return e;
      const r = e.getBoundingClientRect();
      x -= r.left + e.clientLeft; y -= r.top + e.clientTop;
      const inner = d.elementFromPoint(x, y);
      if (!inner) return e;
      e = inner; continue;
    }
    return e;
  }
  return e;
}
function deepActive(doc) {
  let e = doc.activeElement;
  while (e) {
    if (e.shadowRoot && e.shadowRoot.activeElement) { e = e.shadowRoot.activeElement; continue; }
    if (isFrame(e)) { const d = frameDoc(e); if (!d) return e; e = d.activeElement; continue; }
    return e;
  }
  return e;
}
function info(el) {
  if (!el) return null;
  if (isFrame(el)) return {frame: true, label: '', type: '', autocomplete: '', submit: false, sensitiveForm: false};
  const t = el.closest(SEL) || el;
  const type = (t.getAttribute('type') || '').toLowerCase();
  const textEntry = t.tagName === 'TEXTAREA' || t.isContentEditable
    || (t.tagName === 'INPUT' && !['submit', 'button', 'reset', 'image', 'checkbox', 'radio'].includes(type));
  // A text field's value is what the model typed, not what the control does.
  const parts = [t.getAttribute('aria-label'), t.title, t.alt, t.innerText || t.textContent];
  if (!textEntry) parts.push(t.value);
  const form = t.form || t.closest('form');
  return {
    frame: false,
    label: parts.filter(v => typeof v === 'string' && v).join(' ').slice(0, 500),
    type: type,
    autocomplete: (t.getAttribute('autocomplete') || '').toLowerCase(),
    submit: type === 'submit' || (t.tagName === 'BUTTON' && !type),
    sensitiveForm: !!(form && form.querySelector('input[type=password], input[autocomplete^="cc-"]')),
  };
}
"""
_INFO_OF_ELEMENT = "(el) => {" + _ELEMENT_INFO_JS + "return info(el); }"
_INFO_AT_POINT = "([x, y]) => {" + _ELEMENT_INFO_JS + "return info(deepAt(document, x, y)); }"
_INFO_OF_FOCUS = "() => {" + _ELEMENT_INFO_JS + "return info(deepActive(document)); }"
# Keys that activate the focused control the way a click does.
_ACTIVATING_KEYS = frozenset({"Enter", "NumpadEnter", "Space", " "})


class BrowserActionError(Exception):
    """A member call failed; the message is the error text the model reads."""


class ConfirmationRequired(Exception):
    """A member call would take a consequential step; the task stops and asks the owner."""


def _to_model_refs(snapshot: str) -> str:
    def model_ref(match: re.Match[str]) -> str:
        pw = match.group(1)
        return f"[ref_{pw[1:] if re.fullmatch(r'e[0-9]+', pw) else pw}]"

    return _PW_REF.sub(model_ref, snapshot)


def _to_pw_ref(ref: str) -> str:
    if not isinstance(ref, str) or not re.fullmatch(r"ref_[A-Za-z0-9]+", ref):
        raise BrowserActionError(f"Error: {ref!r} is not an element reference. Use a [ref_N] from read_page or find.")
    tail = ref[4:]
    return f"e{tail}" if tail.isdigit() else tail


def _key_name(key: str) -> str:
    key = key.strip()
    if len(key) == 1:
        return key
    if re.fullmatch(r"[fF]\d{1,2}", key):
        return key.upper()
    return _KEY_ALIASES.get(key.lower(), key)


def _chord(text: str) -> str:
    return "+".join(_key_name(part) for part in text.split("+") if part)


def _modifier_list(modifiers: str | None) -> list[str]:
    return [_key_name(m) for m in (modifiers or "").split("+") if m.strip()]


def _image(b64: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def _seconds(duration: float) -> float:
    return max(0.0, min(30.0, float(duration)))


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


class BrowserExecutor:
    """Dispatches member calls by name; one instance per browser_task run."""

    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self.screenshot_count = 0

    @property
    def blocked_requests(self) -> int:
        return self.session.blocked_requests

    def current_url(self) -> str:
        try:
            return self.session.active_page.url
        except RuntimeError:
            return ""

    async def final_screenshot(self, path: Path) -> str | None:
        """Save the active tab's viewport for the owner; never sent to any model."""
        try:
            await self.session.active_page.screenshot(path=str(path), type="png", timeout=FINAL_SCREENSHOT_TIMEOUT_MS)
        except Exception as e:
            logger.warning("browser_task: final screenshot failed: %s", e)
            return None
        return str(path)

    async def execute(self, name: str, input_: dict[str, Any]) -> list[dict[str, Any]]:
        if name in DISABLED_MEMBERS:
            raise BrowserActionError(f"Error: {name} is not enabled in this environment.")
        handler = getattr(self, f"_m_{name}", None)
        if handler is None:
            raise BrowserActionError(f"Error: {name} is not implemented by this executor.")
        self.session.opened_during_call = []
        error: BrowserActionError | None = None
        try:
            blocks = await handler(**(input_ or {}))
        except ConfirmationRequired:
            raise  # raised before the step, so nothing to re-check
        except BrowserActionError as e:
            error = e
        except Exception as e:  # Playwright timeouts, detached nodes, closed pages, bad input
            logger.warning("browser_task: %s failed", name, exc_info=True)
            first_line = (str(e).splitlines() or [type(e).__name__])[0]
            error = BrowserActionError(f"Error: {name} failed: {first_line[:300]}")
        # Also after a failure: a navigation that timed out may still have left the allowlist.
        await self._enforce_allowlist()
        if error is not None:
            raise error
        if name in NO_STATE_MEMBERS:
            return blocks
        return [*blocks, await self.session.browser_state()]

    def _page(self, tab_id: str | None) -> Any:
        try:
            return self.session.page(tab_id)
        except KeyError:
            raise BrowserActionError(f"Error: tab {tab_id} does not exist. Call list_tabs.") from None

    async def _enforce_allowlist(self) -> None:
        """Routing misses redirects of a request it let through. A child frame that ended off the list is
        blanked; a tab that did is blanked (closed if that fails) and the call fails."""
        blocks = self.session.blocks
        left = False
        for page in list(self.session.tabs.values()):
            off_tab = blocks(page.url)
            for frame in page.frames:
                if frame is not page.main_frame and blocks(frame.url) and not off_tab:
                    self.session.blocked_requests += 1
                    try:
                        await frame.goto("about:blank", timeout=ACTION_TIMEOUT_MS)
                    except Exception:
                        off_tab = True
            if off_tab:
                left = True
                self.session.blocked_requests += 1
                try:
                    await page.goto("about:blank", timeout=ACTION_TIMEOUT_MS)
                except Exception:
                    logger.warning("browser_task: could not blank an off-allowlist tab; closing it", exc_info=True)
                    await page.close()
        if left:
            raise BrowserActionError(
                "Error: the page left the domain allowlist and was blocked. Only the allowed domains can be visited."
            )

    # -- targets and the guard ------------------------------------------------

    async def _locator(self, page: Any, target: dict[str, Any]) -> Any:
        locator = page.locator(f"aria-ref={_to_pw_ref(target.get('ref'))}")
        if await locator.count() == 0:
            raise BrowserActionError(
                f"Error: {target.get('ref')} is stale or not found on the current page. "
                "Re-read the page to get fresh references."
            )
        return locator.first

    @staticmethod
    def _point(target: dict[str, Any]) -> tuple[int, int]:
        try:
            x, y = int(target["x"]), int(target["y"])
        except (KeyError, TypeError, ValueError) as e:
            raise BrowserActionError("Error: a coordinate target needs integer x and y.") from e
        if not (0 <= x < VIEWPORT["width"] and 0 <= y < VIEWPORT["height"]):
            raise BrowserActionError(
                f"Error: ({x}, {y}) is outside the {VIEWPORT['width']}x{VIEWPORT['height']} viewport."
            )
        return x, y

    async def _info_at(self, page: Any, target: dict[str, Any]) -> dict[str, Any] | None:
        if target.get("type") == "ref":
            return await (await self._locator(page, target)).evaluate(_INFO_OF_ELEMENT)
        return await page.evaluate(_INFO_AT_POINT, list(self._point(target)))

    async def _focused_info(self, page: Any) -> dict[str, Any] | None:
        return await page.evaluate(_INFO_OF_FOCUS)

    @staticmethod
    def _guard(info: dict[str, Any] | None, action: str) -> None:
        """Stop before acting on a consequential control. The reason names only our own vocabulary,
        never the page's label text, because it reaches the outer agent."""
        if not info:
            return
        if info.get("frame"):
            raise ConfirmationRequired(f"{action} inside an embedded frame that the guard cannot inspect")
        match = _CONSEQUENTIAL.search(" ".join(info.get("label", "").split()))
        if match:
            word = " ".join(match.group(0).lower().split())
            raise ConfirmationRequired(
                f'{action} on a control labelled "{word}" looks like a consequential step '
                "(login, payment, message, terms or deletion)"
            )
        if info.get("submit") and info.get("sensitiveForm"):
            raise ConfirmationRequired(f"{action} would submit a form with password or payment fields")

    @staticmethod
    def _guard_entry(info: dict[str, Any] | None) -> None:
        if not info:
            return
        if info.get("frame"):
            raise ConfirmationRequired("typing inside an embedded frame that the guard cannot inspect")
        if info.get("type") == "password" or info.get("autocomplete", "").startswith(("cc-", "current-password")):
            raise ConfirmationRequired(
                "entering text into a password or payment field (credentials are never provided)"
            )

    async def _press(self, page: Any, chord: str) -> None:
        """Press one chord, checking the focused control first when the key activates it."""
        if chord.split("+")[-1] in _ACTIVATING_KEYS:
            info = await self._focused_info(page)
            if info and info.get("sensitiveForm") and "Enter" in chord:
                raise ConfirmationRequired("pressing Enter would submit a form with password or payment fields")
            self._guard(info, f"pressing {chord}")
        await page.keyboard.press(chord)

    # -- navigation and capture ------------------------------------------------

    async def _m_navigate(self, url: str, tab_id: str | None = None) -> list[dict[str, Any]]:
        page = self._page(tab_id)
        keyword = url.strip().lower()
        if keyword in ("back", "forward", "reload"):
            await {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[keyword](
                timeout=NAV_TIMEOUT_MS
            )
            return [_text(f"Navigated {keyword}.")]
        if "://" not in url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = "https://" + url
        try:
            scheme = urlsplit(url).scheme
        except ValueError:
            scheme = ""
        if scheme not in ("http", "https"):
            raise BrowserActionError("Error: Navigation refused. Only http and https URLs are allowed.")
        if not self.session.allowlist.allows(url):
            raise BrowserActionError(
                f"Error: Navigation refused. {urlsplit(url).hostname} is not on the domain allowlist "
                f"({', '.join(sorted(self.session.allowlist.hosts))})."
            )
        await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        return [_text(f"Navigated to {url}")]

    async def _m_screenshot(self, tab_id: str | None = None) -> list[dict[str, Any]]:
        self.screenshot_count += 1
        path = self.session.artifacts_dir / f"step-{self.screenshot_count:03d}.png"
        data = await self._page(tab_id).screenshot(type="png", path=str(path), timeout=ACTION_TIMEOUT_MS)
        return [_image(base64.b64encode(data).decode()), _text("Screenshot captured.")]

    async def _m_zoom(self, region: list[int], tab_id: str | None = None) -> list[dict[str, Any]]:
        try:
            x0, y0, x1, y1 = (int(v) for v in region)
        except (TypeError, ValueError) as e:
            raise BrowserActionError("Error: region must be [x0, y0, x1, y1] in viewport pixels.") from e
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(VIEWPORT["width"], x1), min(VIEWPORT["height"], y1)
        if x1 <= x0 or y1 <= y0:
            raise BrowserActionError("Error: region is empty or outside the viewport.")
        width, height = x1 - x0, y1 - y0
        scale = min(VIEWPORT["width"] / width, VIEWPORT["height"] / height, 4.0)
        page = self._page(tab_id)
        cdp = await page.context.new_cdp_session(page)
        try:
            shot = await cdp.send(
                "Page.captureScreenshot",
                {"format": "png", "clip": {"x": x0, "y": y0, "width": width, "height": height, "scale": scale}},
            )
        finally:
            await cdp.detach()
        return [_image(shot["data"])]

    # -- pointer ------------------------------------------------------------------

    async def _click(
        self, target: dict[str, Any], modifiers: str | None, tab_id: str | None, *, button: str, clicks: int, label: str
    ) -> list[dict[str, Any]]:
        page = self._page(tab_id)
        if button == "left":
            self._guard(await self._info_at(page, target), label)
        mods = _modifier_list(modifiers)
        if target.get("type") == "ref":
            locator = await self._locator(page, target)
            await locator.click(button=button, click_count=clicks, modifiers=mods or None, timeout=ACTION_TIMEOUT_MS)
            return [_text(f"Clicked element {target['ref']}.")]
        x, y = self._point(target)
        for m in mods:
            await page.keyboard.down(m)
        try:
            await page.mouse.click(x, y, button=button, click_count=clicks)
        finally:
            for m in reversed(mods):
                await page.keyboard.up(m)
        return [_text(f"Clicked at ({x}, {y}).")]

    async def _m_left_click(self, target: dict, modifiers: str | None = None, tab_id: str | None = None) -> list[dict]:
        return await self._click(target, modifiers, tab_id, button="left", clicks=1, label="click")

    async def _m_right_click(self, target: dict, modifiers: str | None = None, tab_id: str | None = None) -> list[dict]:
        return await self._click(target, modifiers, tab_id, button="right", clicks=1, label="right-click")

    async def _m_middle_click(
        self, target: dict, modifiers: str | None = None, tab_id: str | None = None
    ) -> list[dict]:
        return await self._click(target, modifiers, tab_id, button="middle", clicks=1, label="middle-click")

    async def _m_double_click(
        self, target: dict, modifiers: str | None = None, tab_id: str | None = None
    ) -> list[dict]:
        return await self._click(target, modifiers, tab_id, button="left", clicks=2, label="double-click")

    async def _m_triple_click(
        self, target: dict, modifiers: str | None = None, tab_id: str | None = None
    ) -> list[dict]:
        return await self._click(target, modifiers, tab_id, button="left", clicks=3, label="triple-click")

    async def _m_hover(self, target: dict, tab_id: str | None = None) -> list[dict]:
        page = self._page(tab_id)
        if target.get("type") == "ref":
            await (await self._locator(page, target)).hover(timeout=ACTION_TIMEOUT_MS)
        else:
            await page.mouse.move(*self._point(target))
        return [_text("Hovered.")]

    async def _m_left_click_drag(self, target: dict, tab_id: str | None = None, **kwargs: Any) -> list[dict]:
        page = self._page(tab_id)
        source = kwargs.get("from") or {}
        start, end = self._point(source), self._point(target)
        self._guard(await self._info_at(page, source), "drag")
        await page.mouse.move(*start)
        await page.mouse.down()
        await page.mouse.move(*end, steps=10)
        await page.mouse.up()
        return [_text(f"Dragged from {start} to {end}.")]

    async def _m_left_mouse_down(self, target: dict, tab_id: str | None = None) -> list[dict]:
        page = self._page(tab_id)
        self._guard(await self._info_at(page, target), "mouse down")
        await page.mouse.move(*self._point(target))
        await page.mouse.down()
        return [_text("Left button pressed.")]

    async def _m_left_mouse_up(self, target: dict, tab_id: str | None = None) -> list[dict]:
        page = self._page(tab_id)
        await page.mouse.move(*self._point(target))
        await page.mouse.up()
        return [_text("Left button released.")]

    async def _m_mouse_move(self, target: dict, tab_id: str | None = None) -> list[dict]:
        await self._page(tab_id).mouse.move(*self._point(target))
        return [_text("Moved the pointer.")]

    async def _m_scroll(
        self, target: dict, scroll_direction: str, scroll_amount: int = 3, tab_id: str | None = None
    ) -> list[dict]:
        page = self._page(tab_id)
        notches = max(1, min(10, int(scroll_amount))) * SCROLL_NOTCH_PX
        offsets = {"up": (0, -notches), "down": (0, notches), "left": (-notches, 0), "right": (notches, 0)}
        if scroll_direction not in offsets:
            raise BrowserActionError("Error: scroll_direction must be up, down, left or right.")
        dx, dy = offsets[scroll_direction]
        await page.mouse.move(*self._point(target))
        await page.mouse.wheel(dx, dy)
        await asyncio.sleep(0.2)  # let smooth scrolling settle before the next observation
        return [_text(f"Scrolled {scroll_direction}.")]

    async def _m_scroll_to(self, target: dict, tab_id: str | None = None) -> list[dict]:
        await (await self._locator(self._page(tab_id), target)).scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
        return [_text(f"Scrolled {target.get('ref')} into view.")]

    # -- keyboard and timing ------------------------------------------------------

    async def _m_type(self, text: str, tab_id: str | None = None) -> list[dict]:
        page = self._page(tab_id)
        # Typed newlines and tabs are key presses (Enter submits, Tab moves focus): send them through _press.
        for i, chunk in enumerate(re.split(r"(\r\n|[\r\n\t])", str(text))):
            if i % 2:
                await self._press(page, "Tab" if chunk == "\t" else "Enter")
            elif chunk:
                self._guard_entry(await self._focused_info(page))
                await page.keyboard.type(chunk)
        return [_text("Typed the text.")]

    async def _m_key(self, text: str, repeat: int = 1, tab_id: str | None = None) -> list[dict]:
        page = self._page(tab_id)
        chords = [_chord(part) for part in str(text).split()]
        for _ in range(max(1, min(100, int(repeat)))):
            for chord in chords:
                await self._press(page, chord)
        return [_text(f"Pressed {text}.")]

    async def _m_hold_key(self, text: str, duration: float, tab_id: str | None = None) -> list[dict]:
        page = self._page(tab_id)
        keys = [_key_name(k) for k in str(text).split("+") if k]
        if keys and keys[-1] in _ACTIVATING_KEYS:
            info = await self._focused_info(page)
            if info and info.get("sensitiveForm") and "Enter" in keys[-1]:
                raise ConfirmationRequired("holding Enter would submit a form with password or payment fields")
            self._guard(info, f"holding {keys[-1]}")
        for k in keys:
            await page.keyboard.down(k)
        try:
            await asyncio.sleep(_seconds(duration))
        finally:
            for k in reversed(keys):
                await page.keyboard.up(k)
        return [_text(f"Held {text}.")]

    async def _m_wait(self, duration: float, tab_id: str | None = None) -> list[dict]:
        await asyncio.sleep(_seconds(duration))
        return [_text(f"Waited {duration} seconds.")]

    # -- page reading ---------------------------------------------------------------

    async def _snapshot(self, page: Any, depth: int | None, ref: str | None) -> str:
        depth = max(1, int(depth)) if depth is not None else 15
        if ref:
            locator = await self._locator(page, {"ref": ref})
            raw = await locator.aria_snapshot(mode="ai", depth=depth, timeout=ACTION_TIMEOUT_MS)
        else:
            raw = await page.aria_snapshot(mode="ai", depth=depth, timeout=ACTION_TIMEOUT_MS)
        return _to_model_refs(raw)

    async def _m_read_page(
        self,
        filter: str | None = None,  # noqa: A002 -- the member's parameter name
        depth: int | None = None,
        ref: str | None = None,
        tab_id: str | None = None,
    ) -> list[dict]:
        text = await self._snapshot(self._page(tab_id), depth, ref)
        if filter == "interactive":
            text = "\n".join(line.strip() for line in text.splitlines() if _line_role(line) in _INTERACTIVE_ROLES)
        if len(text) > READ_PAGE_LIMIT:
            text = (
                text[:READ_PAGE_LIMIT]
                + f"\n[truncated at {READ_PAGE_LIMIT} characters; narrow with a smaller depth or a ref]"
            )
        return [_text(text or "(no elements)")]

    async def _m_find(self, query: str, tab_id: str | None = None) -> list[dict]:
        words = {w for w in re.findall(r"\w+", str(query).lower()) if len(w) > 1}
        lines = [
            line.strip()
            for line in (await self._snapshot(self._page(tab_id), None, None)).splitlines()
            if "[ref_" in line
        ]
        scored = []
        field_words = len(words & _FIND_FIELD_WORDS)
        for i, line in enumerate(lines):
            role = _line_role(line)
            score = sum(w in line.lower() for w in words) + (role in _INTERACTIVE_ROLES) * 0.5
            if role in ("textbox", "searchbox"):
                score += 0.5 * field_words
            if score >= 1:
                scored.append((-score, i, line))
        matches = [line for _, _, line in sorted(scored)[:FIND_LIMIT]]
        return [_text("\n".join(matches) if matches else f"No elements match {query!r}.")]

    async def _m_get_page_text(self, tab_id: str | None = None) -> list[dict]:
        text = await self._page(tab_id).evaluate(
            "() => (document.querySelector('main, article, [role=main]') || document.body || {innerText: ''}).innerText"
        )
        text = (text or "").strip()
        if len(text) > PAGE_TEXT_LIMIT:
            text = text[:PAGE_TEXT_LIMIT] + f"\n[truncated at {PAGE_TEXT_LIMIT} characters]"
        return [_text(text or "(the page has no visible text)")]

    # -- forms ------------------------------------------------------------------------

    async def _m_form_input(self, target: dict, value: str | float | bool, tab_id: str | None = None) -> list[dict]:
        locator = await self._locator(self._page(tab_id), target)
        info = await locator.evaluate(_INFO_OF_ELEMENT)
        self._guard_entry(info)
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
        if isinstance(value, bool) or info.get("type") in ("checkbox", "radio"):
            if value:
                self._guard(info, "checking")
            await locator.set_checked(bool(value), timeout=ACTION_TIMEOUT_MS)
        elif tag == "select":
            values = await locator.evaluate("el => [...el.options].map(o => o.value)")
            option = {"value": str(value)} if str(value) in values else {"label": str(value)}
            await locator.select_option(**option, timeout=ACTION_TIMEOUT_MS)
        else:
            await locator.fill(str(value), timeout=ACTION_TIMEOUT_MS)
        return [_text(f"Set {target.get('ref')}.")]

    # -- tabs -----------------------------------------------------------------------

    async def _m_new_tab(self) -> list[dict]:
        await self.session.new_tab()
        return [await self.session.browser_state()]

    async def _m_list_tabs(self) -> list[dict]:
        return [await self.session.browser_state()]

    async def _m_switch_tab(self, tab_id: str) -> list[dict]:
        page = self._page(tab_id)
        await page.bring_to_front()
        self.session.active_tab = tab_id
        return [await self.session.browser_state()]

    async def _m_close_tab(self, tab_id: str) -> list[dict]:
        await self._page(tab_id).close()
        return [await self.session.browser_state()]


def _line_role(line: str) -> str:
    match = re.match(r"\s*-\s*([a-z]+)", line)
    return match.group(1) if match else ""
