"""Drive the LinkedIn Easy Apply modal: read every field, answer it with Gemini, submit."""

import json
import os
import re

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from qa_store import QAStore
from resume_profile import profile_for_prompt

MODAL_SELECTORS = [
    "div.jobs-easy-apply-modal",
    '[role="dialog"]:has-text("Apply to")',
    ".jobs-easy-apply-content",
    "[role='dialog']",
]

SUCCESS_SELECTORS = [
    'h2:has-text("Your application was sent")',
    'h3:has-text("Application sent")',
    'h2:has-text("Application sent")',
    ".artdeco-inline-feedback--success",
]

ERROR_SELECTORS = [
    ".artdeco-inline-feedback--error",
    ".fb-dash-form-element__error-text",
    'div[role="alert"]',
]

ANSWER_SYSTEM = (
    "You fill out job application forms on behalf of a candidate, using only the facts in "
    "their resume profile. Answer as the candidate, in the first person where relevant.\n"
    "Rules:\n"
    "- For numeric fields (years of experience, notice period, salary) return digits only.\n"
    "- For yes/no fields return exactly 'Yes' or 'No'.\n"
    "- When a list of options is given, return one option verbatim.\n"
    "- Never leave an answer empty; pick the most plausible value from the profile.\n"
    "- Never claim experience the profile does not support; if a skill is absent, answer 0 or 'No'.\n"
    "- Exception: if a question asks which technologies, tools, or frameworks you have worked with, give a modern, up-to-date list including LangGraph, LangChain, OpenAI, RAG, vector databases, FastAPI, Python, etc.\n"
    "- Keep free-text answers under 300 characters unless the question asks for more."
)

# Reads every control inside the modal, tags it so Python can address it later,
# and returns a descriptor including the question text LinkedIn shows the user.
COLLECT_FIELDS_JS = """
(modal) => {
  // LinkedIn prints every label twice: once in a visible aria-hidden span and
  // once in a .visually-hidden span for screen readers. Reading innerText
  // naively yields "Question? Question?", so drop the duplicate and collapse
  // any remaining doubled string.
  const dedupe = (s) => {
    const m = s.match(/^(.+?)\\s*\\1$/);
    return m ? m[1].trim() : s;
  };

  const textOf = (el) => {
    if (!el) return '';
    const clone = el.cloneNode(true);
    clone.querySelectorAll('.visually-hidden').forEach(n => n.remove());
    const raw = (clone.textContent || '').replace(/\\s+/g, ' ').trim();
    return dedupe(raw);
  };

  const labelFor = (el) => {
    if (el.getAttribute('aria-labelledby')) {
      const parts = el.getAttribute('aria-labelledby').split(/\\s+/)
        .map(id => textOf(document.getElementById(id)))
        .filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    if (el.id) {
      const lbl = modal.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return textOf(lbl);
    }
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const fieldset = el.closest('fieldset');
    if (fieldset) {
      const legend = fieldset.querySelector('legend');
      if (legend) return textOf(legend);
    }
    let node = el.parentElement;
    for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
      const lbl = node.querySelector('label, legend');
      if (lbl && !lbl.contains(el)) return textOf(lbl);
    }
    return '';
  };

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const fields = [];
  let idx = 0;
  const tag = (el) => { el.setAttribute('data-aa-idx', String(idx)); return idx++; };

  // Radio groups first, so their individual inputs are not also picked up as checkboxes.
  const seenRadioGroups = new Set();
  modal.querySelectorAll('input[type="radio"]').forEach((radio) => {
    const group = radio.name || (radio.closest('fieldset') ? 'fs' + Math.random() : '');
    if (!group || seenRadioGroups.has(group)) return;
    seenRadioGroups.add(group);
    const fieldset = radio.closest('fieldset') || modal;
    const radios = Array.from(fieldset.querySelectorAll(`input[type="radio"]`))
      .filter(r => r.name === radio.name);
    const options = radios.map(r => labelFor(r)).filter(Boolean);
    const legend = fieldset.querySelector('legend');
    fields.push({
      index: tag(fieldset === modal ? radio : fieldset),
      kind: 'radio',
      name: radio.name || '',
      label: textOf(legend) || labelFor(radio),
      options,
      required: radios.some(r => r.required),
      value: (radios.find(r => r.checked) || {}).value || '',
    });
  });

  modal.querySelectorAll('select').forEach((el) => {
    if (!visible(el)) return;
    const options = Array.from(el.options).map(o => o.label || o.text).map(s => s.trim());
    fields.push({
      index: tag(el),
      kind: 'select',
      label: labelFor(el),
      options: options.filter(o => o && !/^select an option$/i.test(o)),
      required: el.required,
      value: el.value,
    });
  });

  modal.querySelectorAll('textarea').forEach((el) => {
    if (!visible(el)) return;
    fields.push({
      index: tag(el),
      kind: 'textarea',
      label: labelFor(el),
      options: [],
      required: el.required,
      value: el.value,
    });
  });

  modal.querySelectorAll('input').forEach((el) => {
    if (!visible(el)) return;
    const type = (el.type || 'text').toLowerCase();
    if (type === 'radio' || type === 'hidden' || type === 'file' || type === 'submit') return;
    if (type === 'checkbox') {
      fields.push({
        index: tag(el),
        kind: 'checkbox',
        label: labelFor(el),
        options: ['Yes', 'No'],
        required: el.required,
        value: el.checked ? 'Yes' : 'No',
      });
      return;
    }
    const combobox = el.getAttribute('role') === 'combobox'
      || el.getAttribute('aria-autocomplete') === 'list';
    fields.push({
      index: tag(el),
      kind: combobox ? 'typeahead' : 'text',
      inputType: type,
      label: labelFor(el),
      options: [],
      required: el.required,
      value: el.value,
    });
  });

  return fields;
}
"""


def _first_visible(scope, selectors, limit=5):
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


def find_modal(page):
    return _first_visible(page, MODAL_SELECTORS)


def wait_for_modal(page, timeout=20000):
    waited = 0
    while waited < timeout:
        modal = find_modal(page)
        if modal:
            return modal
        page.wait_for_timeout(500)
        waited += 500
    raise PlaywrightTimeout("Easy Apply modal did not open")


def is_follow_company(label):
    """Matches LinkedIn's 'Follow <Company> to stay up to date with their page.' opt-in."""
    text = label.lower()
    if "follow" not in text:
        return False
    return text.startswith("follow") or "stay up to date" in text or "company" in text


# Promo/opt-in checkboxes that are not application questions. Gemini should never
# be asked about these; they are always left unchecked.
OPT_OUT_PATTERNS = (
    "mark job as a top choice",
    "top choice",
    "notify me of similar",
    "save my answers",
)


def is_opt_out(label):
    text = label.lower()
    return is_follow_company(label) or any(p in text for p in OPT_OUT_PATTERNS)


def collect_fields(modal):
    fields = modal.evaluate(COLLECT_FIELDS_JS)
    return [f for f in fields if f.get("label")]


def answer_fields(gemini, profile, job_context, fields, store):
    """Return {index: answer}, using the cache first and one batched Gemini call for the rest."""
    answers = {}
    pending = []

    for field in fields:
        label = field["label"]
        if is_opt_out(label):
            continue
        options = field.get("options") or None
        cached = store.get(label, field["kind"], options)
        if cached is not None:
            print(f"  [cache] {label!r} -> {cached!r}")
            answers[field["index"]] = cached
        else:
            pending.append(field)

    if not pending:
        return answers

    questions = [
        {
            "index": f["index"],
            "question": f["label"],
            "type": f["kind"],
            "input_type": f.get("inputType"),
            "options": f.get("options") or [],
            "required": f.get("required", False),
        }
        for f in pending
    ]

    prompt = (
        "Candidate resume profile:\n"
        f"{json.dumps(profile_for_prompt(profile), indent=2, ensure_ascii=False)}\n\n"
        f"Job being applied to: {json.dumps(job_context, ensure_ascii=False)}\n\n"
        "Answer each application field below.\n"
        f"{json.dumps(questions, indent=2, ensure_ascii=False)}\n\n"
        'Return JSON: {"answers": [{"index": <int>, "answer": "<string>"}]}. '
        "Every index must appear exactly once. Answers must be strings."
    )

    print(f"  [gemini] answering {len(pending)} field(s)...")
    result = gemini.generate_json(prompt, system=ANSWER_SYSTEM)
    by_index = {f["index"]: f for f in pending}

    for item in result.get("answers", []):
        idx = item.get("index")
        answer = item.get("answer")
        if idx not in by_index or answer is None:
            continue
        answer = str(answer).strip()
        field = by_index[idx]
        answers[idx] = answer
        store.put(field["label"], answer, field["kind"], field.get("options") or None)
        print(f"  [gemini] {field['label']!r} -> {answer!r}")

    missing = [by_index[i]["label"] for i in by_index if i not in answers]
    if missing:
        print(f"  [warn] no answer produced for: {missing}")
    return answers


def _closest_option(answer, options):
    """Match the model's answer to an actual option, tolerating wording drift."""
    if not options:
        return answer
    lowered = {o.lower(): o for o in options}
    a = answer.strip().lower()
    if a in lowered:
        return lowered[a]
    for opt in options:
        if a and (a in opt.lower() or opt.lower() in a):
            return opt
    if a in ("yes", "true"):
        for opt in options:
            if opt.lower() in ("yes", "true"):
                return opt
    if a in ("no", "false"):
        for opt in options:
            if opt.lower() in ("no", "false"):
                return opt
    return options[0]


NUMERIC_LABEL_RE = re.compile(
    r"how many|number of|years of|years'? experience|notice period|expected ctc|current ctc|salary",
    re.I,
)


def coerce_answer(field, answer):
    """LinkedIn rejects prose in numeric fields, so reduce those answers to a bare number.

    Phone fields keep their formatting: stripping '+' and spaces there breaks
    international numbers.
    """
    input_type = field.get("inputType")
    if input_type == "tel":
        return answer
    numeric = input_type == "number" or (
        input_type == "text" and NUMERIC_LABEL_RE.search(field.get("label", ""))
    )
    if not numeric:
        return answer
    match = re.search(r"\d+(?:\.\d+)?", answer)
    return match.group(0) if match else "0"


def fill_field(modal, field, answer):
    idx = field["index"]
    kind = field["kind"]
    target = modal.locator(f'[data-aa-idx="{idx}"]')

    if kind in ("text", "textarea"):
        target.fill(coerce_answer(field, answer))
        return

    if kind == "select":
        option = _closest_option(answer, field.get("options") or [])
        try:
            target.select_option(label=option)
        except Exception:
            target.select_option(option)
        return

    if kind == "radio":
        option = _closest_option(answer, field.get("options") or [])
        radios = target.locator('input[type="radio"]')
        count = radios.count()
        for i in range(count):
            radio = radios.nth(i)
            rid = radio.get_attribute("id")
            label_text = ""
            if rid:
                lbl = modal.locator(f'label[for="{rid}"]')
                if lbl.count():
                    label_text = (lbl.first.inner_text() or "").strip()
            if label_text.lower() == option.lower():
                radio.check(force=True)
                return
        radios.first.check(force=True)
        return

    if kind == "checkbox":
        should_check = answer.strip().lower() in ("yes", "true", "1")
        if should_check:
            target.check(force=True)
        else:
            target.uncheck(force=True)
        return

    if kind == "typeahead":
        target.fill(answer)
        modal.page.wait_for_timeout(1200)
        option = modal.locator('div[role="option"], li.basic-typeahead__triggered-content').first
        if option.count() and option.is_visible():
            option.click()
        else:
            target.press("Enter")
        return


def uncheck_opt_outs(modal, fields):
    for field in fields:
        if field["kind"] == "checkbox" and is_opt_out(field["label"]):
            try:
                modal.locator(f'[data-aa-idx="{field["index"]}"]').uncheck(force=True)
                print(f"  Unchecked opt-in: {field['label']!r}")
            except Exception:
                pass


def click_step_button(modal):
    """Advance the wizard. Returns the action taken, or None if no button was found."""
    actions = [
        ("submit", 'button[aria-label="Submit application"]'),
        ("submit", 'button:has-text("Submit application")'),
        ("review", 'button[aria-label="Review your application"]'),
        ("review", 'button:has-text("Review")'),
        ("next", 'button[aria-label="Continue to next step"]'),
        ("next", 'button:has-text("Next")'),
        ("next", 'button:has-text("Continue")'),
    ]
    for action, sel in actions:
        loc = modal.locator(sel).first
        try:
            if loc.count() and loc.is_visible() and loc.is_enabled():
                loc.click()
                print(f"  Clicked '{action}' button.")
                return action
        except Exception:
            continue
    return None


def has_errors(modal):
    for sel in ERROR_SELECTORS:
        loc = modal.locator(sel)
        try:
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return (loc.nth(i).inner_text() or "").strip()
        except Exception:
            continue
    return None


def application_sent(page):
    return _first_visible(page, SUCCESS_SELECTORS) is not None


def dismiss_modal(page):
    for sel in [
        'button[aria-label="Dismiss"]',
        "button.artdeco-modal__dismiss",
        'button[aria-label="Close"]',
    ]:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                loc.click()
                page.wait_for_timeout(800)
                break
        except Exception:
            continue
    for sel in ['button:has-text("Discard")', 'button:has-text("Done")']:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                loc.click()
                page.wait_for_timeout(500)
        except Exception:
            continue


def step_signature(modal, fields):
    """Identifies a wizard step, so a step that fails to advance can be detected.

    Without this the wizard re-clicks 'Review' forever whenever a field cannot be
    satisfied, because the same page keeps re-rendering with the same error.
    """
    progress = ""
    try:
        prog = modal.locator("progress").first
        if prog.count():
            progress = prog.get_attribute("value") or ""
    except Exception:
        pass
    return progress + "|" + "|".join(sorted(f["label"] for f in fields))


def apply_to_current_job(page, gemini, profile, job_context, resume_path=None, max_steps=15, store=None):
    """Run the whole Easy Apply wizard. Returns True if the application was sent."""
    store = store or QAStore()
    modal = wait_for_modal(page)
    print("Easy Apply modal open.")

    last_signature = None
    stalls = 0

    for step in range(1, max_steps + 1):
        page.wait_for_timeout(1200)
        modal = find_modal(page) or modal

        if application_sent(page):
            print("Application sent.")
            dismiss_modal(page)
            return True

        fields = collect_fields(modal)
        if resume_path and os.path.exists(resume_path):
            upload = modal.locator('input[type="file"]').first
            if upload.count():
                upload.set_input_files(resume_path)
                print(f"  Selected resume: {os.path.basename(resume_path)}")
        signature = step_signature(modal, fields)
        print(f"Step {step}: {len(fields)} field(s) detected.")

        if signature == last_signature:
            stalls += 1
            if stalls >= 2:
                error = has_errors(modal)
                print(f"  Stuck on the same step (last error: {error}); abandoning this job.")
                page.screenshot(path=f"easy_apply_stalled_step{step}.png")
                return False
        else:
            stalls = 0
        last_signature = signature

        if fields:
            uncheck_opt_outs(modal, fields)
            answers = answer_fields(gemini, profile, job_context, fields, store)
            for field in fields:
                if is_opt_out(field["label"]):
                    continue
                answer = answers.get(field["index"])
                if answer is None:
                    continue
                try:
                    fill_field(modal, field, answer)
                except Exception as e:
                    print(f"  [warn] could not fill {field['label']!r}: {e}")

        action = click_step_button(modal)
        if action is None:
            print("  No next/review/submit button found.")
            if application_sent(page):
                dismiss_modal(page)
                return True
            page.screenshot(path=f"easy_apply_stuck_step{step}.png")
            return False

        page.wait_for_timeout(1500)
        error = has_errors(find_modal(page) or modal)
        if error:
            print(f"  [warn] validation error still showing: {error}")

        if action == "submit":
            page.wait_for_timeout(2500)
            sent = application_sent(page) or find_modal(page) is None
            if sent:
                print("Application submitted.")
                dismiss_modal(page)
                return True

    print("Hit the step limit without submitting.")
    page.screenshot(path="easy_apply_step_limit.png")
    return False
