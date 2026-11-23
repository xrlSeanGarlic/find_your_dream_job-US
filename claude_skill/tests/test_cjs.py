from contextlib import redirect_stdout
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import cjs  # noqa: E402

WS_SRC = ROOT / 'assets'


def ws():
    d = Path(tempfile.mkdtemp())
    for f in ('config', 'profile'):
        shutil.copy(WS_SRC / f'{f}.example.json', d / f'{f}.json')
    return d


def _no_network(req, *a, **k):
    url = getattr(req, 'full_url', req)
    raise AssertionError(f'tests must not touch the network: {url}')


_real_urlopen = cjs.urlopen


def setUpModule():
    # every fetcher goes through cjs.urlopen; block it so no test can reach a real server
    cjs.urlopen = _no_network


def tearDownModule():
    cjs.urlopen = _real_urlopen


class Identity(unittest.TestCase):
    def test_greenhouse_variants(self):
        a = cjs.identity('https://boards.greenhouse.io/figureai/jobs/123?gh_src=x')
        b = cjs.identity('https://job-boards.greenhouse.io/figureai/jobs/123/apply')
        self.assertEqual(a, b)
        self.assertEqual(a, 'greenhouse:figureai:123')

    def test_lever_ashby(self):
        self.assertEqual(cjs.identity('https://jobs.lever.co/Acme/abc-1/apply'), 'jobs.lever.co/acme/abc-1')
        self.assertEqual(cjs.identity('https://jobs.ashbyhq.com/1x/UUID/application'), 'jobs.ashbyhq.com/1x/uuid')

    def test_tracking_params_dropped(self):
        self.assertEqual(cjs.identity('https://x.com/j/1?utm_source=a&nl=1&id=5'), cjs.identity('https://www.x.com/j/1/?id=5'))


class Prefilter(unittest.TestCase):
    def setUp(self):
        self.c = cjs.compile_cfg(json.loads((WS_SRC / 'config.example.json').read_text(encoding='utf-8')))

    def test_lanes(self):
        self.assertEqual(cjs.prefilter('Robot Data Collection Operator', 'Mountain View, CA', self.c)[:2], (True, ['C']))
        self.assertTrue(cjs.prefilter('Data Analyst', 'Remote - US', self.c)[0])
        self.assertIn('B', cjs.prefilter('Machine Learning Engineer, New Grad', 'San Diego, CA', self.c)[1])

    def test_excludes(self):
        self.assertFalse(cjs.prefilter('Senior Data Analyst', 'Austin, TX', self.c)[0])
        self.assertFalse(cjs.prefilter('Software Engineering Intern', 'Seattle, WA', self.c)[0])
        self.assertFalse(cjs.prefilter('Data Analyst', 'London, UK', self.c)[0])
        self.assertFalse(cjs.prefilter('Account Executive', 'New York, NY', self.c)[0])

    def test_state_codes_case_sensitive(self):
        self.assertFalse(cjs.prefilter('Data Analyst', 'Singapore / Ang Mo Kio', self.c)[0])
        self.assertTrue(cjs.prefilter('Data Analyst', 'Salem, OR', self.c)[0])
        self.assertTrue(cjs.prefilter('Data Analyst', 'Onsite- Salem, OR', self.c)[0])

    def test_unknown_location_kept(self):
        keep, _, note = cjs.prefilter('Data Analyst', 'Remote', self.c)
        self.assertTrue(keep)
        self.assertEqual(note, 'location unknown')


class Screen(unittest.TestCase):
    def ids(self, text):
        return {h['rule']: h['severity'] for h in cjs.screen(text)}

    def test_blocks(self):
        self.assertEqual(self.ids('Applicants must be U.S. citizens due to ITAR.')['citizenship_required'], 'block')
        self.assertEqual(self.ids('An active Secret clearance is required.')['clearance_required'], 'block')
        self.assertEqual(self.ids('Candidates must be currently enrolled in a degree program.')['enrolled_student_required'], 'block')
        self.assertEqual(self.ids('We do not accept OPT or CPT candidates.')['no_opt_or_f1'], 'block')

    def test_risks_not_blocks(self):
        r = self.ids('We are unable to sponsor visas. 5+ years of experience in analytics.')
        self.assertEqual(r['no_sponsorship'], 'risk')
        self.assertEqual(r['experience_5plus'], 'risk')
        self.assertNotIn('citizenship_required', r)

    def test_clean(self):
        self.assertEqual(self.ids('Work with SQL and Python to build dashboards for operations.'), {})

    def test_visa_risk(self):
        self.assertEqual(cjs.visa_risk_from(cjs.screen('We will not sponsor work visas.')), 'high')
        self.assertEqual(cjs.visa_risk_from(cjs.screen('We participate in E-Verify.')), 'low')
        self.assertEqual(cjs.visa_risk_from([]), 'unknown')


def job(**kw):
    base = dict(company='Acme Robotics', title='Robot Data Collection Operator',
                url='https://boards.greenhouse.io/acme/jobs/1',
                description='Collect teleoperation data for robot learning. Record episodes and QA.',
                location='San Diego, CA', apply_url='https://boards.greenhouse.io/acme/jobs/1', source='greenhouse:acme')
    base.update(kw)
    return cjs.rec(base['company'], base['title'], base['url'], base['description'], base['location'],
                   base['apply_url'], '', base['source'])


class Flow(unittest.TestCase):
    def setUp(self):
        self.ws = ws()
        self.cfg = json.loads((self.ws / 'config.json').read_text(encoding='utf-8'))
        self.c = cjs.compile_cfg(self.cfg)
        self.conn = cjs.open_db(self.ws)
        self.profile = cjs.profile_at(self.ws)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.ws)

    def test_dedupe_and_merge(self):
        r1 = job()
        r2 = job(url='https://job-boards.greenhouse.io/acme/jobs/1?utm_source=simplify', description='', source='simplify')
        out = cjs.store(self.conn, [r1], self.c)
        self.assertEqual(out['new'], 1)
        out = cjs.store(self.conn, [r2], self.c)
        self.assertEqual(out['new'], 0)
        p = json.loads(self.conn.execute('SELECT payload FROM jobs').fetchone()[0])
        self.assertIn('Collect teleoperation', p['description'])  # richer description kept
        self.assertEqual(p['sources'], ['greenhouse:acme', 'simplify'])

    def test_apply_url_alias_merges(self):
        cjs.store(self.conn, [job(url='https://acme.com/careers/robot-op', apply_url='https://boards.greenhouse.io/acme/jobs/9')], self.c)
        cjs.store(self.conn, [job(url='https://boards.greenhouse.io/acme/jobs/9')], self.c)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_rule_block(self):
        cjs.store(self.conn, [job(description='Must be a U.S. citizen. Robot data collection.')], self.c)
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'blocked')

    def test_external_before_and_after_collect(self):
        led = self.ws / 'ledger.json'
        led.write_text(json.dumps({'items': [{'apply_url': 'https://boards.greenhouse.io/acme/jobs/1', 'verified_aliases': [],
                                              'identity': 'greenhouse:acme:1'}]}))
        cjs.import_external(self.conn, led)
        cjs.store(self.conn, [job()], self.c)
        self.assertEqual(cjs.mark_external(self.conn), 1)
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'external_seen')

    def test_authoritative_refresh_marks_missing(self):
        cjs.store(self.conn, [job()], self.c, 'greenhouse:acme')
        out = cjs.store(self.conn, [], self.c, 'greenhouse:acme')
        self.assertEqual(out['no_longer_listed'], 1)

    def review(self, item, q, **kw):
        d = {'job_id': item['job_id'], 'job_sha256': item['job_sha256'], 'profile_sha256': q['profile_sha256'],
             'disposition': 'strong', 'reason': 'Teleop data collection matches the teleop project.', 'fact_ids': ['teleop_project'],
             'resume': 'C', 'visa_risk': 'unknown', 'score': 8, 'opening_checked': True, 'jd_complete': True}
        d.update(kw)
        return d

    def test_queue_record_shortlist(self):
        cjs.store(self.conn, [job(), job(company='Beta', url='https://boards.greenhouse.io/beta/jobs/2',
                                         apply_url='https://boards.greenhouse.io/beta/jobs/2', title='Data Analyst')], self.c)
        cfg = dict(self.cfg, lane_priority=['C', 'B', 'A'])
        q = cjs.queue(self.conn, self.profile, cfg, 15, 2)
        self.assertEqual(len(q['jobs']), 2)
        self.assertEqual(q['jobs'][0]['lanes'], ['C'])  # first lane in lane_priority comes first
        q2 = cjs.queue(self.conn, self.profile, dict(self.cfg, lane_priority=['A', 'B', 'C']), 15, 2)
        self.assertEqual(q2['jobs'][0]['lanes'], ['A'])
        decs = [self.review(q['jobs'][0], q),
                self.review(q['jobs'][1], q, resume='A', visa_risk='high', disposition='stretch', score=6,
                            fact_ids=['sql'], visa_note='JD says no sponsorship')]
        cjs.record(self.conn, self.profile, decs)
        text, st = cjs.shortlist(self.conn, self.profile, 0, '2026-11-20', minimum=3)
        self.assertEqual(st['listed'], 1)  # no upper cap; floor of 3 not met
        self.assertTrue(st['below_minimum'])
        self.assertIn('低于下限 3', text)
        self.assertEqual(st['held'], 1)
        self.assertIn('C 机器人数据', text)
        self.assertIn('暂不推荐', text)
        # next day: already shortlisted job not repeated
        _, st2 = cjs.shortlist(self.conn, self.profile, 0, '2026-11-21')
        self.assertEqual(st2['listed'], 0)
        # reviewed jobs leave the queue
        self.assertEqual(cjs.queue(self.conn, self.profile, self.cfg, 15, 2)['jobs'], [])

    def test_rescreen_respects_reviews(self):
        cjs.store(self.conn, [job(), job(company='Beta', url='https://boards.greenhouse.io/beta/jobs/2',
                                         apply_url='https://boards.greenhouse.io/beta/jobs/2', title='Data Analyst')], self.c)
        q = cjs.queue(self.conn, self.profile, self.cfg, 15, 2)
        cjs.record(self.conn, self.profile, [self.review(q['jobs'][0], q)])
        # both JDs now say 6+ years required (as if the rule were new); only the unreviewed one gets blocked
        self.conn.execute("UPDATE jobs SET payload=json_set(payload,'$.description','You have:\n6+ years of experience required.')")
        out = cjs.rescreen(self.conn, self.profile)
        self.assertEqual(out['newly_blocked'], 1)
        states = dict(self.conn.execute('SELECT id, state FROM jobs').fetchall())
        self.assertEqual(states[q['jobs'][0]['job_id']], 'new')
        self.assertEqual(states[q['jobs'][1]['job_id']], 'blocked')

    def test_open_candidates(self):
        cjs.store(self.conn, [job(), job(company='Beta', url='https://boards.greenhouse.io/beta/jobs/2',
                                         apply_url='https://boards.greenhouse.io/beta/jobs/2', title='Data Analyst'),
                              job(company='Gamma', url='https://boards.greenhouse.io/gamma/jobs/3',
                                  apply_url='https://boards.greenhouse.io/gamma/jobs/3', title='Data Analyst')], self.c)
        q = cjs.queue(self.conn, self.profile, self.cfg, 15, 2)
        cjs.record(self.conn, self.profile, [self.review(it, q) for it in q['jobs']])
        cjs.shortlist(self.conn, self.profile, 0, '2026-11-20')
        text, st = cjs.open_candidates(self.conn, '2026-11-21')
        self.assertEqual(st['open'], 3)
        self.assertIn('首次列出', text)
        cjs.mark_applied(self.conn, 'https://boards.greenhouse.io/beta/jobs/2')
        cjs.mark_dropped(self.conn, 'https://boards.greenhouse.io/gamma/jobs/3')
        text, st = cjs.open_candidates(self.conn, '2026-11-21')
        self.assertEqual(st['open'], 1)
        self.assertNotIn('Beta', text)
        self.assertNotIn('Gamma', text)
        _, st = cjs.open_candidates(self.conn, '2027-01-30')  # review far older than 14 days
        self.assertEqual((st['open'], st['aged_out']), (0, 1))

    def test_twin_postings_collapse(self):
        jd = 'Analytics Engineer. Build dashboards with Python and SQL for utility customers. ' * 5
        twins = [job(company='Tantalus', title='Analytics Engineer', description=jd,
                     url=f'https://tantalus.bamboohr.com/careers/{n}', apply_url=f'https://tantalus.bamboohr.com/careers/{n}',
                     source='bamboohr') for n in (194, 195, 196)]
        cjs.store(self.conn, twins, self.c)
        q = cjs.queue(self.conn, self.profile, self.cfg, 15, 5)
        cjs.record(self.conn, self.profile, [self.review(it, q, resume='A', fact_ids=['sql']) for it in q['jobs']])
        _, st = cjs.shortlist(self.conn, self.profile, 0, '2026-11-20')
        self.assertEqual((st['listed'], st['duplicate']), (1, 2))
        text, st = cjs.open_candidates(self.conn, '2026-11-21')
        self.assertEqual(st['open'], 1)
        # applying to the listed one also hides its twins (even ones never shortlisted)
        listed = self.conn.execute('SELECT id FROM shortlisted').fetchone()[0]
        self.conn.execute("UPDATE jobs SET state='applied' WHERE id=?", (listed,))
        _, st = cjs.shortlist(self.conn, self.profile, 0, '2026-11-22')
        self.assertEqual(st['listed'], 0)
        _, st = cjs.open_candidates(self.conn, '2026-11-22')
        self.assertEqual(st['open'], 0)

    def test_learning_flag(self):
        cjs.store(self.conn, [job(), job(company='Beta', url='https://boards.greenhouse.io/beta/jobs/2',
                                         apply_url='https://boards.greenhouse.io/beta/jobs/2', title='Data Analyst')], self.c)
        q = cjs.queue(self.conn, self.profile, self.cfg, 15, 2)
        cjs.record(self.conn, self.profile, [self.review(it, q, learning=['Tableau/Power BI']) for it in q['jobs']])
        day_text, _ = cjs.shortlist(self.conn, self.profile, 0, '2026-11-20')
        self.assertIn('在学标记：Tableau/Power BI', day_text)
        text, st = cjs.open_candidates(self.conn, '2026-11-21')
        self.assertEqual(st['learning'], {'Tableau/Power BI': 2})
        self.assertIn('先补哪个技能', text)
        self.assertIn('· 在学', text)

    def test_custom_resume_needs_note(self):
        cjs.store(self.conn, [job()], self.c)
        q = cjs.queue(self.conn, self.profile, self.cfg, 15, 2)
        with self.assertRaises(ValueError):
            cjs.record(self.conn, self.profile, [self.review(q['jobs'][0], q, resume='custom')])
        cjs.record(self.conn, self.profile, [self.review(q['jobs'][0], q, resume='custom',
                                                         resume_note='base C; move teleop_project bullet first')])

    def test_record_validation(self):
        cjs.store(self.conn, [job()], self.c)
        q = cjs.queue(self.conn, self.profile, self.cfg, 15, 2)
        with self.assertRaises(ValueError):
            cjs.record(self.conn, self.profile, [self.review(q['jobs'][0], q, fact_ids=['made_up'])])
        with self.assertRaises(ValueError):
            cjs.record(self.conn, self.profile, [self.review(q['jobs'][0], q, resume='')])
        with self.assertRaises(ValueError):
            cjs.record(self.conn, self.profile, [self.review(q['jobs'][0], q, job_sha256='stale')])

    def test_need_jd_and_set_jd(self):
        cjs.store(self.conn, [job(description='')], self.c)
        q = cjs.queue(self.conn, self.profile, self.cfg, 15, 2, need_jd=True)
        self.assertEqual(len(q['jobs']), 1)
        cjs.set_description(self.conn, [{'job_id': q['jobs'][0]['job_id'], 'description': 'Robot data. U.S. citizenship is required.'}], self.c)
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'blocked')

    def test_refilter(self):
        cjs.store(self.conn, [job(title='Senior Robot Operator')], self.c)
        self.assertEqual(cjs.refilter(self.conn, self.c), 1)
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'filtered')

    def test_mark_applied(self):
        cjs.store(self.conn, [job()], self.c)
        cjs.mark_applied(self.conn, 'https://job-boards.greenhouse.io/acme/jobs/1/apply')
        self.assertEqual(self.conn.execute('SELECT state FROM jobs').fetchone()[0], 'applied')



class ExtraATS(unittest.TestCase):
    """Workday / Oracle / BambooHR / Jobvite / iCIMS / JazzHR / Rippling fill, with fake fetchers (no network)."""

    def job(self, url):
        return {'listing_url': url, 'apply_url': url}

    def test_workday_url_to_cxs(self):
        u = 'https://mtb.wd5.myworkdayjobs.com/en-US/Campus/job/Buffalo-NY/Dev-Program_R92035'
        self.assertEqual(cjs.workday_api(u),
                         'https://mtb.wd5.myworkdayjobs.com/wday/cxs/mtb/Campus/job/Buffalo-NY/Dev-Program_R92035')
        self.assertEqual(cjs.workday_api(u + '/apply'), cjs.workday_api(u))
        self.assertTrue(cjs.api_resolvable(u))

    def test_workday_description_and_closed(self):
        calls = []

        def get(url):
            calls.append(url)
            return {'jobPostingInfo': {'title': 'X', 'jobDescription': '<p>Build <b>robots</b></p>', 'canApply': True}}
        out = cjs.resolve_ext(self.job('https://acme.wd1.myworkdayjobs.com/Ext/job/LA/X_R1'), get=get, get_html=None)
        self.assertEqual(out, {'description': 'Build robots'})
        self.assertIn('/wday/cxs/acme/Ext/job/LA/X_R1', calls[0])
        closed = cjs.resolve_ext(self.job('https://acme.wd1.myworkdayjobs.com/Ext/job/LA/X_R1'),
                                 get=lambda u: {'jobPostingInfo': {'jobDescription': '<p>x</p>', 'canApply': False}},
                                 get_html=None)
        self.assertEqual(closed['status'], 'closed')

    def test_oracle(self):
        u = 'https://emit.fa.ca3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/job/98898'
        api = cjs.oracle_api(u)
        self.assertIn('finder=ById;Id=%2298898%22,siteNumber=CX_2001', api)
        out = cjs.resolve_ext(self.job(u), get=lambda x: {'items': [{'ExternalDescriptionStr': '<p>Analyze data</p>',
                                                                      'ExternalQualificationsStr': '<p>SQL</p>'}]},
                              get_html=None)
        self.assertEqual(out['description'], 'Analyze data\nSQL')
        self.assertEqual(cjs.resolve_ext(self.job(u), get=lambda x: {'items': []}, get_html=None),
                         {'description': '', 'status': 'closed'})

    def test_bamboohr(self):
        seen = []

        def get(url):
            seen.append(url)
            return {'result': {'jobOpening': {'description': '<p>Data Scientist duties</p>'}}}
        out = cjs.resolve_ext(self.job('https://contextlabs.bamboohr.com/careers/187/'), get=get, get_html=None)
        self.assertEqual(out['description'], 'Data Scientist duties')
        self.assertEqual(seen, ['https://contextlabs.bamboohr.com/careers/187/detail'])

    def test_ld_json_pages(self):
        html = ('<html><script type="application/ld+json">{"@type":"JobPosting","title":"SWE",'
                '"description":"&lt;p&gt;Write tests&lt;/p&gt;"}</script></html>')
        for u in ('https://jobs.jobvite.com/laborie/job/oQOXAfwV', 'https://careers-ice.icims.com/jobs/14039/job'):
            self.assertTrue(cjs.api_resolvable(u))
            self.assertEqual(cjs.resolve_ext(self.job(u), get=None, get_html=lambda x: html)['description'], 'Write tests')

    def test_jazzhr_section(self):
        html = ('<div id="x">nav</div><div class="description" id="job-description"><p>Build <b>AI</b> apps</p>'
                '<div><ul><li>Python</li></ul></div></div><div>footer</div>')
        out = cjs.resolve_ext(self.job('https://techstra.applytojob.com/apply/h0W1/AI-Engineer'), get=None,
                              get_html=lambda x: html)
        self.assertEqual(out['description'], 'Build AI apps\nPython')

    def test_rippling_next_data(self):
        nd = {'props': {'pageProps': {'apiData': {'jobPost': {'description': {'role': '<p>Full stack</p>',
                                                                              'company': '<p>About us</p>'}}}}}}
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
        out = cjs.resolve_ext(self.job('https://ats.rippling.com/empowerly/jobs/bf9b'), get=None, get_html=lambda x: html)
        self.assertEqual(out['description'], 'Full stack\nAbout us')

    def test_malformed_ld_json_salvaged(self):
        html = ('<script type="application/ld+json">{"@type": "JobPosting", "description": "<p>Ship \\"code\\"</p>",'
                ' "hiringOrganization": {"logo": "x"} "industry": "y"}</script>')
        out = cjs.resolve_ext(self.job('https://jobs.jobvite.com/acme/job/oX'), get=None, get_html=lambda x: html)
        self.assertEqual(out['description'], 'Ship "code"')

    def test_polite_spacing_and_429_backoff(self):
        from urllib.error import HTTPError
        t = [0.0]
        sleeps, calls = [], []

        def sleep(x):
            sleeps.append(round(x, 2))
            t[0] += x

        def fn(url):
            calls.append(url)
            if len(calls) == 2:
                raise HTTPError(url, 429, 'Too Many', {}, None)
            return 'ok'
        g = cjs.polite(fn, gap=2, sleep=sleep, clock=lambda: t[0])
        self.assertEqual(g('https://a.wd1.myworkdayjobs.com/x'), 'ok')
        self.assertEqual(g('https://b.wd1.myworkdayjobs.com/y'), 'ok')  # same wd1 group: waits, 429, backs off, retries
        self.assertEqual(sleeps, [2, 5])
        self.assertEqual(len(calls), 3)
        g('https://c.wd5.myworkdayjobs.com/z')  # other cluster: no wait
        self.assertEqual(sleeps, [2, 5])

    def test_gh_jid_embed(self):
        seen = []

        def get(url):
            seen.append(url)
            return {'content': '&lt;p&gt;Data ops&lt;/p&gt;'}
        out = cjs.resolve_ext(self.job('https://www.autura.com/careers?gh_jid=5470341008'), get=get, get_html=None)
        self.assertEqual(out['description'], 'Data ops')
        self.assertEqual(seen, ['https://boards-api.greenhouse.io/v1/boards/autura/jobs/5470341008'])

    def test_unknown_host_not_resolvable(self):
        self.assertFalse(cjs.api_resolvable('https://careers.example.com/job/1'))
        self.assertEqual(cjs.resolve_ext(self.job('https://careers.example.com/job/1'), get=None, get_html=None), {})


class LanePlanAndShortlist(unittest.TestCase):
    def test_lane_ties(self):
        r = cjs.lane_ranks({'lane_priority': [['B', 'A'], 'C']})
        self.assertEqual(r, {'B': 0, 'A': 0, 'C': 1})
        self.assertEqual(cjs.lane_ranks({'lane_priority': ['C', 'B', 'A']}), {'C': 0, 'B': 1, 'A': 2})

    def test_review_plan(self):
        plan = cjs.review_plan({'budgets': {'max_jd_reviews_per_run': 60, 'review_plan': {'B': 24, 'A': 24, 'C': 12},
                                            'extra_reviews_if_below_min': 15}})
        self.assertEqual(plan['lanes']['C'], 12)
        with self.assertRaises(ValueError):
            cjs.review_plan({'budgets': {'max_jd_reviews_per_run': 30, 'review_plan': {'B': 24, 'A': 24}}})


class ScreenRules2026_11_21(unittest.TestCase):
    """Clearance / no-OPT / sponsorship / contract wording found in real JDs on 2026-11-21."""

    def rules(self, text):
        return {h['rule']: h['severity'] for h in cjs.screen(text)}

    def test_clearance(self):
        for t in ['Requirements\nA TS/SCI with Polygraph security clearance.',
                  'Clearance: Fully cleared before starting, with a polygraph completed within the last seven years',
                  'Requirements\nActive TS/SCI with Polygraph level of security clearance.']:
            self.assertEqual(self.rules(t).get('clearance_required'), 'block', t)
        for t in ['How to Stand Out From the Crowd\nAn active U.S. government Public Trust or security clearance.',
                  'Requirements\nNo security clearance required for this role.',
                  'All offers are contingent on a cleared background and possible reference check.']:
            self.assertNotIn('clearance_required', self.rules(t), t)

    def test_no_opt(self):
        for t in ['Individuals with temporary visas such as E, F-1 (including those with OPT or CPT), H-1, or TN '
                  'are not eligible for hire.',
                  'Oracle is not in a position now, nor in the future, to offer US immigration sponsorship for this role '
                  'which includes, but is not limited to, support of H-1B, TN, O-1, green card or F-1 e.g. OPT, CPT, '
                  'I-20, Training Plans/Evaluations.']:
            self.assertEqual(self.rules(t).get('no_opt_or_f1'), 'block', t)
        stem = ('This position is not eligible for employer-sponsored work authorization. The employer also does not '
                'provide employer participation or documentation required for STEM OPT extensions for this position.')
        r = self.rules(stem)
        self.assertNotIn('no_opt_or_f1', r)
        self.assertEqual(r.get('no_stem_opt'), 'risk')
        self.assertEqual(cjs.visa_risk_from(cjs.screen(stem)), 'high')
        self.assertNotIn('no_opt_or_f1', self.rules('We do not sponsor H-1B visas; F-1 OPT candidates are welcome to apply.'))

    def test_no_sponsorship_false_positive(self):
        note = ('If you are an F-1 student on CPT, OPT or STEM OPT, you have work authorization in the U.S. and do not '
                'require employer visa sponsorship for this role.')
        self.assertNotIn('no_sponsorship', self.rules(note))
        self.assertIn('no_sponsorship', self.rules('Candidates must be able to work without sponsorship now or in the future.'))

    def test_contract(self):
        for t in ['This role is a contractor position based in Vance, AL.', 'Employment type: 12-month temporary assignment',
                  'This is a contract-to-hire position.', 'Our client is seeking a Jr. Developer.',
                  'Job Type: Freelance/Independent Contract']:
            self.assertIn('contract_or_staffing', self.rules(t), t)
        for t in ['Support contract reviews, legal research, and product reviews.',
                  'Experience working with contract manufacturers.',
                  'Part-time, contract, or freelance roles may not be eligible for certain benefits.']:
            self.assertNotIn('contract_or_staffing', self.rules(t), t)


class HardRequirements(unittest.TestCase):
    """Only clearly *required* years / advanced degrees block; alternatives and preferences never do."""

    def rules(self, text):
        return [h['rule'] for h in cjs.hard_mismatches(text)]

    def test_required_years_block(self):
        self.assertEqual(self.rules('You have:\n5+ years of software engineering experience'), ['hard_years_required'])
        self.assertEqual(self.rules('Minimum of 4 years of professional experience building backend systems.'),
                         ['hard_years_required'])
        self.assertEqual(self.rules('Requirements\nBachelor\u2019s degree and 5 years of progressive experience'),
                         ['hard_years_required'])

    def test_years_not_blocked(self):
        for t in ['Requirements\n3+ years of experience with Python',                 # threshold is > 3
                  'Requirements\nOne to four years of relevant professional experience',  # range starts at 1
                  'Preferred Qualifications\n6+ years of industry experience',          # preferred section
                  '5+ years of experience is a plus.',                                  # soft wording
                  'Requirements\nBS + 6 years of experience, or MS + 4 years, or PhD + 2 years of experience',
                  'Requirements\nHigh School Diploma or GED and four years of relevant experience',
                  "Four years of experience may substitute for the bachelor's degree.",
                  'Requirements\nMust be at least 18 years of age.',
                  'Must have no DUI conviction in the past 10 years.',
                  'We have 20 years of experience serving customers.']:                 # no requirement marker
            self.assertEqual(self.rules(t), [], t)

    def test_degree(self):
        self.assertEqual(self.rules('Education: PhD degree in Computer Science required'), ['hard_degree_required'])
        self.assertEqual(self.rules("Required Education and Training:\nMaster\u2019s degree in CS with 3 years of "
                                    "experience."), ['hard_degree_required'])
        for t in ["Requirements\nBachelor's or Master's degree in Computer Science",
                  'Requirements\n3+ years of industry experience or a PhD in a quantitative field',
                  'Requirements\nPhD in robotics, or an equivalent record of research',
                  "Master's degree preferred",
                  'Alternatively, a Ph.D. degree with no experience.']:
            self.assertEqual(self.rules(t), [], t)

    def test_regressions_2026_11_21(self):
        # a separate "Alternatively, Master's + 3 years" sentence does not lower the bar for a bachelor's-only profile
        self.assertEqual(self.rules("You have:\nBachelor\u2019s degree in CS and 5 years of progressive experience in the job "
                                    "offered. Alternatively, the employer will accept a Master\u2019s degree in CS and 3 years "
                                    "of experience."), ['hard_years_required'])
        # PhD as a place where experience was gained is not a degree requirement; soft "highly considered"
        for t in ['Requirements\nYou have 3+ years of experience with deep networks either in industry or as part of a PhD.',
                  'Requirements\nMasters or Advanced degree highly considered.']:
            self.assertEqual(self.rules(t), [], t)
        # "(preferable)" qualifies only the PhD; the MS minimum still blocks
        self.assertEqual(self.rules('Requirements\nEducation: You have an M.Sc. or Ph.D. (preferable) in Computer Science'),
                         ['hard_degree_required'])

    def test_config_switch(self):
        old = dict(cjs.HARD)
        try:
            cjs.HARD.update(enabled=False)
            self.assertEqual(self.rules('You have:\n5+ years of experience'), [])
            cjs.HARD.update(enabled=True, max_years_required_ok=5)
            self.assertEqual(self.rules('You have:\n5+ years of experience'), [])
        finally:
            cjs.HARD.clear(); cjs.HARD.update(old)


class BoardFetchers(unittest.TestCase):
    """Greenhouse / Lever / Ashby / SmartRecruiters board and single-posting fetchers, with fake fetchers."""

    def setUp(self):
        self.c = cjs.compile_cfg(json.loads((WS_SRC / 'config.example.json').read_text(encoding='utf-8')))

    def test_greenhouse_board(self):
        seen = []

        def get(url):
            seen.append(url)
            return {'jobs': [{'title': 'Data Analyst', 'location': {'name': 'New York, NY'},
                              'absolute_url': 'https://job-boards.greenhouse.io/acme/jobs/5',
                              'content': '&lt;p&gt;SQL&lt;/p&gt;', 'first_published': '2026-01-01'},
                             {'title': 'Data Analyst', 'location': {'name': 'London, UK'},
                              'absolute_url': 'https://job-boards.greenhouse.io/acme/jobs/6'}]}
        out, n = cjs.fetch_board('greenhouse', 'acme', 'Acme', self.c, get=get)
        self.assertEqual((n, len(out)), (2, 1))
        self.assertEqual(out[0]['description'], 'SQL')
        self.assertEqual(out[0]['source'], 'greenhouse:acme')
        self.assertEqual(seen, ['https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true'])

    def test_lever_board_pages(self):
        def item(i, title='Data Analyst'):
            return {'text': title, 'categories': {'location': 'Remote - US'}, 'hostedUrl': f'https://jobs.lever.co/acme/{i}',
                    'applyUrl': f'https://jobs.lever.co/acme/{i}/apply', 'descriptionPlain': 'Build reports.',
                    'lists': [{'text': 'Requirements', 'content': '<li>SQL</li>'}], 'createdAt': 1767225600000}
        pages = {0: [item(i, 'Data Analyst' if i == 0 else 'Account Executive') for i in range(100)], 100: [item(100)]}
        seen = []

        def get(url):
            seen.append(url)
            return pages[int(url.rsplit('skip=', 1)[1])]
        out, n = cjs.fetch_board('lever', 'acme', 'Acme', self.c, get=get)
        self.assertEqual((n, len(out)), (101, 2))
        self.assertEqual(len(seen), 2)  # stops after a short page
        self.assertIn('Requirements\nSQL', out[0]['description'])
        self.assertTrue(out[0]['posted'].startswith('2026-01-01'))

    def test_ashby_board(self):
        get = lambda url: {'jobs': [
            {'title': 'Machine Learning Engineer, New Grad', 'location': 'Toronto', 'isRemote': True,
             'secondaryLocations': [{'location': 'San Francisco, CA'}], 'jobUrl': 'https://jobs.ashbyhq.com/acme/u1',
             'descriptionPlain': 'Train models.', 'publishedAt': '2026-01-02'},
            {'title': 'Data Analyst', 'location': 'Remote - US', 'isListed': False, 'jobUrl': 'https://jobs.ashbyhq.com/acme/u2'}]}
        out, n = cjs.fetch_board('ashby', 'acme', 'Acme', self.c, get=get)
        self.assertEqual((n, len(out)), (2, 1))  # unlisted posting skipped
        self.assertEqual(out[0]['location'], 'Toronto / San Francisco, CA / Remote')
        self.assertEqual(out[0]['apply_url'], 'https://jobs.ashbyhq.com/acme/u1')

    def test_smartrecruiters_board_and_detail(self):
        def get(url):
            if url.endswith('/postings/7'):
                return {'postingUrl': 'https://jobs.smartrecruiters.com/Acme/7-data-analyst',
                        'applyUrl': 'https://jobs.smartrecruiters.com/Acme/7-data-analyst?apply',
                        'jobAd': {'sections': {'jobDescription': {'text': '<p>Analyze data</p>'},
                                               'qualifications': {'text': '<p>SQL</p>'}}}}
            return {'totalFound': 2, 'content': [
                {'id': '7', 'name': 'Data Analyst', 'location': {'city': 'Austin', 'region': 'TX', 'country': 'us'},
                 'releasedDate': '2026-01-03'},
                {'id': '8', 'name': 'Senior Data Analyst', 'location': {'city': 'Austin', 'region': 'TX', 'country': 'us'}}]}
        out, n = cjs.fetch_board('smartrecruiters', 'Acme', 'Acme', self.c, get=get)
        self.assertEqual((n, len(out)), (2, 1))
        self.assertEqual(out[0]['listing_url'], 'https://jobs.smartrecruiters.com/Acme/7-data-analyst')
        self.assertIn('Analyze data', out[0]['description'])
        self.assertIn('SQL', out[0]['description'])

    def test_unknown_provider(self):
        with self.assertRaises(ValueError):
            cjs.fetch_board('workable', 'acme', 'Acme', self.c, get=lambda u: {})

    def test_resolve_jd_per_provider(self):
        cases = [
            ('https://boards.greenhouse.io/acme/jobs/9', 'https://boards-api.greenhouse.io/v1/boards/acme/jobs/9',
             {'content': '&lt;p&gt;GH duties&lt;/p&gt;'}, 'GH duties'),
            ('https://jobs.lever.co/acme/abc', 'https://api.lever.co/v0/postings/acme/abc',
             {'descriptionPlain': 'Lever duties'}, 'Lever duties'),
            ('https://jobs.ashbyhq.com/acme/u1', 'https://api.ashbyhq.com/posting-api/job-board/acme',
             {'jobs': [{'id': 'u1', 'descriptionPlain': 'Ashby duties'}]}, 'Ashby duties'),
        ]
        for url, api, body, want in cases:
            seen = []

            def get(u, body=body):
                seen.append(u)
                return body
            self.assertEqual(cjs.resolve_jd({'apply_url': url, 'listing_url': url}, get=get), want, url)
            self.assertEqual(seen, [api])
        self.assertEqual(cjs.resolve_jd({'apply_url': '', 'listing_url': 'https://example.test/jobs/1'},
                                        get=_no_network), '')


class FakeHTTP:
    """Stand-in for urlopen: serves canned JSON by URL prefix and records every call."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                return io.BytesIO(json.dumps(body).encode('utf-8'))
        return _no_network(req)


class CommandLine(unittest.TestCase):
    """End-to-end through main(): collect (mocked HTTP), import, status, and a failing source."""

    def setUp(self):
        self.ws = ws()
        self.addCleanup(shutil.rmtree, self.ws)
        cfg = json.loads((self.ws / 'config.json').read_text(encoding='utf-8'))
        cfg['boards'] = [{'board': 'greenhouse:acme', 'company': 'Acme Data'}]
        cfg['simplify']['url'] = 'https://example.test/listings.json'
        (self.ws / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cjs.main(['--workspace', str(self.ws), *args])
        return code, json.loads(out.getvalue())

    def test_collect_import_status(self):
        fake = FakeHTTP({
            'https://boards-api.greenhouse.io/v1/boards/acme/jobs': {'jobs': [
                {'title': 'Data Analyst', 'location': {'name': 'Remote - US'},
                 'absolute_url': 'https://job-boards.greenhouse.io/acme/jobs/11',
                 'content': '&lt;p&gt;Build SQL reports.&lt;/p&gt;', 'first_published': '2026-01-01T00:00:00Z'},
                {'title': 'Senior Data Analyst', 'location': {'name': 'Remote - US'},
                 'absolute_url': 'https://job-boards.greenhouse.io/acme/jobs/12', 'content': ''}]},
            'https://example.test/listings.json': [
                {'active': True, 'is_visible': True, 'id': 's1', 'company_name': 'Beta Robotics',
                 'title': 'Robot Data Collection Operator', 'locations': ['San Diego, CA'],
                 'url': 'https://jobs.lever.co/beta/abc-1', 'date_posted': int(cjs.now().timestamp()),
                 'sponsorship': 'Offers Sponsorship'},
                {'active': True, 'is_visible': True, 'id': 's2', 'company_name': 'Gamma',
                 'title': 'Data Analyst', 'locations': ['Austin, TX'], 'url': 'https://example.test/j/2',
                 'date_posted': int(cjs.now().timestamp()), 'sponsorship': 'U.S. Citizenship is Required'}],
        })
        cjs.urlopen = fake
        try:
            code, res = self.run_cli('collect')
        finally:
            cjs.urlopen = _no_network
        self.assertEqual(code, 0)
        self.assertEqual(res['errors'], {})
        self.assertEqual(res['sources']['greenhouse:acme']['seen'], 2)
        self.assertEqual(res['sources']['greenhouse:acme']['kept'], 1)  # senior title prefiltered
        self.assertEqual(res['sources']['simplify']['kept'], 1)         # citizenship-only listing skipped
        self.assertEqual(len(fake.calls), 2)

        manual = self.ws / 'manual.json'
        manual.write_text(json.dumps([{'company': 'Delta Labs', 'title': 'Research Data Analyst',
                                       'listing_url': 'https://delta.example/careers/7',
                                       'description': 'Check research datasets with Python and SQL.',
                                       'location': 'Boston, MA', 'source': 'manual'}]), encoding='utf-8')
        code, _ = self.run_cli('import', '--input', str(manual))
        self.assertEqual(code, 0)
        code, st = self.run_cli('status')
        self.assertEqual(code, 0)
        self.assertEqual(st['new'], 3)
        self.assertEqual(st['no_jd'], 1)  # the Simplify lead still needs its JD
        self.assertEqual([r['command'] for r in st['last_runs']], ['import', 'collect'])
        out = self.ws / 'daily' / '2026-01-02.md'  # parent folder does not exist yet
        code, res = self.run_cli('shortlist', '--day', '2026-01-02', '--out', str(out))
        self.assertEqual(code, 0)
        self.assertIn('每日候选清单 2026-01-02', out.read_text(encoding='utf-8'))

    def test_failing_source_keeps_old_data(self):
        self.run_cli('import', '--input', str(self._one_job()))
        code, res = self.run_cli('collect', '--only', 'boards')  # urlopen is blocked -> board fails
        self.assertEqual(code, 0)
        self.assertIn('AssertionError', res['errors']['greenhouse:acme'])
        self.assertEqual(self.run_cli('status')[1]['new'], 1)

    def _one_job(self):
        f = self.ws / 'one.json'
        f.write_text(json.dumps([{'company': 'Acme Data', 'title': 'Data Analyst',
                                  'listing_url': 'https://job-boards.greenhouse.io/acme/jobs/11',
                                  'description': 'Build SQL reports.', 'location': 'Remote - US',
                                  'source': 'greenhouse:acme'}]), encoding='utf-8')
        return f


if __name__ == '__main__':
    unittest.main()
