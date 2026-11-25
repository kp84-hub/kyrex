"""Safe failure categories for shell commands; never retrieve credentials."""


def command_failure(returncode, output, *, sandboxed, credentials_filtered):
    """Explain an unsuccessful command without echoing its arguments/secrets.

    Credential filtering is evidence about the command environment, not proof
    that a token expired. In particular, a credential helper may still have
    provided a rejected credential. Do not recommend changing credentials until
    the owner has compared the host terminal with the isolated runner.
    """
    lowered = output.lower()
    result = {
        "status": "error",
        "error": f"Command exited with code {returncode}.",
        "error_type": "command_failed",
        "retryable": False,
        "execution_context": {
            "sandboxed": sandboxed,
            "github_token_environment_filtered": credentials_filtered,
        },
    }
    if any(marker in lowered for marker in (
        "fatal: authentication failed", "remote: invalid username or token",
        "fatal: could not read username", "fatal: could not read password",
        "fatal: unable to read askpass response", "permission denied (publickey)",
    )):
        result.update(
            error="Git remote authentication failed; do not assume current remote state or successful publication.",
            error_type="git_authentication",
            recovery=(
                "Stop dependent Git operations and preserve local work. Compare authentication "
                "in the owner's normal terminal with this command runner. If the host also "
                "fails, the owner must repair GitHub authentication or repository access. "
                "If only the runner fails, check sandbox/helper availability and environment "
                "filtering. Do not assume the token expired, repeatedly retry, expose tokens, "
                "disable sandboxing, or replace authentication with a patch transfer."
            ),
        )
    elif sandboxed and any(marker in lowered for marker in (
        "could not resolve host:", "could not resolve hostname", "network is unreachable",
    )):
        result.update(
            error="Network access failed inside the command sandbox.",
            error_type="sandbox_network_unavailable",
            recovery=(
                "The sandbox uses an isolated network namespace. Compare with the host "
                "terminal before diagnosing credentials. Use an authorized host-mediated "
                "Git operation when available; do not weaken command isolation."
            ),
        )
    return result
