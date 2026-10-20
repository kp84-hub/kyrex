"""Use the existing profile owner's OS identity for its browser worker.

The authenticated agent retains its connection and profile lock. Only its
operator subprocess drops identity, so atomic Chrome writes remain readable by
the non-root private viewer. No UID is accepted from a Cloud task or model.
"""
import os
from pathlib import Path
import stat


CONTROL_FILES = ('Local State', 'Default/Preferences')


def profile_info(raw):
    path = Path(raw).absolute()
    if path != path.resolve(strict=True):
        raise ValueError('Browser profile identity requires a real directory.')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('Browser profile identity requires a real directory.')
    return path, info


def operator_options(environment):
    env = dict(environment)
    # Browser/page processes never need the enrolled agent's transport secret
    # or private viewer password, including after the UID transition.
    for key in ('KYREX_HOST_ENROLLMENT_SECRET', 'KYREX_VIEWER_VNC_PASSWORD'):
        env.pop(key, None)
    raw = env.get('KYREX_BROWSER_SESSION_DIR')
    if not raw or not hasattr(os, 'geteuid'):
        return env, {}
    profile, info = profile_info(raw)
    if info.st_uid == 0:
        return env, {}  # Existing root-only profiles retain their current path.
    if os.geteuid() not in (0, info.st_uid):
        raise ValueError('The browser worker cannot use this profile owner.')
    import pwd
    try:
        account = pwd.getpwuid(info.st_uid)
    except KeyError:
        raise ValueError('The browser profile owner needs a local runtime account.') from None
    home = Path(account.pw_dir)
    if not home.is_dir() or home.stat().st_uid != info.st_uid:
        raise ValueError('The browser profile owner needs its own runtime home.')
    env.update(HOME=str(home), USER=account.pw_name, LOGNAME=account.pw_name)
    if not env.get('KYREX_FS_ROOT') and not env.get('KYREX_BROWSER_ROOT'):
        # The previous global /tmp workspace may have been made by root. A
        # private profile-local artifact directory avoids sharing or chowning it.
        fd = os.open(profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                os.mkdir('.kyrex-operator-files', 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                created = False
            child = os.open('.kyrex-operator-files', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if created and os.geteuid() == 0:
                    os.fchown(child, info.st_uid, account.pw_gid)
                current = os.fstat(child)
                if current.st_uid != info.st_uid or current.st_mode & 0o077:
                    raise ValueError('The browser artifact directory must belong privately to its profile owner.')
            finally:
                os.close(child)
        finally:
            os.close(fd)
        env['KYREX_FS_ROOT'] = str(profile / '.kyrex-operator-files')
    identity = ({'user': info.st_uid, 'group': account.pw_gid,
                 'extra_groups': [], 'umask': 0o077} if os.geteuid() == 0 else {})
    return env, identity


def repair_control_files(profile):
    """Caller must hold the existing profile lock; touch only confirmed files."""
    if os.geteuid() != 0:
        raise ValueError('Chrome control-file ownership repair requires the host administrator.')
    path, owner = profile_info(profile)
    if owner.st_uid == 0:
        raise ValueError('This repair requires an existing non-root-owned profile.')
    changed = []
    root = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in CONTROL_FILES:
            parent = root
            try:
                parts = name.split('/')
                if len(parts) > 1:
                    parent = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o6000:
                        raise ValueError('Chrome control-file repair requires a regular file with one link.')
                    if info.st_uid == 0:
                        os.fchown(fd, owner.st_uid, owner.st_gid)
                        changed.append(name)
                    elif info.st_uid != owner.st_uid:
                        raise ValueError('Chrome control-file repair found an unrelated owner.')
                finally:
                    os.close(fd)
            except FileNotFoundError:
                continue
            finally:
                if parent != root:
                    os.close(parent)
    finally:
        os.close(root)
    return changed


def main():
    import argparse
    import json
    import manual_mode
    import profiles
    parser = argparse.ArgumentParser(description='Repair ownership of two Chrome control files under the profile lock.')
    parser.add_argument('--owner', required=True)
    parser.add_argument('--bot', required=True)
    args = parser.parse_args()
    if args.owner != os.environ.get('KYREX_HOST_OWNER'):
        raise ValueError('The profile repair must name this enrolled host owner.')
    profile = profiles.profile_dir(args.owner, args.bot)
    with manual_mode.acquire(args.owner, args.bot, kind=manual_mode.KIND_AUTOMATION, ttl=60):
        print(json.dumps({'repaired': repair_control_files(profile)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never dump profile contents, arbitrary paths or an exception payload.
        print('Chrome control-file repair refused (' + type(exc).__name__ + '). End the viewer and verify the enrolled profile.')
        raise SystemExit(1) from None
