from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1] / '.agents/skills/job-search'
sys.path.insert(0, str(SKILL / 'scripts'))
import job_search as jobs


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.conn = jobs.open_database(self.workspace)
        self.addCleanup(self.conn.close)
        self.profile = {'facts': {'project': 'Used SQL to check research data.'},
                        'target_keywords': ['data analyst']}
        self.url = 'https://job-boards.greenhouse.io/example/jobs/123'
        self.posting = jobs.posting('Example Lab', 'Research Data Analyst', self.url,
                                    'Duties and requirements include SQL and checking datasets.',
                                    'Remote', self.url + '/apply', 'listed', 'greenhouse:example')

    def decision(self):
        result = jobs.queue(self.conn, self.profile, 15)
        job = result['jobs'][0]
        return {k: job[k] for k in ('job_id', 'job_sha256')} | {
            'profile_sha256': result['profile_sha256'],
            'disposition': 'strong',
            'reason': 'SQL project covers data quality checks.',
            'fact_ids': ['project'],
            'resume': 'Analytics version',
            'checked_at': jobs.now().isoformat(),
            **{check: True for check in jobs.CHECKS},
        }

    def test_rerun_deduplicates_and_preserves_review(self):
        variant = self.posting | {'listing_url': self.url.replace('job-boards', 'boards') + '/apply?utm_source=example'}
        result = jobs.collect(self.conn, [self.posting, variant])
        self.assertEqual((result['new'], result['duplicate_inputs']), (1, 1))
        jobs.record(self.conn, self.profile, [self.decision()])
        self.assertEqual(jobs.collect(self.conn, [variant])['unchanged'], 1)
        self.assertEqual(jobs.queue(self.conn, self.profile, 15)['pending'], 0)
        self.assertIn('Analytics version', jobs.report(self.conn, self.profile))
        jobs.mark_applied(self.conn, variant['listing_url'])
        self.assertNotIn('Analytics version', jobs.report(self.conn, self.profile))

    def test_old_review_does_not_reappear_when_board_posting_returns(self):
        board = self.posting['source']
        jobs.collect(self.conn, [self.posting], board)
        old = self.decision()
        jobs.record(self.conn, self.profile, [old])
        self.assertEqual(jobs.collect(self.conn, [], board)['no_longer_listed'], 1)
        self.assertNotIn('Analytics version', jobs.report(self.conn, self.profile))
        jobs.collect(self.conn, [self.posting], board)
        self.assertEqual(jobs.queue(self.conn, self.profile, 15)['pending'], 1)

    def test_changed_profile_or_posting_requires_a_new_review(self):
        jobs.collect(self.conn, [self.posting])
        old = self.decision()
        jobs.record(self.conn, self.profile, [old])
        amended = self.profile | {'facts': {'project': 'Experience under review.'}}
        self.assertEqual(jobs.queue(self.conn, amended, 15)['pending'], 1)
        with self.assertRaises(ValueError):
            jobs.record(self.conn, amended, [old])
        jobs.collect(self.conn, [self.posting | {'description': 'New requirements.'}])
        with self.assertRaises(ValueError):
            jobs.record(self.conn, self.profile, [old])

    def test_recommendation_requires_evidence_and_batch_rolls_back(self):
        jobs.collect(self.conn, [self.posting])
        decision = self.decision()
        with self.assertRaises(ValueError):
            jobs.record(self.conn, self.profile, [decision | {'jd_complete': False}])
        with self.assertRaises(ValueError):
            jobs.record(self.conn, self.profile, [decision | {'checked_at': '2020-01-01T00:00:00Z'}])
        with self.assertRaises(ValueError):
            jobs.record(self.conn, self.profile, [decision, decision | {'job_id': 'missing'}])
        self.assertEqual(jobs.queue(self.conn, self.profile, 15)['pending'], 1)

    def test_employer_rotation_keeps_two_distinct_openings(self):
        second = self.posting | {'listing_url': self.url + '4'}
        other = self.posting | {'company': 'Other Org', 'listing_url': self.url + '5'}
        jobs.collect(self.conn, [self.posting, second, other])
        result = jobs.queue(self.conn, self.profile, 3)['jobs']
        self.assertEqual(len({row['company'] for row in result[:2]}), 2)
        self.assertEqual(len({row['job_id'] for row in result}), 3)

    def test_offline_demo(self):
        path = self.workspace / 'demo'
        self.assertIn('1 strong · 0 stretch · 1 hold', jobs.demo(path))
        with self.assertRaises(ValueError):
            jobs.demo(path)
        with closing(jobs.open_database(path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)


class BoardTests(unittest.TestCase):
    def test_public_board_formats(self):
        responses = {
            'greenhouse:demo': {'jobs': [{
                'title': 'Analyst', 'absolute_url': 'https://job-boards.greenhouse.io/demo/jobs/1',
                'content': '&lt;p&gt;Use SQL.&lt;/p&gt;', 'location': {'name': 'Remote'},
            }]},
            'lever:demo': [{
                'text': 'Analyst', 'hostedUrl': 'https://jobs.lever.co/demo/one',
                'applyUrl': 'https://jobs.lever.co/demo/one/apply', 'descriptionPlain': 'Use SQL.',
                'lists': [{'text': 'Requirements', 'content': '<li>Python</li>'}],
            }],
            'ashby:demo': {'jobs': [{
                'title': 'Analyst', 'jobUrl': 'https://jobs.ashbyhq.com/demo/one',
                'applyUrl': 'https://jobs.ashbyhq.com/demo/one/application', 'descriptionPlain': 'Use SQL.',
            }, {'isListed': False}]},
        }
        for board, response in responses.items():
            with self.subTest(board=board):
                row, = jobs.fetch_board(board, 'Demo Employer', lambda _: response)
                self.assertIn('Use SQL.', row['description'])
                self.assertEqual(row['source'], board)
                self.assertEqual(row['status'], 'listed')

    def test_board_slug_is_validated_before_network_access(self):
        with self.assertRaises(ValueError):
            jobs.fetch_board('greenhouse:../other', 'Demo', lambda _: self.fail('Unexpected network call'))


if __name__ == '__main__':
    unittest.main()
