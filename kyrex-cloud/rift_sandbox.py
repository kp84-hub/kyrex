"""Opt-in Railway verification of the Developer Bot's public Kyrex Rift."""
from collections import Counter
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import time

import bots
from developer_repositories import RepositoryWorkspaces, SANDBOX_MAX_BYTES, git
from github_connection import GitHubError
from task_store import CloudTaskStore, DuplicateTaskId, STATUS_RUNNING


EXECUTOR = 'rift_sandbox'
TASK_TEXT = 'Run the tests in a Railway Sandbox'
REPOSITORY = 'kp84-hub/kyrex'
TOKEN_ENV = 'KYREX_RIFT_SANDBOX_TOKEN'
_REQUEST = re.compile(
    r'/sandbox test|(?:(?:can|could|would) you )?(?:please )?run '
    r'(?:(?:the|these|kyrex) )?tests? (?:in|on) (?:a |the )?railway sandbox[.!?]?',
    re.I,
)
_WORKFLOW_SECRET_REFERENCE = re.compile(
    r'^([ \t]*[A-Za-z_][A-Za-z0-9_-]*):(?=[ \t]+([\'\"]?)\$\{\{[ \t]*'
    r'(?:secrets\.[A-Za-z_][A-Za-z0-9_]*|github\.token)[ \t]*\}\}\2[ \t]*\r?$)',
    re.MULTILINE,
)
# Exact, public values used by CI test jobs. Keep each exception tied to its
# workflow, environment variable, and literal value.
_WORKFLOW_TEST_FIXTURES = {
    'build-chat-desktop.yml': ('GITHUB_CLIENT_SECRET', 'test-secret'),
    'check-accounts.yml': ('WEB_SESSION_SECRET', 'account-ci-only'),
    'check-android-chat.yml': ('WEB_SESSION_SECRET', 'android-chat-ci-only'),
    'check-booking-flow.yml': ('WEB_SESSION_SECRET', 'booking-workflow-ci-only'),
    'check-chat-notifications.yml': ('WEB_SESSION_SECRET', 'notification-ci-only'),
    'check-composio.yml': ('WEB_SESSION_SECRET', 'composio-ci-offline-only'),
    'check-job-contracts.yml': ('WEB_SESSION_SECRET', 'job-contract-ci-only'),
}
_JS_ENV_REFERENCE = re.compile(
    r'^([ \t]*(?:(?:export|const|let|var)[ \t]+)*[A-Za-z_][A-Za-z0-9_]*[ \t]*)='
    r'(?=[ \t]*process\.env(?:\.[A-Za-z_][A-Za-z0-9_]*|'
    r'\[([\'\"])[A-Za-z_][A-Za-z0-9_]*\2\])'
    r'[ \t]*;?[ \t]*\r?$)', re.MULTILINE,
)
_SHELL_ENV_REFERENCE = re.compile(
    r'^([ \t]*(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*[ \t]*)='
    r'(?=[ \t]*("?)\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})\2'
    r'[ \t]*;?[ \t]*\r?$)', re.MULTILINE,
)
_TYPESCRIPT_FIELD = re.compile(
    r'^([ \t]*[A-Za-z_][A-Za-z0-9_]*):'
    r'(?=[ \t]*(?:string|number|boolean|unknown|never|any)[ \t]*;[ \t]*\r?$)',
    re.MULTILINE,
)
_DOTENV_PLACEHOLDER = re.compile(
    r'^([ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*)='
    r'(?=[ \t]*([\'\"]?)(?:change-me|replace-me)\2[ \t]*\r?$)',
    re.IGNORECASE | re.MULTILINE,
)
_DISPLAY_PATH = re.compile(r'[A-Za-z0-9._/-]{1,160}')
_ROOT = Path(__file__).resolve().parent
_STAGES = {
    'copying': 'Copying and verifying the Rift snapshot…',
    'testing': 'Running Kyrex Chat smoke tests in Railway…',
    'cleaning': 'Destroying the Railway Sandbox…',
}
_ERRORS = {
    'starting': 'Could not start the Railway Sandbox. Check the pilot environment and project token.',
    'copying': 'Could not reconstruct the Rift snapshot in Railway. The tests did not run.',
    'testing': 'Railway did not confirm a test result.',
    'cleaning': 'Railway did not confirm VM destruction. The VM has a two-minute idle timeout.',
}


class SandboxError(Exception):
    pass


def requested(text):
    return bool(_REQUEST.fullmatch(' '.join(str(text or '').split())))


def _config(owner):
    configured_owner = os.environ.get('KYREX_RIFT_SANDBOX_OWNER', '').strip()
    environment = os.environ.get('KYREX_RIFT_SANDBOX_ENVIRONMENT_ID', '').strip()
    token = os.environ.get(TOKEN_ENV, '').strip()
    if not configured_owner or owner != configured_owner or not token or not environment:
        raise SandboxError('Railway Sandbox testing is not configured for this account.')
    if environment == os.environ.get('RAILWAY_ENVIRONMENT_ID'):
        raise SandboxError('Configure a separate Railway environment for sandbox testing.')
    return environment, token


def _workspace(workspaces, owner, bot):
    from serve import developer_repository_granted

    if bot.get('owner') != owner or not developer_repository_granted(bot.get('policy')):
        raise SandboxError('Railway Sandbox testing requires your own Developer Bot.')
    current = workspaces.current(owner, bot)
    if not current or current['repository'].casefold() != REPOSITORY:
        raise SandboxError('Select the public kp84-hub/kyrex repository in Developer Bot settings first.')
    return current


def submit(owner, bot, conversation, task_id, store):
    existing = store.get(task_id)
    if not existing:
        _config(owner)
        if not bots.is_running(bot):
            raise SandboxError('Start Developer Bot before requesting a Railway Sandbox test.')
        _workspace(RepositoryWorkspaces(), owner, bot)
        try:
            return store.submit(session_key=bot['id'], bot_id=bot['id'], rift=bot['rift'],
                                chat_id=owner, conversation_id=conversation, task_id=task_id,
                                executor_prefix=EXECUTOR, task_text=TASK_TEXT)
        except DuplicateTaskId:
            existing = store.get(task_id) or {}
    if (existing.get('chat_id'), existing.get('bot_id'), existing.get('conversation_id'),
            existing.get('executor_prefix'), existing.get('task_text')) != (
            owner, bot['id'], conversation, EXECUTOR, TASK_TEXT):
        raise SandboxError('This request ID already belongs to a different task.')
    return task_id


def _source_findings(privacy, path, data):
    source = data.decode('utf-8', errors='replace')
    # Remove only the delimiter of a whole, recognized reference. The
    # scanner still sees every value and catches configured credentials.
    suffix = Path(path).suffix
    if suffix in {'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.cts', '.mts'}:
        source = _JS_ENV_REFERENCE.sub(r'\1 ', source)
    if suffix in {'.sh', '.bash', '.zsh'}:
        source = _SHELL_ENV_REFERENCE.sub(r'\1 ', source)
    if path.startswith('.github/workflows/'):
        source = _WORKFLOW_SECRET_REFERENCE.sub(r'\1 ', source)
        fixture = _WORKFLOW_TEST_FIXTURES.get(path.removeprefix('.github/workflows/'))
        if fixture:
            key, value = map(re.escape, fixture)
            source = re.sub(rf'^([ \t]*{key}):(?=[ \t]+([\'\"]?){value}\2[ \t]*\r?$)',
                            r'\1 ', source, flags=re.MULTILINE)
    if suffix in {'.ts', '.tsx', '.cts', '.mts'}:
        source = _TYPESCRIPT_FIELD.sub(r'\1 ', source)
    if path.rsplit('/', 1)[-1] in {'.env.example', '.env.sample', '.env.template'}:
        source = _DOTENV_PLACEHOLDER.sub(r'\1 ', source)
    filtered = privacy.text(source)
    if source == filtered:
        return Counter()
    original_lines, filtered_lines = source.split('\n'), filtered.split('\n')
    if '\ufffd' in source or len(original_lines) != len(filtered_lines):
        return None
    return Counter((line, masked) for line, masked in zip(original_lines, filtered_lines)
                   if line != masked)


def _in_public_base(current, path, privacy, findings):
    # The runner independently fetches this exact public commit before
    # uploading the patch. Ignore local replacement refs when reading it.
    blob = current['base_sha'] + ':' + path
    try:
        root = current['path']
        if int(git(root, '--no-replace-objects', 'cat-file', '-s', blob)) > SANDBOX_MAX_BYTES:
            return False
        original = git(root, '--no-replace-objects', 'cat-file', 'blob', blob)
    except GitHubError:
        return False
    baseline = _source_findings(privacy, path, original)
    return baseline is not None and findings <= baseline


def _blocked_file(privacy, path, reason):
    location = (repr(path) if _DISPLAY_PATH.fullmatch(path) and privacy.text(path) == path
                else 'a file with a redacted name')
    return f'{location} ({reason})'


def _snapshot(workspaces, current):
    from kyrex.providers.privacy import SecretFilter

    tree, patch, entries = workspaces.sandbox_snapshot(current)
    sensitive_reason = runpy.run_path(str(_ROOT.parent / 'scripts/check_sensitive_paths.py'))['sensitive_reason']
    privacy = SecretFilter()
    blocked = []
    for entry in entries:
        if 'data' not in entry:
            continue
        path = entry['path']
        if sensitive_reason(path):
            reason = 'protected file path'
        else:
            findings = _source_findings(privacy, path, entry['data'])
            if findings is not None and (not findings or _in_public_base(current, path, privacy, findings)):
                continue
            reason = 'possible credential or private identifier'
        blocked.append(_blocked_file(privacy, path, reason))
        if len(blocked) == 5:
            break
    if blocked:
        raise SandboxError('The Rift snapshot includes a sensitive change in ' + ', '.join(blocked)
                           + '. No files were sent to Railway.')
    return current['base_sha'], tree, patch


def _remote_check(config, base, tree, patch, cancelled, progress):
    environment, token = config
    with tempfile.TemporaryDirectory(prefix='kyrex-rift-check-') as temp:
        path = Path(temp)
        (path / 'snapshot.patch').write_bytes(patch)
        with (path / 'progress.jsonl').open('w+') as events:
            command = ['node', str(_ROOT / 'railway-sandbox/run.mjs'), environment,
                       base, tree, str(path / 'snapshot.patch')]
            env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': temp, TOKEN_ENV: token}
            try:
                process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                           stdout=events, stderr=subprocess.DEVNULL)
            except OSError:
                raise SandboxError('The Railway Sandbox runner is unavailable on this host.') from None
            # The runner writes short protocol frames to a regular file so a
            # dropped task viewer or a quiet build cannot block cancellation.
            # Separate open descriptions are required for independent offsets.
            deadline = time.monotonic() + 180
            try:
                with (path / 'progress.jsonl').open() as reader:
                    while True:
                        if cancelled() or time.monotonic() >= deadline:
                            break
                        while line := reader.readline():
                            frame = json.loads(line)
                            if frame.get('stage') in _STAGES:
                                progress(_STAGES[frame['stage']])
                            elif 'result' in frame:
                                return frame['result']
                        if process.poll() is not None:
                            # The process may have written its last line just
                            # after the read above reached EOF.
                            for line in reader:
                                frame = json.loads(line)
                                if 'result' in frame:
                                    return frame['result']
                            return {'outcome': 'unavailable', 'stage': 'starting'}
                        time.sleep(0.2)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    return {'outcome': 'cancelled' if cancelled() else 'timeout'}


def run_task(ctx, owner, task_text, task_id, conversation, on_progress, on_result):
    result = {'mode': 'developer', 'status': 'error'}
    store = CloudTaskStore()

    def progress(stage):
        if on_progress:
            on_progress({'stage': stage, 'category': 'developer'})

    def cancelled():
        return store.is_cancel_requested(task_id)

    try:
        task = store.get(task_id) or {}
        bot = bots.get_bot(ctx.bot_id) or {}
        if (not conversation or task.get('status') != STATUS_RUNNING or task.get('parent_delegation_id')
                or task.get('executor_prefix') != EXECUTOR or task_text != TASK_TEXT
                or (task.get('chat_id'), task.get('bot_id'), task.get('conversation_id'), task.get('rift'))
                != (owner, ctx.bot_id, conversation, bot.get('rift')) or owner != ctx.bot_owner):
            raise SandboxError('The Developer Bot or its Rift changed after this test was queued.')
        if cancelled():
            return
        config = _config(owner)
        workspaces = RepositoryWorkspaces()
        with workspaces.locked(owner, ctx.bot_id):
            current = _workspace(workspaces, owner, bot)
            workspaces.auth(owner, current['repository'])
            base, tree, patch = _snapshot(workspaces, current)
        if cancelled():
            return

        progress('Starting a Railway Sandbox for this Rift snapshot…')
        observed = _remote_check(config, base, tree, patch, cancelled, progress)
        if cancelled() or observed.get('outcome') == 'cancelled':
            return
        outcome = observed.get('outcome')
        code = observed.get('exitCode')
        result['sandbox_snapshot'] = {'base': base, 'tree': tree}
        if outcome in ('success', 'failure') and type(code) is int and 0 <= code <= 255:
            status = 'passed' if code == 0 else f'failed (exit code {code})'
            result.update(status='completed', final_response=(
                f'Kyrex Chat smoke tests {status} in Railway Sandbox.\n\n'
                f'Rift tree `{tree}` (base `{base[:12]}`).\n\n'
                'Railway accepted the request to destroy the sandbox.'), verification=[{
                    'kind': 'tests', 'runner': 'node --test', 'outcome': outcome, 'exit_code': code}])
        elif outcome == 'timeout':
            raise SandboxError('The Railway Sandbox test reached its time limit.')
        else:
            raise SandboxError(_ERRORS.get(observed.get('stage'), _ERRORS['testing']))
    except (GitHubError, SandboxError) as exc:
        result['errors'] = [str(exc)]
    finally:
        store.close()
    if on_result:
        on_result(result)
