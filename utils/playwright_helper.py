"""
Playwright-based JS page renderer.

Used by scrapers that target JavaScript Single-Page Applications (SPAs)
that return empty HTML shells when fetched with plain requests.

Usage:
    from utils.playwright_helper import render_page, render_page_with_wait

    html = await render_page("https://example.com/jobs")
    html = await render_page_with_wait("https://example.com/jobs", wait_selector=".job-card")

Design:
  - Headless Chromium (fastest, most compatible with bot-detection bypass)
  - Blocks images, fonts, media — not needed for job text extraction
  - Rotates User-Agent from anti_block pool
  - Single shared browser instance per process (lazy-init, reused across calls)
  - Falls back gracefully: raises PlaywrightRenderError on failure so callers
    can catch and fall back to plain requests
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Optional

import config
from utils.logger import logger

_SESSION_DIR = config.BASE_DIR / "data" / "playwright_sessions"

# ── Playwright availability check ─────────────────────────────────────────────
try:
    from playwright.async_api import async_playwright, Browser, BrowserContext
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    logger.warning("[playwright] playwright not installed — JS rendering disabled. "
                   "Run: playwright install chromium")


class PlaywrightRenderError(Exception):
    """Raised when Playwright rendering fails."""


def _playwright_exception_handler(loop, context: dict) -> None:
    """Suppress TargetClosedError from Playwright's internal CDPSession futures.

    When a page is closed while networkidle tracking or other Playwright-internal
    futures are pending, those futures raise TargetClosedError. Python's asyncio
    logs "Future exception was never retrieved" for them because they are internal
    implementation details — our code never holds a reference to await them.
    This handler silences that noise without hiding real errors.
    """
    exc = context.get("exception")
    if exc is not None and type(exc).__name__ in ("TargetClosedError", "ConnectionClosedError"):
        return
    msg = context.get("message", "")
    if "TargetClosedError" in msg or "ConnectionClosedError" in msg:
        return
    loop.default_exception_handler(context)


# Install at import time — affects only futures created on the running event loop.
try:
    asyncio.get_event_loop().set_exception_handler(_playwright_exception_handler)
except RuntimeError:
    pass  # No running loop yet; handler will be re-applied when the loop starts


# ── Shared browser instance (lazy init, process-level singleton) ──────────────
_browser: Optional["Browser"] = None
_playwright_instance = None
_lock = asyncio.Lock()

# Resource types to block — speeds up page load significantly
_BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}

# Realistic User-Agent pool
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0",
]


async def _get_browser() -> "Browser":
    """Return the shared Chromium browser, launching it if needed."""
    global _browser, _playwright_instance
    # Re-apply the exception handler on the running loop (import-time install
    # may have targeted a different loop in frameworks that replace the default).
    try:
        asyncio.get_running_loop().set_exception_handler(_playwright_exception_handler)
    except RuntimeError:
        pass
    async with _lock:
        if _browser is None or not _browser.is_connected():
            if _playwright_instance is not None:
                try:
                    await _playwright_instance.stop()
                except Exception:
                    pass
            pw = await async_playwright().start()
            _playwright_instance = pw
            _browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1920,1080",
                ],
            )
            logger.info("[playwright] Chromium browser launched")
    return _browser


def _session_path(domain: str) -> Path:
    """Return the cookie storage file path for a given domain."""
    safe = domain.replace(".", "_").replace("/", "_")
    return _SESSION_DIR / f"{safe}.json"


async def save_session(ctx: "BrowserContext", domain: str) -> None:
    """Persist browser cookies for `domain` to disk."""
    try:
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        cookies = await ctx.cookies()
        path = _session_path(domain)
        path.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[playwright] Session saved for {domain} ({len(cookies)} cookies)")
    except Exception as exc:
        logger.warning(f"[playwright] Session save failed for {domain}: {exc}")


async def load_session(ctx: "BrowserContext", domain: str) -> bool:
    """Load persisted cookies for `domain` into context. Returns True if loaded."""
    path = _session_path(domain)
    if not path.exists():
        return False
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
        if cookies:
            await ctx.add_cookies(cookies)
            logger.info(f"[playwright] Session loaded for {domain} ({len(cookies)} cookies)")
            return True
    except Exception as exc:
        logger.warning(f"[playwright] Session load failed for {domain}: {exc}")
    return False


def clear_session(domain: str) -> None:
    """Delete persisted session for a domain (call on auth failure)."""
    path = _session_path(domain)
    if path.exists():
        path.unlink()
        logger.info(f"[playwright] Session cleared for {domain}")


async def _new_context(browser: "Browser", session_domain: Optional[str] = None) -> "BrowserContext":
    """Create a stealth-configured browser context, optionally loading a persisted session."""
    ua = random.choice(_USER_AGENTS)
    ctx = await browser.new_context(
        user_agent=ua,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="Europe/Berlin",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    # Hide webdriver fingerprint
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        window.chrome = { runtime: {} };
    """)
    if session_domain:
        await load_session(ctx, session_domain)
    return ctx


async def render_page(
    url: str,
    timeout_ms: int = 45_000,
    wait_until: str = "networkidle",
    extra_wait_ms: int = 0,
    session_domain: Optional[str] = None,
) -> str:
    """
    Render a URL with headless Chromium and return the full HTML.

    Args:
        url:           Target URL
        timeout_ms:    Max ms to wait for page load (default 45s)
        wait_until:    Playwright wait condition — "networkidle" (default),
                       "domcontentloaded" (faster), or "load"
        extra_wait_ms: Additional ms to wait after page load before capturing HTML.
                       Use for SPAs that populate content asynchronously after
                       the initial load event (e.g. CATL, BYD).

    Returns:
        Full rendered HTML as a string.

    Raises:
        PlaywrightRenderError: If Playwright is unavailable or rendering fails.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        raise PlaywrightRenderError("playwright not installed")

    browser = await _get_browser()
    ctx = await _new_context(browser, session_domain=session_domain)
    page = await ctx.new_page()

    # Block unnecessary resource types for speed
    async def _abort_heavy(route):
        if route.request.resource_type in _BLOCKED_TYPES:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", _abort_heavy)

    try:
        logger.debug(f"[playwright] Navigating to {url}")
        await page.goto(url, timeout=timeout_ms, wait_until=wait_until)
        if extra_wait_ms > 0:
            logger.debug(f"[playwright] Extra wait {extra_wait_ms}ms for JS population")
            await page.wait_for_timeout(extra_wait_ms)
        html = await page.content()
        logger.debug(f"[playwright] Rendered {url} — {len(html):,} chars")
        if session_domain:
            await save_session(ctx, session_domain)
        return html
    except Exception as exc:
        raise PlaywrightRenderError(f"Render failed for {url}: {exc}") from exc
    finally:
        await page.close()
        await ctx.close()


async def render_page_with_wait(
    url: str,
    wait_selector: str,
    timeout_ms: int = 45_000,
    extra_wait_ms: int = 0,
    session_domain: Optional[str] = None,
) -> str:
    """
    Render a URL and additionally wait for a CSS selector to appear.
    Useful when the page loads fast but content populates asynchronously.

    Args:
        url:            Target URL
        wait_selector:  CSS selector to wait for before capturing HTML
        timeout_ms:     Max ms to wait (applies to both navigation + selector)
        extra_wait_ms:  Additional ms to wait after the selector appears,
                        for SPAs that continue populating after the first
                        matching element is injected.

    Returns:
        Full rendered HTML.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        raise PlaywrightRenderError("playwright not installed")

    browser = await _get_browser()
    ctx = await _new_context(browser, session_domain=session_domain)
    page = await ctx.new_page()

    async def _abort_heavy(route):
        if route.request.resource_type in _BLOCKED_TYPES:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", _abort_heavy)

    try:
        logger.debug(f"[playwright] Navigating to {url} (waiting for '{wait_selector}')")
        await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(wait_selector, timeout=timeout_ms)
        except Exception:
            logger.debug(f"[playwright] Selector '{wait_selector}' not found — capturing anyway")
        if extra_wait_ms > 0:
            logger.debug(f"[playwright] Extra wait {extra_wait_ms}ms after selector")
            await page.wait_for_timeout(extra_wait_ms)
        html = await page.content()
        logger.debug(f"[playwright] Rendered {url} — {len(html):,} chars")
        if session_domain:
            await save_session(ctx, session_domain)
        return html
    except Exception as exc:
        raise PlaywrightRenderError(f"Render failed for {url}: {exc}") from exc
    finally:
        await page.close()
        await ctx.close()



async def render_with_form_interaction(
    url: str,
    search_selector: str,
    search_text: str,
    result_selector: str,
    timeout_ms: int = 60_000,
    extra_wait_ms: int = 3_000,
    consent_selector: str = 'button:has-text("Accept all")',
    block_resources: bool = False,
) -> str:
    """
    Navigate to a page, accept a consent popup if present, fill a search input,
    wait for results to appear, and return the rendered HTML.

    Designed for portals that ignore URL query params and require real form interaction
    to trigger a search (e.g. BMW Group Jobs).

    Args:
        url:               Page URL to load
        search_selector:   CSS selector for the search input field
        search_text:       Text to type into the search field
        result_selector:   CSS selector to wait for after submitting the search
        timeout_ms:        Max ms for both navigation and result wait
        extra_wait_ms:     Additional ms to wait after result_selector appears
        consent_selector:  CSS selector for the consent accept button (optional)

    Returns:
        Rendered HTML after search results are present.

    Raises:
        PlaywrightRenderError: If Playwright is unavailable or rendering fails.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        raise PlaywrightRenderError("playwright not installed")

    browser = await _get_browser()
    ctx = await _new_context(browser)
    page = await ctx.new_page()

    async def _abort_heavy(route):
        if route.request.resource_type in _BLOCKED_TYPES:
            await route.abort()
        else:
            await route.continue_()

    if block_resources:
        await page.route("**/*", _abort_heavy)

    try:
        # domcontentloaded is sufficient — form interaction waits for results
        # via wait_for_selector below. Using networkidle here leaves internal
        # CDPSession futures open; when the page closes they emit TargetClosedError
        # as unretrieved asyncio futures ("Future exception was never retrieved").
        await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

        # Accept consent popup if present
        if consent_selector:
            try:
                await page.click(consent_selector, timeout=4_000)
                await page.wait_for_timeout(1_500)
            except Exception:
                pass  # Consent already accepted or not present

        # Fill the search box and submit
        await page.fill(search_selector, search_text)
        await page.keyboard.press("Enter")

        # Wait for results to populate
        try:
            await page.wait_for_selector(result_selector, timeout=timeout_ms)
        except Exception:
            logger.debug(f"[playwright] Result selector '{result_selector}' not found after search")

        if extra_wait_ms > 0:
            await page.wait_for_timeout(extra_wait_ms)

        html = await page.content()
        logger.debug(f"[playwright] Form search done — {len(html):,} chars")
        return html

    except Exception as exc:
        raise PlaywrightRenderError(f"Form interaction failed for {url}: {exc}") from exc
    finally:
        try:
            await page.close()
        except Exception:
            pass
        try:
            await ctx.close()
        except Exception:
            pass


async def close_browser() -> None:
    """Cleanly shut down the shared browser (call on app exit)."""
    global _browser, _playwright_instance
    async with _lock:
        if _browser and _browser.is_connected():
            await _browser.close()
            _browser = None
        if _playwright_instance:
            await _playwright_instance.stop()
            _playwright_instance = None
        logger.info("[playwright] Browser closed")
