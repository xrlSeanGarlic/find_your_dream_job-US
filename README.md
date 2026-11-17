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

## Job-Search Skills
搜到一堆职位之后，真正花时间的是点开官网确认还在招、读完职位描述，再判断哪些值得投。Job Search Skill 把这些步骤串成一个工作流，帮你把搜索结果整理成一份有依据的投递清单。
适合刚毕业、考虑转行，或者同时探索多个方向的人。你可以一次比较 Data Analyst、Software Engineer 和 Machine Learning Engineer，也可以只发一条职位链接，让它帮你判断是否值得申请。

## 它能帮你做什么

找职位、核实申请入口。 从雇主官网、公开招聘系统或你提供的链接收集职位，去除重复，核对职位描述和官方申请入口。
说明你为什么匹配。 用你简历里的项目、实习和技能对照职位要求。比如，明确指出哪个 PyTorch 项目能证明你做过模型训练和评估。
把申请前的问题列清楚。 标注毕业时间、经验年限、地点和 sponsorship 等条件。招聘公告没有说明的内容会标为待确认。
给出简历修改方向。 推荐使用哪一版简历、把哪个项目往前放，以及哪些 bullet points 值得突出。`v0.1.0` 提供修改建议，投递由你自行完成。

## 怎么开始

下载并解压项目，在 Codex/Claude 中打开包含 .agents 文件夹的目录。也可以把整个 .agents/skills/job-search 文件夹复制到自己项目的同一路径。
准备一份简历，或者简单列出你的专业、技能、项目和实习。第一次使用时，可以让它先读简历，再问地点、毕业时间或工作授权等缺失信息。需要持续使用同一份资料时，可把 profile.example.json 复制为 .job-search/profile.json 并填写。
在 Codex 对话框里输入 $job-search，告诉它你想找什么。

## 第一次使用可以这样问

$job-search 我是刚毕业的本科生，想找美国的初级 Financial Analyst、Operations Analyst 或 Project Coordinator 岗位。
请先根据我的简历确认背景，帮我找X个适合的岗位，如果需要可以询问缺失的筛选条件。
