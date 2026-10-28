"""Saved booking failures project to safe, accurate diagnostic stages."""
import json
import unittest

import browser_diagnostics as diag


class SessionDiagnosticsTests(unittest.TestCase):
    def row(self, result):
        return {
            'dispatch_id': 'bd-' + 'a' * 16, 'status': 'done', 'result': result,
            'progress': [{diag.ACTION: {'stage': 'connection', 'state': 'passed',
                                       'cloud_build': 'b' * 40}}],
        }

    def test_actual_booking_message_is_a_session_failure_not_delivery_failure(self):
        result = {'mode': 'workout_browser', 'status': 'error', 'final_response':
                  'The Glofox member session could not be verified. '
                  'Check the existing session in the private viewer. Nothing was booked.'}
        for stored in (result, json.dumps(result)):
            with self.subTest(serialized=isinstance(stored, str)):
                report = diag.report(self.row(stored))
                stages = {s['id']: s['state'] for s in report['stages']}
                self.assertEqual(report['code'], 'session_unverified')
                self.assertEqual(stages['sign_in'], 'failed')
                self.assertEqual(stages['connection'], 'passed')
                self.assertEqual(stages['dispatch'], 'not_checked')
                self.assertEqual(stages['browser_launch'], 'not_checked')
                self.assertEqual(stages['site_access'], 'not_checked')
                self.assertEqual(report['cloud_build'], 'b' * 12)
                self.assertEqual(report['host_build'], 'unknown')
                self.assertNotIn('Sign in through', report['message'])

    def test_fixed_probe_session_messages_are_classified_without_structured_detail(self):
        for code in ('session_unverified', 'session_verification_blocked', 'login_required'):
            with self.subTest(code=code):
                result = {'mode': 'workout_browser', 'status': 'error',
                          'errors': [diag.FAILURES[code][1]]}
                report = diag.report(self.row(result))
                self.assertEqual(report['code'], code)
                self.assertEqual(report['message'], diag.FAILURES[code][1])
                self.assertEqual(report['stages'][-1], {'id': 'sign_in', 'state': 'failed'})

    def test_transport_error_keeps_precedence_over_a_session_result(self):
        for reason, code in (
            ('HostUnavailable: browser host connection lost', 'connection_lost'),
            ('HostUnavailable: browser dispatch deadline expired', 'dispatch_timeout'),
            ('BrowserChannelUnavailable: no channel', 'dispatch_unavailable'),
        ):
            with self.subTest(reason=reason):
                row = self.row({'mode': 'workout_browser', 'status': 'error',
                                'final_response': diag.FAILURES['session_unverified'][1]})
                row.update(error=reason, status='failed')
                self.assertEqual(diag.report(row)['code'], code)

    def test_unrecognized_failure_does_not_invent_a_session_result(self):
        report = diag.report(self.row({'mode': 'workout_browser', 'status': 'error',
                                      'final_response': 'Unknown adapter problem'}))
        self.assertEqual(report['code'], 'failed')
        self.assertEqual(report['stages'][-1], {'id': 'sign_in', 'state': 'not_checked'})

    def test_private_error_content_is_never_projected(self):
        secret = 'private-page-and-account-content'
        result = {'mode': 'workout_browser', 'status': 'error', 'final_response':
                  'The Glofox member session could not be verified. ' + secret,
                  'cookies': secret}
        report = diag.report(self.row(result))
        self.assertEqual(report['code'], 'session_unverified')
        self.assertNotIn(secret, json.dumps(report))

    def test_completed_website_check_explicitly_distinguishes_unchecked_sign_in(self):
        for stages, checked in (({'site_access': 'passed'}, False),
                                ({'site_access': 'passed', 'sign_in': 'passed'}, True)):
            report = diag.report(self.row(diag.host_result(stages=stages)))
            self.assertEqual(report['code'], '')
            self.assertEqual('Member sign-in was not checked.' in report['message'], not checked)


if __name__ == '__main__':
    unittest.main()
