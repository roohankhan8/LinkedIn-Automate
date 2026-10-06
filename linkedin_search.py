"""Search LinkedIn jobs and run Easy Apply on the results using Gemini for the form answers."""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from easy_apply import apply_to_current_job, dismiss_modal
from gemini_client import Gemini, GeminiError
from job_intelligence import normalize_config, recency_filter
from qa_store import QAStore
from resume_profile import get_or_build_profile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
LINKS_PATH = Path(__file__).resolve().parent / "links.txt"
NOT_TARGETED_PATH = Path(__file__).resolve().parent / "not_targeted_jobs.txt"
NOT_TARGETED_JSON_PATH = Path(__file__).resolve().parent / "not_targeted_jobs.json"
APPLIED_JOBS_PATH = Path(__file__).resolve().parent / "applied_jobs.json"

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""

# `/jobs/search/` renders the classic list: every card is an <li> carrying
# data-occludable-job-id, but only the ones near the viewport are hydrated,
# so each must be scrolled into view before its contents exist.
JOB_LIST_ITEM_SELECTORS = [
    "li[data-occludable-job-id]",
    "li.scaffold-layout__list-item",
    "div.job-card-container",
    "div[data-job-id]",
]

JOB_LIST_CONTAINER_SELECTORS = [
    "div.scaffold-layout__list > ul",
    "ul.scaffold-layout__list-container",
    ".jobs-search-results-list",
]

APPLICANT_COUNT_SELECTORS = [
    ".job-details-jobs-unified-top-card__primary-description-container",
    ".jobs-unified-top-card__applicant-count",
    ".job-details-jobs-unified-top-card__tertiary-description-container",
]

JOB_TITLE_SELECTORS = [
    ".job-details-jobs-unified-top-card__job-title",
    ".jobs-unified-top-card__job-title",
    "h1.t-24",
    "h1",
]

JOB_COMPANY_SELECTORS = [
    ".job-details-jobs-unified-top-card__company-name",
    ".jobs-unified-top-card__company-name",
]

JOB_LOCATION_SELECTORS = [
    ".job-details-jobs-unified-top-card__primary-description-container",
    ".jobs-unified-top-card__tertiary-description-container",
]

JOB_DESCRIPTION_SELECTORS = [
    ".jobs-description__content",
    ".jobs-box__html-content",
]


def load_config(path="config.json"):
    if not os.path.exists(path):
        print(f"{path} not found. Create it first.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return normalize_config(json.load(f))


def first_visible(scope, selectors, limit=10):
    for sel in selectors:
        locs = scope.locator(sel)
        try:
            count = locs.count()
        except Exception:
            continue
        for i in range(min(count, limit)):
            loc = locs.nth(i)
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                continue
    return None


def text_of(page, selectors):
    loc = first_visible(page, selectors, limit=3)
    if not loc:
        return ""
    try:
        return (loc.inner_text() or "").strip()
    except Exception:
        return ""


def find_easy_apply_button(page):
    """The details panel uses hashed class names, so match the button by role/name."""
    candidates = [
        page.get_by_role("button", name=re.compile(r"Easy Apply", re.I)),
        page.locator("button.jobs-apply-button"),
        page.locator('button:has-text("Easy Apply")'),
    ]
    for locs in candidates:
        try:
            count = locs.count()
        except Exception:
            continue
        for i in range(min(count, 5)):
            loc = locs.nth(i)
            try:
                if loc.is_visible() and loc.is_enabled():
                    return loc
            except Exception:
                continue
    return None


def click_easy_apply(page, timeout=20000):
    start = time.time()
    while (time.time() - start) * 1000 < timeout:
        loc = find_easy_apply_button(page)
        if loc:
            loc.scroll_into_view_if_needed()
            try:
                loc.click()
            except Exception:
                loc.evaluate("el => el.click()")
            return True
        page.wait_for_timeout(500)
    return False


def search_url(keywords, config):
    """Build a classic /jobs/search/ URL.

    This matters: the global search bar routes to /jobs/search-results/, LinkedIn's
    server-driven-UI page whose cards are hashed `componentkey` divs with no job id
    and no anchor. /jobs/search/ still serves the classic, automatable DOM.
    """
    params = {"keywords": keywords, "f_AL": "true"}
    params["f_TPR"] = recency_filter(config.get("posted_within_days", 7))
    if config.get("location"):
        params["location"] = config["location"]
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


def open_jobs_search(page, keywords, config, target_url=None):
    url = target_url or search_url(keywords, config)
    print(f"Opening: {url}")
    try:
        # LinkedIn often keeps network requests open indefinitely. Waiting for
        # the DOM is enough; the result list is hydrated below afterward.
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=int(config.get("navigation_timeout_ms", 60000)),
        )
    except PlaywrightTimeoutError:
        print("Navigation is still loading; continuing with the page received so far.")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(config.get("page_load_wait_ms", 3000))

    if "/jobs/search-results/" in page.url:
        print("LinkedIn opened the SDUI results page; switching the same search to the classic list.")
        classic_url = url.replace("/jobs/search-results/", "/jobs/search/")
        try:
            page.goto(
                classic_url,
                wait_until="domcontentloaded",
                timeout=int(config.get("navigation_timeout_ms", 60000)),
            )
        except PlaywrightTimeoutError:
            print("Classic results are still loading; continuing with the page received so far.")
        page.wait_for_timeout(config.get("page_load_wait_ms", 3000))

    print(f"Jobs URL: {page.url}")


def hydrate_cards(page, passes=4):
    """Scroll the results list so occluded cards render, then report the count."""
    container = first_visible(page, JOB_LIST_CONTAINER_SELECTORS, limit=2)
    last = 0
    for _ in range(passes):
        cards = job_cards(page)
        count = cards.count() if cards else 0
        if count and count == last:
            break
        last = count
        if container:
            try:
                container.evaluate("el => el.scrollTo(0, el.scrollHeight)")
            except Exception:
                page.mouse.wheel(0, 1500)
        else:
            page.mouse.wheel(0, 1500)
        page.wait_for_timeout(1200)

    if container:
        try:
            container.evaluate("el => el.scrollTo(0, 0)")
        except Exception:
            pass
    page.wait_for_timeout(800)
    cards = job_cards(page)
    return cards.count() if cards else 0


def job_cards(page):
    for sel in JOB_LIST_ITEM_SELECTORS:
        locs = page.locator(sel)
        try:
            if locs.count() > 0:
                return locs
        except Exception:
            continue
    return None


def applicant_count(page):
    """Parse the '36 applicants' figure from the details panel, if shown."""
    for sel in APPLICANT_COUNT_SELECTORS:
        loc = page.locator(sel)
        try:
            for i in range(min(loc.count(), 3)):
                text = (loc.nth(i).inner_text() or "")
                match = re.search(r"([\d,]+)\s+applicant", text, re.I)
                if match:
                    return int(match.group(1).replace(",", ""))
        except Exception:
            continue
    return None


def job_context(page):
    return {
        "title": text_of(page, JOB_TITLE_SELECTORS),
        "company": text_of(page, JOB_COMPANY_SELECTORS),
        "location": text_of(page, JOB_LOCATION_SELECTORS),
        "description": text_of(page, JOB_DESCRIPTION_SELECTORS),
        "url": page.url,
    }


def choose_resume_path(ctx, config):
    root = Path(__file__).resolve().parent / "resumes"
    resumes = list(root.glob("*.pdf")) + list(root.glob("*.docx")) + list(root.glob("*.doc"))
    text = " ".join((ctx.get(k) or "") for k in ("title", "company", "description")).lower()
    groups = {
        "backend": ("backend", "back-end", "django", "fastapi", "api", "server"),
        "frontend": ("frontend", "front-end", "react", "vue", "javascript", "ui", "web"),
        "fullstack": ("fullstack", "full-stack", "full stack", "mern", "node"),
        "dataengineer": ("data engineer", "data pipeline", "etl", "airflow", "spark", "warehouse"),
        "dataanalyst": ("data analyst", "data analysis", "sql", "analytics", "bi analyst"),
        "softwareengineer": ("software engineer", "software developer", "engineering", "developer"),
        "fde": ("founding", "founder", "early stage", "full-stack", "backend", "frontend"),
    }

    def score(path):
        stem = re.sub(r"[^a-z0-9]+", " ", path.stem.lower())
        matched_group = next((group for group in groups if group in stem), "")
        keywords = groups.get(matched_group, ())
        value = sum(3 for keyword in keywords if keyword in text)
        value += sum(2 for keyword in keywords if keyword in stem)
        if matched_group and matched_group in text:
            value += 5
        return value

    ranked = sorted(resumes, key=lambda path: (-score(path), path.name.lower()))
    if ranked and score(ranked[0]) > 0:
        return str(ranked[0])

    configured = config.get("default_resume_path") or config.get("resume_path")
    if configured and os.path.exists(configured):
        return configured
    return None


def job_id(ctx):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(ctx.get("url", "")).query)
    return (query.get("currentJobId") or [None])[0]


def load_applied_ids():
    if not APPLIED_JOBS_PATH.exists():
        return set()
    try:
        data = json.loads(APPLIED_JOBS_PATH.read_text(encoding="utf-8"))
        return set(str(value) for value in data if value)
    except (OSError, json.JSONDecodeError):
        return set()


def save_applied_id(identifier):
    applied = load_applied_ids()
    applied.add(str(identifier))
    APPLIED_JOBS_PATH.write_text(
        json.dumps(sorted(applied), indent=2), encoding="utf-8"
    )


def not_targeted_key(ctx):
    identifier = job_id(ctx)
    if identifier:
        return f"linkedin:{identifier}"
    url = (ctx.get("url") or "").split("&", 1)[0]
    if url:
        return f"url:{url}"
    parts = [re.sub(r"\s+", " ", (ctx.get(key) or "").strip().lower())
             for key in ("title", "company", "location")]
    return "details:" + "|".join(parts)


def load_not_targeted():
    if not NOT_TARGETED_JSON_PATH.exists():
        return {}
    try:
        data = json.loads(NOT_TARGETED_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    return {
        item.get("key"): item
        for item in data
        if isinstance(item, dict) and item.get("key")
    }


def save_not_targeted(ctx, reason, role):
    title = (ctx.get("title") or "Untitled job").replace("\n", " ").strip()
    company = (ctx.get("company") or "Unknown company").replace("\n", " ").strip()
    location = (ctx.get("location") or "Unknown location").replace("\n", " ").strip()
    key = not_targeted_key(ctx)
    records = load_not_targeted()
    records[key] = {
        "key": key,
        "job_id": job_id(ctx),
        "title": title,
        "company": company,
        "location": location,
        "url": ctx.get("url") or "",
        "reason": reason,
        "target_role": role,
    }
    NOT_TARGETED_JSON_PATH.write_text(
        json.dumps(sorted(records.values(), key=lambda item: item["key"]), indent=2),
        encoding="utf-8",
    )
    
    line = f"{title} | {company} | {location} | {reason} | target: {role}"
    existing = (NOT_TARGETED_PATH.read_text(encoding="utf-8").splitlines()
                if NOT_TARGETED_PATH.exists() else [])
    lines = list(dict.fromkeys(existing + [line]))
    NOT_TARGETED_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def allowed_location(ctx):
    # Use the structured header fields only. Searching the full description
    # would incorrectly classify jobs that merely mention remote work.
    searchable = " ".join(
        (ctx.get(key) or "") for key in ("title", "company", "location")
    ).lower()
    remote = "remote" in searchable or "work from home" in searchable
    karachi = "karachi" in searchable
    onsite_or_hybrid = any(
        word in searchable for word in ("on-site", "onsite", "on site", "hybrid")
    )
    return remote or (karachi and onsite_or_hybrid)


def load_links(path=LINKS_PATH):
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def already_applied(page):
    for sel in [
        '.jobs-s-apply span:has-text("Applied")',
        '.artdeco-inline-feedback:has-text("Applied")',
        'span:has-text("Application submitted")',
    ]:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def run(config):
    # LinkedIn's keyword matcher treats the whole template as literal terms, which
    # buries good results. The "recent" and "Easy Apply" parts are already handled
    # by the f_TPR/f_AL URL filters, and the applicant cap is enforced below from
    # the details panel, so search on the role alone by default.
    if config.get("keywords_mode", "role") == "template":
        keywords = config["query_template"].format(
            role=config["role"], applicants=config["applicants"]
        )
    else:
        keywords = config["role"]

    max_applicants = int(config.get("applicants") or 0) or None
    max_applications = int(config.get("max_applications", 5))
    applied_ids = load_applied_ids()
    resume_path = config.get("resume_path")
    if not resume_path:
        print("Set 'resume_path' in config.json to your resume PDF.")
        sys.exit(1)

    gemini = Gemini(config)
    store = QAStore()
    print(f"Loaded {len(store)} cached question/answer pair(s).")

    if not os.path.exists("linkedin_state.json"):
        print("linkedin_state.json not found. Run linkedin_login.py first.")
        sys.exit(1)

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": bool(config.get("headless", False)),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        try:
            browser = p.chromium.launch(channel="chrome", **launch_kwargs)
        except Exception:
            browser = p.chromium.launch(**launch_kwargs)

        context = browser.new_context(
            storage_state="linkedin_state.json",
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )
        context.add_init_script(STEALTH_SCRIPT)
        page = context.new_page()

        open_jobs_search(page, keywords, config, target_url=config.get("target_url"))

        total = hydrate_cards(page)
        print(f"{total} job card(s) on this page.")
        if not total:
            page.screenshot(path="error_no_cards.png", full_page=True)
            print("No cards found. Saved error_no_cards.png; run dump_page.py to inspect the DOM.")

        applied = 0
        skipped = 0

        for i in range(total):
            if applied >= max_applications:
                break

            cards = job_cards(page)
            if not cards or i >= cards.count():
                break

            card = cards.nth(i)
            try:
                card.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
                card.click()
            except Exception as e:
                print(f"[{i + 1}] could not open card: {e}")
                continue

            page.wait_for_timeout(2500)
            ctx = job_context(page)
            count = applicant_count(page)
            identifier = job_id(ctx)
            label = f"{count} applicants" if count is not None else "applicant count unknown"
            print(f"\n[{i + 1}/{total}] {ctx['title']} @ {ctx['company']} ({label})")

            if identifier and identifier in applied_ids:
                print(f"  Already applied; tracked currentJobId {identifier}; skipping.")
                skipped += 1
                continue

            prior_skip = load_not_targeted().get(not_targeted_key(ctx))
            if prior_skip:
                print(f"  Previously skipped ({prior_skip['reason']}); skipping.")
                skipped += 1
                continue

            if not allowed_location(ctx):
                save_not_targeted(ctx, "location not eligible", config["role"])
                print(f"  Location not eligible: {ctx['location'] or 'unknown'}; skipping.")
                skipped += 1
                continue

            resume_path = choose_resume_path(ctx, config)
            if not resume_path or not os.path.exists(resume_path):
                save_not_targeted(ctx, "no matching resume", config["role"])
                print("  No matching resume found; skipping.")
                skipped += 1
                continue
            profile = get_or_build_profile(resume_path, gemini=gemini)

            if max_applicants and count is not None and count > max_applicants:
                save_not_targeted(ctx, f"over {max_applicants}-applicant cap", config["role"])
                print(f"  Over the {max_applicants}-applicant cap; skipping.")
                skipped += 1
                continue

            if already_applied(page):
                print("  Already applied; skipping.")
                skipped += 1
                continue

            if not click_easy_apply(page, timeout=8000):
                save_not_targeted(ctx, "no Easy Apply button", config["role"])
                print("  No Easy Apply button; skipping.")
                skipped += 1
                continue

            try:
                sent = apply_to_current_job(page, gemini, profile, ctx, resume_path=resume_path, store=store)
            except GeminiError:
                raise
            except Exception as e:
                print(f"  [error] {e}")
                page.screenshot(path=f"error_apply_{i + 1}.png")
                sent = False

            if sent:
                applied += 1
                if identifier:
                    save_applied_id(identifier)
                    applied_ids.add(identifier)
                    print(f"  Tracked currentJobId: {identifier}")
                print(f"  Applied ({applied}/{max_applications}).")
            else:
                save_not_targeted(ctx, "application not submitted", config["role"])
                skipped += 1
                print("  Not submitted; moving on.")
                dismiss_modal(page)

            page.wait_for_timeout(2000)

        print(f"\nDone. Applied to {applied} job(s), skipped {skipped}.")
        context.close()
        browser.close()
        return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", nargs="?", help="Override config.json role")
    parser.add_argument("applicants", nargs="?", help="Override config.json applicants")
    parser.add_argument("--max", type=int, help="Override max_applications")
    args = parser.parse_args()

    config = load_config()
    if args.role:
        config["role"] = args.role
    if args.applicants:
        config["applicants"] = args.applicants
    if args.max:
        config["max_applications"] = args.max

    # A positional role is an explicit one-role override. Otherwise, process
    # each configured target independently so one search cannot hide the next.
    roles = [config.get("role", "Software Engineer")]
    if not args.role:
        configured_roles = config.get("roles")
        if isinstance(configured_roles, list):
            roles = [str(role).strip() for role in configured_roles if str(role).strip()]

    links = load_links()
    if links:
        # links.txt is authoritative: its search URL already contains the
        # user's combined role and location targeting.
        for link in links:
            for page_number in range(int(config.get("max_search_pages", 10))):
                link_config = dict(config)
                parsed_link = urllib.parse.urlsplit(link)
                query = dict(urllib.parse.parse_qsl(parsed_link.query, keep_blank_values=True))
                query["start"] = str(page_number * 25)
                link_config["target_url"] = urllib.parse.urlunsplit(
                    parsed_link._replace(query=urllib.parse.urlencode(query))
                )
                link_config["role"] = "roles from links.txt"
                print(f"\n=== Target search link, page {page_number + 1}: {link_config['target_url']} ===")
                total = run(link_config)
                if not total:
                    break
    else:
        for role in roles:
            role_config = dict(config)
            role_config["role"] = role
            print(f"\n=== Target role: {role} ===")
            run(role_config)


if __name__ == "__main__":
    main()
