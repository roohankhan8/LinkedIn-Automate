Work directly on the existing repository `roohankhan8/LinkedIn-Automate`.

IMPORTANT: Do NOT rely on README.md or AGENTS.md to determine what the application currently supports. Inspect the actual Python, HTML, JavaScript, JSON, and configuration files first. The implementation already supports multiple resumes, so preserve that functionality.

Before making changes, inspect at minimum:

* linkedin_search.py
* easy_apply.py
* resume_profile.py
* gemini_client.py
* qa_store.py
* app.py
* templates/
* static/
* config.json/config.example.json
* resumes/
* all existing application/job tracking JSON files

Understand the existing flow before modifying anything.

CURRENT FUNCTIONALITY TO PRESERVE

The existing application already:

* searches LinkedIn jobs
* supports multiple resumes under `resumes/`
* has `choose_resume_path()` logic
* extracts a structured profile from the selected resume
* uses Gemini for application questions
* caches application answers
* handles Easy Apply multi-step forms
* tracks applied jobs
* tracks not-targeted jobs
* has a Flask dashboard
* uploads the selected resume during Easy Apply

Do NOT rebuild these features from scratch.

The goal is to improve the intelligence and reliability of the existing system.

PRIMARY OBJECTIVE

I need a new job this month.

Turn the existing bot from:

Search → crude filtering → choose resume → Easy Apply

into:

Search → normalize → deduplicate → understand job → score fit → select best resume → prepare application → human review → submit → track outcome

The system should optimize for QUALITY OF APPLICATIONS, not maximum application volume.

==================================================

1. MULTI-ROLE SEARCH
   ==================================================

The current implementation is centered around one configured role.

Change this to support multiple configurable target roles.

My priorities:

TIER 1:

* Backend Engineer
* Backend Developer
* Python Backend Engineer
* Python Software Engineer
* Software Engineer Python
* Django Backend Engineer
* Django Developer
* FastAPI Developer
* Backend Engineer APIs
* Full Stack Engineer Python React

TIER 2:

* AI Backend Engineer
* AI Software Engineer
* AI Application Engineer
* LLM Engineer
* Generative AI Engineer
* AI Integration Engineer
* Software Engineer AI
* Full Stack Engineer AI
* Data Engineer

TIER 3:

* Laravel Developer
* PHP Backend Developer
* PHP Laravel Software Engineer
* Full Stack Developer
* Node.js Backend Engineer
* Software Engineer

Represent these as configurable search profiles rather than hard-coding them into the search loop.

Each search profile should support:

{
"name": "...",
"priority": 1,
"keywords": [],
"minimum_fit_score": 70,
"enabled": true
}

Do not make the system dependent on a single `config["role"]`.

==================================================
2. JOB ANALYSIS
===============

Currently the system primarily checks:

* title
* company
* location
* description
* applicant count

Expand the job representation.

Create a normalized job object containing:

* LinkedIn job ID
* URL
* title
* company
* location
* employment type
* workplace type
* seniority
* posting date
* applicant count
* description
* extracted required skills
* extracted preferred skills
* years of experience requirement
* education requirement
* responsibilities
* application method

Do not use Gemini unnecessarily for every small operation.

Use deterministic parsing where possible and Gemini only where semantic understanding is actually needed.

==================================================
3. JOB FIT SCORING
==================

Create a transparent 0–100 job fit score.

Suggested initial weighting:

Role/title match: 20
Required technical skills: 30
Backend/API relevance: 15
Experience/seniority: 10
Location/workplace compatibility: 10
AI relevance: 5
Database/data engineering: 5
Cloud/infrastructure: 5

Make weights configurable.

The result must contain:

{
"score": 87,
"tier": "A",
"strong_matches": [],
"partial_matches": [],
"missing_required": [],
"missing_preferred": [],
"concerns": [],
"reasoning": "..."
}

Do NOT produce an opaque AI score.

I should be able to understand why a job received 87 rather than 64.

==================================================
4. RESUME SELECTION
===================

IMPORTANT:

The repository ALREADY supports multiple resumes.

Do not replace this.

Instead, improve the existing `choose_resume_path()` implementation.

The current approach relies heavily on filename/group keyword matching.

Change it so each resume gets its own structured profile.

For example:

resumes/
backend.pdf
fullstack.pdf
frontend.pdf
data-analyst.pdf
fde.pdf
software-engineer.pdf

Create/cache a profile for EACH resume rather than maintaining one global `resume_profile.json`.

The profile should include:

* skills
* skill_years
* roles
* experience
* domains
* projects
* education
* certifications
* keywords
* target role categories

Then compare:

JOB PROFILE × RESUME PROFILE

For every job, rank the available resumes.

Example output:

Recommended resume:
Backend Resume

Resume match:
94/100

Why:

* Python/Django: strong
* REST APIs: strong
* SQL: strong
* Docker: strong
* AI integration: partial

Alternative:
Software Engineer Resume — 88/100

Do not automatically change the resume files themselves.

==================================================
5. RESUME PROFILE CACHE
=======================

Refactor `resume_profile.py`.

Instead of:

resume → resume_profile.json

support:

resume A → cached profile A
resume B → cached profile B
resume C → cached profile C

Use a deterministic identifier based on the resume path and/or file hash.

Do not repeatedly send unchanged resumes to Gemini.

If a resume changes, rebuild only that resume's profile.

Preserve the factual-only extraction behavior.

Never invent candidate information.

==================================================
6. APPLICATION QUESTIONS
========================

Improve `qa_store.py`.

Current caching is useful and should remain.

Add:

* confidence
* source
* resume/profile source
* question category
* last verified
* requires_confirmation

Classify questions into:

SAFE_REUSABLE
PROFILE_FACT
JOB_SPECIFIC
HIGH_RISK
UNKNOWN

Examples:

"Are you legally authorized to work in Pakistan?"
→ PROFILE_FACT / potentially HIGH_RISK

"How many years of Python experience do you have?"
→ PROFILE_FACT

"Why do you want to work here?"
→ JOB_SPECIFIC

"What is your expected salary?"
→ HIGH_RISK

Do not let Gemini invent answers.

For profile facts, use structured resume data.

For high-risk questions, require human confirmation.

==================================================
7. APPLICATION PREPARATION MODE
===============================

Add a mode where the system prepares applications but does NOT immediately submit them.

For example:

python linkedin_search.py --prepare

The system should:

1. Find jobs.
2. Score them.
3. Select appropriate resume.
4. Open Easy Apply.
5. Fill safe fields.
6. Stop before final submission.
7. Show what will be submitted.
8. Wait for user confirmation.

The existing automatic submission path can remain available as an explicit opt-in configuration, but it must NOT be the default.

Do not implement or improve anti-detection or platform-evasion mechanisms.

Remove the current browser `STEALTH_SCRIPT` that modifies:

* navigator.webdriver
* navigator.plugins
* window.chrome

Also do not add randomized behavior specifically designed to evade detection.

==================================================
8. APPLICATION TRACKING
=======================

The current code has:

* applied_jobs.json
* not_targeted_jobs.json
* not_targeted_jobs.txt

Preserve backward compatibility.

If practical, introduce SQLite as the canonical store while retaining migration compatibility with the existing JSON files.

Track:

DISCOVERED
REVIEWED
RECOMMENDED
PREPARED
SUBMITTED
SKIPPED
REJECTED
EXPIRED
FAILED

For each application record:

* job
* selected resume
* fit score
* application date
* status
* application URL
* notes
* failure reason

Never mark an application as submitted unless submission was actually detected.

==================================================
9. DUPLICATE DETECTION
======================

Improve duplicate detection.

Use, in order of reliability:

1. LinkedIn job ID
2. canonical URL
3. normalized company + title + location

A job discovered through multiple search profiles should appear once.

Store which search profiles matched it.

Example:

Backend Engineer
AI Software Engineer

→ same job

Store:

matched_roles:
[
"Backend Engineer",
"AI Software Engineer"
]

==================================================
10. LOCATION
============

My primary location target is Karachi.

Preferred:

1. Karachi
2. Remote Pakistan
3. Pakistan
4. Other locations only when explicitly remote-compatible

The existing `allowed_location()` logic should be improved.

Do not classify a job as remote merely because its description mentions the word "remote".

Use structured workplace/location information whenever available.

==================================================
11. JOB SEARCH WINDOW
=====================

The existing code heavily defaults to the past 24 hours.

Change discovery to support:

24 hours
3 days
7 days

Default to 7 days.

Deduplication should prevent repeatedly processing the same job.

A strong job posted 5 days ago is still valuable.

==================================================
12. EASY APPLY
==============

Preserve the existing Easy Apply implementation.

Do not rewrite its field detection unless necessary.

Improve it so that:

* selected resume is passed correctly
* answers come from the selected resume profile
* safe cached answers can be reused
* high-risk questions stop for confirmation
* validation errors are surfaced clearly
* failed applications are tracked
* submission status is verified

Do not bypass CAPTCHA or other access controls.

==================================================
13. EXTERNAL APPLICATIONS
=========================

Do not restrict the system to Easy Apply.

When an application requires an external site:

capture:

* application URL
* company
* role
* selected resume
* fit score

Then show:

EXTERNAL APPLICATION REQUIRED

and allow me to open the application manually.

Do not attempt to bypass external authentication or anti-bot controls.

==================================================
14. DASHBOARD
=============

Improve the existing Flask dashboard instead of replacing it.

Add:

TODAY

* jobs discovered
* A-tier jobs
* B-tier jobs
* applications prepared
* applications submitted

RECOMMENDED JOBS

For each:

Title
Company
Location
Fit score
Tier
Matched roles
Recommended resume
Strong matches
Missing requirements
Application method
Posted date
Status

APPLICATION QUEUE

Show:

Job
Resume
Fit score
Preparation status
Open application
Mark submitted

ANALYTICS

Show:

Applications
Interviews
Rejections
Average fit score
Applications by role
Applications by company
Resume usage
Interview conversion

==================================================
15. AI USAGE
============

Do not call Gemini for everything.

Use deterministic logic for:

* duplicate detection
* URL normalization
* location matching
* applicant count
* resume file discovery
* configuration
* basic keyword matching

Use Gemini for:

* job requirement extraction
* semantic skill matching
* application-question reasoning
* tailored motivation/cover letter
* ambiguous job classification

Require structured JSON from Gemini.

Validate every AI response.

==================================================
16. CONFIGURATION
=================

Redesign configuration without breaking existing configs.

Support:

target_roles
locations
remote
posted_within_days
minimum_fit_score
stretch_fit_score
max_applications
max_prepared_applications
human_review_required
easy_apply_enabled
external_applications_enabled

Also support company preferences:

preferred_companies
excluded_companies

Preferred companies should influence ranking but never override poor skill fit.

==================================================
17. APPLICATION QUALITY
=======================

The system should explicitly optimize:

10 excellent applications > 50 weak applications.

Do not optimize the system around an arbitrary daily application count.

The ranking should favor:

* strong technical match
* appropriate seniority
* realistic experience requirement
* good location
* recent posting
* appropriate resume
* credible application route

==================================================
18. TESTS
=========

Add tests for:

* job normalization
* duplicate detection
* fit scoring
* role matching
* resume profile selection
* multi-resume ranking
* question classification
* safe answer reuse
* high-risk question handling
* malformed Gemini response
* configuration validation

Do not use live LinkedIn in unit tests.

Mock Gemini and job data.

==================================================
19. BACKWARD COMPATIBILITY
==========================

Do not unnecessarily break:

* existing config.json
* existing resume directory
* existing QA cache
* applied_jobs.json
* not_targeted_jobs.json
* existing dashboard endpoints
* existing Easy Apply field handling

Provide migration logic where necessary.

==================================================
20. IMPORTANT IMPLEMENTATION RULE
=================================

Do not blindly implement this entire specification in one rewrite.

First inspect the actual code and produce:

1. Current architecture
2. Existing functionality
3. Problems/gaps
4. Proposed changes
5. Files that need modification

Then implement incrementally.

Do not duplicate functionality that already exists.

Do not create another resume-selection system alongside `choose_resume_path()`.

Refactor the existing implementation into a proper multi-resume ranking system.

At the end:

* run the available tests
* run Python compilation checks
* report exactly what changed
* report anything that could not be tested
* update documentation only after the implementation is complete

The final system should be an intelligent personal job-search/application assistant, not merely a high-volume Easy Apply script.
