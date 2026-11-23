---
name: "daily-job-search"
description: "Run a daily job search - collect Greenhouse/Lever/Ashby/SmartRecruiters/Simplify postings, screen for work-authorization blockers (e.g. F-1/OPT), dedupe against another workflow's ledger, match each job to one of the candidate's resume versions and write the daily list of every qualifying job. Never applies."
---

# Daily job search (Claude version of find_your_dream_job-US)

The generated daily lists are in Chinese (labels come from `scripts/cjs.py`); resumes and cover letters stay in English.
This skill finds and ranks jobs. **It never submits applications, fills application forms, contacts employers or marks anything applied without the user saying so.**

## 0. Facts first

**Required reading before any other step:** the user's standing rules (`<workspace>/RULES.md`), the application tracker (`<workspace>/APPLICATIONS.md`), the shortlist the user already picked (`<workspace>/TO_APPLY.md`) and the run log (`<workspace>/RUN_LOG.md`), if they exist. The rules file holds the user's standing decisions; where it conflicts with this skill, the rules win. The run's RUN_LOG entry starts with a line listing the docs actually read; if one could not be read, say so there. Skipping this step leads to wrong recommendations, such as re-listing a job the user already picked.

Candidate facts used for matching live in `<workspace>/profile.json` (`facts` with stable IDs, confirmed by the user). Only cite those facts. If a JD needs something not in `facts` (driver license, a tool, a physical requirement…), list it under `followups` or `gaps` — never assume it.

Work-authorization constraints come from the profile's `work_auth` fact and drive every visa decision (for a student on OPT, for example: whether the EAD has arrived, and that a STEM OPT extension needs an E-Verify employer, degree-related duties, ≥20 h/week paid work and an I-983). Not legal advice.

## 1. Workspace (state lives on the user's computer)

Skill folder (this directory) and a private workspace `<workspace>` (default `.claude-job-search/`, git-ignored):

```
scripts/cjs.py                  the CLI (python3, stdlib only)
tests/test_cjs.py               unit tests
assets/config.example.json      copy to <workspace>/config.json and edit
assets/profile.example.json     copy to <workspace>/profile.json and replace with your own facts
<workspace>/jobs.sqlite3        jobs, aliases, reviews, shortlisted, runs (created by the CLI)
<workspace>/external_*.json     optional read-only ledgers of jobs another workflow already handled
<workspace>/daily/YYYY-MM-DD.md daily candidate lists
```

Start of run:
1. List the workspace. If it cannot be reached (for example, the run happens in a sandbox that reaches the user's computer through a file bridge and the computer is off), stop and tell the user — do not rebuild state from scratch.
2. If the run works on a copy of the workspace, copy the script, tests and every workspace file to a local working dir with the same layout, and keep each file's modification time for the write-back.
3. Refresh any external ledger (read-only; never write into the other workflow's folder).
4. `python3 -m unittest discover -s tests` (run from the skill folder) must pass before touching the database.
5. Sync applications from `APPLICATIONS.md`. For every row whose status is applied / OA / interview / rejected / offer, run `mark-applied --url <job link>`; for status dropped run `mark-dropped --url <job link>`. Either way the job never comes back in a list. A link the database does not know → report it, don't guess. If the file does not exist yet, skip this step and say so. Rows without a link stay records only: while reviewing, if a job's company **and** title match such a row, treat it as already applied (skip, say which row); same company with a different or unknown title is not excluded. Also skip jobs already listed in `TO_APPLY.md` (they are chosen, not new).

End of run (only when working on a copy): write the changed files back (jobs.sqlite3, the daily list, and config.json/profile.json only if they changed), refusing to overwrite a file whose modification time changed since it was read. If a write is rejected because the file changed, re-read it and tell the user; never force. After every write-back, re-read the written files and compare sha256 with the working copies; a successful write response alone is not proof. If a hash differs, wait and write that file again; never report the write-back as done until every hash matches.

## 2. Pipeline

All commands: `python3 scripts/cjs.py --workspace <workspace> <cmd>`.

| Step | Command | Notes |
|---|---|---|
| Collect | `collect` | Fetches every board in config + Simplify (active, ≤14 days, not "U.S. Citizenship is Required"). Title/location prefilter **before** storing, so multi-MB feeds never enter the conversation. Re-applies the prefilter (`refilter`) at the end. Errors per source are reported and the old data kept. |
| External dedupe | `external --input <ledger.json> [--input ...]` | Marks jobs another workflow already delivered as `external_seen`; they never reach the list. Accepts a ledger (`{"items": [{identity, apply_url, verified_aliases}]}`) or a plain list of URLs. |
| Fill JDs | `resolve --limit 80` | Fills missing JDs through public Greenhouse/Lever/Ashby/SmartRecruiters APIs, plus Workday, Oracle, BambooHR, Jobvite, iCIMS, JazzHR, Rippling and employer-hosted Greenhouse pages. |
| JD fallback | `queue --need-jd --limit 15` → WebFetch each URL → save `[{job_id, description, status?}]` → `set-jd --input f.json` | For pages `resolve` cannot read (Workable and other ATSs). Workday pages are JS-rendered: if WebFetch returns only a summary, leave the lead (or use a browser if the user is around). Never treat a summary as a full JD. |
| Review batch | `queue --limit 15 --per-company 2 [--lane A]` | Returns JDs (truncated at 12k chars, flagged), rule hits, hashes. Lane order comes from config `lane_priority`, newest first, round-robin across companies. Follow `budgets.review_plan`: run one batch per lane until each lane's quota is used, so the fallback lane always gets its reviews; quota a lane cannot use (nothing pending) goes to the other lanes. |
| Record | `record --input decisions.json` | See §4. Rejects stale hashes, unknown fact IDs, recommendations without full JD/apply URL/resume. |
| List | `shortlist --day YYYY-MM-DD --out <workspace>/daily/YYYY-MM-DD.md` (user's local date) | Every strong/stretch not shown on a previous day, no upper cap (`--top N` only if the user asks); header shows the count per resume lane; warns when below `budgets.shortlist_min`; high visa risk and holds go to a separate "keep but not recommended" section. The same job posted under several URLs (same company + title + identical JD, e.g. one BambooHR id per city) is listed once; twins of an applied/dropped/previously listed job are dropped (`duplicate` count). |
| Applied | `mark-applied --url URL` | Only after the user says they applied (normally from `APPLICATIONS.md`, start-of-run step 5). |
| Dropped | `mark-dropped --url URL` | User decided not to apply. |
| Open list | `open --day YYYY-MM-DD --out <workspace>/daily/OPEN_CANDIDATES.md` | Every job ever recommended that is still open (not applied/dropped/closed, review ≤ 14 days old, `--max-age-days` to change). Run after `shortlist` every time. |
| Status | `status` | Counts by state and last runs. |
| Rescreen | `rescreen` | Re-runs JD screening (incl. hard blocks) on stored, unreviewed jobs after a rule/config change. |

Manual sources (browser, email, user-pasted links): build `[{company,title,listing_url,description,location,apply_url,source}]` and run `import --input f.json`.

Budgets (config `budgets`): ≤60 full-JD reviews per run in batches of 15, ≤2 per company per batch. When browsing job platforms, set a small fixed budget per platform (list pages, cards, detail pages) and do not move unused budget to another platform. Do not stop early when the minimum is reached: spend the review plan and list everything that qualifies. If fewer than `shortlist_min` qualify after the plan, review up to `extra_reviews_if_below_min` more, then stop and say how many were found and why it fell short.

## 3. Sources, in this order

1. **APIs** (script): Greenhouse, Lever, Ashby, SmartRecruiters boards in `config.json`. Add a board only after confirming the slug on the employer's careers page and a 200 from the API; note it in `config.json`'s `note`.
2. **List**: Simplify New-Grad `listings.json` (script). Simplify links are leads; the JD comes from the employer page.
3. **Web pages**: WebFetch employer career pages / Workable / other ATS for single postings (not for big lists). The Workable API may rate-limit (429) — use WebFetch.
4. **Browsers** (only when the user has logged in and either the run is attended or the computer is on): job platforms the user names, such as Handshake, Indeed or LinkedIn. If a site blocks one browser (e.g. Cloudflare), use the user's own browser if it is connected; otherwise skip the platform and say so. Read only: no Save / Applied / Easy Apply / hide clicks, no account changes. Platform tags (e.g. "sponsors OPT") are leads; confirm on the full JD. Captcha, login wall or empty page → skip that platform and continue. Job-alert emails, if the user allows reading them, give title + company leads only.

## 4. Screening and matching rules

Automatic rules (script, from JD text; evidence snippet saved):
- **block** (removed from review): explicit U.S. citizenship / green card / U.S. person requirement; security clearance required or must be obtainable; explicitly no OPT/CPT/F-1; must be currently enrolled.
- **hard block** (config `hard_requirements`, profile-dependent): a clearly *required* sentence (requirement wording, or under a Minimum/Basic/Required/"You have"/"About you" heading) asking for more than `max_years_required_ok` years in every alternative, or an MS/PhD with no bachelor's / equivalent-experience / years-or-PhD alternative. Preferred/bonus wording, lower-education paths (high school, "may substitute"), age and driving-record years never block. Rule ids `hard_years_required` / `hard_degree_required`, evidence sentence saved. In the run report, list how many were hard-blocked this run and show up to 5 evidence sentences so the user can spot-check. After changing `hard_requirements` (e.g. the user gains experience or a degree) run `rescreen`; it never touches jobs with a current review.
- **risk** (agent reads and decides): no sponsorship / must work without sponsorship; 5+ or 3–4 years required; advanced degree required; graduation window; contract/staffing/temp; driver license.
- **info**: E-Verify mentioned, sponsorship offered.
- Title exclusions (config `exclude_title`): e.g. senior/staff/lead/manager/principal/intern/co-op/PhD/fellow/level II+, language-specific roles, talent pools. Location: non-US dropped, unknown kept.

Agent rules when reviewing (the script cannot judge these):
- Read the real duties and requirements. Reject only on a confirmed mandatory mismatch (JD text + a confirmed fact). Unknowns become `followups`, not rejections.
- "No sponsorship" alone does not prove the employer rejects OPT, but such a job must not be a top recommendation: record `visa_risk: high` with the exact wording in `visa_note` (it lands in the "keep but not recommended" section).
- Employment must reach ≥20 h/week and be degree-related; shifts under 20 h or pure gig work → skip.
- Staffing / short-term W-2 / contract-to-hire → `visa_risk: medium`, and note that a STEM OPT extension is unlikely.
- Start "ASAP" while work authorization is pending → followup, not a blocker.
- Years of experience: 1–2 years preferred is fine; 2–3 required can be a stretch if projects transfer; 5+ required → skip.
- Never invent an applicant count or hiring probability.

Resume choice (`resume`) — the three lanes match the resume versions in `profile.json` `resumes` and the `title_lanes` in config. Example lanes:
- **A Data Analyst** — analyst, reporting, data quality, research data, ops analytics.
- **B Software/AI Engineer** — AI application / junior ML engineer, AI evaluation, data science, test/automation.
- **C Robotics Data** — robot operation, teleop, data collection, data ops, annotation/QA.
- `custom`: do not force a lane. Use it when none of the versions fits the role well, or when one fits but the JD stresses something the user really has (a fact in `profile.json`) that the base version leaves out or buries. `resume_note` is required (the script rejects custom without it) and must name the base version, which fact IDs to add/move/reword, and the JD keywords to mirror. True facts only.
- If what the JD wants is not true yet but quick to make true (roughly a weekend to a week to have something to show: a small dashboard, a small web app, a small RAG project, cloud/Docker basics, a short course), still recommend the job and put the skill in `learning`. The resume may only say it as in progress, e.g. "Tableau (currently learning)", never as already used. Not for things that cannot be closed fast (a new language for production work, years of experience, a degree): those stay in `gaps`.
- Reuse the same `learning` names across runs so counts add up (e.g. `Tableau/Power BI`, `Streamlit/Flask`, `RAG/LLM 应用`, `AWS 基础`, `Docker`); add a new name only when none fits. `open` puts a "which skill first" table at the top of OPEN_CANDIDATES (how many open jobs each learning item blocks); mention the top one or two in the final message.
- Once the user has really done it (confirmed, added to `profile.json`), it becomes a normal fact and leaves `learning`.
- This skill only records the plan; resume files are generated elsewhere.

Decision object (one per job, copy `job_id`, `job_sha256`, `profile_sha256` from the queue):
```json
{"job_id": "...", "job_sha256": "...", "profile_sha256": "...",
 "disposition": "strong|stretch|hold|skip", "score": 0-10,
 "resume": "A|B|C|custom|", "resume_note": "",
 "reason": "2-4 core duties mapped to facts",
 "fact_ids": ["teleop_project"], "gaps": [], "learning": [], "followups": [], "blockers": [],
 "visa_risk": "low|medium|high|unknown", "visa_note": "exact JD wording / why",
 "opening_checked": true, "jd_complete": true}
```
strong ≈ core duties directly evidenced; stretch ≈ worth applying with clear gaps; hold = needs one user answer (put it in followups); skip = not worth it (say why). Reviews expire after 7 days or when the JD/profile changes.

## 5. Output

Daily list `<workspace>/daily/YYYY-MM-DD.md`. Then rewrite the open list `<workspace>/daily/OPEN_CANDIDATES.md` with `open`; this is the one file the user picks jobs from. Final message: counts (collected / blocked / external duplicate / reviewed / recommended), the table of every listed job (link, resume version, match, visa risk), the questions the user must answer (followups that recur), and source failures. Then the next action for the user: pick roles from `OPEN_CANDIDATES.md` and record applications in `APPLICATIONS.md`.

Materials are not written in this skill. If the user insists, draft the cover letter in English only from `facts` and give concrete resume edits (which bullet to move/reword), never new claims, and remind them to record the application in `APPLICATIONS.md`.

## 6. Boundaries

- No submissions, uploads, form filling, employer contact, Save/Applied clicks.
- No passwords in chat; sites needing login are logged in by the user.
- Never write into another workflow's folder. This skill's results stay in `<workspace>`.
- Don't add facts to `profile.json` without the user confirming them; when they do, add a fact with a new ID and today's date.
