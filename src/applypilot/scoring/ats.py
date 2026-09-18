"""ATS keyword match scoring, the gap-closing pass, and the study plan it produces.

The tailor pass in `tailor_tex` is deliberately conservative: it may only reword
what is already in your resume. That keeps it honest, but a posting often asks
for terms your resume simply does not contain, and an ATS scores you on those
literal terms.

So after tailoring we:
  1. pull the posting's ATS keywords out of the requirements (LLM, with a
     deterministic fallback),
  2. score the tailored .tex against them,
  3. if the score is below target, run a further pass that adds the missing
     terms whether or not the candidate has used them -- skills onto the skills
     line, domain language into the summary, never a new employer, date, metric,
     bullet or credential -- retrying while the verifier rejects the edit, and
  4. write every added term to things_to_learn.txt, so anything the resume now
     claims is something you can go and get familiar with before the interview.

Step 4 is the point of step 3: the file is the list of what to study.

None of these numbers survive the one-page trim that may follow, because that pass rewrites
the text they were measured on. `rescore` takes them again from the .tex that actually goes
out, so what the run reports is what the PDF carries.
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from datetime import date
from pathlib import Path

from applypilot.llm import get_client
from applypilot.scoring import tailor_tex as tt

log = logging.getLogger(__name__)

TARGET_SCORE = 90           # what we aim the keyword match at
MAX_ADDED_TERMS = 12        # never claim more than this many new terms in one pass
MAX_KEYWORDS = 30           # keywords pulled from one posting
GAP_RETRIES = 2             # rejected gap attempts to retry with the verifier's complaints
GAP_ROUNDS = 2              # accepted rounds: a second one picks up terms the first dropped
GAP_LENGTH_GROWTH = 0.08    # the gap pass may grow the body this much (page check still applies)
REQUIRED_WEIGHT = 2         # a must-have term counts double a nice-to-have
PREFERRED_WEIGHT = 1


# ── 1. Keywords from the posting ──────────────────────────────────────────

EXTRACT_PROMPT = """You are an ATS (applicant tracking system) parser. Read the job posting and
list the concrete keywords the ATS would match a resume against.

Return JSON and nothing else:
{"keywords": [{"term": "Kubernetes", "kind": "skill", "weight": "required", "aliases": ["k8s"]}]}

RULES:
- 15 to 25 terms, most important first.
- Read the ROLE: the requirements, responsibilities and qualifications. IGNORE the
  company description, the mission statement, the perks and the culture blurb -- an
  ATS does not score a resume on what the company sells.
- "kind" is "skill" for something a person can put on a skills line (language,
  framework, library, tool, platform, database, protocol, practice, methodology)
  or "domain" for the business/industry context of the work (fintech, enterprise
  analytics, healthcare, e-commerce).
- NO soft skills ("team player"), NO years of experience, NO degrees, NO
  certifications, NO company names, NO job titles, NO generic words ("software").
- "term" must be spelled exactly as the posting spells it.
- "weight" is "required" if it appears in requirements / must-haves / minimum
  qualifications, otherwise "preferred".
- "aliases" are other spellings an ATS or a resume might use (e.g. "CI/CD" for
  "continuous integration"), or [] if there are none."""


def extract_keywords(job: dict) -> list[dict]:
    """ATS keywords for one posting: [{term, weight, aliases}]. Falls back to frequency analysis."""
    description = job.get("full_description", "")[: tt.MAX_DESC_CHARS]
    try:
        raw = get_client().chat(
            [
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": (
                    f"TITLE: {job.get('title', '')}\nCOMPANY: {job.get('company', '')}\n\n"
                    f"POSTING:\n{description}\n\nReturn the JSON:"
                )},
            ],
            max_tokens=2048,
            temperature=0.0,
        )
        keywords = _parse_keywords(raw)
        if keywords:
            return keywords[:MAX_KEYWORDS]
        log.warning("Keyword extraction returned nothing usable; falling back to frequency analysis")
    except Exception as e:  # noqa: BLE001 - scoring must never sink the tailor run
        log.warning("Keyword extraction failed (%s); falling back to frequency analysis", e)
    return _fallback_keywords(description)


def _parse_keywords(raw: str) -> list[dict]:
    data = _json_object(raw)
    out: list[dict] = []
    seen: set[str] = set()
    for item in (data or {}).get("keywords", []):
        term = str(item.get("term", "") if isinstance(item, dict) else item).strip()
        if not term or len(term) > 40 or term.lower() in seen:
            continue
        seen.add(term.lower())
        weight = "required" if str(item.get("weight", "")).lower().startswith("req") else "preferred"
        kind = "domain" if str(item.get("kind", "")).lower().startswith("dom") else "skill"
        aliases = [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()][:4]
        out.append({"term": term, "kind": kind, "weight": weight, "aliases": aliases})
    return out


def _json_object(raw: str) -> dict | None:
    """First JSON object in a reply, tolerating fences and surrounding prose."""
    text = raw.strip()
    if "```" in text:
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text.strip("`\n "), flags=re.MULTILINE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


# Words that look like skills to a frequency counter but are not worth matching on.
_NOISE = {
    "the", "and", "for", "with", "you", "our", "work", "team", "role", "job", "will", "have",
    "experience", "years", "year", "skills", "ability", "strong", "good", "knowledge", "working",
    "software", "development", "developer", "engineer", "engineering", "company", "business",
    "customer", "customers", "product", "products", "solution", "solutions", "technology",
    "technologies", "responsibilities", "requirements", "qualifications", "preferred", "required",
    "including", "etc", "environment", "opportunity", "position", "candidate", "candidates",
    # function words that slip through because a sentence starts with them
    "we", "they", "this", "that", "these", "those", "your", "their", "who", "what", "when", "where",
    "as", "at", "in", "on", "of", "to", "or", "if", "be", "is", "are", "was", "an", "a", "it", "its",
    "you'll", "we're", "join", "about", "apply", "please", "note", "plus", "must", "should", "can",
}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./_-]{1,29}")


def _fallback_keywords(description: str) -> list[dict]:
    """No LLM: take the frequent, capitalised or symbol-bearing tokens as keywords."""
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for token in _TOKEN_RE.findall(description):
        clean = token.strip("./_-")
        low = clean.lower()
        if len(clean) < 2 or low in _NOISE or low.isdigit():
            continue
        # keep things that read like tech: CamelCase, ALLCAPS, or containing + # . /
        if not (clean[0].isupper() or any(c in clean for c in "+#./")):
            continue
        counts[low] = counts.get(low, 0) + 1
        display.setdefault(low, clean)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_KEYWORDS]
    return [
        {"term": display[low], "kind": "skill", "weight": "required" if n >= 3 else "preferred", "aliases": []}
        for low, n in ranked
    ]


# ── 2. Scoring a resume against them ──────────────────────────────────────

# Flattening a .tex and matching one term against it live in `tailor_tex`: the shortening
# pass has to answer the same question ("is this keyword still in the file?") and the two
# have to answer it identically, or a term this scorer counts is one that pass may cut.
resume_text = tt.plain_text


def contains_term(text: str, keyword: dict) -> bool:
    """True if the resume text carries the keyword or any of its aliases."""
    return any(tt.term_pattern(t).search(text) for t in [keyword["term"], *keyword.get("aliases", [])])


def score(tex: str, keywords: list[dict]) -> dict:
    """Weighted keyword coverage: {"score": 0-100, "matched": [...], "missing": [...]}."""
    text = resume_text(tex)
    matched, missing, got, total = [], [], 0, 0
    for kw in keywords:
        weight = REQUIRED_WEIGHT if kw["weight"] == "required" else PREFERRED_WEIGHT
        total += weight
        if contains_term(text, kw):
            matched.append(kw)
            got += weight
        else:
            missing.append(kw)
    return {
        "score": round(100 * got / total) if total else 100,
        "matched": matched,
        "missing": missing,
        "keywords": keywords,
    }


# ── 3. The gap-closing pass ───────────────────────────────────────────────

GAP_PROMPT = """You are editing a LaTeX resume that has already been tailored to one job posting.
An ATS keyword scan says it is missing terms the posting asks for. Your job is to put those
terms into the resume. Return the complete compilable .tex source and nothing else: no
markdown fences, no commentary, no refusal.

The candidate has NOT necessarily used these technologies. Add them anyway -- they are
studying them before the interview, and every term you add is recorded for them to learn.
Do not skip a term because you cannot see it in their history: that is the whole point of
this pass. Do not add a note, comment or disclaimer about it.

The skills section in this file is "{skills_section}" and the summary section is "{summary_section}".

ADD THESE TERMS ({skill_count} of them go in the skills section):
{skill_block}
{domain_block}
WHERE EACH ONE GOES:
1. "{skills_section}": put every term above into the most fitting existing category line.
   Keep the "\\textbf{{Category}}{{: items}} \\\\" pattern and the trailing \\\\ on every visible
   line except the last one. If no category fits a term, add ONE new category line in the same
   pattern (a name like "Cloud and DevOps" or "Data and Messaging") and put the leftovers there.
   Drop the least relevant existing items to pay for the new ones if the line gets long.
2. "{summary_section}": reword the single \\item so the job's most important terms and the
   business domain appear naturally. Same length, max 3 sentences.
3. Experience and Projects bullets: you may reword a bullet to name a technology alongside the
   work it already describes ("built REST APIs" -> "built REST APIs with Spring Boot").
   Keep what the bullet achieved and its numbers exactly.

FORBIDDEN (any of these fails the job and the edit is thrown away):
- Adding, removing, splitting or merging bullets; changing the bullet count anywhere.
- Changing any company, job title, project name, location, date, or the education section.
- Changing any number, percentage, duration or count (90%, 3.3s to 380ms, 500+, 40%).
- Changing anything before \\begin{{document}}, or the header block.
- Inventing a job, employer, certification or degree. You add technology TERMS to the skills
  line and to wording -- never new history and never a credential.
- Unescaped LaTeX specials in prose: write \\%, \\&, \\#, \\_. Use -- for dashes.

HARD LIMITS (checked by a program):
{hard_limits}
- The document must still compile and fit on one page: keep it tight.

Return the complete .tex source."""


def close_gaps(
    original: str,
    tailored: str,
    job: dict,
    missing: list[dict],
    max_terms: int = MAX_ADDED_TERMS,
    max_retries: int = GAP_RETRIES,
) -> tuple[str, list[str]]:
    """LLM pass(es) that ADD the missing terms to the resume. Returns (tex, problems).

    Skills go on the skills line, domain terms into the summary wording. Rejected attempts
    are retried with the verifier's complaints fed back; if every attempt fails the tailored
    .tex comes back unchanged with the problems listed. A weak keyword score never fails a run.
    """
    wanted = missing[:max_terms]
    if not wanted:
        return tailored, []
    skills = [kw for kw in wanted if kw.get("kind", "skill") != "domain"]
    domains = [kw for kw in wanted if kw.get("kind", "skill") == "domain"]
    summary_section, skills_section = tt._detect_special_sections(original)
    system = GAP_PROMPT.format(
        skills_section=skills_section,
        summary_section=summary_section,
        skill_count=len(skills),
        skill_block="\n".join(_term_line(kw) for kw in skills) or "- (none)",
        domain_block=(
            "\nAND WEAVE THIS BUSINESS DOMAIN INTO THE SUMMARY WORDING (not the skills line):\n"
            + "\n".join(_term_line(kw) for kw in domains) + "\n"
            if domains else ""
        ),
        hard_limits=tt._hard_limits(tailored, GAP_LENGTH_GROWTH),
    )
    allow = {t.lower() for kw in wanted for t in [kw["term"], *kw.get("aliases", [])]}
    user = (
        f"TAILORED LATEX RESUME:\n{tailored}\n\n---\n\n"
        f"TARGET JOB: {job.get('title', '')} at {job.get('company', '')}\n\n"
        "Return the complete .tex source with the missing terms added:"
    )

    problems: list[str] = []
    for attempt in range(max_retries + 1):
        prompt = system
        if problems:
            prompt += "\n\nFIX THESE PROBLEMS FROM YOUR PREVIOUS ATTEMPT:\n" + "\n".join(
                f"- {p}" for p in problems[:8]
            )
        try:
            raw = get_client().chat(
                [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
                max_tokens=tt.MAX_OUTPUT_TOKENS,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001 - keep the tailored resume we already have
            log.warning("Gap-closing pass failed (%s); keeping the tailored resume", e)
            return tailored, [f"gap pass failed: {e}"]

        candidate = tt._splice_preamble(tailored, tt._strip_fences(raw))
        problems = tt.verify_latex_edit(
            tailored, candidate, allow_skills=allow, allow_new_categories=True, max_growth=GAP_LENGTH_GROWTH,
        )
        if not problems:
            return candidate, []
        log.warning("Gap-closing attempt %d rejected: %s", attempt + 1, "; ".join(problems))

    return tailored, problems


def _term_line(kw: dict) -> str:
    aliases = f" [also written: {', '.join(kw['aliases'])}]" if kw.get("aliases") else ""
    return f"- {kw['term']} ({kw['weight']}){aliases}"


def added_terms(before: str, after: str, keywords: list[dict]) -> list[dict]:
    """Keywords the gap pass put into the resume: exactly what the candidate now claims."""
    before_text, after_text = resume_text(before), resume_text(after)
    return [kw for kw in keywords if contains_term(after_text, kw) and not contains_term(before_text, kw)]


# ── 4. things_to_learn.txt ────────────────────────────────────────────────

STUDY_PROMPT = """You are preparing a candidate for an interview. Each term below is on their resume
for this job but they have NOT used it -- it was added to pass the keyword scan. Write the
fastest path to being credible about it in an interview for THIS role, starting from zero.

Return JSON and nothing else:
{"items": [{"term": "Kubernetes", "what": "one sentence: what it is and where it fits",
"learn": ["concrete thing to do or know", "another", "another"],
"asked": "the question an interviewer for this role would most likely open with",
"hours": 6}]}

RULES:
- "learn" is 2 to 4 items, concrete and specific to this role's stack, not "read the docs".
- "hours" is a realistic estimate of focused hours to be conversational, not expert.
- Keep every string under 200 characters. One object per term, same order, same spelling."""


def study_notes(terms: list[dict], job: dict) -> dict[str, dict]:
    """{term: {what, learn, asked, hours}} for the added terms. Empty dict if the LLM is unavailable."""
    if not terms:
        return {}
    try:
        raw = get_client().chat(
            [
                {"role": "system", "content": STUDY_PROMPT},
                {"role": "user", "content": (
                    f"ROLE: {job.get('title', '')} at {job.get('company', '')}\n"
                    f"POSTING (excerpt):\n{job.get('full_description', '')[:2500]}\n\n"
                    f"TERMS:\n" + "\n".join(f"- {kw['term']}" for kw in terms) + "\n\nReturn the JSON:"
                )},
            ],
            max_tokens=3072,
            temperature=0.3,
        )
        data = _json_object(raw) or {}
        notes = {}
        for item in data.get("items", []):
            term = str(item.get("term", "")).strip()
            if term:
                notes[term.lower()] = {
                    "what": str(item.get("what", "")).strip(),
                    "learn": [str(x).strip() for x in (item.get("learn") or []) if str(x).strip()][:4],
                    "asked": str(item.get("asked", "")).strip(),
                    "hours": item.get("hours"),
                }
        return notes
    except Exception as e:  # noqa: BLE001 - the file is still worth writing without notes
        log.warning("Study notes failed (%s); writing the plain term list", e)
        return {}


RULE = "=" * 72


def _count(items: list) -> str:
    return f"{len(items)} term" + ("s" if len(items) != 1 else "")


def _wrap(text: str, indent: str = "") -> list[str]:
    """Hard-wrap a paragraph to 72 columns: this file is read in Notepad."""
    return textwrap.wrap(text, width=72, initial_indent=indent, subsequent_indent=indent) or [""]


def write_learning_plan(out_dir: Path, job: dict, result: dict, added: list[dict], notes: dict[str, dict]) -> Path:
    """Write things_to_learn.txt: what the resume now claims, and how to be ready for it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "things_to_learn.txt"
    before, after = result["score_before"], result["score_after"]
    origin = result.get("score_original", before)
    still_missing = result["missing_after"]
    company = job.get("company", "").strip()
    title = job.get("title", "this role").strip() + (f"  at  {company}" if company else "")

    lines = [
        RULE,
        "THINGS TO LEARN",
        title,
        RULE,
        # yours -> tailored -> keyword pass, with the steps that moved nothing collapsed
        f"ATS keyword match : {progression(origin, before, after)}"
        f"   (your resume -> generated; target {result.get('target', TARGET_SCORE)}%)",
        f"Generated         : {date.today().isoformat()} from the posting in job.txt",
        "",
    ]

    if added:
        hours = [notes.get(kw["term"].lower(), {}).get("hours") for kw in added]
        total = sum(h for h in hours if isinstance(h, (int, float)))
        lines += [
            RULE,
            f"ON YOUR RESUME NOW -- BUT NOT YET TRUE  ({_count(added)})",
            RULE,
        ]
        lines += _wrap(
            "These terms were not in your resume before this run. They are in the PDF "
            "you are about to send, so an interviewer can ask about any of them and you "
            "have to be able to answer. Learn them before you reply to the recruiter"
            + (f" -- roughly {int(total)} focused hours in total." if total else ".")
        )
        lines += _wrap(
            "If you are not willing to be questioned on one of these, delete it from the "
            ".tex and recompile."
        )
        lines.append("")
        for i, kw in enumerate(added, 1):
            note = notes.get(kw["term"].lower(), {})
            hrs = note.get("hours")
            tag = kw["weight"] + (f", ~{int(hrs)}h" if isinstance(hrs, (int, float)) else "")
            lines.append(f"[{i}] {kw['term']}  ({tag})")
            if note.get("what"):
                lines += _wrap(note["what"], indent="    ")
            if note.get("learn"):
                lines.append("    Learn:")
                for item in note["learn"]:
                    wrapped = _wrap(item, indent="          ")
                    wrapped[0] = "      [ ] " + wrapped[0].lstrip()
                    lines += wrapped
            if note.get("asked"):
                lines += _wrap(f'They will ask: "{note["asked"]}"', indent="    ")
            lines.append("")

    if result.get("problems") and not added:
        lines += [
            RULE,
            "NOTHING WAS ADDED -- THE KEYWORD PASS WAS REJECTED",
            RULE,
        ]
        lines += _wrap(
            "The edit the model returned broke a rule that protects your facts, so the safe "
            "resume was kept instead. Re-run to try again. What the checker said:"
        )
        for problem in result["problems"][:5]:
            wrapped = _wrap(problem, indent="      ")
            wrapped[0] = "    - " + wrapped[0].lstrip()
            lines += wrapped
        lines.append("")

    if still_missing:
        lines += [
            RULE,
            f"STILL NOT MATCHED  ({_count(still_missing)})",
            RULE,
        ]
        lines += _wrap(
            "Not added -- either they did not fit on one page or they are company/domain "
            "language rather than a skill. Worth knowing if this kind of role is the target:"
        )
        lines.append("")
        lines += [f"    - {kw['term']}  ({kw['weight']})" for kw in still_missing]
        lines.append("")

    matched_before = [kw["term"] for kw in result["matched_before"]]
    if matched_before:
        lines += [
            RULE,
            f"ALREADY YOURS  ({_count(matched_before)})",
            RULE,
        ]
        lines += _wrap(
            "The posting asks for these and your resume already showed them. Lead with "
            "these in the interview and steer toward them when you can:"
        )
        lines.append("")
        lines += _wrap(", ".join(matched_before), indent="    ")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── 5. One call that does the whole thing ─────────────────────────────────

def progression(*scores: int) -> str:
    """"40% -> 80%": the score at each stage, with steps that moved nothing collapsed."""
    parts: list[str] = []
    for s in scores:
        text = f"{s}%"
        if not parts or parts[-1] != text:
            parts.append(text)
    return " -> ".join(parts)


def baseline(original: str, job: dict) -> dict:
    """Score the untouched resume against one posting, before anything is generated.

    Returns {"keywords", "score", "matched", "missing"}. The keyword list comes back so
    the caller can hand it to `boost`, which would otherwise re-extract it: the before
    and after numbers only mean anything when both are measured against the same terms.
    """
    keywords = extract_keywords(job)
    result = score(original, keywords)
    return {"keywords": keywords, **result}


def boost(
    original: str,
    tailored: str,
    job: dict,
    target: int = TARGET_SCORE,
    rounds: int = GAP_ROUNDS,
    keywords: list[dict] | None = None,
) -> tuple[str, dict]:
    """Score the tailored .tex, close the gaps until it reaches target, and report.

    One round rarely lands every term -- the model drops the ones it cannot place --
    so a round that improves the score but stays under target is run again with only
    what is still missing. Returns (tex, result) carrying the scores, the added terms
    and any problems. Never raises: a failed boost just means the tailored resume as-is.

    `keywords` skips the extraction call when the caller already scored the untouched
    resume against this posting (see `baseline`); the two scores are only comparable
    when they come from the same keyword list.
    """
    if keywords is None:
        keywords = extract_keywords(job)
    untouched = score(original, keywords)
    before = score(tailored, keywords)
    result = {
        "keywords": keywords,
        "score_original": untouched["score"],
        "score_before": before["score"],
        "score_after": before["score"],
        "matched_before": before["matched"],
        "missing_after": before["missing"],
        "added": [],
        "problems": [],
        "target": target,
    }
    if not keywords:
        return tailored, result

    current, tex = before, tailored
    problems: list[str] = []
    for round_no in range(rounds):
        if current["score"] >= target or not current["missing"]:
            break
        boosted, problems = close_gaps(original, tex, job, current["missing"])
        if boosted == tex:  # every attempt in that round was rejected
            break
        after = score(boosted, keywords)
        log.info("Gap round %d: %d%% -> %d%%", round_no + 1, current["score"], after["score"])
        if after["score"] <= current["score"]:  # no progress: keep the round, stop spending
            tex, current = boosted, after
            break
        tex, current = boosted, after

    result.update(
        score_after=current["score"],
        missing_after=current["missing"],
        added=added_terms(tailored, tex, keywords),
        problems=problems,
    )
    return tex, result


def rescore(original: str, tex: str, result: dict) -> dict:
    """Re-measure a `boost` result against the .tex that is actually going out.

    `boost` reports on the text it produced, but the one-page trim then rewrites that text
    and a trim pays for space with words -- sometimes the keyword words. So the score, the
    unmatched list and the terms the study plan calls "not yet true" all have to be taken
    again from the final file, or the run reports a number the PDF does not have.

    Returns a new dict; `result` is left alone. `score_before` keeps its meaning: what the
    tailored resume was worth before the keyword pass.
    """
    keywords = result.get("keywords") or []
    if not keywords:
        return dict(result)
    now = score(tex, keywords)
    return {
        **result,
        "score_after": now["score"],
        "missing_after": now["missing"],
        "added": added_terms(original, tex, keywords),
    }
