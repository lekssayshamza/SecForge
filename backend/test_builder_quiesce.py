"""Offline persistent-transport regression tests; no SSH or filesystem freeze."""
import os
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ParamValidationError, ReadTimeoutError

from app import vpn as module
from app.ami_manager import AMIManager, AMIError, ImageUnfreezeError, AmbiguousImageCreateError
from app.vpn import VpnManager, VpnManagerError
from test_ami_manager import FakeEC2, builder_instance


class PipeSession:
    """Real local pipes, simulated remote guest; no subprocess is launched."""
    def __init__(self, fail_freeze=False, fail_release=False):
        read, self.writer = os.pipe()
        self.stdout = os.fdopen(read, 'rb', buffering=0)
        read, writer = os.pipe()
        self.stderr = os.fdopen(read, 'rb', buffering=0)
        os.close(writer)
        self.stdin = self
        self.closed = False
        self.returncode = None
        self.frozen = False
        self.released = False
        self.program = None
        self.fail_freeze = fail_freeze
        self.fail_release = fail_release
        self.release_requests = 0
        self.terminations = 0

    def write(self, data):
        if self.program is None:
            self.program = data.decode()
            self.frozen = True
            if self.fail_freeze:
                os.close(self.writer)
                self.writer = None
            else:
                os.write(self.writer, b'SF_FROZEN\n')
        else:
            assert data == b'UNFREEZE\n'
            self.release_requests += 1
            if self.fail_release:
                os.close(self.writer)
                self.writer = None
                raise BrokenPipeError('simulated lost transport')
            self.released = True
            self.frozen = False
            os.write(self.writer, b'SF_UNFROZEN\n')
            self.returncode = 0

    def flush(self): pass
    def close(self): self.closed = True
    def poll(self): return self.returncode
    def wait(self, timeout=None):
        assert self.returncode is not None
        return self.returncode
    def terminate(self):
        self.terminations += 1
        self.returncode = -15
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None
    def kill(self): self.returncode = -9
    def dispose(self):
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None


@pytest.fixture
def transport(monkeypatch):
    sessions = []
    flags = {}
    def popen(argv, **kwargs):
        assert argv[0] == 'ssh'
        assert argv[-1] == 'sudo -n bash -s'
        assert 'StrictHostKeyChecking=yes' in argv
        assert 'BatchMode=yes' in argv
        assert 'PasswordAuthentication=no' in argv
        session = PipeSession(**flags)
        sessions.append(session)
        return session
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    # These pipe tests exercise the authenticated freeze/release session.
    # Console-backed trust verification is covered separately without SSH.
    monkeypatch.setattr(module.VpnManager, 'verified_builder_host',
                        lambda *args: nullcontext())
    monkeypatch.setattr(module.settings, 'builder_ssh_key_path', '/offline/test-key')
    yield sessions, flags
    for session in sessions: session.dispose()


def manager(fake):
    result = AMIManager(dry_run=False, remote=VpnManager())
    result._ec2_client = fake
    # The separately tested sync transport does not run in these pipe tests.
    result._flush_builder = MagicMock()
    return result


def test_same_authenticated_session_freezes_and_releases(transport):
    sessions, _ = transport
    with VpnManager().frozen_builder('10.0.1.55', timeout=1):
        assert len(sessions) == 1 and sessions[0].frozen
    assert len(sessions) == 1
    session = sessions[0]
    assert session.released and not session.frozen
    assert session.release_requests == 1 and session.returncode == 0
    assert session.stdin.closed and session.stdout.closed and session.stderr.closed


def test_exception_inside_capture_still_releases_same_session(transport):
    sessions, _ = transport
    with pytest.raises(RuntimeError, match='capture failure'):
        with VpnManager().frozen_builder('10.0.1.55', timeout=1):
            raise RuntimeError('capture failure')
    assert len(sessions) == 1 and sessions[0].released


def test_missing_freeze_acknowledgment_refuses_creation_and_closes_transport(transport):
    sessions, flags = transport
    flags['fail_freeze'] = True
    fake = FakeEC2(instances=builder_instance())
    with pytest.raises(AMIError, match='Failed to freeze'):
        manager(fake).create_image('i-offline-builder', 'offline-test')
    assert fake.created == []
    assert sessions[0].terminations == 1
    assert 'trap' in sessions[0].program  # EOF/guest deadline protects partial freeze.


def test_real_create_path_only_runs_while_session_is_frozen(transport):
    sessions, _ = transport
    fake = FakeEC2(instances=builder_instance())
    original = fake.create_image
    def create(**params):
        assert len(sessions) == 1 and sessions[0].frozen
        assert 'ClientToken' not in params
        return original(**params)
    fake.create_image = create
    assert manager(fake).create_image('i-offline-builder', 'offline-test') == 'ami-0created'
    assert len(fake.created) == 1 and sessions[0].released


@pytest.mark.parametrize('error,expected', [
    (ParamValidationError(report='invalid offline request'), AMIError),
    (ReadTimeoutError(endpoint_url='https://offline', error='lost response'), AmbiguousImageCreateError),
])
def test_sdk_failures_release_without_reauthenticating_or_retrying(transport, error, expected):
    sessions, _ = transport
    fake = FakeEC2(instances=builder_instance(), create_error=error)
    with pytest.raises(expected):
        manager(fake).create_image('i-offline-builder', 'offline-test')
    assert len(fake.created) == 1 and len(sessions) == 1 and sessions[0].released


def test_lost_release_ack_keeps_known_image_and_never_starts_second_ssh(transport):
    sessions, flags = transport
    flags['fail_release'] = True
    fake = FakeEC2(instances=builder_instance())
    with pytest.raises(ImageUnfreezeError) as error:
        manager(fake).create_image('i-offline-builder', 'offline-test')
    assert error.value.image_id == 'ami-0created'
    assert len(fake.created) == len(sessions) == 1
    assert sessions[0].terminations == 1


def test_guest_guard_is_installed_before_freeze_and_covers_disconnect(transport):
    sessions, _ = transport
    with VpnManager().frozen_builder('10.0.1.55', timeout=1):
        script = sessions[0].program
        assert script.index('trap') < script.index('fsfreeze -f')
        assert 'HUP INT TERM' in script and 'EXIT' in script
        assert f'read -r -t {module.BUILDER_FREEZE_GUARD_SECONDS}' in script
        assert 'fsfreeze -u /' in script


def test_sdk_single_attempt_deadline_fits_guest_guard(monkeypatch):
    config = {}
    def client(service, **kwargs):
        config.update(kwargs)
        return object()
    monkeypatch.setattr('app.ami_manager.boto3.client', client)
    assert AMIManager(dry_run=False).ec2_client is not None
    conf = config['config']
    assert conf.retries['total_max_attempts'] == 1
    assert conf.connect_timeout + conf.read_timeout < module.BUILDER_FREEZE_GUARD_SECONDS


def test_request_serialization_failure_never_freezes_builder(transport):
    sessions, _ = transport
    fake = FakeEC2(instances=builder_instance())
    class InvalidValue:
        def __str__(self):
            raise ValueError("invalid tag value")
    with pytest.raises(ValueError, match="invalid tag value"):
        manager(fake).create_image('i-offline-builder', 'offline-test',
                                   tags={"invalid": InvalidValue()})
    assert sessions == [] and fake.created == []
