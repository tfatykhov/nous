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
import contextlib
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

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
# URL guard (navigation is an action too: a GET can unsubscribe, sign in or delete). Heuristic, deliberately
# blunt: the URL's path and query (names and values, camelCase split, punctuation as spaces; never the
# fragment, which is not sent) are searched for these words. A plain page such as
# /blog/how-to-cancel-a-subscription trips it as well; confirmation is cheap. Sign-in words count only
# beside a token-like parameter, but a parameter named token, code or key trips on its own.
_URL_CONSEQUENTIAL = re.compile(
    r"\b(unsubscribe|delete|remove|cancel|confirm|approve|verify|magic|logout|log out|sign ?out"
    r"|pay|payment|checkout|check out|purchase|subscribe|transfer)\b"
)
_URL_SIGN_IN = re.compile(r"\b(sign ?in|log ?in)\b")
_URL_SECRET_PARAM = re.compile(r"\b(token|code|key)\b")
_URL_TOKENISH_PARAM = re.compile(r"token|code|key|auth|otp|sig|ticket|nonce|secret")
_URL_TOKENISH_VALUE = re.compile(r"[A-Za-z0-9_-]{16,}")


def _url_words(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"([a-z])([A-Z])", r"\1 \2", unquote(value)).lower()).strip()


def consequential_url(url: str) -> str | None:
    """The word that makes loading `url` look like a consequential step, or None. Only our own vocabulary
    is returned, never any part of the URL: the query may carry a token."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    params = parse_qsl(parts.query, keep_blank_values=True)
    text = _url_words(parts.path + " " + " ".join(f"{k} {v}" for k, v in params))
    if match := _URL_CONSEQUENTIAL.search(text):
        return " ".join(match.group(0).split())
    if any(_URL_SECRET_PARAM.search(_url_words(k)) for k, _ in params):
        return "a token, code or key parameter"
    if (match := _URL_SIGN_IN.search(text)) and any(
        _URL_TOKENISH_PARAM.search(k.lower()) or _URL_TOKENISH_VALUE.fullmatch(v) for k, v in params
    ):
        return " ".join(match.group(0).split())
    return None


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
# The label joins the control's aria-label, its associated <label>s and aria-labelledby targets, its title,
# alt, text and (for a non-text control) value; for a ref target the guard adds Playwright's accessible name.
# A payment field is one whose autocomplete is cc-*, whose shape is a card number (inputmode=numeric with a
# maxlength of 12 to 19), or whose name, id, aria-label, placeholder or label names a card, its number, CVC,
# expiry or security code, an IBAN, a routing number or an account number. `sensitiveForm`: the control's form
# (its form owner, or the <form> around it) has a password or payment field.
_ELEMENT_INFO_JS = """
const SEL = 'a,button,input,select,textarea,label,[role=button],[role=link],[role=menuitem],[role=checkbox],'
  + '[role=option],[role=switch],[role=tab]';
const NOT_TEXT = ['submit', 'button', 'reset', 'image', 'checkbox', 'radio'];
const NOT_FIELD = NOT_TEXT.concat(['hidden', 'file']);
const PAYMENT = new RegExp('\\\\b(?:card(?: ?(?:number|num|no))?|cc(?: ?(?:number|num|no|exp|csc|cvc|cvv))?'
  + '|cvc2?|cvv2?|csc|expiry|expiration(?: ?date)?|exp ?date|security ?code|iban|routing(?: ?number)?'
  + '|account ?(?:number|num|no))\\\\b');
function words(v) {
  return String(v).replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[^A-Za-z0-9]+/g, ' ').toLowerCase();
}
function labelText(e) {
  const out = [];
  for (const l of (e.labels || [])) out.push(l.innerText || l.textContent);
  const root = e.getRootNode();
  for (const id of (e.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean)) {
    const r = (root && root.getElementById && root.getElementById(id)) || e.ownerDocument.getElementById(id);
    if (r) out.push(r.innerText || r.textContent);
  }
  return out.join(' ');
}
function isField(e) {
  if (e.tagName === 'INPUT') return !NOT_FIELD.includes((e.type || '').toLowerCase());
  return e.tagName === 'SELECT' || e.tagName === 'TEXTAREA' || !!e.isContentEditable;
}
function isPassword(e) { return e.tagName === 'INPUT' && (e.type || '').toLowerCase() === 'password'; }
function isPayment(e) {
  if (!isField(e)) return false;
  if (/(^|\\s)cc-/.test((e.getAttribute('autocomplete') || '').toLowerCase())) return true;
  const max = parseInt(e.getAttribute('maxlength') || '', 10);
  if ((e.getAttribute('inputmode') || '').toLowerCase() === 'numeric' && max >= 12 && max <= 19) return true;
  return [e.getAttribute('name'), e.id, e.getAttribute('aria-label'), e.getAttribute('placeholder'), labelText(e)]
    .some(v => v && PAYMENT.test(words(v)));
}
function closestDeep(e, sel) {
  while (e) {
    const hit = e.closest(sel);
    if (hit) return hit;
    const root = e.getRootNode();
    e = root && root.host ? root.host : null;
  }
  return null;
}
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
const IMPLICIT_TYPES = ['text', 'search', 'url', 'tel', 'email', 'password', 'date', 'month', 'week', 'time',
  'datetime-local', 'number'];
function isSubmitter(e) {
  const ty = (e.getAttribute('type') || '').toLowerCase();
  if (e.tagName === 'BUTTON') return ty === '' || ty === 'submit';
  return e.tagName === 'INPUT' && (ty === 'submit' || ty === 'image');
}
// What Enter in a text field of a form would submit through: the form's default (first) submit control, with its
// label, or `opaque` when there is none to inspect but the form still has an action or an onsubmit handler and
// a single text field (which submits on Enter alone). Null when Enter submits nothing.
function implicitSubmit(c, form) {
  if (c.tagName !== 'INPUT' || !IMPLICIT_TYPES.includes((c.getAttribute('type') || 'text').toLowerCase())) return null;
  if (!form) return null;
  const btn = [...form.elements].find(isSubmitter);
  if (btn) {
    const parts = [btn.getAttribute('aria-label'), labelText(btn), btn.title, btn.alt, btn.innerText || btn.textContent,
      btn.value];
    return {opaque: false, label: parts.filter(v => typeof v === 'string' && v).join(' ').slice(0, 500)};
  }
  const fields = [...form.elements].filter(
    e => e.tagName === 'INPUT' && IMPLICIT_TYPES.includes((e.getAttribute('type') || 'text').toLowerCase()));
  if (fields.length > 1) return null;
  if (form.hasAttribute('action') || form.hasAttribute('onsubmit')) return {opaque: true, label: ''};
  return null;
}
function info(el) {
  if (!el) return null;
  if (isFrame(el)) {
    return {frame: true, label: '', type: '', autocomplete: '', textEntry: false, payment: false, sensitiveForm: false};
  }
  const t = el.closest(SEL) || el;
  // A label acts for its control: clicking it clicks the control.
  const c = t.tagName === 'LABEL' && t.control ? t.control : t;
  const type = (c.getAttribute('type') || '').toLowerCase();
  const textEntry = c.tagName === 'TEXTAREA' || c.isContentEditable
    || (c.tagName === 'INPUT' && !NOT_TEXT.includes(type));
  const parts = [t.getAttribute('aria-label'), labelText(t), t.title, t.alt, t.innerText || t.textContent];
  if (c !== t) parts.push(c.getAttribute('aria-label'), labelText(c), c.title);
  // A text field's value is what the model typed, not what the control does.
  if (!textEntry) parts.push(c.value);
  const form = c.form || closestDeep(t, 'form');
  return {
    frame: false,
    label: parts.filter(v => typeof v === 'string' && v).join(' ').slice(0, 500),
    type: type,
    autocomplete: (c.getAttribute('autocomplete') || '').toLowerCase(),
    textEntry: !!textEntry,
    payment: isPayment(c),
    sensitiveForm: !!form && [...form.elements].some(e => isPassword(e) || isPayment(e)),
    implicitSubmit: implicitSubmit(c, form),
  };
}
"""
_INFO_OF_ELEMENT = "(el) => {" + _ELEMENT_INFO_JS + "return info(el); }"
_INFO_AT_POINT = "([x, y]) => {" + _ELEMENT_INFO_JS + "return info(deepAt(document, x, y)); }"
_INFO_OF_FOCUS = "() => {" + _ELEMENT_INFO_JS + "return info(deepActive(document)); }"
# The text a select_option(value) would act on: the chosen option's text, label and value, plus the select's
# aria-label, title and <label>s (not the other options).
_SELECT_CHOICE_JS = """(el, want) => {
  const opts = [...el.options];
  const o = opts.find(x => x.value === want) || opts.find(x => x.label === want || x.text === want);
  const own = [el.getAttribute('aria-label'), el.title];
  for (const l of (el.labels || [])) own.push(l.innerText || l.textContent);
  const chosen = o ? [o.text, o.label, o.value] : [];
  return [...own, ...chosen].filter(v => typeof v === 'string' && v).join(' ');
}"""
# Keys that activate the focused control the way a click does.
_ACTIVATING_KEYS = frozenset({"Enter", "NumpadEnter", "Space", " "})
_CREDENTIAL_AUTOCOMPLETE = re.compile(r"(?:^|\s)(?:cc-|current-password)")
# The guard's fixed reason when the target cannot be inspected; the action is not taken.
UNINSPECTABLE = "could not inspect the target"


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
        """Save the active tab's viewport for the owner; never sent to any model. A tab that left the
        allowlist since the last call is blanked first."""
        try:
            with contextlib.suppress(BrowserActionError):
                await self._enforce_allowlist()
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
        # A navigation an earlier call started (a delayed script, a slow redirect) may have left the allowlist
        # after that call's check: never read, capture or act on such a page. It is blanked and the call fails.
        await self._enforce_allowlist()
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
        """Runs before and after every call, behind the egress proxy and routing (which misses redirects of a
        request it let through). A child frame off the list is blanked; a tab that is off it is blanked
        (closed if that fails) and the call fails."""
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
            return await self._info_of(await self._locator(page, target))
        return await page.evaluate(_INFO_AT_POINT, list(self._point(target)))

    async def _info_of(self, locator: Any) -> dict[str, Any] | None:
        """The element's description, its label extended with the computed accessible name from Playwright's
        AI snapshot (the element's own line), which also counts names the page script cannot see, such as CSS
        generated content."""
        info = await locator.evaluate(_INFO_OF_ELEMENT)
        if info and not info.get("frame"):
            raw = await locator.aria_snapshot(mode="ai", depth=1, timeout=ACTION_TIMEOUT_MS)
            own = next((line for line in raw.splitlines() if line.strip()), "")
            info["label"] = f"{info.get('label', '')} {own[:500]}"
        return info

    async def _focused_info(self, page: Any) -> dict[str, Any] | None:
        return await page.evaluate(_INFO_OF_FOCUS)

    @staticmethod
    def _guard(info: dict[str, Any] | None, action: str) -> None:
        """Stop before acting on a consequential control, or on one it cannot inspect. Any activation inside a
        form with a password or payment field counts, whatever the control's label or type. The reason names
        only our own vocabulary, never the page's label text, because it reaches the outer agent."""
        if not info:
            raise ConfirmationRequired(f"{action}: {UNINSPECTABLE}")
        if info.get("frame"):
            raise ConfirmationRequired(f"{action} inside an embedded frame that the guard cannot inspect")
        match = _CONSEQUENTIAL.search(" ".join(info.get("label", "").split()))
        if match:
            word = " ".join(match.group(0).lower().split())
            raise ConfirmationRequired(
                f'{action} on a control labelled "{word}" looks like a consequential step '
                "(login, payment, message, terms or deletion)"
            )
        if info.get("sensitiveForm"):
            raise ConfirmationRequired(f"{action} inside a form with password or payment fields")

    @staticmethod
    def _guard_entry(info: dict[str, Any] | None) -> None:
        if not info:
            raise ConfirmationRequired(f"entering text: {UNINSPECTABLE}")
        if info.get("frame"):
            raise ConfirmationRequired("typing inside an embedded frame that the guard cannot inspect")
        if (
            info.get("type") == "password"
            or info.get("payment")
            or _CREDENTIAL_AUTOCOMPLETE.search(info.get("autocomplete", ""))
        ):
            raise ConfirmationRequired(
                "entering text into a password or payment field (credentials are never provided)"
            )

    @classmethod
    def _guard_implicit_submit(cls, info: dict[str, Any] | None, chord: str) -> None:
        """Enter in a text field submits its form through the form's default submit control: guard that control
        (its label and values) as if it were clicked, and fail closed on a form with nothing to inspect."""
        implicit = (info or {}).get("implicitSubmit")
        if not implicit:
            return
        if implicit.get("opaque"):
            raise ConfirmationRequired(
                f"pressing {chord} would submit a form that has no submit control the guard can inspect"
            )
        cls._guard({"label": implicit.get("label", "")}, f"pressing {chord} submits a form; its submit control")

    async def _press(self, page: Any, chord: str) -> None:
        """Press one chord, checking the focused control first when the key activates it."""
        key = chord.split("+")[-1]
        if key in _ACTIVATING_KEYS:
            info = await self._focused_info(page)
            self._guard(info, f"pressing {chord}")
            if key in ("Enter", "NumpadEnter"):
                self._guard_implicit_submit(info, chord)
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
        if word := consequential_url(url):
            raise ConfirmationRequired(f'navigating to a URL containing "{word}" looks like a consequential step')
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
        # Every button is guarded: a page can act from an auxclick or contextmenu handler as well as from click.
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
        # The drop target, before pressing (a dragged item that follows the pointer can cover it later) ...
        self._guard(await self._info_at(page, target), "drop")
        await page.mouse.move(*start)
        await page.mouse.down()
        await page.mouse.move(*end, steps=10)
        # ... and again before releasing, because drop zones often appear only once a drag starts.
        self._guard(await self._info_at(page, target), "drop")
        await page.mouse.up()
        return [_text(f"Dragged from {start} to {end}.")]

    async def _m_left_mouse_down(self, target: dict, tab_id: str | None = None) -> list[dict]:
        page = self._page(tab_id)
        self._guard(await self._info_at(page, target), "mouse down")
        await page.mouse.move(*self._point(target))
        await page.mouse.down()
        return [_text("Left button pressed.")]

    async def _m_left_mouse_up(self, target: dict, tab_id: str | None = None) -> list[dict]:
        # A release completes a click or a drop on whatever is under it: checked like a drop target.
        page = self._page(tab_id)
        point = self._point(target)
        self._guard(await self._info_at(page, target), "mouse release")
        await page.mouse.move(*point)
        self._guard(await self._info_at(page, target), "mouse release")
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
                info = await self._focused_info(page)
                self._guard_entry(info)
                if " " in chunk and not info.get("textEntry"):
                    # A typed space presses a focused button, link or checkbox, like the Space key.
                    self._guard(info, "typing a space")
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
            self._guard(await self._focused_info(page), f"holding {keys[-1]}")
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
        info = await self._info_of(locator)
        self._guard_entry(info)
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
        if isinstance(value, bool) or info.get("type") in ("checkbox", "radio"):
            if value:
                self._guard(info, "checking")
            await locator.set_checked(bool(value), timeout=ACTION_TIMEOUT_MS)
        elif tag == "select":
            values = await locator.evaluate("el => [...el.options].map(o => o.value)")
            option = {"value": str(value)} if str(value) in values else {"label": str(value)}
            # Choosing an option fires input/change, and a page can act on that: guard the chosen option's
            # text and the select's own labels like a click, and the form context.
            chosen = await locator.evaluate(_SELECT_CHOICE_JS, str(value))
            self._guard({**info, "label": chosen}, "selecting")
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
