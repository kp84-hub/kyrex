"""Regression: announcement details must survive abbreviated plain MIME parts."""
import base64
import connectors
import serve


def part(mime, text, **extra):
    return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}, **extra}


def test_richer_html_retains_kickoff_after_plain_intro():
    intro = '2026 Homecoming Spirit Week October 5th - October 9th Elementary Parents, Our students are looking forward to Spirit Week!'
    details = intro + ' ' + ('School activities and announcements. ' * 15) + 'Homecoming football: Friday, October 9. Kickoff at 7 PM.'
    payload = {"mimeType": "multipart/alternative", "parts": [part('text/plain', intro), part('text/html', '<p>' + details + '</p>')]}
    body, kind = connectors._gmail_body_text(payload)
    assert 'Kickoff at 7 PM' in body
    assert kind == 'html'


def test_equivalent_or_unrelated_html_preserves_plain():
    for html in ['<p>Hello plain body</p>', '<p>' + 'unrelated content ' * 100 + '</p>']:
        payload = {"parts": [part('text/plain', 'Hello plain body'), part('text/html', html)]}
        assert connectors._gmail_body_text(payload) == ('Hello plain body', 'text')


def test_richer_attachment_is_not_a_body():
    payload = {"parts": [part('text/plain', 'Intro'), part('text/html', '<p>Intro ' + 'private ' * 100 + '</p>', filename='secret.html')]}
    assert connectors._gmail_body_text(payload) == ('Intro', 'text')


def test_bodyless_read_explicitly_marks_preview_incomplete():
    rendered = serve._render_gmail_read({'headers': {'Subject': 'Announcement'}, 'snippet': 'Intro preview'})
    assert 'preview only' in rendered
    assert 'not a complete read' in rendered
    assert 'Intro preview' in rendered


def test_external_html_body_is_fetched_from_exact_message(monkeypatch):
    calls = []
    def transport(method, url, token, params):
        calls.append(url)
        if '/attachments/' in url:
            return part('text/html', '<p>Homecoming Friday October 9, 2026 at 7 PM.</p>')['body']
        return {'id': 'kelly-2026', 'payload': {'mimeType': 'text/html', 'body': {'attachmentId': 'external-body', 'size': 500}}}
    reader = connectors.GmailRead(None, 'alice', provider='google', transport=transport)
    monkeypatch.setattr(reader, '_authorize', lambda: 'private-token')
    result = reader.read_message('kelly-2026')
    assert '2026 at 7 PM' in result['body']
    assert calls[-1].endswith('/messages/kelly-2026/attachments/external-body')
    assert len(calls) == 2
    assert 'private-token' not in str(result)


def test_external_files_and_oversize_parts_are_never_fetched(monkeypatch):
    for extra in [{'filename': 'document.html'}, {'headers': [{'name': 'Content-Disposition', 'value': 'attachment'}]}, {'body': {'attachmentId': 'external', 'size': 200001}}]:
        calls = []
        payload = {'mimeType': 'text/html', 'body': {'attachmentId': 'external', 'size': 100}, **extra}
        def transport(*args):
            calls.append(args)
            return {'id': 'm1', 'payload': payload}
        reader = connectors.GmailRead(None, 'alice', provider='google', transport=transport)
        monkeypatch.setattr(reader, '_authorize', lambda: 'token')
        assert reader.read_message('m1')['body'] == ''
        assert len(calls) == 1


def test_external_body_failure_preserves_incomplete_read(monkeypatch):
    def transport(method, url, *args):
        if '/attachments/' in url:
            raise connectors.ConnectorUnavailable('unavailable')
        return {'id': 'm1', 'payload': {'mimeType': 'text/plain', 'body': {'attachmentId': 'body', 'size': 100}}}
    reader = connectors.GmailRead(None, 'alice', provider='google', transport=transport)
    monkeypatch.setattr(reader, '_authorize', lambda: 'token')
    assert reader.read_message('m1')['body'] == ''


def test_external_body_fetches_are_bounded_and_skip_attachment_subtrees(monkeypatch):
    calls = []
    def transport(method, url, *args):
        calls.append(url)
        if '/attachments/' in url:
            return part('text/plain', 'Readable body')['body']
        external = lambda aid: {'mimeType': 'text/plain', 'body': {'attachmentId': aid, 'size': 20}}
        return {'id': 'm1', 'payload': {'parts': [
            {'mimeType': 'multipart/mixed', 'filename': 'attached-message', 'parts': [external('secret')]},
            external('body1'), external('body2'), external('body3')]}}
    reader = connectors.GmailRead(None, 'alice', transport=transport)
    monkeypatch.setattr(reader, '_authorize', lambda: 'token')
    assert reader.read_message('m1')['body'] == 'Readable body'
    assert len(calls) == 3
    assert not any(url.endswith(('secret', 'body3')) for url in calls)
