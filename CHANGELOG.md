# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `applypilot tailor-url --url <posting> --resume <file.tex>`: tailor a LaTeX resume to a single
  job URL without running discover/enrich/score. Reuses the enrichment cascade, edits only summary,
  bullet wording and skills ordering, verifies facts/structure deterministically, compiles to PDF.
- `tailor-url` scores your untouched resume against the posting *before* generating anything, and
  reports the whole progression (`54% -> 71% -> 92%`: yours -> tailored -> keyword pass) on screen
  and at the top of `things_to_learn.txt`. Keywords are extracted once and reused by the gap pass,
  so the baseline costs no extra LLM call. `ats.baseline()` is the new entry point.
- `tailor-url --skip-above <n>`: a resume already scoring that well against the posting is sent
  untouched, skipping the tailor and gap passes (the two whole-`.tex` round trips that dominate
  the token cost). The job still gets its folder and PDF, named `<company>_<role>_ALREADY_MATCHED`
  and with no `things_to_learn.txt` -- nothing was added, so there is nothing to study.
  Defaults to `--ats-target` (90), so feeding a tailored resume back in -- the same posting a
  second time, or a resume already written for that stack -- costs one scoring call instead of
  two whole-`.tex` round trips. Pass `--skip-above 0` to tailor every time.
- `tailor-url --headline` (on by default, `--no-headline` to turn off): the tailor pass may add one
  line under your name naming the role this posting is for, in the posting's own words, optionally
  with 3-5 of its core technologies. Title matching is the first thing a recruiter and a title filter
  do, and the sb2nov template has no such line. The header stays frozen otherwise: every original
  header line must still be present and unchanged, at most one line may be added, and that line may
  not contain a number, a date or a credential -- a headline says which job you are applying for,
  which is not a claim anyone can check and find false.
- `tailor-url` takes several postings in one run: repeat `--url`, pass a comma-separated list, or
  use `--urls-file <file>` (one URL per line, `#` comments ignored). Jobs are tailored one after
  another into their own folders, a failure does not stop the rest (`--stop-on-error` to change
  that), and the run ends with an `N/M tailored` summary.
- `LLM_MODEL_FAST`: a second model for the calls whose output never reaches the resume -- reading the
  job title off the page, extracting the posting's keywords, and writing `things_to_learn.txt`. The
  study plan alone is the largest single output of a run (9.5k characters for 18 terms), and none of
  these three affect the generated `.tex`, so running them at top-tier rates buys nothing. The
  tailoring, keyword-gap and one-page passes stay on `LLM_MODEL`. Unset, everything behaves as before.
- `APPLYPILOT_DIR` can now be set from a project-local `.env` file (loaded before paths resolve).
- JSON-LD enrichment now also returns `title` and `company`.
- `applypilot doctor` reports whether a LaTeX compiler (tectonic/pdflatex) is available.
- `tailor-url` keeps the original preamble verbatim (spliced back after every LLM attempt) and, when
  compiling with tectonic (XeTeX), comments out pdfTeX-only lines such as `\input{glyphtounicode}`
  in a temporary build copy so Overleaf templates compile unchanged.
- `LLM_PROVIDER=claude` routes all LLM calls (scoring, tailoring, cover letters, tailor-url) through
  the Claude Code CLI (`claude -p`, tools disabled), so no Gemini/OpenAI key or quota is needed.

### Fixed
- A LaTeX length is no longer mistaken for an invented metric. `\\[0.5ex]`, `\vspace{-4pt}` and
  `0.15in` carry a digit but claim nothing, and counting them as facts rejected honest edits --
  most visibly a headline that copied the name line's `\\[0.5ex]` spacing, which failed with
  "Metric '0.5' is not in the original". `_fact_numbers()` now makes that distinction once, by
  looking at the unit that follows each number, and the verifier, the headline check and the
  shortening pass's must-keep list all use it.
- The one-page trim no longer quietly undoes the keyword pass. It used to be told only that the
  ATS terms *may* stay, while the prompt's must-keep list held numbers and dates only -- so a
  skills-line tail was the cheapest thing in the file to cut, and a run that reported `51% -> 97%`
  could write a `.tex` scoring 58%. The added terms are now named in the must-keep list in the
  spelling the file uses, a trim that drops one is sent back for another attempt, and the score,
  `things_to_learn.txt` and the run summary are all re-measured from the file that goes out
  (`ats.rescore()`). If every attempt costs a term, the least lossy one still ships -- one page is
  worth more than the last keyword -- but the run says which terms it cost and what the real score is.
- Default Gemini model is now `gemini-3.6-flash`; `gemini-2.0-flash` and `gemini-2.5-flash` return 404
  for new API keys. A Gemini 404 now reports Google's message (which names the replacement model)
  and the `LLM_MODEL` override instead of failing silently.
- `applypilot init` now merges AI settings into an existing `.env` instead of overwriting it
  (previously dropped `APPLYPILOT_DIR`, `CAPSOLVER_API_KEY` and proxy settings), and defaults
  to `gemini-3.6-flash`.
- Enrichment recognises `job-boards.greenhouse.io` descriptions (`.job__description`) at the CSS tier
  instead of falling through to an LLM call.
- `tailor-url` falls back to the installed Google Chrome when Playwright's bundled Chromium is
  blocked by Windows Application Control ("spawn UNKNOWN").

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
