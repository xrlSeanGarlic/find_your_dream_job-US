# Job Search Skill

A Codex skill and small Python CLI for evidence-backed job shortlists. The CLI collects listings, keeps a local review queue, and formats the report. The agent reads job descriptions, verifies openings, and evaluates fit against candidate facts.

## Try it offline

Requires Python 3.10 or later. The CLI uses the Python standard library.

```text
python .agents/skills/job-search/scripts/job_search.py --workspace .job-search/demo demo
```

The demo uses fictional postings and saves `.job-search/demo/report.md`. Choose an empty directory for each demo. On Windows, `py -3` can be used in place of `python`.

## Use in Codex

1. Open this repository in Codex, or copy `.agents/skills/job-search` to the same location in your own repository.
2. Copy `.agents/skills/job-search/assets/profile.example.json` to `.job-search/profile.json` and replace the example facts. Add your role, location and authorization preferences as appropriate. Keep your real profile and reports in `.job-search/`.
3. Invoke `$job-search` and provide an employer careers page, a confirmed public recruiting board, or a set of posting URLs. Ask for a shortlist and any follow-up questions.

The [CLI reference](.agents/skills/job-search/references/cli.md) explains manual intake, public board imports and review decisions. Built-in board adapters use the public [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html), [Lever Postings API](https://github.com/lever/postings-api) and [Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api).

The workflow runs on demand. Reviews and application status are stored locally. Resume output is a version suggestion or tailoring direction. Email, scheduling, document rendering and submission are outside the initial release.

## Verify

```text
python -m unittest discover -s tests -v
```
