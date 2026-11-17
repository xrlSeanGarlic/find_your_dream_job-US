"""Collect public job postings and track evidence-bound reviews locally."""
import argparse
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


CHECKS = ('opening_checked', 'entry_checked', 'jd_complete')
RECOMMENDED = ('strong', 'stretch')
DISPOSITIONS = (*RECOMMENDED, 'hold', 'skip')


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return sha256(dump(value).encode('utf-8')).hexdigest()


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def now():
    return datetime.now(timezone.utc)


def string(value, field, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f'{field} must be {"a nonempty string" if required else "a string"}')
    return value.strip()


def strings(value, field):
    if not isinstance(value, list):
        raise ValueError(f'{field} must be a list of strings')
    return [string(item, field, required=True) for item in value]


def public_url(url):
    url = string(url, 'URL', required=True)
    if re.search(r'[\s<>\\]', url):
        raise ValueError('Use an encoded HTTP(S) posting URL')
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Use an HTTP(S) posting URL without credentials')
    return parts


def identity(url):
    parts = public_url(url)
    host = parts.hostname.lower()
    path = parts.path.rstrip('/')
    if host in ('jobs.ashbyhq.com', 'jobs.lever.co'):
        match = re.fullmatch(r'/([^/]+)/([^/]+)(?:/(?:application|apply))?', path)
        if match:
            return f'{host}/{match[1].casefold()}/{match[2].casefold()}'
    if host in ('boards.greenhouse.io', 'job-boards.greenhouse.io'):
        match = re.fullmatch(r'/([^/]+)/jobs/(\d+)(?:/apply)?', path)
        if match:
            return f'greenhouse:{match[1].casefold()}:{match[2]}'
    if host.endswith('.myworkdayjobs.com'):
        path = re.sub(r'^/[a-z]{2}-[a-z]{2}/', '/', path.casefold()).removesuffix('/apply')
    query = [(key, value) for key, value in parse_qsl(parts.query)
             if not key.lower().startswith('utm_') and key.lower() not in {'source', 'ref', 'gh_src'}]
    return urlunsplit(('https', parts.netloc.lower(), path, urlencode(sorted(query)), ''))


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def text_of(html):
    parser = PlainHTML()
    parser.feed(unescape(html or ''))
    parser.close()
    return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())


def get_json(url):
    request = Request(url, headers={'User-Agent': 'job-search-skill/0.1', 'Accept': 'application/json'})
    with urlopen(request, timeout=20) as response:
        limit = 20_000_000
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Public board response exceeded the size limit')
    return json.loads(raw)


def posting(company, title, url, description, location='', apply_url='', status='unknown', source='manual'):
    return dict(company=company, title=title, listing_url=url, description=description,
                location=location, apply_url=apply_url, status=status, source=source)


def fetch_board(board, company, get=get_json):
    provider, _, slug = board.partition(':')
    if provider not in {'greenhouse', 'lever', 'ashby'} or not re.fullmatch(r'[A-Za-z0-9_.-]+', slug) or slug in ('.', '..'):
        raise ValueError('Use greenhouse:SLUG, lever:SLUG or ashby:SLUG')
    company = string(company, 'company', required=True)
    if provider == 'greenhouse':
        jobs = get(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true')['jobs']
        return [posting(company, item['title'], item['absolute_url'], text_of(item.get('content')),
                        (item.get('location') or {}).get('name') or '', item['absolute_url'], 'listed', board)
                for item in jobs]
    if provider == 'ashby':
        jobs = get(f'https://api.ashbyhq.com/posting-api/job-board/{slug}')['jobs']
        return [posting(company, item['title'], item['jobUrl'],
                        item.get('descriptionPlain') or text_of(item.get('descriptionHtml')),
                        item.get('location') or '', item.get('applyUrl') or '', 'listed', board)
                for item in jobs if item.get('isListed') is not False]
    records = []
    page_size = 100
    for offset in range(0, 2000, page_size):
        entries = get(f'https://api.lever.co/v0/postings/{slug}?mode=json&limit={page_size}&skip={offset}')
        for item in entries:
            description = '\n'.join([
                item.get('descriptionPlain') or text_of(item.get('description')),
                *(f"{group.get('text', '')}\n{text_of(group.get('content'))}" for group in (item.get('lists') or [])),
                item.get('additionalPlain') or text_of(item.get('additional')),
            ]).strip()
            records.append(posting(company, item['text'], item['hostedUrl'], description,
                                   (item.get('categories') or {}).get('location') or '',
                                   item.get('applyUrl') or '', 'listed', board))
        if len(entries) < page_size:
            return records
    raise ValueError('Lever board exceeded the page budget; existing workspace data was kept')


def open_database(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(workspace / 'jobs.sqlite3')
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, job_sha TEXT NOT NULL,
            first_seen TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY, job_sha TEXT NOT NULL, profile_sha TEXT NOT NULL,
            reviewed_at TEXT NOT NULL, payload TEXT NOT NULL
        );
    """)
    return conn


def normalize(item):
    job = {key: string(item.get(key, ''), key, required=key in ('company', 'title', 'listing_url'))
           for key in ('company', 'title', 'listing_url', 'description', 'location', 'apply_url')}
    job['status'] = item.get('status', 'unknown')
    if job['status'] not in ('listed', 'unknown', 'closed'):
        raise ValueError('status must be listed, unknown or closed')
    key = identity(job['listing_url'])
    if job['apply_url']:
        public_url(job['apply_url'])
    job['source'] = string(item.get('source', 'manual'), 'source', required=True)
    return key, job


def job_digest(key, job):
    return digest({k: v for k, v in job.items() if k not in ('source', 'listing_url')} |
                  {'listing_identity': key, 'apply_url': identity(job['apply_url']) if job['apply_url'] else ''})


def collect(conn, records, board=None):
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError('Job input must be a JSON list of posting objects')
    by_id = {}
    for item in records:
        key, job = normalize(item)
        old = by_id.get(key)
        if old and old['status'] != job['status']:
            raise ValueError(f'Conflicting listing statuses for {key}')
        if old is None or len(job['description']) > len(old['description']):
            by_id[key] = job
    counts = dict(new=0, changed=0, unchanged=0, no_longer_listed=0)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for key, job in by_id.items():
            previous = conn.execute('SELECT job_sha, payload FROM jobs WHERE id = ?', (key,)).fetchone()
            if previous:
                old = json.loads(previous['payload'])
                if not job['description'] and job['status'] == 'unknown' and old['description']:
                    counts['unchanged'] += 1
                    continue
            hashed = job_digest(key, job)
            if previous and previous['job_sha'] == hashed:
                if board and old['source'] != board:
                    conn.execute('UPDATE jobs SET payload=? WHERE id=?', (dump(job), key))
                counts['unchanged'] += 1
                continue
            conn.execute("""
                INSERT INTO jobs (id, payload, job_sha, first_seen) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, job_sha=excluded.job_sha
            """, (key, dump(job), hashed, now().isoformat()))
            if previous:
                conn.execute('DELETE FROM reviews WHERE id=?', (key,))
            counts['changed' if previous else 'new'] += 1
        if board:
            for row in conn.execute('SELECT id, payload FROM jobs').fetchall():
                job = json.loads(row['payload'])
                if job['source'] == board and job['status'] == 'listed' and row['id'] not in by_id:
                    job['status'] = 'unknown'
                    conn.execute('UPDATE jobs SET payload=?, job_sha=? WHERE id=?',
                                 (dump(job), job_digest(row['id'], job), row['id']))
                    conn.execute('DELETE FROM reviews WHERE id=?', (row['id'],))
                    counts['no_longer_listed'] += 1
    return counts | {'duplicate_inputs': len(records) - len(by_id)}


def profile_at(workspace):
    profile = load(workspace / 'profile.json')
    if not isinstance(profile, dict):
        raise ValueError('profile.json must contain a JSON object')
    facts = profile.get('facts')
    if not isinstance(facts, dict) or not facts:
        raise ValueError('profile.json must contain at least one fact with a stable ID')
    for key, fact in facts.items():
        string(key, 'fact ID', required=True)
        string(fact, 'fact text', required=True)
    strings(profile.get('target_keywords', []), 'target_keywords')
    return profile


def current_review(row, profile_sha, cutoff):
    return (row['job_sha'] == row['review_job_sha'] and profile_sha == row['profile_sha']
            and row['reviewed_at'] >= cutoff)


def job_rows(conn):
    return conn.execute("""
        SELECT jobs.*, reviews.job_sha AS review_job_sha, profile_sha, reviewed_at,
               reviews.payload AS review_payload
        FROM jobs LEFT JOIN reviews USING(id) WHERE applied=0
        ORDER BY first_seen, id
    """)


def queue(conn, profile, limit):
    if limit < 1:
        raise ValueError('Queue limit must be positive')
    cutoff = (now() - timedelta(days=7)).isoformat()
    profile_sha = digest(profile)
    keywords = [item.casefold() for item in profile.get('target_keywords', [])]
    candidates = []
    for row in job_rows(conn):
        job = json.loads(row['payload'])
        if job['status'] == 'closed' or current_review(row, profile_sha, cutoff):
            continue
        title = job['title'].casefold()
        score = sum(keyword in title for keyword in keywords)
        candidates.append((score, row['first_seen'], job | {'job_id': row['id'], 'job_sha256': row['job_sha']}))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    employers = {}
    for _, _, job in candidates:
        employers.setdefault(job['company'].casefold(), deque()).append(job)
    selected = []
    rings = deque(employers.values())
    while rings and len(selected) < limit:
        employer = rings.popleft()
        selected.append(employer.popleft())
        if employer:
            rings.append(employer)
    return {'profile_sha256': profile_sha, 'pending': len(candidates), 'jobs': selected}


def record(conn, profile, reviews):
    if not isinstance(reviews, list) or any(not isinstance(review, dict) for review in reviews):
        raise ValueError('Review input must be a JSON list of decision objects')
    ids = set()
    profile_sha = digest(profile)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for decision in reviews:
            key = string(decision.get('job_id'), 'job_id', required=True)
            if key in ids:
                raise ValueError(f'Duplicate decision for {key}')
            ids.add(key)
            payload = conn.execute('SELECT * FROM jobs WHERE id = ? AND applied=0', (key,)).fetchone()
            if not payload or decision.get('job_sha256') != payload['job_sha'] or decision.get('profile_sha256') != profile_sha:
                raise ValueError(f'Stale or missing evidence for {key}. Read a fresh queue.')
            job = json.loads(payload['payload'])
            review = {name: strings(decision.get(name, []), name) for name in ('fact_ids', 'gaps', 'followups', 'blockers')}
            review['reason'] = string(decision.get('reason'), 'reason', required=True)
            review['resume'] = string(decision.get('resume', ''), 'resume')
            review['disposition'] = decision.get('disposition')
            if review['disposition'] not in DISPOSITIONS or not set(review['fact_ids']) <= profile['facts'].keys():
                raise ValueError(f'Invalid disposition or candidate fact reference for {key}')
            saved_at = now()
            if decision.get('checked_at') or review['disposition'] in RECOMMENDED:
                raw_date = string(decision.get('checked_at'), 'checked_at', required=True)
                checked = datetime.fromisoformat(raw_date[:-1] + '+00:00' if raw_date.endswith('Z') else raw_date)
                if (checked.tzinfo is None or checked > saved_at + timedelta(minutes=1)
                        or checked < saved_at - timedelta(days=7)):
                    raise ValueError('Use the actual check time in the last seven days with its UTC offset')
                saved_at = checked.astimezone(timezone.utc)
            for check in CHECKS:
                review[check] = decision.get(check, False)
                if type(review[check]) is not bool:
                    raise ValueError(f'{check} must be a boolean')
            if review['disposition'] in RECOMMENDED and (
                job['status'] != 'listed' or not job['description'] or not job['apply_url']
                or review['blockers'] or not review['fact_ids'] or not all(review[check] for check in CHECKS)
            ):
                raise ValueError(f'Recommendation requires a verified opening, complete JD, entry and matching facts for {key}')
            conn.execute("""
                INSERT INTO reviews VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET job_sha=excluded.job_sha, profile_sha=excluded.profile_sha,
                    reviewed_at=excluded.reviewed_at, payload=excluded.payload
            """, (key, payload['job_sha'], profile_sha, saved_at.isoformat(), dump(review)))
    return {'recorded': len(ids)}


def markdown(text):
    text = ' '.join(text.split())
    return re.sub(r'([\\\x60*_[\]<>|])', r'\\\1', text)


def report(conn, profile):
    groups = {name: [] for name in DISPOSITIONS}
    pending = 0
    cutoff = (now() - timedelta(days=7)).isoformat()
    profile_sha = digest(profile)
    for row in job_rows(conn):
        job = json.loads(row['payload'])
        if job['status'] == 'closed':
            continue
        if not current_review(row, profile_sha, cutoff):
            pending += 1
            continue
        review = json.loads(row['review_payload'])
        groups[review['disposition']].append((job, review))
    lines = ['# Job search report', '',
             f"{len(groups['strong'])} strong · {len(groups['stretch'])} stretch · "
             f"{len(groups['hold'])} hold · {len(groups['skip'])} skip · {pending} due for review.",
             '', 'Resume entries are suggestions. Confirm opening and eligibility before applying.']
    for level in ('strong', 'stretch', 'hold'):
        if not groups[level]:
            continue
        lines.extend(['', '## ' + level.title(), ''])
        for job, review in groups[level]:
            url = job['apply_url'] if level in RECOMMENDED else job['listing_url']
            label = markdown(job['company'] + ' — ' + job['title'])
            lines.append(f"- [{label}](<{url}>) · {markdown(job['location'] or 'Location unspecified')}")
            for label, value in (
                ('Fit', review['reason']), ('Facts', ', '.join(review['fact_ids'])),
                ('Gaps', '; '.join(review['gaps'])), ('Follow up', '; '.join(review['followups'])),
                ('Blockers', '; '.join(review['blockers'])), ('Resume', review['resume']),
            ):
                if value:
                    lines.append(f'  - **{label}** — {markdown(value)}')
    return '\n'.join(lines) + '\n'


def mark_applied(conn, url):
    key = identity(url)
    with conn:
        cursor = conn.execute('UPDATE jobs SET applied=1 WHERE id=?', (key,))
        if not cursor.rowcount:
            raise ValueError('Posting was not found. Use the saved listing URL.')
    return {'applied': key}


def demo(workspace):
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError('Use a new or empty workspace for the offline demo')
    example = load(Path(__file__).resolve().parents[1] / 'assets/profile.example.json')
    with closing(open_database(workspace)) as conn:
        (workspace / 'profile.json').write_text(dump(example) + '\n', encoding='utf-8')
        url = 'https://employer.example.test/jobs/analyst'
        job = posting('Fictional Research Lab', 'Research Data Analyst', url,
                      'Responsibilities: check research datasets with SQL and document anomalies. '
                      'Qualifications: SQL joins and clear reports. Coursework is accepted.',
                      'Remote', url + '/apply', 'listed')
        collect(conn, [job, job | {'listing_url': url + '?utm_source=fixture'},
                       posting('Fictional Studio', 'AI Evaluator', 'https://studio.example.test/jobs/eval', '')])
        batch = queue(conn, example, 15)
        decisions = []
        for candidate in batch['jobs']:
            strong = bool(candidate['description'])
            decisions.append({key: candidate[key] for key in ('job_id', 'job_sha256')} |
                             {'profile_sha256': batch['profile_sha256'],
                              'disposition': 'strong' if strong else 'hold',
                              'reason': 'The example SQL project covers the stated duties.' if strong else 'Verify the full listing.',
                              'fact_ids': ['sql-coursework'] if strong else [],
                              'resume': 'Data analysis version' if strong else '',
                              'checked_at': now().isoformat(),
                              **{key: strong for key in CHECKS}})
        record(conn, example, decisions)
        output = '> Offline demonstration using fictional postings.\n\n' + report(conn, example)
    (workspace / 'report.md').write_text(output, encoding='utf-8')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path('.job-search'))
    commands = parser.add_subparsers(dest='command', required=True)
    ingest = commands.add_parser('collect')
    source = ingest.add_mutually_exclusive_group(required=True)
    source.add_argument('--board')
    source.add_argument('--input', type=Path)
    ingest.add_argument('--company')
    commands.add_parser('queue').add_argument('--limit', type=int, default=15)
    commands.add_parser('record').add_argument('--input', type=Path, required=True)
    commands.add_parser('report').add_argument('--out', type=Path)
    commands.add_parser('mark-applied').add_argument('--url', required=True)
    commands.add_parser('demo')
    args = parser.parse_args()
    try:
        if args.command == 'demo':
            print(demo(args.workspace), end='')
            return 0
        with closing(open_database(args.workspace)) as conn:
            if args.command == 'collect':
                result = collect(conn, fetch_board(args.board, args.company) if args.board else load(args.input), args.board)
            elif args.command == 'mark-applied':
                result = mark_applied(conn, args.url)
            else:
                profile = profile_at(args.workspace)
                if args.command == 'queue':
                    result = queue(conn, profile, args.limit)
                elif args.command == 'record':
                    result = record(conn, profile, load(args.input))
                else:
                    output = report(conn, profile)
                    if args.out:
                        args.out.write_text(output, encoding='utf-8')
                    print(output, end='')
                    return 0
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f'job-search: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    sys.exit(main())
