"""Offline builder trust bootstrap: no AWS, SSH or global known-hosts edits."""
import base64
import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app import vpn
from app.ami_manager import AMIError, AMIManager
from test_ami_manager import FakeEC2, builder_instance


HOST = '10.0.1.100'
KEY = base64.b64encode(b'offline-public-host-key').decode()
FINGERPRINT = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(KEY)).digest()).decode().rstrip('=')


@pytest.fixture
def scan(monkeypatch):
    response = SimpleNamespace(returncode=0, stdout=f'{HOST} ssh-ed25519 {KEY}\n', stderr='')
    def run(argv, **kwargs):
        assert argv == ['ssh-keyscan', '-T', '8', '-t', 'ed25519', HOST]
        assert kwargs['timeout'] == 10
        return response
    monkeypatch.setattr(vpn.subprocess, 'run', run)
    return response


@pytest.mark.parametrize('encoded', [False, True])
def test_verified_pin_scoped_to_builder_and_removed_after_operation(scan, encoded):
    manager = vpn.VpnManager()
    console = 'Boot host key: ' + FINGERPRINT + ' root@guest\n'
    if encoded:
        console = base64.b64encode(console.encode()).decode()
    with manager.verified_builder_host(HOST, console):
        argv = manager.build_ssh_command(['sync'], host=HOST, user='ubuntu', key_path='/offline/key')
        assert 'StrictHostKeyChecking=yes' in argv
        assert 'HostKeyAlgorithms=ssh-ed25519' in argv
        known = Path(next(x.split('=',1)[1] for x in argv if x.startswith('UserKnownHostsFile=')))
        assert known.read_text() == f'{HOST} ssh-ed25519 {KEY}\n'
        assert known.stat().st_mode & 0o777 == 0o600
        assert known.parent.stat().st_mode & 0o777 == 0o700
        gateway = manager.build_ssh_command(['true'], host='gateway.example', user='ubuntu', key_path='/offline/key')
        assert not any(x.startswith('UserKnownHostsFile=') for x in gateway)
    assert not known.exists() and not known.parent.exists()
    assert manager._builder_known_hosts == {}


@pytest.mark.parametrize('console', ['', 'SHA256:different', FINGERPRINT + 'suffix'])
def test_missing_or_mismatched_console_key_fails_closed(scan, console):
    manager = vpn.VpnManager()
    with pytest.raises(vpn.VpnManagerError, match='could not be verified'):
        with manager.verified_builder_host(HOST, console):
            pytest.fail('Unverified SSH session must not start')
    assert not manager._builder_known_hosts


@pytest.mark.parametrize('output', ['garbage', f'10.0.1.99 ssh-ed25519 {KEY}\n', f'{HOST} ssh-ed25519 !!!\n', f'{HOST} ssh-ed25519 {KEY}\n{HOST} ssh-ed25519 {KEY}\n'])
def test_scan_malformed_or_ambiguous_fails_closed(scan, output):
    scan.stdout = output
    with pytest.raises(vpn.VpnManagerError):
        with vpn.VpnManager().verified_builder_host(HOST, FINGERPRINT):
            pytest.fail('Unsafe scan accepted')


def test_pin_cleaned_when_capture_body_fails(scan):
    manager = vpn.VpnManager()
    with pytest.raises(RuntimeError):
        with manager.verified_builder_host(HOST, FINGERPRINT):
            known = Path(manager._builder_known_hosts[HOST])
            raise RuntimeError('offline failure')
    assert not known.exists() and not manager._builder_known_hosts


@pytest.mark.parametrize('missing_console', [False, True])
def test_capture_does_not_sync_freeze_or_create_when_trust_unavailable(scan, missing_console):
    fake = FakeEC2(instances=builder_instance())
    if missing_console:
        def fail(**kwargs):
            raise ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'offline'}}, 'GetConsoleOutput')
        fake.get_console_output = fail
    manager = AMIManager(dry_run=False, remote=vpn.VpnManager())
    manager._ec2_client = fake
    with pytest.raises(AMIError) as error:
        manager.create_image('i-offline', 'offline')
    assert not error.value.ambiguous
    assert fake.created == []
    if missing_console:
        assert 'AccessDenied' in str(error.value)
    else:
        assert 'could not be verified against its EC2 console' in str(error.value)
        assert 'reboot' in str(error.value)


def test_same_console_pin_used_for_sync_freeze_and_release(scan, monkeypatch):
    fake = FakeEC2(instances=builder_instance())
    fake.get_console_output = lambda **kwargs: {'Output': FINGERPRINT}
    remote = vpn.VpnManager()
    pins = []
    def sync(host, args, timeout=None):
        assert args == ['sync']
        pins.append(remote._builder_known_hosts[host])
        assert Path(pins[-1]).exists()
    @contextmanager
    def freeze(host, timeout=None):
        pins.append(remote._builder_known_hosts[host])
        try:
            yield
        finally:
            pins.append(remote._builder_known_hosts[host])
            assert Path(pins[-1]).exists()
    monkeypatch.setattr(remote, 'run_builder_command', sync)
    monkeypatch.setattr(remote, 'frozen_builder', freeze)
    manager = AMIManager(dry_run=False, remote=remote)
    manager._ec2_client = fake
    assert manager.create_image('i-offline','offline') == 'ami-0created'
    assert len(pins) == 3 and len(set(pins)) == 1
    assert not Path(pins[0]).exists()
    assert 'ClientToken' not in fake.created[0]


def test_reboot_console_without_identity_still_fails_closed(scan):
    # A real reboot log is not proof of the scanned key's identity.
    with pytest.raises(vpn.VpnManagerError):
        with vpn.VpnManager().verified_builder_host(HOST, 'Ubuntu 24.04 ttyS0\n'):
            pytest.fail('Missing identity must not fall back to TOFU')


def test_builder_bootstrap_emits_public_identity_on_every_boot(tmp_path, monkeypatch):
    """Run the actual bootstrap in a disposable tree with a systemctl adapter.

    No local service, SSH connection, guest, AWS call or host file is changed.
    Exercise its generated unit with a real disposable OpenSSH host key, then
    feed the boot output into the production console verification function.
    """
    import os
    import shlex
    import subprocess

    root = Path(__file__).resolve().parent.parent
    source = (root / 'infrastructure/terraform/builder/modules/builder_env/bootstrap.sh').read_text()
    units = tmp_path / 'units'
    units.mkdir()
    commands = tmp_path / 'commands'
    commands.mkdir()
    calls = tmp_path / 'systemctl-calls'
    adapter = commands / 'systemctl'
    adapter.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    adapter.chmod(0o700)
    env = dict(os.environ, PATH=str(commands) + ':' + os.environ['PATH'], CALLS=str(calls))
    subprocess.run(['bash', '-eu'], input=source.replace('/etc/systemd/system/', str(units) + '/'),
                   text=True, env=env, capture_output=True, check=True)
    unit = (units / 'secforge-builder-host-identity.service').read_text()
    assert 'enable --now secforge-builder-host-identity.service' in calls.read_text()
    assert 'WantedBy=multi-user.target' in unit
    assert 'After=ssh.service cloud-init.service' in unit
    assert 'StandardOutput=tty' in unit and 'TTYPath=/dev/ttyS0' in unit
    assert 'TTYReset=no' in unit and 'TTYVHangup=no' in unit
    args = shlex.split(next(line.removeprefix('ExecStart=') for line in unit.splitlines()
                            if line.startswith('ExecStart=')))
    assert args == ['/usr/bin/ssh-keygen', '-E', 'sha256', '-lf', '/etc/ssh/ssh_host_ed25519_key.pub']
    key = tmp_path / 'host-key'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    public = key.with_suffix('.pub').read_text().split()
    # First boot and subsequent boot execute the same public-only command;
    # no cached IP/key, credentials, regeneration or process restart is used.
    for _ in range(2):
        console = subprocess.check_output(args[:-1] + [str(key.with_suffix('.pub'))], text=True)
        with monkeypatch.context() as patch:
            patch.setattr(vpn.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
                returncode=0, stdout=f'{HOST} {public[0]} {public[1]}\n'))
            with vpn.VpnManager().verified_builder_host(HOST, console):
                pass
        assert key.read_text() not in console
