"""Tailor a LaTeX resume to a single job URL.

Bypasses the discover / enrich-from-search / score stages entirely: you bring
the job URL, ApplyPilot fetches the posting (reusing the enrichment cascade),
asks the LLM to tailor your .tex, verifies the edit deterministically, writes
the result to <data dir>/output/<company>_<role>/ and compiles it to PDF.

Nothing here touches the database or the apply stage.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from applypilot.config import OUTPUT_DIR
from applypilot.llm import get_client

log = logging.getLogger(__name__)

MAX_DESC_CHARS = 6000       # same cap the batch tailor uses
MAX_RETRIES = 2             # LLM attempts after the first
LENGTH_TOLERANCE = 0.15     # output may be up to this much SHORTER than the original
LENGTH_GROWTH = 0.03        # ...but at most this much LONGER (a full one-page resume has no slack)
REWRITE_GROWTH = 0.10       # rewriting restructures whole bullets, so the word count moves more.
HEADLINE_WORDS = 12         # a headline line is added, not swapped in: the budget has to fund it.
                            # The real limit is still one page: pdf_page_count checks it after
                            # compiling, and the shorten pass tightens anything that overflows.
MAX_PDF_PAGES = 1
MAX_OUTPUT_TOKENS = 16384   # a full .tex plus the reasoning tokens thinking models spend


class TailorError(RuntimeError):
    """Raised when the job cannot be fetched or the tailored .tex fails verification."""


# ── 1. Fetch the job (reuses enrichment) ──────────────────────────────────

def fetch_job(url: str) -> dict:
    """Follow redirects to the real posting and extract title, company, description."""
    from playwright.sync_api import sync_playwright

    from applypilot.enrichment import detail

    log.info("Fetching job posting: %s", url)
    try:
        with sync_playwright() as p:
            browser = _launch_browser(p)
            try:
                page = browser.new_context(user_agent=detail.UA).new_page()
                result = detail.scrape_detail_page(page, url)
            finally:
                browser.close()
    except TailorError:
        raise
    except Exception as e:  # surface a readable message instead of a Playwright traceback
        raise TailorError(f"Browser error while fetching the posting: {_first_line(e)}") from e

    if not result.get("full_description"):
        raise TailorError(
            f"Could not extract a job description from {url} "
            f"({result.get('error') or 'no data extracted'})."
        )

    job = {
        "url": url,
        "final_url": result.get("final_url") or url,
        "title": (result.get("title") or "").strip(),
        "company": (result.get("company") or "").strip(),
        "full_description": result["full_description"],
        "application_url": result.get("application_url"),
        "tier_used": result.get("tier_used"),
    }
    if not job["title"] or not job["company"]:
        _fill_title_company(job, result.get("page_title") or "")
    return job


def _launch_browser(p):
    """Launch Playwright's bundled Chromium; fall back to the installed Google Chrome.

    Some Windows machines block the bundled headless shell via Application Control
    policy ("spawn UNKNOWN"); the system Chrome that ApplyPilot already needs for
    the apply stage is allowed there.
    """
    errors = []
    for kwargs in ({}, {"channel": "chrome"}):
        try:
            return p.chromium.launch(headless=True, **kwargs)
        except Exception as e:  # noqa: BLE001 - try the next launch strategy
            errors.append(f"{kwargs.get('channel', 'bundled chromium')}: {_first_line(e)}")
            log.warning("Browser launch failed (%s); trying next option", errors[-1])
    raise TailorError(
        "Could not launch a headless browser.\n  " + "\n  ".join(errors)
        + "\n  Fix: run `playwright install chromium`, or install Google Chrome."
    )


def _first_line(e: Exception) -> str:
    text = str(e).strip()
    return text.splitlines()[0] if text else type(e).__name__


_ATS_WORDS = {"greenhouse", "lever", "workday", "myworkdayjobs", "ashby", "ashbyhq", "smartrecruiters", "icims",
              "linkedin", "indeed", "glassdoor", "ziprecruiter", "jobvite", "bamboohr", "workable", "job-boards",
              "boards", "jobs", "careers", "apply", "www", "job", "career"}


def _fill_title_company(job: dict, page_title: str) -> None:
    """Fill missing title/company: parse the page title first (free), then ask the LLM, then use the domain."""
    title, company = _parse_page_title(page_title)
    job["title"] = job["title"] or title
    job["company"] = job["company"] or company
    if job["title"] and job["company"]:
        return

    from applypilot.scoring.tailor import extract_json

    prompt = (
        "From this job posting, identify the job title and the hiring company.\n"
        'Return ONLY JSON: {"title": "...", "company": "..."}. Use null if unknown.\n\n'
        f"PAGE TITLE: {page_title}\n\nPOSTING:\n{job['full_description'][:3000]}"
    )
    try:
        data = extract_json(get_client(fast=True).ask(prompt, max_tokens=2048))
        job["title"] = job["title"] or (data.get("title") or "").strip()
        job["company"] = job["company"] or (data.get("company") or "").strip()
    except Exception as e:  # noqa: BLE001 - best effort, fallbacks below
        log.warning("Title/company extraction failed: %s", e)

    if not job["title"]:
        job["title"] = "role"
    if not job["company"]:
        host = urlparse(job["final_url"]).hostname or "company"
        parts = [x for x in host.split(".") if x not in _ATS_WORDS]
        job["company"] = parts[0] if parts else "company"


def _parse_page_title(page_title: str) -> tuple[str, str]:
    """Best-effort (title, company) from patterns like 'Job Application for X at Y' or 'X - Y | Board'."""
    text = re.sub(r"^\s*(?:job application for|apply for|job opening:|careers?:)\s*", "", page_title, flags=re.IGNORECASE)
    text = text.strip()
    if not text:
        return "", ""
    m = re.match(r"^(?P<title>.+?)\s+(?:at|@)\s+(?P<company>[^|]+?)\s*(?:[|\-–—]\s*.*)?$", text)
    if m:
        return m.group("title").strip(), m.group("company").strip()
    parts = [p.strip() for p in re.split(r"\s[|\-–—]\s", text) if p.strip()]
    if len(parts) >= 2:
        candidates = [p for p in parts[1:] if p.lower().replace(" ", "") not in _ATS_WORDS]
        return parts[0], (candidates[-1] if candidates else "")
    return parts[0] if parts else "", ""


# ── 2. Tailor with the LLM ────────────────────────────────────────────────

SYSTEM_PROMPT = """You are editing a LaTeX resume so it targets one specific job posting. You will
receive the complete .tex source and the job description. Return the complete,
compilable .tex source and nothing else: no markdown fences, no commentary.

In this file the summary section is "{summary_section}" and the skills section is
"{skills_section}".

ALLOWED EDITS (only these):
1. Summary: rewrite the text inside the single \\item of the "{summary_section}"
   section so it mirrors the job's language and priorities. Keep it one
   paragraph of similar length (max 3 sentences). Keep the surrounding
   \\item \\small{{...}} wrapper exactly, and use \\textbf{{}} on 3 to 6 keywords that
   appear in the job description AND are true of the candidate.
2. Experience and Projects bullets: {bullet_rule}
3. Skills: in the "{skills_section}" section, reorder categories and reorder
   items within a category so the most relevant come first. You may uncomment a
   category line that is currently commented out with %, and you may comment out
   a category that is irrelevant, but only using lines already present in the
   file. Keep the "\\textbf{{Category}}{{: items}} \\\\" pattern and keep the trailing
   \\\\ on every visible line except the last visible one.
{headline_rule}
FORBIDDEN (any of these fails the job):
- Changing anything before \\begin{{document}}: preamble, packages, macros.
{header_rule}
- Changing any \\section name or order, or adding or removing sections.
- Touching a heading line. Every \\resumeSubheading / \\resumeProjectHeading line and
  the {{...}} argument lines under it are copied CHARACTER FOR CHARACTER: the job
  title, the employer, the project name, the location, the dates, and the
  \\emph{{...}} tech stack. That tech stack is a fact about what that project was
  actually built with -- do not add this posting's technologies to it, do not
  reorder it, do not drop anything from it. Work the posting's terms into the
  BULLETS instead, where they describe what was actually done.
- Changing anything in the education section.
- Inventing any number, percentage, duration, currency amount, or count that is
  not already in the resume (for example 90%, 3.3s to 380ms, 500+, 40%, 24/7).
- Adding a skill, tool, framework, language, certification, or achievement that
  does not appear somewhere in the original file, including in comments.
- Adding, removing, or renaming any \\begin{{...}}/\\end{{...}} environment.
- Using unescaped LaTeX special characters in prose: write \\%, \\&, \\#, \\_.
  Use -- for dashes, never a Unicode em dash or smart quotes.
- Any text in the output that is not valid LaTeX.

STYLE:
- Strong action verbs, specific and concrete, no buzzword filler.
- Prefer the job posting's exact terms when they truthfully describe the work
  (for example if the posting says "REST APIs" and the resume says "REST API",
  match the posting).
- The result must still fit on one page when compiled.

HARD LIMITS (checked by a program; violating any one rejects your output):
{hard_limits}
- Do NOT add new skill categories. Do NOT add a skill item unless that exact term
  already appears somewhere in the original file.
- Do NOT touch anything before \\begin{{document}}, the header block, or the
  education section: copy those lines character for character.

SELF-CHECK before you answer: confirm the word count is inside the budget, confirm
every number and date in your output also appears in the original, confirm no new
skills, and confirm the output starts with the original first line and ends with
\\end{{document}}, with no fences or notes."""


# The header is the first thing a screener reads and the first thing a title filter matches on,
# and this template gives it a name and contact links but no statement of what the candidate is.
# So it is the one part of the file that is worth opening up: a headline is a claim about which
# job you are applying for, not a claim about your history, and nothing in it can be checked and
# found false. Everything identifying stays frozen -- name, email, phone, every profile link.
HEADLINE_RULE = """4. Headline: the header block has the candidate's name and contact links. You may
   add ONE new line to it, directly under the name, naming the role this posting
   is for as the posting itself spells it, optionally followed by 3 to 5 of the
   core technologies it asks for that are genuinely in this resume. Match the
   file's existing style (for example "{\\scshape Job Title} $|$ Tech, Tech, Tech \\\\").
   If such a line is already there, rewrite it for this posting instead of adding
   a second one. Never change the name, the email, the phone number or any link,
   and never put a number, a date or a span of years in this line."""

HEADER_FROZEN_RULE = "- Changing the header block (the first \\begin{center}...\\end{center})."

HEADER_HEADLINE_RULE = """- Changing the name, the email, the phone number or any \\href link in the header
  block. The single headline line described above is the only thing you may add there."""


# Rule 2 has two modes. The default freezes the section: same bullets, same count,
# same work described -- safe, but too tight to actually argue for a role, because a
# posting's priorities rarely map one-to-one onto how the bullets were first written.
# REWRITE_BULLET_RULE lets the model restructure the experience instead. What does not
# move in either mode is the part an interviewer can check: employer, title, dates,
# degree, and every number.

SAFE_BULLET_RULE = """rephrase existing \\item bullets to surface
   the most relevant responsibilities and use the job's terminology. Each bullet
   must describe the same work as the original. You may reorder bullets within
   one role. Keep the same number of bullets per role and roughly the same length
   per bullet (within 15 percent). Move \\textbf{{}} emphasis to the keywords that
   matter for this job."""

REWRITE_BULLET_RULE = """REWRITE THEM. This is the main event, not a
   touch-up. For each role, work out what this posting is actually looking for and
   make the bullets say it, in the posting's own vocabulary. You may:
     - rewrite a bullet completely, in your own words, at whatever length serves it;
     - merge two bullets, or split one into two;
     - cut a bullet that says nothing for this job, to buy room for one that does;
     - add a bullet for work the candidate really did that the original buried or
       left out, including work evidenced by the skills and projects sections;
     - revive a bullet that is sitting commented out with % in this file: those are
       real, already-written bullets the candidate parked, and a commented-out one
       that matches this posting is better than a live one that does not. Write it
       as a normal live bullet; leave the original comment line where it is;
     - reorder bullets so the most relevant leads each role.

   Do this for EVERY role, not just the most recent one. An older role is often where
   the experience this posting wants actually sits, and leaving it as first written
   wastes the space it takes up.
   Lead with the outcome or the system built, never with "Responsible for". Name the
   technologies the posting names wherever they genuinely apply to that work, and put
   \\textbf{{}} on the terms this employer is scanning for.

   The line you do not cross: the WORK must be work this person actually did. You are
   re-presenting real history in this employer's language -- choosing what to feature,
   how to frame it, what to call it. You are not giving them a project, an employer, a
   responsibility, a metric or a result they never had. If the posting wants something
   their history simply does not contain, leave it out: it is something to study before
   the interview, not something to claim on the page."""


def _hard_limits(tex: str, max_growth: float = LENGTH_GROWTH, allow_rewrite: bool = False,
                 headline: bool = False) -> str:
    """Concrete numbers for the prompt: bullets per section and the word budget.

    With `allow_rewrite` the per-section bullet counts are not stated, because the model
    is allowed to change them. The word budget still applies -- the page is still a page.

    `headline` raises the ceiling by a headline's worth, because that line is added and
    replaces nothing: charging it to the ordinary budget makes the model fail the length
    check for obeying the instruction that told it to write the line.
    """
    clean = _strip_comments(tex)
    body = _split_preamble(clean)
    words = len((body[1] if body else clean).split())
    lo, hi = int(words * (1 - LENGTH_TOLERANCE)), int(words * (1 + max_growth)) + (HEADLINE_WORDS if headline else 0)
    budget = (
        f"- Word count of the document body (excluding comments) must stay between {lo} and {hi} "
        f"(original: {words}). The original already fills the page: if you uncomment a skills line, "
        "trim words elsewhere to pay for it."
    )
    lines = [budget]
    if allow_rewrite:
        lines.append(
            "- Bullet counts are yours to choose: merge, split, cut or add within a role so the "
            "page argues for THIS job. Every section must still end with at least one bullet."
        )
    else:
        for name, text in _sections(clean):
            if name:
                lines.append(f"- Section \"{name}\": exactly {len(_ITEM_RE.findall(text))} \\item-style entries, same as now.")
    return "\n".join(lines)


def tailor_latex(tex: str, job: dict, max_retries: int = MAX_RETRIES,
                 rewrite: bool = True, headline: bool = False) -> tuple[str, dict]:
    """Return (tailored_tex, report). Raises TailorError if no attempt passes verification.

    `rewrite=True` (the default) lets the model restructure the experience bullets for this
    posting instead of only rewording them in place. Employers, titles, dates, degrees and
    numbers are still fixed -- see `verify_latex_edit`.

    `headline` opens the header block for one added line naming the target role. Everything
    identifying in there -- name, email, phone, links -- stays frozen either way.
    """
    summary_section, skills_section = _detect_special_sections(tex)
    system = SYSTEM_PROMPT.format(
        summary_section=summary_section, skills_section=skills_section,
        hard_limits=_hard_limits(tex, REWRITE_GROWTH if rewrite else LENGTH_GROWTH,
                                 allow_rewrite=rewrite, headline=headline),
        bullet_rule=REWRITE_BULLET_RULE if rewrite else SAFE_BULLET_RULE,
        headline_rule=(HEADLINE_RULE + "\n") if headline else "",
        header_rule=HEADER_HEADLINE_RULE if headline else HEADER_FROZEN_RULE,
    )
    job_text = (
        f"TITLE: {job['title']}\nCOMPANY: {job['company']}\nURL: {job['final_url']}\n\n"
        f"DESCRIPTION:\n{job['full_description'][:MAX_DESC_CHARS]}"
    )
    client = get_client()
    report: dict = {"attempts": 0, "problems": [], "status": "pending"}
    fix_notes: list[str] = []

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1
        prompt = system
        if fix_notes:
            notes = "\n".join(f"- {n}" for n in fix_notes[:8])
            prompt += f"\n\nFIX THESE PROBLEMS FROM YOUR PREVIOUS ATTEMPT:\n{notes}"
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"ORIGINAL LATEX RESUME:\n{tex}\n\n---\n\nTARGET JOB:\n{job_text}\n\n"
                "Return the complete tailored .tex source:"
            )},
        ]
        raw = client.chat(messages, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.3)
        candidate = _splice_preamble(tex, _strip_fences(raw))
        problems = verify_latex_edit(
            tex, candidate, allow_rewrite=rewrite, allow_headline=headline,
            max_growth=REWRITE_GROWTH if rewrite else LENGTH_GROWTH,
        )
        report["problems"] = problems
        if not problems:
            report["status"] = "verified"
            return candidate, report
        log.warning("Attempt %d failed verification: %s", attempt + 1, "; ".join(problems))
        fix_notes = problems

    report["status"] = "failed_verification"
    raise TailorError("Tailored resume failed verification after retries:\n  - " + "\n  - ".join(report["problems"]))


def shorten_latex(
    tex: str,
    tailored: str,
    job: dict,
    allow_skills: set[str] | None = None,
    max_retries: int = MAX_RETRIES,
    rewrite: bool = True,
    headline: bool = False,
) -> tuple[str, dict]:
    """Trim wording so the PDF fits on one page. Returns (tex, report); never raises.

    `allow_skills` are terms the ATS gap pass already added to `tailored`; they are
    not in the original, so the verifier has to be told they may stay. They are also
    the whole point of that pass, and a shortening model treats a skills-line tail as
    the cheapest thing in the file to cut -- so they are named in the prompt as
    must-keep tokens and a candidate that drops one is sent back for another attempt.
    If no attempt verifies, `tailored` comes back untouched with status
    "failed_verification" -- a resume that runs long is still a resume, so this never
    sinks the run. If attempts verify but all of them lose a keyword, the one that
    lost the fewest comes back with status "verified_lossy" and the lost terms in
    `report["dropped"]`: one page is worth more than the last term, but the caller
    has to re-score rather than trust the number the gap pass reported.

    `rewrite` and `headline` must match the mode that produced `tailored`. Verification here
    compares against the ORIGINAL, so a tailored resume whose bullets were restructured -- or
    whose header gained a headline -- fails those checks unless the verifier is told they were
    allowed, which would make every shorten attempt fail and leave the long version in place.
    """
    summary_section, skills_section = _detect_special_sections(tex)
    system = SYSTEM_PROMPT.format(
        summary_section=summary_section, skills_section=skills_section,
        hard_limits=_hard_limits(tex, allow_rewrite=rewrite),
        bullet_rule=SAFE_BULLET_RULE,
        headline_rule="",  # the trim shortens what is there; it never writes a new headline
        header_rule=HEADER_HEADLINE_RULE if headline else HEADER_FROZEN_RULE,
    )
    system += (
        "\n\nThe previous version compiled to more than one page. Tighten the wording of the "
        "summary and bullets (fewer words, same facts, same bullet count) so it fits on one page.\n"
        "Shorten by cutting filler words, not facts. Every one of these tokens must still appear "
        "in your output, spelled exactly as it is here:\n" + _must_keep(tailored, allow_skills)
    )
    client = get_client()
    user = (
        f"ORIGINAL LATEX RESUME:\n{tex}\n\n---\n\nTOO-LONG TAILORED VERSION:\n{tailored}\n\n---\n\n"
        f"TARGET JOB: {job['title']} at {job['company']}\n\nReturn the shortened complete .tex source:"
    )

    problems: list[str] = []
    best: tuple[str, list[str]] | None = None  # the verified candidate that lost the fewest terms
    for attempt in range(max_retries + 1):
        prompt = system
        if problems:
            prompt += "\n\nFIX THESE PROBLEMS FROM YOUR PREVIOUS ATTEMPT:\n" + "\n".join(
                f"- {p}" for p in problems[:8]
            )
        candidate = _splice_preamble(
            tex, _strip_fences(client.chat(
                [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
                max_tokens=MAX_OUTPUT_TOKENS, temperature=0.2,
            )),
        )
        problems = verify_latex_edit(
            tex, candidate, check_length=False, allow_skills=allow_skills,
            allow_new_categories=bool(allow_skills), allow_rewrite=rewrite,
            allow_headline=headline,
        )
        if not problems:
            dropped = _dropped_terms(tailored, candidate, allow_skills)
            if not dropped:
                return candidate, {"attempts": attempt + 1, "problems": [], "dropped": [],
                                   "status": "verified"}
            if best is None or len(dropped) < len(best[1]):
                best = (candidate, dropped)
            problems = ["you cut these keyword terms, which have to survive the trim: "
                        + ", ".join(dropped)
                        + ". Put them back and buy the space by cutting filler words, or by "
                          "dropping a skills item this posting does not ask for."]
        log.warning("Shorten attempt %d rejected: %s", attempt + 1, "; ".join(problems))

    # Every attempt cost a keyword. One page is still worth more than the last term or two,
    # so the least lossy one goes out -- but it goes out labelled, because the score the gap
    # pass reported was measured on the text this one just replaced.
    if best is not None:
        log.warning("Shortened to one page but lost %d keyword term(s): %s",
                    len(best[1]), ", ".join(best[1]))
        return best[0], {"attempts": max_retries + 1, "problems": [], "dropped": best[1],
                         "status": "verified_lossy"}
    return tailored, {"attempts": max_retries + 1, "problems": problems, "dropped": [],
                      "status": "failed_verification"}


_TYPESETTING_NUMBER_RE = re.compile(r"(?:in|ex|pt|em|cm|mm)\b")


def _fact_numbers(text: str) -> list[str]:
    """The numbers in `text` that are claims -- 90%, 3.3s, 500+, 24/7 -- and not typesetting.

    A LaTeX length is not a metric: `\\\\[0.5ex]`, `\\vspace{-4pt}` and `0.15in` carry a digit
    but claim nothing, and treating them as facts makes an honest edit look like an invented
    number. Position matters, so this checks the unit that follows each match rather than
    asking whether the token appears as a length anywhere in the file.
    """
    return [m.group(0).strip() for m in _NUMBER_RE.finditer(text)
            if not _TYPESETTING_NUMBER_RE.match(text[m.end():])]


def plain_text(tex: str) -> str:
    """Flatten a .tex to the running text an ATS reads out of the PDF. Case is preserved."""
    text = _strip_comments(tex)
    text = _split_preamble(text)[1] if _split_preamble(text) else text
    text = re.sub(r"\\href\{[^}]*\}", " ", text)                          # keep the link label, drop the URL
    text = re.sub(r"\\(?:begin|end)\{[^}]*\}(?:\[[^\]]*\])?", " ", text)  # environment names are not skills
    text = re.sub(r"\\[A-Za-z@]+\*?", " ", text)                          # command names
    text = re.sub(r"[{}$&~^\\]", " ", text)
    return re.sub(r"\s+", " ", text)


def term_pattern(term: str) -> re.Pattern[str]:
    """Match one keyword the way an ATS would: word-bounded, plural- and separator-tolerant."""
    parts = [re.escape(p) for p in term.lower().split() if p]
    if not parts:
        return re.compile(r"(?!)")
    core = r"[\s\-/]+".join(parts)
    return re.compile(rf"(?<![A-Za-z0-9+#]){core}(?:s|es)?(?![A-Za-z0-9+#])", re.IGNORECASE)


def _dropped_terms(before: str, after: str, terms: set[str] | None) -> list[str]:
    """Keyword terms `before` carried and `after` lost, in the spelling `before` used.

    Only terms actually present in `before` are owed: `terms` carries every alias of every
    added keyword, and a resume that says "CI/CD" never owed the phrase it was aliased from.
    """
    if not terms:
        return []
    before_text, after_text = plain_text(before), plain_text(after)
    dropped = []
    for term in sorted(terms):
        found = term_pattern(term).search(before_text)
        if found and not term_pattern(term).search(after_text):
            dropped.append(found.group(0))
    return dropped


def _must_keep(tex: str, keep_terms: set[str] | None = None) -> str:
    """The tokens the shortening pass keeps losing, listed for the prompt.

    Metrics and dates, plus `keep_terms`: the keywords the ATS gap pass put in, quoted in
    the spelling this file uses so "spelled exactly as it is here" is true of them too.
    """
    body = _strip_comments(_split_preamble(tex)[1] if _split_preamble(tex) else tex)
    body = re.sub(r"\\begin\{center\}.*?\\end\{center\}", " ", body, flags=re.DOTALL)  # header: phone, profile ids
    tokens: list[str] = []
    for tok in _fact_numbers(body) + [d.strip() for d in _DATE_RE.findall(body)]:
        if not tok or tok in tokens:
            continue
        if re.fullmatch(r"\d{7,}", tok):  # phone numbers and profile ids
            continue
        tokens.append(tok)
    text = plain_text(tex)
    for term in sorted(keep_terms or ()):
        found = term_pattern(term).search(text)
        if found and found.group(0) not in tokens:
            tokens.append(found.group(0))
    return "  " + ", ".join(tokens)


def _splice_preamble(original: str, candidate: str) -> str:
    """Replace the candidate's preamble with the original's, verbatim.

    Models tend to drop comment headers or reflow the preamble; nothing there
    should ever change, so we don't rely on the model to reproduce it.
    """
    o_split, c_split = _split_preamble(original), _split_preamble(candidate)
    if not o_split or not c_split:
        return candidate
    return o_split[0] + c_split[1]


def external_inputs(tex: str, resume_dir: Path) -> list[str]:
    """`\\input`/`\\include` targets that are real files next to the resume (multi-file resumes)."""
    found = []
    for name in re.findall(r"\\(?:input|include)\{([^}]*)\}", _strip_comments(tex)):
        stem = name.strip()
        if (resume_dir / stem).exists() or (resume_dir / f"{stem}.tex").exists():
            found.append(stem)
    return found


def _strip_fences(raw: str) -> str:
    """Pull the LaTeX document out of the reply, tolerating fences and commentary around it."""
    text = raw.strip()
    # Prefer a fenced block that contains the document; a lone fenced block is taken as-is
    blocks = re.findall(r"```[a-zA-Z]*[ \t]*\n(.*?)\n```", text, re.DOTALL)
    for block in blocks:
        if "\\begin{document}" in block:
            text = block
            break
    else:
        if len(blocks) == 1 and text.startswith("```") and text.endswith("```"):
            return blocks[0].strip() + "\n"
        # No usable fence: slice from \documentclass to \end{document} if the model added notes around it
        start, end = text.find("\\documentclass"), text.rfind("\\end{document}")
        if start > 0 and end > start:
            text = text[start:end + len("\\end{document}")]
    return text.strip() + "\n"


# ── 3. Deterministic verification ─────────────────────────────────────────

_SECTION_RE = re.compile(r"\\section\*?\{([^}]*)\}")
_ENV_RE = re.compile(r"\\(begin|end)\{([A-Za-z*]+)\}")
_ITEM_RE = re.compile(r"\\[A-Za-z]*[Ii]tem\b")  # \item, \resumeItem, \cvitem ... (not \resumeItemListStart)
_ITALIC_RE = re.compile(r"\\(?:textit|emph|it)\{([^{}]*)\}")
_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE_RE = re.compile(rf"\b{_MONTHS}\s+\d{{4}}\b|\b(?:19|20)\d{{2}}\b|\bPresent\b|\bCurrent\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<![A-Za-z\\])\d[\d,.]*(?:\s?(?:\\%|%|\+|x|k|K|M|ms|s|GB|TB))?")
_BAD_UNICODE = "\u2014\u2013\u2018\u2019\u201c\u201d"


def _strip_comments(text: str) -> str:
    """Drop LaTeX comments (a % not preceded by a backslash, to end of line)."""
    return "\n".join(re.split(r"(?<!\\)%", line, maxsplit=1)[0] for line in text.splitlines())


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _split_preamble(tex: str) -> tuple[str, str] | None:
    idx = tex.find("\\begin{document}")
    if idx < 0:
        return None
    return tex[:idx], tex[idx:]


def _sections(body: str) -> list[tuple[str, str]]:
    """Return [(section_name, section_text)] for the body; text before the first section is named ''."""
    parts = _SECTION_RE.split(body)
    out = [("", parts[0])]
    for i in range(1, len(parts), 2):
        out.append((parts[i].strip(), parts[i + 1]))
    return out


def _detect_special_sections(tex: str) -> tuple[str, str]:
    names = [n for n, _ in _sections(tex) if n]
    first = names[0] if names else "Summary"
    summary = next((n for n in names if re.search(r"summary|objective|profile|about", n, re.IGNORECASE)), first)
    skills = next((n for n in names if re.search(r"skill|technolog|competenc", n, re.IGNORECASE)), "Skills")
    return summary, skills


def _skill_items(text: str) -> set[str]:
    """Tokenise a skills section (comments included) into lowercase items."""
    text = re.sub(r"\\(?:textbf|textit|small|item|hfill)\b", " ", text)
    text = re.sub(r"[{}\\%]", " ", text)
    items = set()
    for raw in re.split(r"[,:;\n]|\\\\", text):
        token = raw.strip(" .()")
        if token and not token.isdigit():
            items.add(token.lower())
    return items


# Things that are never "just a keyword": claiming one is a checkable lie, so no pass may add them.
_CREDENTIAL_RE = re.compile(
    r"\b(?:certified|certificat\w*|licen[cs]ed?|accredited|bachelor\w*|master'?s|ph\.?\s?d|"
    r"doctorate|degree|diploma|b\.?tech|m\.?tech|b\.?e\.?|m\.?s\.?c|mba)\b",
    re.IGNORECASE,
)


_FILLER_WORDS = {"and", "or", "with", "for", "the", "in", "of", "to", "on", "a", "an", "using", "via"}


def _covered_by(item: str, terms: set[str], extra_words: set[str] = frozenset()) -> bool:
    """True if this skills item is a rephrasing of the terms the gap pass was allowed to add.

    The pass is told to add "enterprise analytics" and "AI agents"; models write
    "Analytics and AI" or "Business Intelligence (Bitcoin)". Rejecting those for not
    matching the term list character for character is what made the pass a no-op, so
    an item passes if it contains an allowed term OR is built entirely out of words
    from allowed terms and the skills the resume already lists.

    Credentials are excluded whatever the wording: that is the one phrasing that turns
    a keyword into a claim someone can check.
    """
    if not terms or _CREDENTIAL_RE.search(item):
        return False
    if any(re.search(rf"(?<![A-Za-z0-9+#]){re.escape(term)}(?![A-Za-z0-9+#])", item) for term in terms):
        return True
    allowed_words = {w for term in terms for w in _words(term)} | extra_words
    item_words = _words(item)
    return bool(item_words) and item_words <= allowed_words


def _words(text: str) -> set[str]:
    """Meaningful lowercase words of a skills item ('AI agents' -> {ai, agents})."""
    return {w for w in re.split(r"[^A-Za-z0-9+#]+", text.lower()) if w and w not in _FILLER_WORDS}


# A headline is allowed to carry the file's own line-break styling (`\\[0.5ex]`, `{\scshape ...}`):
# that is typesetting copied from the name line, not a claim. Strip it before looking for numbers.
_HEADLINE_STYLE_RE = re.compile(r"\\\\\[[^\]]*\]|\\[A-Za-z@]+\*?|[{}]")


def _headline_problems(o_head: str, t_head: str) -> list[str]:
    """Check a changed header block against the one edit a headline is allowed to be.

    The rule is subtraction, not pattern matching: every line of the original header has to
    still be there, in order, and what is left over may be at most one new line. That keeps
    the name, the phone number and every profile link exactly as they were whatever the model
    does with the layout, without this having to know which line is which.
    """
    def lines(text: str) -> list[str]:
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    o_lines, t_lines = lines(o_head), lines(t_head)
    problems: list[str] = []
    added: list[str] = []
    it = iter(t_lines)
    for want in o_lines:
        for got in it:
            if got == want:
                break
            added.append(got)
        else:
            return [f"Header line {want!r} is missing or was altered; the name, phone and links "
                    "are copied verbatim -- only a headline line may be added."]
    added.extend(it)

    if len(added) > 1:
        problems.append(f"Only one headline line may be added to the header, got {len(added)}: {added}.")
    for line in added:
        claimed = _fact_numbers(_HEADLINE_STYLE_RE.sub(" ", line))
        if claimed:
            problems.append(f"Headline line {line!r} states {claimed}; a headline names the role, "
                            "never years, dates or metrics. LaTeX spacing like \\\\[0.5ex] is fine.")
        if _CREDENTIAL_RE.search(line):
            problems.append(f"Headline line {line!r} claims a degree or certification; not allowed.")
    return problems


def verify_latex_edit(
    original: str,
    tailored: str,
    check_length: bool = True,
    allow_skills: set[str] | None = None,
    allow_new_categories: bool = False,
    max_growth: float = LENGTH_GROWTH,
    allow_rewrite: bool = False,
    allow_headline: bool = False,
) -> list[str]:
    """Return a list of human-readable problems; empty list means the edit is acceptable.

    `allow_skills` / `allow_new_categories` / `max_growth` are the knobs the ATS gap pass
    turns: that pass is allowed to add specific missing terms (see `scoring.ats`), while
    every other rule -- facts, dates, metrics, bullet counts -- still holds.

    `allow_headline` permits exactly one added line in the header block -- the target role --
    while every original header line, and so the name, phone and links, still has to be there
    unchanged. See `_headline_problems`.

    `allow_rewrite` lets the model restructure the experience itself: merge, split, drop
    and reorder bullets so the page says what this posting asks for. The honesty rule
    changes shape rather than relaxing -- instead of "every original number must survive"
    it becomes "no number, date or employer that was not already there". You may cut your
    own history; you may not acquire history you do not have. Titles, date ranges and the
    education section stay verbatim in both modes.
    """
    problems: list[str] = []

    if "```" in tailored:
        problems.append("Output contained markdown fences; return raw .tex only.")
    bad = sorted({c for c in tailored if c in _BAD_UNICODE})
    if bad:
        problems.append(f"Output contains Unicode dashes/quotes {bad}; use -- and plain quotes.")

    o_split, t_split = _split_preamble(original), _split_preamble(tailored)
    if not o_split:
        return ["Original resume has no \\begin{document}; only single-file LaTeX resumes are supported."]
    if not t_split:
        return problems + ["Output has no \\begin{document}."]
    o_pre, o_body = o_split
    t_pre, t_body = t_split

    if _norm(o_pre) != _norm(t_pre):
        problems.append("Preamble (everything before \\begin{document}) was modified; copy it verbatim.")
    if "\\end{document}" not in t_body:
        problems.append("Output is missing \\end{document}.")
    if tailored.count("{") != tailored.count("}"):
        problems.append("Unbalanced braces in output.")
    if re.search(r"\\(usepackage|newcommand|renewcommand|def)\b", _strip_comments(t_body)):
        problems.append("New package/macro definitions inside the document body are not allowed.")

    # A degree or certification the original does not have is a checkable lie, not a keyword.
    o_creds = Counter(m.lower() for m in _CREDENTIAL_RE.findall(_strip_comments(o_body)))
    t_creds = Counter(m.lower() for m in _CREDENTIAL_RE.findall(_strip_comments(t_body)))
    invented = sorted(word for word, n in t_creds.items() if n > o_creds[word])
    if invented:
        problems.append(f"New certification/degree wording {invented} is never allowed; remove it.")

    # Header block must be identical, except for the one headline line when that is allowed
    o_head = re.search(r"\\begin\{center\}.*?\\end\{center\}", o_body, re.DOTALL)
    t_head = re.search(r"\\begin\{center\}.*?\\end\{center\}", t_body, re.DOTALL)
    if o_head and not t_head:
        problems.append("Header block (\\begin{center}...\\end{center}) is missing.")
    elif o_head and _norm(o_head.group(0)) != _norm(t_head.group(0)):
        if allow_headline:
            problems += _headline_problems(o_head.group(0), t_head.group(0))
        else:
            problems.append("Header block (\\begin{center}...\\end{center}) was modified; copy it verbatim.")

    # Sections and environments (ignoring comments)
    o_clean, t_clean = _strip_comments(o_body), _strip_comments(t_body)
    o_secs, t_secs = _SECTION_RE.findall(o_clean), _SECTION_RE.findall(t_clean)
    if o_secs != t_secs:
        problems.append(f"Section list changed: expected {o_secs}, got {t_secs}.")
    if _ENV_RE.findall(o_clean) != _ENV_RE.findall(t_clean):
        problems.append("The sequence of \\begin/\\end environments changed.")

    # Per-section checks
    summary_name, skills_name = _detect_special_sections(original)
    o_by_name = dict(_sections(o_clean))
    t_by_name = dict(_sections(t_clean))
    for name, o_text in o_by_name.items():
        t_text = t_by_name.get(name)
        if t_text is None:
            continue
        label = name or "text before the first section"

        if name == skills_name:
            allowed = _skill_items(o_text + "\n" + _section_raw(original, name)) | (allow_skills or set())
            whole_file = re.sub(r"\s+", " ", original.lower())
            new_items = sorted(
                i for i in _skill_items(t_text)
                if i not in allowed
                and re.sub(r"\s+", " ", i) not in whole_file
                and not _covered_by(i, allow_skills or set(), _words(_section_raw(original, name)))
            )
            if new_items:
                problems.append(f"Skills added that are not in the original file: {new_items[:8]}.")
            # Categories (the \textbf{...} labels) must be ones the file already has
            o_cats = set(re.findall(r"\\textbf\{([^}]*)\}", _section_raw(original, name)))
            new_cats = sorted(set(re.findall(r"\\textbf\{([^}]*)\}", t_text)) - o_cats)
            if new_cats and not allow_new_categories:
                problems.append(f"New skill categories are not allowed: {new_cats}.")
            elif len(new_cats) > 1:
                problems.append(f"At most one new skill category is allowed, got {new_cats}.")
            continue

        o_items, t_items = len(_ITEM_RE.findall(o_text)), len(_ITEM_RE.findall(t_text))
        if not allow_rewrite and o_items != t_items:
            problems.append(f"Bullet count changed in '{label}': {o_items} -> {t_items}.")
        elif allow_rewrite and o_items and not t_items:
            problems.append(f"'{label}' lost all of its bullets; a section cannot be emptied.")

        if name == summary_name:
            continue  # wording is free; facts checked globally below

        # Education is facts only: nothing may change
        if re.search(r"education|academic", name, re.IGNORECASE):
            if _norm(o_text) != _norm(t_text):
                problems.append(f"'{label}' section changed; copy it verbatim.")
            continue

        # Role/project header lines (anything using \hfill: title, company, dates) must be verbatim and in order
        if _protected_lines(o_text) != _protected_lines(t_text):
            problems.append(
                f"A job title / employer / date line changed in '{label}'; copy those lines verbatim."
            )

        o_italics, t_italics = Counter(_ITALIC_RE.findall(o_text)), Counter(_ITALIC_RE.findall(t_text))
        o_dates = Counter(m.lower() for m in _DATE_RE.findall(o_text))
        t_dates = Counter(m.lower() for m in _DATE_RE.findall(t_text))
        o_nums, t_nums = Counter(_fact_numbers(o_text)), Counter(_fact_numbers(t_text))

        if allow_rewrite:
            # Rewriting means a bullet can be cut or merged, so a fact may legitimately
            # disappear. What must never happen is one appearing: a date, a metric or an
            # employer the candidate cannot back up in the interview.
            for arg in sorted(a for a, n in t_italics.items() if n > o_italics[a]):
                problems.append(f"Italic text '{arg}' in '{label}' is not in the original; invent nothing.")
            for tok in sorted(t for t, n in t_dates.items() if n > o_dates[t]):
                problems.append(f"Date '{tok}' in '{label}' is not in the original; dates are facts.")
            for tok in sorted(t for t, n in t_nums.items() if n > o_nums[t]):
                problems.append(f"Metric '{tok}' in '{label}' is not in the original; never invent a number.")
        else:
            # Italic arguments (locations, tech stacks, employer names) must survive
            for arg, cnt in o_italics.items():
                if t_italics[arg] < cnt:
                    problems.append(f"Italic text '{arg}' missing or altered in '{label}'; keep it exactly.")

            # Every date and metric must survive
            for tok, cnt in o_dates.items():
                if t_dates[tok] < cnt:
                    problems.append(f"Date '{tok}' missing or altered in '{label}'.")
            for tok, cnt in o_nums.items():
                if t_nums[tok] < cnt:
                    problems.append(f"Metric '{tok}' missing or altered in '{label}'; keep every number exactly.")

    if check_length:
        o_words, t_words = len(o_clean.split()), len(t_clean.split())
        lo = int(o_words * (1 - LENGTH_TOLERANCE))
        hi = int(o_words * (1 + max_growth)) + (HEADLINE_WORDS if allow_headline else 0)
        if o_words and not lo <= t_words <= hi:
            problems.append(
                f"Word count {t_words} is outside the allowed {lo}-{hi} (original {o_words}); "
                "the PDF must stay one page, so never make it longer than the original."
            )

    return problems


def _section_raw(tex: str, name: str) -> str:
    """Raw (comments included) text of one section of the original file."""
    for n, text in _sections(tex):
        if n == name:
            return text
    return ""


_HEADING_RE = re.compile(
    r"\\(?:hfill|resume(?:Sub)?(?:Sub)?[Hh]eading|resumeProjectHeading|resumeEducationHeading|cventry)\b"
)


def _protected_lines(section_text: str) -> list[str]:
    """Lines carrying employer / title / date facts, which must survive an edit verbatim.

    Two conventions cover the common Overleaf resume templates: aligning the fields with
    \\hfill, and the \\resumeSubheading{title}{dates}{company}{location} family. Matching
    only \\hfill silently left the second kind unprotected -- the employer name was then
    guarded by nothing but the prompt, which is not a guarantee.
    """
    lines = section_text.splitlines()
    protected: list[str] = []
    i = 0
    while i < len(lines):
        if not _HEADING_RE.search(lines[i]):
            i += 1
            continue
        # \hfill templates put the whole row on one line; \resumeSubheading puts its
        # {title}{dates}{company}{location} arguments on the lines below the macro, so
        # keep taking following lines while they are still argument groups.
        block = [lines[i].strip()]
        i += 1
        while i < len(lines) and lines[i].lstrip().startswith("{"):
            block.append(lines[i].strip())
            i += 1
        protected.append(" ".join(block))
    return protected


# ── 4. Output and PDF ─────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text[:max_len].rstrip("_") or "unknown"


MATCHED_SUFFIX = "_ALREADY_MATCHED"   # marks a folder whose resume needed no rewrite


def job_folder_name(job: dict, suffix: str = "") -> str:
    """`<company>_<role>` for one job, plus an optional marker like MATCHED_SUFFIX."""
    return f"{slugify(job['company'])}_{slugify(job['title'])}{suffix}"


def output_dir_for(job: dict, suffix: str = "") -> Path:
    """<output>/<DD-MM-YYYY>/<company>_<role>/ -- one dated folder per day of applying.

    Everything tailored today lands under today's folder, which is created on first use
    and reused for the rest of the day, so the output root stays one directory per day
    instead of an ever-growing flat list of every job ever tailored.
    """
    return OUTPUT_DIR / datetime.now().strftime("%d-%m-%Y") / job_folder_name(job, suffix)


# The candidate's name in the header block: `{\Huge \scshape Jane Doe}`, `\textbf{\LARGE Jane Doe}`,
# `\name{Jane}{Doe}` or `\author{Jane Doe}` — the four shapes Overleaf resume templates use.
_NAME_SIZE_RE = re.compile(
    r"\\(?:Huge|huge|LARGE|Large)\s*(?:\\(?:scshape|bfseries|sc|sffamily|rmfamily)\s*)*([^\\{}\n]+)"
)
_NAME_CMD_RE = re.compile(r"\\(?:name|author)\s*\{([^\\{}\n]*)\}(?:\s*\{([^\\{}\n]*)\})?")


def candidate_name(tex: str) -> str | None:
    """Pull the person's name out of the resume header, or None if it isn't recognisable."""
    body = tex.split(r"\begin{document}", 1)[-1][:2000]
    # Header block first; \name/\author usually sit in the preamble, so scan the whole file for those.
    for regex, text in ((_NAME_SIZE_RE, body), (_NAME_CMD_RE, body), (_NAME_CMD_RE, tex)):
        for m in regex.finditer(text):
            name = " ".join(g.strip() for g in m.groups() if g and g.strip())
            name = re.sub(r"\s+", " ", name.replace("~", " ")).strip(" ,.")
            if name and re.fullmatch(r"[A-Za-z][A-Za-z.'\- ]*", name):
                return name
    return None


def resume_basename(name: str | None) -> str:
    """File stem for the tailored resume: `Keerthivasan_Natarajan`, or `resume` if no name is found."""
    if not name:
        return "resume"
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    stem = "_".join(re.sub(r"[^A-Za-z0-9 ]+", " ", ascii_name).split())
    return stem or "resume"


def write_outputs(out_dir: Path, original: str, tailored: str, job: dict) -> Path:
    """Write <Name>.tex plus job.txt and changes.diff for review. Returns the .tex path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Prefer the tailored copy's header; fall back to the original if the LLM mangled it.
    stem = resume_basename(candidate_name(tailored) or candidate_name(original))
    tex_path = out_dir / f"{stem}.tex"
    tex_path.write_text(tailored, encoding="utf-8")
    (out_dir / "job.txt").write_text(
        f"TITLE: {job['title']}\nCOMPANY: {job['company']}\nURL: {job['url']}\n"
        f"FINAL URL: {job['final_url']}\nAPPLY URL: {job.get('application_url') or ''}\n\n"
        f"{job['full_description']}\n",
        encoding="utf-8",
    )
    diff = difflib.unified_diff(
        original.splitlines(), tailored.splitlines(),
        fromfile=f"original/{stem}.tex", tofile=f"tailored/{stem}.tex", lineterm="",
    )
    (out_dir / "changes.diff").write_text("\n".join(diff) + "\n", encoding="utf-8")
    return tex_path


def find_latex_compiler() -> tuple[str, str] | None:
    """Return (name, path) of the compiler to use.

    Order: LATEX_COMPILER env var (tectonic or pdflatex) if set, then tectonic, then pdflatex.
    Overleaf's default engine is pdflatex; set LATEX_COMPILER=pdflatex to match it exactly.
    """
    preferred = os.environ.get("LATEX_COMPILER", "").strip().lower()
    order = [preferred] if preferred in ("tectonic", "pdflatex") else []
    order += [n for n in ("tectonic", "pdflatex") if n not in order]
    for name in order:
        path = shutil.which(name)
        if path:
            return name, path
    return None


def compile_pdf(tex_path: Path) -> Path:
    """Compile the tailored .tex next to itself. Raises TailorError with the log tail on failure."""
    compiler = find_latex_compiler()
    if not compiler:
        raise TailorError(
            "No LaTeX compiler found. Install one and re-run:\n"
            "  tectonic (recommended): download tectonic.exe from\n"
            "    https://github.com/tectonic-typesetting/tectonic/releases and drop it in .venv/Scripts/\n"
            "  or pdflatex:  winget install MiKTeX.MiKTeX"
        )
    name, exe = compiler
    src = tex_path
    build_dir = None
    if name == "tectonic":
        # tectonic runs XeTeX, which lacks pdfTeX's ToUnicode helpers (\pdfgentounicode etc.,
        # common in Overleaf resume templates). XeTeX maps text natively, so compile a copy
        # with those lines commented out; the saved .tex is left exactly as written.
        text = tex_path.read_text(encoding="utf-8")
        patched = _PDFTEX_ONLY_RE.sub(lambda m: "% [xetex] " + m.group(0), text)
        if patched != text:
            build_dir = tex_path.parent / ".build"
            build_dir.mkdir(exist_ok=True)
            src = build_dir / tex_path.name
            src.write_text(patched, encoding="utf-8")
        runs = [[exe, src.name]]
    else:
        runs = [[exe, "-interaction=nonstopmode", "-halt-on-error", src.name]] * 2

    for cmd in runs:
        proc = subprocess.run(
            cmd, cwd=src.parent, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if proc.returncode != 0:
            tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
            raise TailorError(f"{name} failed (exit {proc.returncode}). Last lines:\n{tail}")

    built = src.with_suffix(".pdf")
    if not built.exists():
        raise TailorError(f"{name} finished but {built} was not produced.")
    pdf_path = tex_path.with_suffix(".pdf")
    if build_dir:
        shutil.move(str(built), str(pdf_path))
        shutil.rmtree(build_dir, ignore_errors=True)
    for junk in ("aux", "out", "log"):
        tex_path.with_suffix(f".{junk}").unlink(missing_ok=True)
    return pdf_path


# pdfTeX primitives that XeTeX does not define (all optional metadata/encoding helpers)
_PDFTEX_ONLY_RE = re.compile(
    r"^[ \t]*\\(?:input\{glyphtounicode\}|pdfgentounicode\s*=\s*\d+|pdfglyphtounicode\b.*|"
    r"pdfminorversion\s*=\s*\d+|pdfcompresslevel\s*=\s*\d+|pdfobjcompresslevel\s*=\s*\d+)[^\n]*",
    re.MULTILINE,
)


def pdf_page_count(pdf_path: Path) -> int:
    """Page count via pypdf (compressed object streams defeat a plain byte scan)."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(pdf_path)).pages)
    except Exception as e:  # noqa: BLE001 - fall back to a byte scan rather than fail the run
        log.warning("pypdf page count failed (%s); falling back to byte scan", e)
        data = pdf_path.read_bytes()
        return len(re.findall(rb"/Type\s*/Page(?![s/A-Za-z])", data)) or 1
