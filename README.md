<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage autonomous job application pipeline. It discovers jobs across 5+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Three commands. That's it.

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot apply         # autonomous browser-driven submission
applypilot apply -w 3    # parallel apply (3 Chrome instances)
applypilot apply --dry-run  # fill forms without submitting
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), Gemini API key (free), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of ApplyPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Claude Code navigates application forms, fills fields, uploads documents, answers questions, and submits |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, `CAPSOLVER_API_KEY` (optional).

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
Claude Code launches a Chrome instance, navigates to each application page, detects the form type, fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions with AI, and submits. A live dashboard shows progress in real-time.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

---

## Bring Your Own Jobs: `tailor-url`

Already have a job-discovery system? Skip discover / enrich / score and tailor a
LaTeX resume to one posting:

```bash
applypilot tailor-url --url "https://boards.example.com/jobs/123" --resume path/to/main.tex
```

Several postings in one run: repeat `--url`, pass a comma-separated list, or point
`--urls-file` at a text file with one URL per line (blank lines and `#` comments are
ignored). Each job is fetched, tailored and compiled in turn into its own folder:

```bash
applypilot tailor-url -r resume.tex \
  -u "https://boards.example.com/jobs/123" \
  -u "https://jobs.other.com/456"

applypilot tailor-url -r resume.tex -f jobs.txt
```

A failed URL does not stop the rest -- the run ends with a `N/M tailored` summary
listing what failed and why, and exits non-zero. `--stop-on-error` aborts at the first
failure instead.

It follows redirects to the employer's real posting, extracts the description
(same cascade as the enrich stage), asks the LLM to edit **only** the summary,
bullet wording and skills ordering, verifies that the preamble, header, sections,
companies, titles, dates and every metric are untouched, then writes:

```
<data dir>/output/<company>_<role>/
  <Your_Name>.tex       tailored LaTeX (same formatting as your original)
  <Your_Name>.pdf       compiled with tectonic or pdflatex (whichever is on PATH)
  job.txt               the posting text the LLM saw
  changes.diff          exactly what changed vs. your original
  things_to_learn.txt   the terms added to hit the ATS target, and how to learn them
```

The file name comes from the name in your resume header (`Keerthivasan_Natarajan.pdf`),
because that is what a recruiter sees in their inbox.

### The ATS keyword pass

**Before anything is generated**, the posting's keywords are pulled out of the
requirements and your resume is scored against them untouched, so you see what it was
worth for that job on its own -- and, at the end, what the tailoring bought:

```
2/6 Your resume as-is: 54%  (13/24 keywords from the posting; 11 missing)
    Missing: Kubernetes, Kafka, Terraform, gRPC, ...
...
4/6 ATS keyword match: 54% -> 71% -> 92%  (yours -> tailored -> keyword pass, target 90%)
```

The same three numbers head `things_to_learn.txt`. `--no-ats` skips the baseline along
with the rest of the keyword work.

Because the baseline is known before any rewriting, `--skip-above <n>` short-circuits
a job you already fit:

```bash
applypilot tailor-url -r resume.tex -f jobs.txt --skip-above 88
```

A posting your resume already scores 88%+ on is sent untouched: no rewrite, no keyword
pass. Those two calls are each a whole `.tex` in and a whole `.tex` back (plus retries),
so they are the bulk of the token bill -- skipping them leaves only the posting fetch and
the keyword extraction.

The default is `--ats-target` (90), which matters most when you feed a tailored resume
back in -- the same posting again, or a resume already written for that stack. Generating
a second time what the first run already got to 90% is the one case where the bill buys
nothing, so it is off by default. Pass `--skip-above 0` to tailor every time.

You still get the full folder and the PDF, but named so you can see at a glance which
resumes were rewritten and which went out as they already were:

```
output/17-09-2026/
  acme_corp_backend_engineer/                    tailored, with a study plan
    Keerthivasan_Natarajan.tex / .pdf
    job.txt  changes.diff  things_to_learn.txt
  globex_platform_engineer_ALREADY_MATCHED/      sent as-is, nothing to study
    Keerthivasan_Natarajan.tex / .pdf
    job.txt  changes.diff
```

A skipped job has no `things_to_learn.txt`: nothing was added to the resume, so there is
nothing you need to study before the call -- the folder name is the whole report.

Rewording alone cannot match a term your resume never contained, and an ATS scores
you on literal terms. So after the safe tailor pass, ApplyPilot scores the tailored
version against those same keywords (required terms count double), and if the match is
under `--ats-target` (default 90%) it runs another
pass that **adds the missing terms whether or not you have used them** -- skills onto
the skills line, business-domain language into the summary. Rejected attempts are
retried with the verifier's complaints fed back.

What it will still never do: invent an employer, job title, project, date, metric,
degree or certification. Those are checked line by line and a violating edit is thrown
away. It adds technology *terms*, not history.

Every added term lands in `things_to_learn.txt` under the heading **"on your resume
now -- but not yet true"**, with what it is, 2-4 concrete things to learn, the question
it invites, and an hour estimate. That file is the price of the higher score: read it
before you reply to a recruiter, and delete from the `.tex` anything you are not
willing to be questioned on. `--no-ats` skips the whole pass and keeps the
reword-only resume.

PDF compilation needs `tectonic` or `pdflatex` on PATH. Easiest: download `tectonic.exe`
from the [tectonic releases](https://github.com/tectonic-typesetting/tectonic/releases)
and drop it into `.venv/Scripts/` (or `pip`'s bin dir); the first compile downloads
the TeX packages it needs. `applypilot doctor` shows which compiler was found.
Overleaf compiles with pdflatex by default; if you want byte-for-byte the same engine,
install MiKTeX (`winget install MiKTeX.MiKTeX`) and set `LATEX_COMPILER=pdflatex` in `.env`.

Options: `--out <folder>` to choose the output folder (with several URLs it is the
parent folder and each job gets its own `<company>_<role>/` subfolder), `--no-pdf` to
skip compiling, `--ats-target <n>` / `--no-ats` for the keyword pass above.
Nothing is submitted; this command never touches the apply stage.

Hitting Gemini free-tier limits? Put `LLM_PROVIDER=claude` in `.env` and every LLM call
(this command and the tailor/score/cover stages) goes through the Claude Code CLI you
already have for auto-apply, using your Claude login instead of an API key.

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply                        # Launch auto-apply
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
