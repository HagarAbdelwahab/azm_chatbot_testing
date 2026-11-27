"""Sign in to the ops portal through the Inheritance Seeder (admin) when SSO fails.

The seeder (<OPS_ADMIN_URL>/inheritance_seeder/inheritanceseeder/seed/) builds an
inheritance case in the testing environment and lists its people, each with a
"Login as User" action that opens the portal already signed in as that person. So when
the normal SSO login of the test heir fails, we seed a case in which ONE OF THE HEIRS
carries the target user's national id, then log in as that heir.

    seeder_login(context, national_id) -> (Page, href)   # portal page signed in as that id,
                                                         # + the Login-as-User link (or None)

Needs in .env (never commit): OPS_ADMIN_URL, OPS_ADMIN_USERNAME, OPS_ADMIN_PASSWORD.
Writes one seeded case to the testing environment per call. Refuses to run unless both
OPS_ADMIN_URL and the portal are on a non-production host (`assert_non_prod`).
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

NID = re.compile(r"[12]\d{9}")
SEED_PATH = "/inheritance_seeder/inheritanceseeder/seed/"
# Host suffixes the seeder may write to. Override with OPS_SEEDER_HOSTS (comma-separated).
DEFAULT_NON_PROD = (".azm-dev.com",)


def assert_non_prod(*urls: str | None) -> None:
    """Raise unless every URL is on an allowed non-production host. Runs BEFORE seeding."""
    allowed = tuple(h.strip().lower() for h in
                    (os.environ.get("OPS_SEEDER_HOSTS") or "").split(",") if h.strip()) \
        or DEFAULT_NON_PROD
    for u in urls:
        host = (urlparse(u or "").hostname or "").lower()
        if not host or not any(host == a.lstrip(".") or host.endswith(a if a.startswith(".") else "." + a)
                               for a in allowed):
            raise RuntimeError(f"seeder refused: {host or u!r} is not an allowed non-production "
                               f"host ({', '.join(allowed)}; set OPS_SEEDER_HOSTS to change). "
                               f"Nothing was seeded.")


def _mask(v: str) -> str:
    return f"••••••{v[-4:]}"


def configured() -> bool:
    return all(os.environ.get(k) for k in ("OPS_ADMIN_URL", "OPS_ADMIN_USERNAME", "OPS_ADMIN_PASSWORD"))


def target_national_id(username: str | None, explicit: str | None = None) -> str | None:
    """The national id to seed as an heir: `explicit` (e.g. the question set's
    login_user.national_id), else OPS_NATIONAL_ID, else OPS_USERNAME when that already is
    one. Anything else is unknown, and we never guess it."""
    for cand in (explicit, os.environ.get("OPS_NATIONAL_ID"), username):
        if cand and NID.fullmatch(cand.strip()):
            return cand.strip()
    return None


def seeder_login(context, national_id: str, portal: str | None = None, timeout_ms: int = 60_000):
    """Seed a case with an heir whose Social ID is `national_id`, then 'Login as User'.
    Returns (the new portal page, the Login-as-User href or None), so a later re-login
    can reuse the same heir instead of seeding another case."""
    if not NID.fullmatch(national_id or ""):
        raise RuntimeError("seeder login needs a 10-digit national id starting with 1 or 2.")
    if not configured():
        raise RuntimeError("seeder login not configured: set OPS_ADMIN_URL, OPS_ADMIN_USERNAME "
                           "and OPS_ADMIN_PASSWORD in .env.")
    base = os.environ["OPS_ADMIN_URL"].rstrip("/")
    assert_non_prod(base, portal or os.environ.get("OPS_PORTAL"))
    admin = context.new_page()
    try:
        admin.goto(f"{base}{SEED_PATH}", wait_until="domcontentloaded")
        # Selectors by name, not label: the session's ar-SA locale translates Django's labels.
        if admin.locator('input[name="username"]').count():
            admin.locator('input[name="username"]').fill(os.environ["OPS_ADMIN_USERNAME"])
            admin.locator('input[name="password"]').fill(os.environ["OPS_ADMIN_PASSWORD"])
            admin.locator('input[type=submit], button[type=submit]').first.click()
            try:
                admin.wait_for_url(lambda u: "/login" not in u, timeout=30_000)
            except Exception:
                raise RuntimeError("seeder admin login was refused "
                                   "(OPS_ADMIN_USERNAME / OPS_ADMIN_PASSWORD).") from None
            admin.wait_for_load_state("networkidle")
        admin.goto(f"{base}{SEED_PATH}", wait_until="networkidle")

        admin.get_by_role("button", name="Generate Random Data").click()
        # The fill is async client-side JS: wait until it has filled the heir's id, so it
        # cannot overwrite ours afterwards.
        heir = admin.locator('input[name="heir_social_id_0"]')
        try:
            admin.wait_for_function(
                "() => { const e = document.querySelector('input[name=\"heir_social_id_0\"]');"
                " return e && e.value.trim() !== ''; }", timeout=15_000)
        except Exception:
            pass                                           # slow fill: ours goes in anyway, checked below
        # Heir card 0 is the applicant/beneficiary — the heir that has a Login-as-User row.
        for _ in range(3):
            heir.fill(national_id)
            admin.wait_for_timeout(500)
            if heir.input_value().strip() == national_id:
                break
        else:
            raise RuntimeError("the heir's Social ID kept being overwritten — nothing was seeded.")
        admin.locator('input[type=submit][value="Seed Case"]').click()
        admin.wait_for_load_state("networkidle")

        body = admin.locator("body").inner_text()
        if "seeded successfully" not in body:
            msg = next((ln for ln in body.splitlines() if re.search(r"error|invalid|refus|exist", ln, re.I)), "")
            raise RuntimeError(f"seeder did not report success for heir {_mask(national_id)}: {msg[:200]}")

        row = admin.locator("tr", has_text=national_id).first
        link = row.locator("a, button").filter(has_text=re.compile(r"Login as User", re.I)).first
        if not link.count():
            raise RuntimeError(f"seeded, but no 'Login as User' action for {_mask(national_id)} on the result page.")
        href = link.get_attribute("href")
        href = admin.evaluate("h => new URL(h, location.href).href", href) if href else None
        with context.expect_page(timeout=timeout_ms) as pop:
            link.click()
        page = pop.value
        page.wait_for_load_state("networkidle")
        return page, href
    finally:
        admin.close()
