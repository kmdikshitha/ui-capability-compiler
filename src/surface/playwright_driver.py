"""The only module in this project that imports playwright.

Everything browser-specific lives behind observe()/act(). The driver knows about
frames, load states and CDP; nothing above it does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Frame, Page, sync_playwright

from src.surface.normalize import build_observation
from src.types import Action, ActResult, Element, Observation

NAV_TIMEOUT_MS = 15000
SETTLE_TIMEOUT_MS = 10000


class PlaywrightSurface:
    """A Surface backed by Chromium.

    Two launch modes. The default is an ephemeral headless browser. The
    escalation path uses a headed persistent context with remote debugging
    enabled, because the browser has to be a separate OS process that outlives
    the decision loop -- see PLAN.md section 10.

    slow_mo_ms pads every Playwright operation. It exists for demonstrations:
    a replay finishes in about a second, which is the point of the system and
    also means a viewer sees nothing at all.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        user_data_dir: str | None = None,
        remote_debugging_port: int | None = None,
        trace_dir: str | None = None,
        slow_mo_ms: int = 0,
    ) -> None:
        self._pw = sync_playwright().start()
        args = []
        if remote_debugging_port:
            args.append(f"--remote-debugging-port={remote_debugging_port}")

        self._browser: Browser | None = None
        if user_data_dir:
            Path(user_data_dir).mkdir(parents=True, exist_ok=True)
            self._ctx: BrowserContext = self._pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir, headless=headless, args=args,
                slow_mo=slow_mo_ms, viewport={"width": 1280, "height": 900},
            )
            self.page: Page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        else:
            self._browser = self._pw.chromium.launch(headless=headless, args=args,
                                                      slow_mo=slow_mo_ms)
            self._ctx = self._browser.new_context(viewport={"width": 1280, "height": 900})
            self.page = self._ctx.new_page()

        self._tracing = False
        if trace_dir:
            Path(trace_dir).mkdir(parents=True, exist_ok=True)
            self._ctx.tracing.start(screenshots=True, snapshots=True, sources=False)
            self._tracing = True

        self.page.set_default_timeout(NAV_TIMEOUT_MS)
        self._last: Observation | None = None

    # -- perception --------------------------------------------------------

    def _live_frames(self) -> list[tuple[list[str], Frame]]:
        """Every attached frame, outermost first, paired with its frame path.

        Navigations leave detached frames behind in page.frames for a while;
        acting on one raises "Frame was detached", so they are filtered here
        rather than everywhere upstream.
        """
        found: list[tuple[list[str], Frame]] = []

        def walk(frame: Frame, path: list[str]) -> None:
            found.append((path, frame))
            for index, child in enumerate(frame.child_frames):
                if child.is_detached():
                    continue
                walk(child, path + [child.name or f"#{index}"])

        walk(self.page.main_frame, [])
        return found

    def frame_for(self, frame_path: list[str]) -> Frame:
        for path, frame in self._live_frames():
            if path == list(frame_path):
                return frame
        raise LookupError(f"no live frame at path {frame_path}")

    def observe(self, screenshot_path: str | None = None) -> Observation:
        self._settle()
        frames: list[tuple[list[str], str]] = []
        texts: list[str] = []
        for path, frame in self._live_frames():
            try:
                frames.append((path, frame.locator("body").aria_snapshot()))
                texts.append(frame.locator("body").inner_text())
            except Exception:
                continue  # frame went away mid-observation; the rest still stands
        if screenshot_path:
            self.screenshot(screenshot_path)
        obs = build_observation(
            url=self.page.url, title=self.page.title(), frames=frames,
            texts=texts, screenshot_path=screenshot_path,
        )
        self._last = obs
        return obs

    def blocking_dialog(self) -> str | None:
        """The accessible name of an open dialog, if one is up.

        Cheap on purpose: replay asks this before every step, so it must not
        cost a full accessibility snapshot.
        """
        for _, frame in self._live_frames():
            try:
                found = frame.get_by_role("dialog")
                if found.count() == 0:
                    continue
                name = (found.first.get_attribute("aria-label") or "").strip()
                return name or "dialog"
            except Exception:
                continue
        return None

    def _settle(self) -> None:
        """Wait for the page and its frames to stop loading.

        Waiting on the top-level page alone is not enough here: a link inside
        the work area navigates that frame while the top document stays put, so
        the page reports "load" immediately and the next locator probe runs
        against a document that is being replaced.
        """
        for state in ("domcontentloaded", "load"):
            try:
                self.page.wait_for_load_state(state, timeout=SETTLE_TIMEOUT_MS)
            except Exception:
                pass
        for _, frame in self._live_frames():
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=SETTLE_TIMEOUT_MS)
            except Exception:
                continue

    # -- action ------------------------------------------------------------

    def element_for_ref(self, ref: str) -> Element:
        if self._last is None:
            raise LookupError("act by ref requires a preceding observe()")
        for element in self._last.elements:
            if element.ref == ref:
                return element
        raise LookupError(f"unknown ref {ref!r} in the current observation")

    def locator_for_element(self, element: Element) -> Any:
        """Resolve a ref back to a live locator.

        The observation records each element's role and its index among
        same-role elements in its frame, and both the ARIA snapshot and
        get_by_role walk the document in order, so the pair addresses exactly
        one element without depending on the regenerating ids.
        """
        frame = self.frame_for(element.frame_path)
        return frame.get_by_role(element.role).nth(element.nth)

    def act(self, action: Action) -> ActResult:
        try:
            if action.type == "navigate":
                self.page.goto(action.url or "", wait_until="domcontentloaded")
                self._settle()
                return ActResult(ok=True)

            element = self.element_for_ref(action.ref or "")
            locator = self.locator_for_element(element)
            bounds = self._bounds(locator)

            if action.type == "click":
                locator.click(timeout=NAV_TIMEOUT_MS)
            elif action.type == "type":
                locator.fill(action.value or "", timeout=NAV_TIMEOUT_MS)
            elif action.type == "select":
                locator.select_option(action.value or "", timeout=NAV_TIMEOUT_MS)
            elif action.type == "read":
                return ActResult(ok=True, read_value=self._read(locator), bounds=bounds)
            else:
                return ActResult(ok=False, error=f"unsupported action {action.type!r}")

            self._settle()
            return ActResult(ok=True, bounds=bounds)
        except Exception as exc:
            return ActResult(ok=False, error=_short(exc))

    def act_on_locator(self, locator: Any, action_type: str, value: str | None) -> ActResult:
        """Act on an already-resolved locator. Used by deterministic replay,
        which resolves through the artifact's locator ladder rather than a ref."""
        try:
            bounds = self._bounds(locator)
            if action_type == "click":
                locator.click(timeout=NAV_TIMEOUT_MS)
            elif action_type == "type":
                locator.fill(value or "", timeout=NAV_TIMEOUT_MS)
            elif action_type == "select":
                locator.select_option(value or "", timeout=NAV_TIMEOUT_MS)
            elif action_type == "read":
                return ActResult(ok=True, read_value=self._read(locator), bounds=bounds)
            else:
                return ActResult(ok=False, error=f"unsupported action {action_type!r}")
            self._settle()
            return ActResult(ok=True, bounds=bounds)
        except Exception as exc:
            return ActResult(ok=False, error=_short(exc))

    @staticmethod
    def _read(locator: Any) -> str:
        try:
            value = locator.input_value(timeout=3000)
            if value:
                return value
        except Exception:
            pass
        try:
            return (locator.inner_text(timeout=3000) or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _bounds(locator: Any) -> tuple[int, int, int, int] | None:
        try:
            box = locator.bounding_box(timeout=2000)
        except Exception:
            return None
        if not box:
            return None
        return (int(box["x"]), int(box["y"]), int(box["width"]), int(box["height"]))

    # -- evidence and teardown --------------------------------------------

    def screenshot(self, path: str) -> str | None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=path, full_page=False)
            return path
        except Exception:
            return None

    def stop_tracing(self, path: str) -> str | None:
        if not self._tracing:
            return None
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._ctx.tracing.stop(path=path)
            self._tracing = False
            return path
        except Exception:
            return None

    def close(self) -> None:
        for shutdown in (self._ctx.close, lambda: self._browser and self._browser.close(),
                         self._pw.stop):
            try:
                shutdown()
            except Exception:
                pass


def _short(exc: Exception) -> str:
    """Playwright errors carry a multi-line call log; the first line is the error."""
    return str(exc).strip().split("\n")[0][:300]
