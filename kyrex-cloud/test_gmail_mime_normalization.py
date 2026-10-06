"""MIME regressions and connector-to-coordinator evidence, without live mail."""
import base64
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import connectors

sys.path.insert(0, str(Path(__file__).resolve().parent / 'web' / 'backend'))
import gmail_inline_result_bridge as bridge


def part(mime, text, encoding='utf-8', **extra):
    return {'mimeType': mime, 'body': {'data': base64.urlsafe_b64encode(text.encode(encoding)).decode()}, **extra}


INTRO = 'Elementary Parents, Our students are looking forward to Spirit Week, October 5th–9th!'
HTML = '<p>Elementary Parents: Our students are looking forward to Spirit Week October 5th-9th.</p>' + '<p>Class cheer and school spirit activities. </p>' * 12 + '<p>FRIDAY NIGHT GAME: Home Varsity Football Game at 7:00pm.</p>'


class GmailMimeTests(unittest.TestCase):
    def reader(self, payload, external=None):
        calls = []
        def transport(method, url, token, params):
            calls.append((url, params))
            if '/attachments/' in url:
                return external or {}
            return {'id': 'kelly-2026', 'snippet': INTRO, 'payload': payload}
        reader = connectors.GmailRead(None, 'alice', transport=transport)
        reader._authorize = lambda: 'private-token'
        return reader, calls

    def test_inline_parameterized_types(self):
        for mime in ['Text/Plain; charset=UTF-8', ' text/html ; charset="utf-8"']:
            with self.subTest(mime=mime):
                reader, calls = self.reader(part(mime, 'Home Varsity Football Game at 7:00pm'))
                result = reader.read_message('kelly-2026')
                self.assertIn('7:00pm', result['body'])
                self.assertEqual(result['body_read_status'], 'full')
                self.assertEqual(len(calls), 1)

    def test_external_parameterized_html(self):
        reader, calls = self.reader({'mimeType': 'text/html; charset=utf-8', 'body': {'attachmentId': 'body', 'size': 500}}, part('text/html', HTML)['body'])
        result = reader.read_message('kelly-2026')
        self.assertIn('7:00pm', result['body'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[-1][0].endswith('/messages/kelly-2026/attachments/body'))

    def test_richer_html_survives_typography_difference(self):
        body, kind = connectors._gmail_body_text({'parts': [part('text/plain', INTRO), part('text/html', HTML)]})
        self.assertEqual(kind, 'html')
        self.assertIn('7:00pm', body)

    def test_unrelated_html_keeps_plain(self):
        self.assertEqual(connectors._gmail_body_text({'parts': [part('text/plain', INTRO), part('text/html', '<p>Unrelated notice. </p>' * 100)]}), (INTRO, 'text'))

    def test_equivalent_html_keeps_plain(self):
        self.assertEqual(connectors._gmail_body_text({'parts': [part('text/plain', INTRO), part('text/html', '<p>' + INTRO + '</p>')]}), (INTRO, 'text'))

    def test_charset_header_and_parameter(self):
        for extra in [{}, {'headers': [{'name': 'Content-Type', 'value': 'text/plain; charset=windows-1252'}]}]:
            with self.subTest(extra=extra):
                payload = part('text/plain; charset=windows-1252', 'Parents’ announcement: 7:00pm', encoding='cp1252', **extra)
                self.assertEqual(connectors._gmail_body_text(payload)[0], 'Parents’ announcement: 7:00pm')

    def test_unknown_charset_falls_back(self):
        self.assertEqual(connectors._gmail_body_text(part('text/plain; charset=unknown', '7:00pm'))[0], '7:00pm')

    def test_parameterized_files_still_excluded(self):
        for extra in [{'filename': 'secret.html'}, {'headers': [{'name': 'Content-Disposition', 'value': 'attachment'}]}]:
            with self.subTest(extra=extra):
                reader, calls = self.reader({'mimeType': 'text/html; charset=utf-8', 'body': {'attachmentId': 'file', 'size': 10}, **extra})
                self.assertEqual(reader.read_message('kelly-2026')['body'], '')
                self.assertEqual(len(calls), 1)

    def test_raw_alternatives_share_normalization(self):
        from email.message import EmailMessage
        mail = EmailMessage()
        mail['Subject'] = 'Announcement'
        mail.set_content(INTRO)
        mail.add_alternative(HTML, subtype='html')
        body, kind, truncated = connectors._gmail_raw_body(base64.urlsafe_b64encode(mail.as_bytes()).decode())
        self.assertEqual(kind, 'html')
        self.assertIn('7:00pm', body)
        self.assertFalse(truncated)

    def test_full_body_reaches_coordinator_after_ui_summary_limit(self):
        html = HTML.replace('FRIDAY NIGHT GAME', 'Extra announcements. ' * 300 + 'FRIDAY NIGHT GAME')
        reader, _ = self.reader({'parts': [part('text/plain', INTRO), part('text/html', html)]})
        selected = reader.read_message('kelly-2026')
        self.assertGreater(selected['body'].index('7:00pm'), 4000)
        record = {'delegation_id': 'd1', 'task_id': 't1', 'status': 'done'}
        store = SimpleNamespace(get=lambda _: {'status': 'done', 'result': {'selected': selected}}, get_delegation=lambda _: record, mark_delegation_relayed=lambda _: None)
        chat = SimpleNamespace(_task_store=lambda: store, delegation=SimpleNamespace(public_view=lambda rec: dict(rec)))
        session = SimpleNamespace(delegation_ctx={'owner': 'alice', 'conversation_id': 'c1'})
        view = bridge._terminal_public_view(chat, session, record)
        self.assertIn('7:00pm', view['email_evidence']['body'])
        self.assertTrue(view['email_evidence']['body_available'])
        self.assertEqual(view['email_evidence']['body_read_status'], 'full')
        self.assertNotIn('private-token', str(view))

    def test_failed_fetches_report_safe_stages_and_status(self):
        calls = []
        def transport(method, url, token, params):
            calls.append((url, params))
            if params.get('format') == 'full':
                return {'id': 'kelly-2026', 'payload': {'mimeType': 'text/html', 'body': {'attachmentId': 'body', 'size': 10}}, 'snippet': INTRO}
            error = connectors.ConnectorUnavailable('secret response private-token')
            error.http_status = 403 if '/attachments/' in url else 500
            raise error
        reader = connectors.GmailRead(None, 'alice', transport=transport)
        reader._authorize = lambda: 'private-token'
        result = reader.read_message('kelly-2026')
        self.assertEqual(result['body_read_status'], 'unavailable')
        self.assertEqual(result['body_reader_version'], 3)
        self.assertEqual(result['body_read_diagnostics'], [
            {'stage': 'body_part', 'outcome': 'failed', 'reason': 'provider_error', 'http_status': 403},
            {'stage': 'parsed', 'outcome': 'no_readable_body'},
            {'stage': 'original', 'outcome': 'failed', 'reason': 'provider_error', 'http_status': 500}])
        self.assertEqual(len(calls), 3)
        self.assertNotIn('secret response', str(result))
        self.assertNotIn('private-token', str(result))

    def test_raw_limit_is_reported_without_extra_fetch(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return {'id': 'kelly-2026', 'sizeEstimate': connectors._GMAIL_RAW_MAX + 1, 'payload': {}}
        reader = connectors.GmailRead(None, 'alice', transport=transport)
        reader._authorize = lambda: 'token'
        result = reader.read_message('kelly-2026')
        self.assertEqual(result['body_read_diagnostics'][-1]['reason'], 'message_size_limit')
        self.assertEqual(len(calls), 1)

    def test_default_transport_retains_http_status_without_secrets(self):
        import urllib.error
        error = urllib.error.HTTPError('https://private.example/secret', 403, 'private-token', {}, None)
        with patch.object(connectors.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(connectors.ConnectorUnavailable) as caught:
                connectors.default_transport('GET', 'https://example.com', 'token')
        self.assertEqual(caught.exception.http_status, 403)
        self.assertNotIn('private-token', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_diagnostics_reach_coordinator_without_arbitrary_fields(self):
        record = {'delegation_id': 'd1', 'task_id': 't1', 'status': 'done'}
        diagnostics = [{'stage': 'original', 'outcome': 'failed', 'reason': 'provider_error', 'http_status': 403, 'token': 'SECRET'}]
        selected = {'body': '', 'body_reader_version': 3, 'body_read_diagnostics': diagnostics}
        store = SimpleNamespace(get=lambda _: {'status': 'done', 'result': {'selected': selected}}, get_delegation=lambda _: record, mark_delegation_relayed=lambda _: None)
        chat = SimpleNamespace(_task_store=lambda: store, delegation=SimpleNamespace(public_view=lambda rec: dict(rec)))
        session = SimpleNamespace(delegation_ctx={})
        view = bridge._terminal_public_view(chat, session, record)
        self.assertEqual(view['email_evidence']['body_read_diagnostics'][0]['http_status'], 403)
        self.assertNotIn('SECRET', str(view))

    def test_rendered_read_reports_actual_failure(self):
        import serve
        result = {'body': '', 'snippet': INTRO, 'body_read_diagnostics': [{'stage': 'original', 'outcome': 'failed', 'reason': 'provider_error', 'http_status': 403}]}
        rendered = serve._render_gmail_read(result)
        self.assertIn('original: provider_error (HTTP 403)', rendered)
        self.assertIn('not a complete read', rendered)


if __name__ == '__main__':
    unittest.main()
