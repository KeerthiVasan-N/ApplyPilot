# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `applypilot tailor-url --url <posting> --resume <file.tex>`: tailor a LaTeX resume to a single
  job URL without running discover/enrich/score. Reuses the enrichment cascade, edits only summary,
  bullet wording and skills ordering, verifies facts/structure deterministically, compiles to PDF.
- `APPLYPILOT_DIR` can now be set from a project-local `.env` file (loaded before paths resolve).
- JSON-LD enrichment now also returns `title` and `company`.
- `applypilot doctor` reports whether a LaTeX compiler (tectonic/pdflatex) is available.
- `tailor-url` keeps the original preamble verbatim (spliced back after every LLM attempt) and, when
  compiling with tectonic (XeTeX), comments out pdfTeX-only lines such as `\input{glyphtounicode}`
  in a temporary build copy so Overleaf templates compile unchanged.
- `LLM_PROVIDER=claude` routes all LLM calls (scoring, tailoring, cover letters, tailor-url) through
  the Claude Code CLI (`claude -p`, tools disabled), so no Gemini/OpenAI key or quota is needed.

### Fixed
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
