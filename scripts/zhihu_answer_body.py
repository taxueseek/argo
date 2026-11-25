"""Read the visible text of one verified, expanded Zhihu answer."""

from __future__ import annotations

import json
import math
import re
import time
import threading

from browser_auth import auth_profile_for_fetch
from chrome_cdp import ChromeCDP


_INSPECT = r"""(url, current) => {
    if (current !== url || document.readyState !== 'complete') return '';
    const visible = el => el.getClientRects().length && el.checkVisibility();
    const all = (root, selector) => Array.from(root.querySelectorAll(selector));
    if (all(document, '[role="dialog"], .Modal-wrapper, .Captcha').some(visible)) return '';
    const cards = all(document, '.AnswerItem').filter(c =>
        all(c, ':scope > meta[itemprop="url"]').some(m => m.content === url));
    if (cards.length !== 1) return '';
    const card = cards[0];
    const sections = all(card, '.RichContent').filter(e => e.closest('.AnswerItem') === card);
    if (sections.length !== 1) return '';
    const section = sections[0];
    const bodies = all(section, '.RichContent-inner .RichText.ztext[itemprop="text"]');
    if (bodies.length !== 1) return '';
    const body = bodies[0];
    if (!visible(body) || body.closest('.is-collapsed, [aria-hidden="true"]') ||
        card.querySelector('[class*="Paywall" i], [class*="PaidMask" i], [class*="Preview" i], .RichContent-cover, .RichContent-inner--collapsed')) return '';
    const buttons = all(section, 'button').filter(visible);
    const label = b => b.innerText.replace(/[\s\u200b]/g, '');
    if (!buttons.some(b => !body.contains(b) && b.matches('.ContentItem-rightButton') && label(b) === '收起')) return '';
    if (buttons.some(b => /阅读全文|展开全文|继续阅读|购买|解锁/.test(label(b))) ||
        /付费阅读|购买后.{0,6}阅读|开通.{0,8}查看全文|试读结束|最低.{0,8}查看完整内容/.test(card.innerText)) return '';
    for (let e = body; e && e !== card; e = e.parentElement) {
        const css = getComputedStyle(e);
        if ((['hidden','clip','auto','scroll'].includes(css.overflowY) && e.scrollHeight > e.clientHeight + 1) ||
            (css.webkitLineClamp && css.webkitLineClamp !== 'none')) return '';
    }
    return body.innerText.trim();
}"""


def read_body(question: str, answer: str, timeout: float) -> str:
    """Return expanded answer text, or an empty string when verification fails."""
    if not all(isinstance(i, str) and re.fullmatch(r'[1-9][0-9]{0,19}', i) for i in (question, answer)):
        return ''
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        return ''
    url = f'https://www.zhihu.com/question/{question}/answer/{answer}'
    deadline = time.monotonic() + timeout
    if not _PROFILE_BUSY.acquire(blocking=False):
        return ''
    result = []

    def run():
        try:
            result.append(_read(url, deadline))
        finally:
            _PROFILE_BUSY.release()

    # ChromeCDP owns its own startup, evaluation and shutdown timeouts. Keep
    # ownership until its context manager exits, even after the caller times out.
    worker = threading.Thread(target=run, name='zhihu-answer-body', daemon=False)
    try:
        worker.start()
    except RuntimeError:
        _PROFILE_BUSY.release()
        return ''
    worker.join(min(threading.TIMEOUT_MAX, max(0, deadline - time.monotonic())))
    return result[0] if result and time.monotonic() < deadline else ''


_PROFILE_BUSY = threading.Lock()


def _read(url: str, deadline: float) -> str:
    probe = f'({_INSPECT})({json.dumps(url)}, location.origin + location.pathname)'
    try:
        profile = auth_profile_for_fetch(url)
        if profile:
            with ChromeCDP(user_data_dir=profile) as browser:
                if time.monotonic() >= deadline:
                    return ''
                browser.navigate(url, wait_until='load', timeout=min(5.0, deadline - time.monotonic()))
                while time.monotonic() < deadline:
                    text = browser.evaluate(probe)
                    if isinstance(text, str) and text and time.monotonic() < deadline:
                        return text
                    time.sleep(max(0.0, min(0.2, deadline - time.monotonic())))
    except Exception:
        # The public result does not carry browser, transport, or session diagnostics.
        return ''
    return ''
