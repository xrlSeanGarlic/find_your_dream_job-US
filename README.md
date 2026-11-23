# Job Search Skill

[中文使用说明](#中文使用说明)

Job Search Skill helps you turn a pile of listings into a shortlist worth acting on. Tell Codex which roles and locations you're targeting, and it will look for openings, check official application links, and explain how each role lines up with your experience.

The repository holds two versions of the workflow:

- **Codex skill** (`.agents/skills/job-search/`): the on-demand version described below; its code was written with Codex.
- **Claude skill** (`claude_skill/`): a daily-run version developed with Claude Code. See [Claude version](#claude-version-claude_skill).

Search across industries and functions, from finance, marketing, and operations to project management and software engineering. For each recommended role, the skill points to relevant experience from your resume or profile and flags requirements you still need to confirm, such as experience level, location, or visa sponsorship.

## Try it offline

Requires Python 3.10 or later. The CLI uses the Python standard library.

```text
python .agents/skills/job-search/scripts/job_search.py --workspace .job-search/demo demo
```

The demo uses fictional postings and saves `.job-search/demo/report.md`. Choose an empty directory for each demo. On Windows, `py -3` can be used in place of `python`.

## Use in Codex

1. Open this repository in Codex, or copy the entire `.agents/skills/job-search` folder to the same location in your own repository.
2. Share a resume or a short summary of your experience. To reuse your details across searches, copy `.agents/skills/job-search/assets/profile.example.json` to `.job-search/profile.json` and replace the example facts. Keep your personal profile and reports in `.job-search/`.
3. Invoke `$job-search` and describe what you're looking for. You can also provide an employer careers page, a public recruiting board, or a specific posting.

```text
$job-search Find up to 10 entry-level financial analyst or project coordinator roles in California or remote within the U.S.
```

Already have a listing? Try `$job-search Review this opening against my resume: [job URL]`.

The [CLI reference](.agents/skills/job-search/references/cli.md) explains manual intake, public board imports and review decisions. Built-in board adapters use the public [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html), [Lever Postings API](https://github.com/lever/postings-api) and [Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api).

The Python CLI collects listings, deduplicates them, and keeps reviews and application status locally. In v0.1.0, searches run on demand. The skill can recommend a resume version or suggest which experience to highlight. You review the recommendations and submit your applications.

## Claude version (`claude_skill/`)

`claude_skill/` is a separate Claude skill, `daily-job-search`, developed with Claude Code. It has its own CLI, `claude_skill/scripts/cjs.py` (Python 3.10+, standard library only), and its own state in `.claude-job-search/`, so it can run next to the Codex version without touching its data. It is built for one run per day; the author runs it as a scheduled Claude task, and the repository itself contains no scheduler. [SKILL.md](claude_skill/SKILL.md) is the agent workflow; the script handles everything that can be checked mechanically.

**Sources**

- Public board APIs: [Greenhouse](https://docs.greenhouse.io/job-board.html), [Lever](https://github.com/lever/postings-api), [Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api) and SmartRecruiters (boards listed in `config.json`).
- The [Simplify New-Grad Positions](https://github.com/SimplifyJobs/New-Grad-Positions) `listings.json` feed (active postings from the last 14 days by default).
- Manual import (`import`) of postings the agent read from employer career pages, other job pages or links the user pasted.
- JD completion (`resolve`) for leads without a description: the four APIs above, plus Workday, Oracle Recruiting Cloud, BambooHR, Jobvite, iCIMS, JazzHR, Rippling and employer-hosted Greenhouse pages. Leads it cannot read are queued (`queue --need-jd`) so the agent can fetch the page and store the text with `set-jd`.

**What the CLI does**

- Prefilters titles and locations before storing, so large feeds never reach the conversation.
- Deduplicates URL variants, apply links and twin postings (same company, title and JD), and skips jobs listed in an external ledger or URL list (`external`).
- Screens JD text with saved evidence: citizenship, security clearance, "no OPT/CPT" and enrollment requirements are blocked; required years or degrees above the limits in `hard_requirements` are hard-blocked; no-sponsorship, contract and experience wording are flagged as risks.
- Serves review batches with job and profile hashes. `record` rejects stale hashes, unknown fact IDs and recommendations without a full JD, apply link or resume version.
- Writes a daily candidate list (`shortlist`), a cumulative list of still-open recommendations (`open`) and a run log (`status`). The lists are generated in Chinese.
- Never applies, fills forms or contacts employers; `mark-applied` and `mark-dropped` only record what the user reports.

**Run it**

Create `.claude-job-search/` (git-ignored), copy `claude_skill/assets/config.example.json` and `claude_skill/assets/profile.example.json` into it as `config.json` and `profile.json`, then replace the example facts with your own. A typical run:

```text
python claude_skill/scripts/cjs.py --workspace .claude-job-search collect
python claude_skill/scripts/cjs.py --workspace .claude-job-search external --input ledger.json
python claude_skill/scripts/cjs.py --workspace .claude-job-search resolve --limit 80
python claude_skill/scripts/cjs.py --workspace .claude-job-search queue --limit 15 --per-company 2
python claude_skill/scripts/cjs.py --workspace .claude-job-search record --input decisions.json
python claude_skill/scripts/cjs.py --workspace .claude-job-search shortlist --out .claude-job-search/daily/2026-01-01.md
python claude_skill/scripts/cjs.py --workspace .claude-job-search open --out .claude-job-search/daily/OPEN_CANDIDATES.md
python claude_skill/scripts/cjs.py --workspace .claude-job-search status
```

`external` is optional. The agent writes `decisions.json` after reading each queued JD; the decision format is in [SKILL.md](claude_skill/SKILL.md#4-screening-and-matching-rules). To load the folder as a skill, copy it to where your Claude client reads skills, for example `.claude/skills/daily-job-search/` in Claude Code.

**Tests**

`claude_skill/tests/test_cjs.py` covers URL identity, prefiltering, screening rules, the queue → record → shortlist flow, every ATS resolver and an end-to-end `collect` through the CLI. Real network access is blocked during the tests; fetchers run against canned API responses. The screening tests include regression cases taken from real job descriptions that were misclassified during daily runs.

## Verify

```text
python -m unittest discover -s tests -v
python -m unittest discover -s claude_skill/tests -v
```

GitHub Actions runs both suites, plus the offline demo, on Ubuntu and Windows with Python 3.10 and 3.13 for every push and pull request.

## 中文使用说明

搜到一堆职位之后，真正花时间的是点开官网确认还在招、读完职位描述，再判断哪些值得投。Job Search Skill 把这些步骤串成一个工作流，帮你把搜索结果整理成一份有依据的投递清单。

适合刚毕业、考虑转行，或者同时探索多个方向的人。你可以按自己的目标搜索金融、运营、项目管理、市场营销或技术岗位，也可以只发一条职位链接，让它帮你判断是否值得申请。

### 它能帮你做什么

- **找职位、核实申请入口。** 从雇主官网、公开招聘系统或你提供的链接收集职位，去除重复，核对职位描述和官方申请入口。
- **说明你为什么匹配。** 用你简历里的项目、实习和技能对照职位要求。比如，指出哪个 PyTorch 项目能证明你做过模型训练和评估。
- **把申请前的问题列清楚。** 标注毕业时间、经验年限、地点和签证赞助（sponsorship）等条件。招聘公告没有说明的内容会标为待确认。
- **给出简历修改方向。** 推荐使用哪一版简历、把哪个项目往前放，以及哪些经历值得突出。`v0.1.0` 提供修改建议，投递由你自行完成。

### 怎么开始

1. 下载并解压项目，在 Codex 中打开包含 `.agents` 的项目根目录。也可以把整个 `.agents/skills/job-search` 文件夹复制到自己项目的同一路径。使用 Claude Code 时，将 `job-search` 文件夹复制到项目的 `.claude/skills/` 下，再用 `/job-search` 调用。 [Claude Code 使用说明](https://code.claude.com/docs/en/skills)
2. 准备一份简历，或者简单列出你的专业、技能、项目和实习。第一次使用时，可以让它先读简历，再问地点、毕业时间或工作授权等缺失信息。要重复使用同一份资料，可以把 [示例配置](.agents/skills/job-search/assets/profile.example.json) 复制为 `.job-search/profile.json` 并填写。
3. 在 Codex 对话框里输入 `$job-search`，告诉它你想找什么。

### Claude 版

仓库里还有一个 Claude 版（`claude_skill/`），用 Claude Code 开发，每天运行一次：从 Greenhouse、Lever、Ashby、SmartRecruiters API、Simplify 列表和手动导入的招聘页面收集职位，按规则筛掉签证和硬性条件不符的岗位，生成中文的每日候选清单。用法、命令和测试见英文部分的 [Claude version](#claude-version-claude_skill)。

### 第一次使用可以这样问

```text
$job-search 我是刚毕业的本科生，想找美国的初级 Financial Analyst、Operations Analyst 或 Project Coordinator 岗位。请根据我的简历帮我找 10 个适合的岗位，缺少筛选条件时可以问我。
```
