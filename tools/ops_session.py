"""A signed-in heir's chat identity, captured from a real browser session on the portal.

The agentic flow (the `tarekat_*` tools) only runs for a FEDERATED chat: the widget
must carry the signed-in user's identity. A publishable widget key alone is anonymous,
and an anonymous chat is told to log in. So this module logs in on the web like a heir
would and takes the user's token from the portal's own code, never from a hand-built call.

Verified on ops (https://ops-merath.web.azm-dev.com, 2026-11-17):

  1. SSO login (IdentityServer, username/password) -> the portal sets the cookies
     `access_token` (lives 2 MINUTES on ops) and `refresh_token` (5 minutes).
  2. The portal registers `AzmChat.identify({getToken})`. That callback is
     `window.__azmChatIdentityHub.getToken()` -> GET <tarekat-api>/api/v1/chat/token
     with `Authorization: Bearer <access_token cookie>` -> a chat JWT (RS256, 10 min,
     aud ["azm-chatbot-studio", "tarekat-tools"], claim `user` = {nid, phone}).
  3. The widget exchanges it: POST <studio-api>/auth/token-exchange
     {"subject_token": <chat JWT>} with X-API-Key -> {access_token (30 min),
     expires_in, verified_claims, user_token_retained}.
  4. Every widget call then carries BOTH `X-API-Key` and `Authorization: Bearer
     <exchanged token>`. That is what makes the agent call `tarekat_*` tools as this user.

Because the portal token lives 2 minutes, the browser stays open for the whole run and
a fresh chat token is always fetched THROUGH the page. When the portal session has
lapsed, reloading the dashboard lets the portal refresh it; when the refresh token has
lapsed too, the session logs in again.

Playwright's sync API is bound to the thread that created it: call this class from one
thread only.

Observed defect (2026-11-17, ops): right after the SSO redirect lands on /dashboard,
`AzmChat.identityState()` stayed "anonymous" and the first chat answered "سجّل الدخول"
to a signed-in user; a page reload made it "federated". `identity_state_after_login`
records this on every run so it is tracked rather than worked around silently.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

from tools import seeder_login

load_dotenv()

DEFAULT_PORTAL = "https://ops-merath.web.azm-dev.com"

# The portal's own identity callback. Returns null when the portal session is gone.
_GET_TOKEN_JS = """async () => {
  const hub = window.__azmChatIdentityHub;
  if (!hub || typeof hub.getToken !== 'function') return null;
  try { return await hub.getToken(); } catch (e) { return null; }
}"""

_WIDGET_JS = """() => {
  const s = document.getElementById('azm-chat-widget');
  return s ? {key: s.dataset.token || null, api: s.dataset.apiUrl || null, src: s.src} : null;
}"""


def jwt_claims(token: str) -> dict:
    """Unverified payload — for routing checks and display, never for trust decisions."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def mask_id(value: str | None) -> str:
    v = str(value or "")
    return f"••••••{v[-4:]}" if len(v) > 4 else "—"


@dataclass
class Identity:
    """One exchanged identity — what the widget would be carrying right now."""
    chat_token: str
    access_token: str
    expires_in: int
    obtained_at: float
    tenant_id: str
    national_id: str
    verified_claims: dict = field(default_factory=dict)
    user_token_retained: bool | None = None

    @property
    def age_s(self) -> float:
        return time.time() - self.obtained_at


class OpsUserSession:
    def __init__(self, portal: str | None = None, username: str | None = None,
                 password: str | None = None, headless: bool = True,
                 refresh_after_s: int = 8 * 60):
        self.portal = (portal or os.environ.get("OPS_PORTAL") or DEFAULT_PORTAL).rstrip("/")
        self.username = username or os.environ.get("OPS_USERNAME")
        self.password = password or os.environ.get("OPS_PASSWORD")
        if not self.username or not self.password:
            raise RuntimeError("OPS_USERNAME / OPS_PASSWORD missing — pass --user/--password "
                               "or add them to .env (never commit them).")
        self.headless = headless
        # The chat JWT the studio may forward to the tools lives 10 minutes; re-exchange
        # well inside that so a tool call never runs on an expired user token.
        self.refresh_after_s = refresh_after_s
        self.widget_key: str | None = None
        self.studio_api: str | None = None
        self.identity_state_after_login: str | None = None
        self.login_method: str | None = None          # "sso" | "seeder"
        self.login_fallback_reason: str | None = None  # why SSO was not used, when it was not
        self.logins = 0
        self.exchanges = 0
        self._pw = self._browser = self._page = None
        self._portal_touched = 0.0
        self._http = httpx.Client(timeout=30.0)
        self.identity: Identity | None = None

    # ---- browser ------------------------------------------------------------

    def start(self) -> "OpsUserSession":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._page = self._browser.new_context(locale="ar-SA").new_page()
        self._login()
        return self

    def _login(self) -> None:
        """SSO first. When SSO fails and the seeder is configured, sign in as an heir
        seeded with the same national id instead (tools/seeder_login.py). Which path was
        used is recorded in `login_method`, so a fallback is a finding, never silent."""
        try:
            self._login_sso()
            self.login_method = "sso"
            return
        except Exception as sso_err:
            nid = seeder_login.target_national_id(self.username)
            if not (seeder_login.configured() and nid):
                raise RuntimeError(
                    f"{sso_err} Seeder fallback unavailable: set OPS_ADMIN_URL, OPS_ADMIN_USERNAME, "
                    f"OPS_ADMIN_PASSWORD and OPS_NATIONAL_ID (10 digits) in .env.") from sso_err
            print(f"  !! SSO login failed ({str(sso_err)[:90]}) — signing in as heir "
                  f"{mask_id(nid)} through the inheritance seeder (seeds one case)")
            self.login_fallback_reason = str(sso_err)[:200]
            self._login_seeder(nid)

    def _login_seeder(self, nid: str) -> None:
        old = self._page
        old.context.clear_cookies()
        portal = seeder_login.seeder_login(old.context, nid)
        self._page = portal
        if old is not portal:
            try:
                old.close()
            except Exception:
                pass
        self.login_method = "seeder"
        self._after_login()

    def _login_sso(self) -> None:
        p = self._page
        # Stale portal cookies make /login bounce straight back to /dashboard (seen
        # 2026-11-17), so a re-login must start from a signed-out browser.
        p.context.clear_cookies()
        p.goto(f"{self.portal}/login", wait_until="domcontentloaded")
        # The clickable line is «الدخول عن طريق <strong>نفاذ</strong>»; the page also has
        # a plain description starting with the same words, which is not a link.
        p.locator("p:has(strong)", has_text="الدخول عن طريق").first.click()
        p.wait_for_url(lambda u: not u.startswith(self.portal), timeout=60_000)
        # The SSO page may render the form inside a frame: look in every frame.
        form = None
        deadline = time.time() + 60
        while form is None and time.time() < deadline:
            for fr in p.frames:
                try:
                    if fr.locator("input[type=password]").first.is_visible():
                        form = fr
                        break
                except Exception:
                    continue
            if form is None:
                for fr in p.frames:                       # collapsed username/password tab
                    try:
                        tab = fr.get_by_role("button", name="اسم المستخدم وكلمة المرور")
                        if tab.count() and tab.first.is_visible():
                            tab.first.click()
                    except Exception:
                        continue
                p.wait_for_timeout(1000)
        if form is None:
            raise RuntimeError(f"SSO login form not found (at {p.url}).")
        form.locator("input:not([type=hidden]):not([type=password])").first.fill(self.username)
        form.locator("input[type=password]").first.fill(self.password)
        form.get_by_role("button", name="تسجيل الدخول").click()
        try:
            p.wait_for_url("**/dashboard**", timeout=60_000)
        except Exception as e:
            raise RuntimeError(f"SSO login did not reach /dashboard (now at {p.url}). "
                               f"Wrong credentials or the SSO page changed.") from e
        self._after_login()

    def _after_login(self) -> None:
        p = self._page
        self._wait_widget()
        self._portal_touched = time.time()
        self.logins += 1
        if self.identity_state_after_login is None:
            # Give the widget the time a heir would give it before typing.
            p.wait_for_timeout(6000)
            self.identity_state_after_login = p.evaluate(
                "() => window.AzmChat ? AzmChat.identityState() : 'no-widget'")

    def _wait_widget(self) -> None:
        self._page.wait_for_function(
            "() => window.__azmChatIdentityHub && "
            "typeof window.__azmChatIdentityHub.getToken === 'function'", timeout=60_000)
        w = self._page.evaluate(_WIDGET_JS)
        if not w or not w.get("key") or not w.get("api"):
            raise RuntimeError("the portal page carries no azm-chat-widget script — "
                               "the chatbot is disabled in this environment.")
        self.widget_key, self.studio_api = w["key"], w["api"].rstrip("/")

    def _chat_token(self) -> str:
        """A fresh chat JWT through the portal's own callback, re-establishing the
        portal session when it has lapsed."""
        for attempt in ("as-is", "reload", "login"):
            if attempt == "reload":
                self._reload()
            elif attempt == "login":
                self._login()
            tok = self._page.evaluate(_GET_TOKEN_JS)
            if tok:
                return tok
        raise RuntimeError("the portal returned no chat token even after a fresh login — "
                           "the user has no national id, or /api/v1/chat/token is failing.")

    def _reload(self) -> None:
        """A dashboard load is what makes the portal refresh its own session."""
        self._page.goto(f"{self.portal}/dashboard", wait_until="networkidle")
        self._wait_widget()
        self._portal_touched = time.time()

    def keepalive(self, every_s: int = 100) -> None:
        """Keep the portal session alive between turns. Ops portal tokens: access 2 min,
        refresh 5 min — a session left alone longer than that cannot refresh any more,
        and the next chat token needs a full login."""
        if time.time() - self._portal_touched > every_s:
            self._reload()

    # ---- identity -----------------------------------------------------------

    def current(self, force: bool = False) -> Identity:
        """The identity to send with the next turn; re-exchanged when it is getting old."""
        if force or self.identity is None or self.identity.age_s > self.refresh_after_s \
                or self.identity.age_s > self.identity.expires_in - 120:
            self.identity = self._exchange(self._chat_token())
        return self.identity

    def _exchange(self, chat_token: str) -> Identity:
        r = self._http.post(
            f"{self.studio_api}/auth/token-exchange",
            headers={"X-API-Key": self.widget_key, "Content-Type": "application/json",
                     "Origin": self.portal, "Referer": self.portal + "/"},
            json={"subject_token": chat_token})
        if r.status_code != 200:
            raise RuntimeError(f"token-exchange refused the user's chat token: "
                               f"HTTP {r.status_code} {r.text[:200]}")
        d = r.json()
        at = d.get("access_token") or ""
        claims = jwt_claims(at)
        if not at or not claims:
            raise RuntimeError(f"token-exchange returned no usable access_token: keys={list(d)}")
        self.exchanges += 1
        return Identity(
            chat_token=chat_token, access_token=at,
            expires_in=int(d.get("expires_in") or 1800), obtained_at=time.time(),
            tenant_id=str(claims.get("tenant_id") or ""),
            national_id=str((claims.get("verified") or {}).get("national_id") or ""),
            verified_claims=d.get("verified_claims") or {},
            user_token_retained=d.get("user_token_retained"))

    def own_request_numbers(self) -> list[dict]:
        """Ground truth: the requests this user is part of, from the same backend tool
        the agent calls (`tarekat_my_estates`), with the user's own chat token."""
        chat = self._chat_token()
        iss = str(jwt_claims(chat).get("iss") or "").rstrip("/")
        if not iss:
            raise RuntimeError("chat token carries no issuer — cannot locate the Tarekat API.")
        r = self._http.get(f"{iss}/api/v1/chat/tools/my-estates",
                           headers={"Authorization": f"Bearer {chat}"})
        r.raise_for_status()
        res = r.json().get("result") or {}
        return [{"request_number": str(e.get("request_number")),
                 "my_roles": e.get("my_roles"), "request_status": e.get("request_status")}
                for e in res.get("estates") or [] if e.get("request_number")]

    def close(self) -> None:
        for closer in (lambda: self._browser and self._browser.close(),
                       lambda: self._pw and self._pw.stop(), self._http.close):
            try:
                closer()
            except Exception:
                pass

    def __enter__(self): return self.start()
    def __exit__(self, *_): self.close()
