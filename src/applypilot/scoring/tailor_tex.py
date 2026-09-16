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
from pathlib import Path
from urllib.parse import urlparse

from applypilot.config import OUTPUT_DIR
from applypilot.llm import get_client

log = logging.getLogger(__name__)

MAX_DESC_CHARS = 6000       # same cap the batch tailor uses
MAX_RETRIES = 2             # LLM attempts after the first
LENGTH_TOLERANCE = 0.15     # output may be up to this much SHORTER than the original
LENGTH_GROWTH = 0.03        # ...but at most this much LONGER (a full one-page resume has no slack)
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
        data = extract_json(get_client().ask(prompt, max_tokens=2048))
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
2. Experience and Projects bullets: rephrase existing \\item bullets to surface
   the most relevant responsibilities and use the job's terminology. Each bullet
   must describe the same work as the original. You may reorder bullets within
   one role. Keep the same number of bullets per role and roughly the same length
   per bullet (within 15 percent). Move \\textbf{{}} emphasis to the keywords that
   matter for this job.
3. Skills: in the "{skills_section}" section, reorder categories and reorder
   items within a category so the most relevant come first. You may uncomment a
   category line that is currently commented out with %, and you may comment out
   a category that is irrelevant, but only using lines already present in the
   file. Keep the "\\textbf{{Category}}{{: items}} \\\\" pattern and keep the trailing
   \\\\ on every visible line except the last visible one.

FORBIDDEN (any of these fails the job):
- Changing anything before \\begin{{document}}: preamble, packages, macros.
- Changing the header block (the first \\begin{{center}}...\\end{{center}}).
- Changing any \\section name or order, or adding or removing sections.
- Changing any company name, job title, project name, location, date range, or
  anything in the education section.
- Changing any number, percentage, duration, currency amount, or count
  (for example 90%, 3.3s to 380ms, 500+, 100+, 40%, 24/7).
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
- Do NOT add, remove, split or merge bullets anywhere.
- Do NOT add new skill categories. Do NOT add a skill item unless that exact term
  already appears somewhere in the original file.
- Do NOT touch anything before \\begin{{document}}, the header block, or the
  education section: copy those lines character for character.

SELF-CHECK before you answer: count the bullets per section, confirm the word
count is inside the budget, confirm no new skills, confirm the output starts with
the original first line and ends with \\end{{document}}, with no fences or notes."""


def _hard_limits(tex: str) -> str:
    """Concrete numbers for the prompt: bullets per section and the word budget."""
    clean = _strip_comments(tex)
    body = _split_preamble(clean)
    words = len((body[1] if body else clean).split())
    lo, hi = int(words * (1 - LENGTH_TOLERANCE)), int(words * (1 + LENGTH_GROWTH))
    budget = (
        f"- Word count of the document body (excluding comments) must stay between {lo} and {hi} "
        f"(original: {words}). The original already fills the page: if you uncomment a skills line, "
        "trim words elsewhere to pay for it."
    )
    lines = [budget]
    for name, text in _sections(clean):
        if name:
            lines.append(f"- Section \"{name}\": exactly {len(_ITEM_RE.findall(text))} \\item-style entries, same as now.")
    return "\n".join(lines)


def tailor_latex(tex: str, job: dict, max_retries: int = MAX_RETRIES) -> tuple[str, dict]:
    """Return (tailored_tex, report). Raises TailorError if no attempt passes verification."""
    summary_section, skills_section = _detect_special_sections(tex)
    system = SYSTEM_PROMPT.format(
        summary_section=summary_section, skills_section=skills_section, hard_limits=_hard_limits(tex),
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
        problems = verify_latex_edit(tex, candidate)
        report["problems"] = problems
        if not problems:
            report["status"] = "verified"
            return candidate, report
        log.warning("Attempt %d failed verification: %s", attempt + 1, "; ".join(problems))
        fix_notes = problems

    report["status"] = "failed_verification"
    raise TailorError("Tailored resume failed verification after retries:\n  - " + "\n  - ".join(report["problems"]))


def shorten_latex(tex: str, tailored: str, job: dict) -> tuple[str, dict]:
    """One extra pass asking the LLM to trim wording so the PDF fits on one page."""
    summary_section, skills_section = _detect_special_sections(tex)
    system = SYSTEM_PROMPT.format(
        summary_section=summary_section, skills_section=skills_section, hard_limits=_hard_limits(tex),
    )
    system += (
        "\n\nThe previous version compiled to more than one page. Tighten the wording of the "
        "summary and bullets (fewer words, same facts, same bullet count) so it fits on one page."
    )
    client = get_client()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            f"ORIGINAL LATEX RESUME:\n{tex}\n\n---\n\nTOO-LONG TAILORED VERSION:\n{tailored}\n\n---\n\n"
            f"TARGET JOB: {job['title']} at {job['company']}\n\nReturn the shortened complete .tex source:"
        )},
    ]
    candidate = _splice_preamble(tex, _strip_fences(client.chat(messages, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.2)))
    problems = verify_latex_edit(tex, candidate, check_length=False)
    if problems:
        raise TailorError("Shortened resume failed verification:\n  - " + "\n  - ".join(problems))
    return candidate, {"attempts": 1, "problems": [], "status": "verified"}


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


def verify_latex_edit(original: str, tailored: str, check_length: bool = True) -> list[str]:
    """Return a list of human-readable problems; empty list means the edit is acceptable."""
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

    # Header block must be identical
    o_head = re.search(r"\\begin\{center\}.*?\\end\{center\}", o_body, re.DOTALL)
    t_head = re.search(r"\\begin\{center\}.*?\\end\{center\}", t_body, re.DOTALL)
    if o_head and (not t_head or _norm(o_head.group(0)) != _norm(t_head.group(0))):
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
            allowed = _skill_items(o_text + "\n" + _section_raw(original, name))
            whole_file = re.sub(r"\s+", " ", original.lower())
            new_items = sorted(
                i for i in _skill_items(t_text)
                if i not in allowed and re.sub(r"\s+", " ", i) not in whole_file
            )
            if new_items:
                problems.append(f"Skills added that are not in the original file: {new_items[:8]}.")
            # Categories (the \textbf{...} labels) must be ones the file already has
            o_cats = set(re.findall(r"\\textbf\{([^}]*)\}", _section_raw(original, name)))
            new_cats = sorted(set(re.findall(r"\\textbf\{([^}]*)\}", t_text)) - o_cats)
            if new_cats:
                problems.append(f"New skill categories are not allowed: {new_cats}.")
            continue

        o_items, t_items = len(_ITEM_RE.findall(o_text)), len(_ITEM_RE.findall(t_text))
        if o_items != t_items:
            problems.append(f"Bullet count changed in '{label}': {o_items} -> {t_items}.")

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
                f"A title/company/date line (one containing \\hfill) changed in '{label}'; copy those lines verbatim."
            )

        # Italic arguments (locations, tech stacks, employer names) must survive
        t_italics = Counter(_ITALIC_RE.findall(t_text))
        for arg, cnt in Counter(_ITALIC_RE.findall(o_text)).items():
            if t_italics[arg] < cnt:
                problems.append(f"Italic text '{arg}' missing or altered in '{label}'; keep it exactly.")

        # Every date and metric must survive
        for tok, cnt in Counter(m.lower() for m in _DATE_RE.findall(o_text)).items():
            if Counter(m.lower() for m in _DATE_RE.findall(t_text))[tok] < cnt:
                problems.append(f"Date '{tok}' missing or altered in '{label}'.")
        t_nums = Counter(_NUMBER_RE.findall(t_text))
        for tok, cnt in Counter(_NUMBER_RE.findall(o_text)).items():
            if t_nums[tok] < cnt:
                problems.append(f"Metric '{tok}' missing or altered in '{label}'; keep every number exactly.")

    if check_length:
        o_words, t_words = len(o_clean.split()), len(t_clean.split())
        lo, hi = int(o_words * (1 - LENGTH_TOLERANCE)), int(o_words * (1 + LENGTH_GROWTH))
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


def _protected_lines(section_text: str) -> list[str]:
    """Lines that carry title/company/date facts: resume templates align those with \\hfill."""
    return [line.strip() for line in section_text.splitlines() if "\\hfill" in line]


# ── 4. Output and PDF ─────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text[:max_len].rstrip("_") or "unknown"


def output_dir_for(job: dict) -> Path:
    return OUTPUT_DIR / f"{slugify(job['company'])}_{slugify(job['title'])}"


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
