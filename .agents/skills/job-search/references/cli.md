# CLI reference

Use Python 3.10 or later. Run `scripts/job_search.py` relative to the installed skill. `--workspace` precedes the subcommand and defaults to `.job-search` in the current directory.

## Profiles and intake

Copy [the example profile](../assets/profile.example.json) to `profile.json` in the workspace. Use stable IDs in `facts`. `target_keywords` affects reading order only.

```text
python PATH_TO_SKILL/scripts/job_search.py --workspace .job-search collect --board greenhouse:BOARD --company "Employer"
python PATH_TO_SKILL/scripts/job_search.py --workspace .job-search collect --input .job-search/jobs.json
python PATH_TO_SKILL/scripts/job_search.py --workspace .job-search queue --limit 15
```

Confirm a board slug from the employer's careers page. Available providers are `greenhouse`, `lever`, and `ashby`. India, EU or custom domains can use manual import when a provider's default endpoint lacks the posting.

An authoritative board refresh moves previously listed postings missing from that board back to verification. An unsuccessful fetch leaves the last saved listings available for review. Check an entry before recommending it, especially after a failed refresh.

For manual intake, `jobs.json` is a list of objects. Each contains `company`, `title`, `listing_url` and `description` (readable text). Optional fields are `location`, `apply_url` and `status` (`listed`, `unknown`, or `closed`, default `unknown`). Use an empty description for a lead. The queue includes unreviewed roles and roles whose review has expired or whose input has changed.

## Reviews and reports

Save decisions as a JSON list. Copy `job_id`, `job_sha256` and `profile_sha256` from the queue. For each decision use the following fields:

| Field | Meaning |
|---|---|
| `disposition` | `strong`, `stretch`, `hold` or `skip` |
| `reason` | Explanation grounded in the JD and facts |
| `fact_ids` | List of profile fact IDs used |
| `opening_checked`, `entry_checked` | Booleans reflecting direct verification |
| `jd_complete` | Boolean reflecting an actual read of duties and requirements |
| `checked_at` | Time of the actual check in ISO 8601 with UTC offset, required for recommendations |
| `gaps`, `followups`, `blockers` | Optional lists of strings |
| `resume` | Optional existing version name or tailoring direction |

Strong and stretch reviews require a readable full JD, affirmative checks, at least one fact, a job-specific entry and no known application blockers. For a hold, tell the user what still needs verification. The seven-day review period determines when to recheck evidence. It does not promise that a role stays open.

```text
python PATH_TO_SKILL/scripts/job_search.py --workspace .job-search record --input .job-search/decisions.json
python PATH_TO_SKILL/scripts/job_search.py --workspace .job-search report
python PATH_TO_SKILL/scripts/job_search.py --workspace .job-search mark-applied --url "https://example.org/jobs/123"
```

`report --out FILE` saves Markdown. Apply-URL aliases for the same supported ATS are canonicalized. Use the saved listing URL if the application is on a different host. Marking a role applied updates only the local SQLite database.
