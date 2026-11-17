# Job Search Skill

[中文使用说明](#中文使用说明)

Job Search Skill helps you turn a pile of listings into a shortlist worth acting on. Tell Codex which roles and locations you're targeting, and it will look for openings, check official application links, and explain how each role lines up with your experience.

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

## Verify

```text
python -m unittest discover -s tests -v
```

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

### 第一次使用可以这样问

```text
$job-search 我是刚毕业的本科生，想找美国的初级 Financial Analyst、Operations Analyst 或 Project Coordinator 岗位。请根据我的简历帮我找 10 个适合的岗位，缺少筛选条件时可以问我。
```
