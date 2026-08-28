"""External-repo writes escalate to T2; own-repo and reads are unchanged."""
import serve


def test_external_pr_escalates_to_t2():
    assert serve.derive_host_tier("repo:pr", is_external=True) == 2


def test_external_push_is_t2():
    assert serve.derive_host_tier("repo:push", is_external=True) == 2


def test_own_pr_stays_t1():
    assert serve.derive_host_tier("repo:pr", is_external=False) == 1


def test_own_push_stays_t2():
    assert serve.derive_host_tier("repo:push", is_external=False) == 2


def test_external_read_not_escalated():
    assert serve.derive_host_tier("repo:read", is_external=True) == 0


def test_external_fs_write_escalates():
    assert serve.derive_host_tier("fs:write", is_external=True) == 2


def test_external_default_off_preserves_base():
    # is_external defaults to False -> no escalation for callers not passing it
    assert serve.derive_host_tier("repo:pr") == 1
