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
