# LinkedIn Apply

A desktop Windows application that automates LinkedIn job searches and Easy Apply using Playwright and an LLM (Gemini or a self-hosted Ollama model).

## What it does

- Searches multiple configured roles through LinkedIn's classic jobs results page.
- Normalizes, deduplicates, scores, and stores every discovered job in SQLite.
- Selects the best available resume and auto-fills LinkedIn Easy Apply forms.
- Uses Gemini or a local Ollama model to answer free-text and numeric form questions.
- Retries discovered jobs and fit-score skips on later runs.
- Runs in a desktop window (Flask + pywebview).

## Requirements

- Windows 10/11 with Python 3.10 or newer.
- A LinkedIn account.
- Either a Google Gemini API key, or Docker Desktop with Ollama for a local LLM.
- A PDF or TXT resume.

## Install

1. Clone or extract this folder.
2. Open PowerShell in the project folder and run:

```powershell
.\install.ps1
```

This will:

- Create a Python virtual environment (`venv`).
- Install all dependencies.
- Download the Playwright Chromium browser.
- Copy the example files to real config files:
  - `.env.example` → `.env`
  - `config.example.json` → `config.json`
- Add a **LinkedIn Apply** shortcut to your desktop.

3. Run the app:

```powershell
.\run.bat
```

Or double-click the **LinkedIn Apply** shortcut on your desktop.

## First-time setup

1. **Env tab**  
   If using Gemini, set your key:  
   `GEMINI_API_KEY=your_key`  
   If you use a local LLM, skip this and set `llm_provider` to `local` in the next step.

2. **Config tab**  
   - `target_roles` – enabled job titles, priorities, keywords, and fit thresholds.
   - `locations` / `remote` – preferred locations; remote jobs are accepted globally.
   - `posted_within_days` – search window: 1, 3, or 7 days.
   - `minimum_fit_score` – minimum score to apply; currently 50.
   - `max_applications` – how many applications to submit in one run.
   - `resume_path` – full path to your PDF resume.
   - `llm_provider` – `gemini` or `local`.
   - `gemini_model` – Gemini model name (e.g. `gemini-2.5-flash`).
   - `local_model` – Ollama model name (e.g. `llama3.2:1b`).

3. **Resume Profile tab**  
   Put resumes in `resumes/`. The bot builds and caches a factual profile for each file, then ranks them per job.
   To build it manually with Gemini, ask:

   > Extract a JSON profile with these fields: full_name, first_name, last_name, email, phone, phone_country_code, city, state, country, linkedin_url, github_url, portfolio_url, headline, summary, total_years_experience, current_title, current_company, highest_degree, field_of_study, university, graduation_year, skills, skill_years, certifications, languages, work_authorization, requires_visa_sponsorship, willing_to_relocate, notice_period_days, current_ctc, expected_ctc.

   Paste the JSON into the Resume Profile tab and click **Save Resume Profile**.  
   You can also run `resume_profile.py --force` from the project folder to generate it from your resume.

4. **Dashboard tab**  
   - Click **Start Login**.
   - Sign in to LinkedIn in the browser with your username and password.
   - Return to the app and click **Login Successful**.
   - Click **Start Search** to begin applying.

## UI tabs

- **Dashboard** – start LinkedIn login and the search/apply pipeline.
- **Config** – edit `config.json` (role, location, resume, LLM provider, etc.).
- **Env** – edit `.env` (Gemini API key).
- **Resume Profile** – edit the structured resume JSON.
- **QA Cache** – view and edit saved question/answer pairs.
- **Logs** – see real-time output and Ollama container logs.
- **Guide** – quick step-by-step instructions inside the app.

## Using a local LLM

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/).  
2. Start Ollama:

   ```powershell
   docker compose up -d
   ```

3. Pull a model:

   ```powershell
   docker exec ollama ollama pull llama3.2:1b
   ```

4. In `config.json` set:

   ```json
   {
     "llm_provider": "local",
     "local_model": "llama3.2:1b"
   }
   ```

## Troubleshooting

- **Tabs don’t click / blank dashboard**: close the app and restart `run.bat`.
- **Search stops with an error**: open the **Logs** tab. Common issues are a missing API key, an invalid `resume_path`, an unreachable LLM, or a model that is not pulled.
- **"Local model is not available"**: pull it first with `docker exec ollama ollama pull <model>`.
- **Playwright browser not found**: run `.\venv\Scripts\python.exe -m playwright install chromium`.

## Application rules and storage

- Jobs scoring 50 or higher are eligible for application.
- Jobs confirmed to be over 30 days old are skipped; unknown posting dates are allowed.
- Applicant count does not block an application.
- Previously discovered jobs and fit-score skips are retried before a new search.
- `scanned_jobs.db` stores job details, applied/skipped/error status, reasons, timestamps, and cached application answers.
- Applied jobs and other terminal statuses are not reset by later scans.

## For developers

- Runtime state and personal files are ignored by Git: `.env`, `config.json`, `linkedin_state.json`, `resume_profile.json`, `resume_profiles/`, `resumes/`, and `scanned_jobs.db`.
- If private files were previously tracked, remove them from the index without deleting local copies:

  ```powershell
  git rm --cached config.json resume_profile.json linkedin_state.json
  ```

## Manual install (without the installer)

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m playwright install chromium
copy .env.example .env
copy config.example.json config.json
python main.py
```
