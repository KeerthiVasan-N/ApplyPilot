"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command("tailor-url")
def tailor_url(
    url: str = typer.Option(..., "--url", "-u", help="Job posting URL. Redirects are followed to the real posting."),
    resume: Path = typer.Option(
        ..., "--resume", "-r", exists=True, dir_okay=False, readable=True,
        help="Path to your LaTeX resume (.tex, single file).",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o",
        help="Output folder. Default: <data dir>/output/<company>_<role>/",
    ),
    no_pdf: bool = typer.Option(False, "--no-pdf", help="Write the .tex only, skip PDF compilation."),
    ats_target: int = typer.Option(
        90, "--ats-target",
        help="Keyword match to aim for. Below it, the missing terms are added and listed in things_to_learn.md.",
    ),
    no_ats: bool = typer.Option(False, "--no-ats", help="Skip the ATS keyword pass entirely (tailor wording only)."),
) -> None:
    """Tailor a LaTeX resume to ONE job URL (skips discover/score; never applies)."""
    from applypilot.config import load_env, ensure_dirs, get_llm_status
    from applypilot.scoring import ats
    from applypilot.scoring import tailor_tex as tt

    load_env()
    ensure_dirs()
    if not get_llm_status():
        console.print("[red]No LLM configured.[/red] Put GEMINI_API_KEY (or OPENAI_API_KEY / LLM_URL) in .env.")
        raise typer.Exit(1)

    original = resume.read_text(encoding="utf-8").replace("\r\n", "\n")
    parts = tt.external_inputs(original, resume.parent)
    if parts:
        console.print(f"[red]Failed:[/red] {resume.name} pulls in other files via \\input/\\include ({', '.join(parts)}). "
                      "Only single-file resumes are supported; inline them first.")
        raise typer.Exit(1)

    try:
        with console.status("[bold]1/5[/bold] Fetching job posting..."):
            job = tt.fetch_job(url)
        console.print(f"[green]1/5[/green] Job: [bold]{job['title']}[/bold] at [bold]{job['company']}[/bold]  "
                      f"[dim]({len(job['full_description'])} chars, tier {job.get('tier_used')})[/dim]")

        with console.status("[bold]2/5[/bold] Tailoring resume with the LLM (may retry)..."):
            tailored, report = tt.tailor_latex(original, job)
        console.print(f"[green]2/5[/green] Tailored and verified in {report['attempts']} attempt(s)")

        ats_result = None
        if no_ats:
            console.print("[yellow]3/5[/yellow] Skipped the ATS keyword pass (--no-ats)")
        else:
            with console.status("[bold]3/5[/bold] Scoring ATS keyword match..."):
                tailored, ats_result = ats.boost(original, tailored, job, target=ats_target)
            before, after = ats_result["score_before"], ats_result["score_after"]
            colour = "green" if after >= ats_target else "yellow"
            arrow = f"{before}% -> {after}%" if after != before else f"{after}%"
            console.print(f"[{colour}]3/5[/{colour}] ATS keyword match: [bold]{arrow}[/bold] "
                          f"[dim]({len(ats_result['keywords'])} keywords from the posting)[/dim]")
            if ats_result["added"]:
                console.print(f"[dim]   Added: {', '.join(k['term'] for k in ats_result['added'])}[/dim]")
            if ats_result["problems"] and not ats_result["added"]:
                console.print(f"[yellow]   Keyword pass rejected, kept the safe version: "
                              f"{ats_result['problems'][0]}[/yellow]")

        out_dir = out or tt.output_dir_for(job)
        tex_path = tt.write_outputs(out_dir, original, tailored, job)
        console.print(f"[green]4/5[/green] Wrote {tex_path}")

        learn_path = None
        if ats_result and (ats_result["added"] or ats_result["missing_after"] or ats_result["problems"]):
            with console.status("[bold]4/5[/bold] Writing the study plan..."):
                notes = ats.study_notes(ats_result["added"], job)
                learn_path = ats.write_learning_plan(out_dir, job, ats_result, ats_result["added"], notes)
            console.print(f"[dim]   Wrote {learn_path.name}: "
                          f"{len(ats_result['added'])} to learn, {len(ats_result['missing_after'])} still unmatched[/dim]")

        pdf_path = None
        if no_pdf:
            console.print("[yellow]5/5[/yellow] Skipped PDF (--no-pdf)")
        else:
            with console.status("[bold]5/5[/bold] Compiling PDF..."):
                pdf_path = tt.compile_pdf(tex_path)
                pages = tt.pdf_page_count(pdf_path)
                if pages > tt.MAX_PDF_PAGES:
                    console.print(f"[yellow]   PDF is {pages} pages; asking the LLM to tighten wording...[/yellow]")
                    keep = {t.lower() for k in (ats_result["added"] if ats_result else [])
                            for t in [k["term"], *k.get("aliases", [])]}
                    shortened, shorten_report = tt.shorten_latex(original, tailored, job, allow_skills=keep)
                    if shorten_report["status"] == "verified":
                        tailored = shortened
                        tex_path = tt.write_outputs(out_dir, original, tailored, job)
                        pdf_path = tt.compile_pdf(tex_path)
                        pages = tt.pdf_page_count(pdf_path)
                    else:
                        console.print(f"[yellow]   Could not shorten without losing a fact "
                                      f"({shorten_report['problems'][0]}); kept the long version.[/yellow]")
            plural = "s" if pages != 1 else ""
            console.print(f"[green]5/5[/green] Compiled PDF ({pages} page{plural})")
            if pages > tt.MAX_PDF_PAGES:
                console.print(f"[yellow]   Still {pages} pages. Check the PDF and trim manually if needed.[/yellow]")
    except tt.TailorError as e:
        console.print(f"[red]Failed:[/red] {e}")
        raise typer.Exit(1) from None
    except Exception as e:  # noqa: BLE001 - LLM/network errors: print a message, not a traceback
        msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        console.print(f"[red]Failed:[/red] {msg}")
        if "429" in msg or "Too Many Requests" in msg or "quota" in msg.lower():
            console.print("[yellow]LLM quota exhausted. Wait a bit, or set LLM_PROVIDER=claude in .env "
                          "to use the Claude Code CLI instead of the Gemini API.[/yellow]")
        raise typer.Exit(1) from None

    console.print()
    console.print(f"[bold]TEX:[/bold]  {tex_path}")
    if pdf_path:
        console.print(f"[bold]PDF:[/bold]  {pdf_path}")
    if learn_path:
        console.print(f"[bold]LEARN:[/bold] {learn_path}  [dim](what the resume now claims -- study before the call)[/dim]")
    console.print("[dim]Also in that folder: job.txt (what the LLM saw) and changes.diff (what it changed)[/dim]")


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    use_claude_cli = os.environ.get("LLM_PROVIDER", "").strip().lower() in ("claude", "claude-cli", "claude-code")
    if use_claude_cli:
        claude_path = shutil.which("claude")
        if claude_path:
            model = os.environ.get("LLM_MODEL") or "CLI default"
            results.append(("LLM API key", ok_mark, f"Claude Code CLI ({model})"))
        else:
            results.append(("LLM API key", fail_mark, "LLM_PROVIDER=claude but 'claude' CLI not found on PATH"))
    elif has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-3.6-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        f"Set GEMINI_API_KEY in {ENV_PATH} (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # LaTeX compiler (only needed for `applypilot tailor-url` PDF output)
    from applypilot.scoring.tailor_tex import find_latex_compiler
    compiler = find_latex_compiler()
    if compiler:
        results.append(("LaTeX compiler", ok_mark, f"{compiler[0]}: {compiler[1]}"))
    else:
        results.append(("LaTeX compiler", "[dim]optional[/dim]",
                        "For tailor-url PDFs: put tectonic.exe in .venv/Scripts (or winget install MiKTeX.MiKTeX)"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
