"""Offline tests for the opt-in dual-VPN hooks; real kernel checks separate."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

from app.vpn import render_client_profile
from test_vpn_profile import CA_PEM, CERT_PEM, KEY_PEM, TA_PEM


def load(name):
    loader = importlib.machinery.SourceFileLoader(name, str(Path(__file__).parent / 'deploy' / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def profile():
    return render_client_profile(host='vpn.example.test', port=1194,
        ca_certificate=CA_PEM, client_certificate=CERT_PEM,
        client_key=KEY_PEM, tls_crypt_key=TA_PEM)


@pytest.fixture
def hook(monkeypatch):
    m = load('learner_routes')
    monkeypatch.setattr(m, 'load_config', lambda: ('10.0.1.0/24', m.network('10.8.0.0/24'), m.ipaddress.IPv4Address('10.8.0.67'), 50))
    return m


def event(device='tun7', action='route-up', source='10.8.0.65'):
    return dict(dev=device, script_type=action, ifconfig_local=source, route_vpn_gateway='10.8.0.1')


def simulator(monkeypatch, hook, devices, routes, kind='tun'):
    mutations = []
    def ip(*args):
        if args[:3] == ('-j', '-d', 'address'):
            dev = args[-1]
            return json.dumps([{'linkinfo': {'info_kind': kind}, 'addr_info': [{'local': devices[dev]}]}])
        if args[:3] == ('-j', 'route', 'show'):
            return json.dumps(routes)
        mutations.append(args)
        if args[1] == 'add':
            routes.append(dict(dev=args[6], gateway=args[4], prefsrc=args[8], metric=int(args[10])))
        else:
            routes[:] = [r for r in routes if not (r['dev'] == args[6] and r['prefsrc'] == args[8] and r['metric'] == int(args[10]))]
        return ''
    monkeypatch.setattr(hook, 'ip', ip)
    return mutations


@pytest.mark.parametrize('management_present', [False, True])
@pytest.mark.parametrize('device', ['tun0', 'tun9', 'learner-test'])
@pytest.mark.parametrize('kind', ['tun', 'ovpn'])
def test_route_binds_learner_device_and_source_even_with_shared_gateway(monkeypatch, hook, management_present, device, kind):
    routes = [dict(dev='management', gateway='10.8.0.1', metric=100)] if management_present else []
    calls = simulator(monkeypatch, hook, {device: '10.8.0.65'}, routes, kind)
    hook.sync(event(device))
    assert calls == [('route', 'add', '10.0.1.0/24', 'via', '10.8.0.1', 'dev', device, 'src', '10.8.0.65', 'metric', '50')]
    assert min(routes, key=lambda r: r['metric'])['dev'] == device
    assert min(routes, key=lambda r: r['metric'])['prefsrc'] == '10.8.0.65'


def test_disconnect_and_reconnect_with_new_interface_is_idempotent(monkeypatch, hook):
    management = dict(dev='management', gateway='10.8.0.1', metric=100)
    routes = [management.copy()]
    calls = simulator(monkeypatch, hook, {'tun3': '10.8.0.65', 'tun8': '10.8.0.65'}, routes)
    hook.sync(event('tun3')); hook.sync(event('tun3'))
    assert len(calls) == 1
    hook.sync(event('tun3', 'route-pre-down')); hook.sync(event('tun3', 'route-pre-down'))
    assert routes == [management]
    hook.sync(event('tun8'))
    assert len(calls) == 3 and routes[-1]['dev'] == 'tun8'


def test_development_hook_overrides_both_misbound_plain_openvpn_routes(monkeypatch, hook):
    # Reproduced on Kali: both the management and ordinary user subnet
    # routes resolve their shared gateway through the management device.
    # Merely having the user's tunnel connected does not select its identity.
    routes = [dict(dev='management', gateway='10.8.0.1', metric=metric)
              for metric in (100, 200)]
    original = [r.copy() for r in routes]
    calls = simulator(monkeypatch, hook, {'user-tunnel': '10.8.0.65'}, routes)
    hook.sync(event('user-tunnel'))
    selected = min(routes, key=lambda r: r['metric'])
    assert selected['dev'] == 'user-tunnel'
    assert selected['prefsrc'] == '10.8.0.65'
    assert routes[:2] == original
    hook.sync(event('user-tunnel', 'route-pre-down'))
    assert routes == original
    assert len(calls) == 2


@pytest.mark.parametrize('missing', [('route_vpn_gateway',), ('route_vpn_gateway', 'ifconfig_local')])
def test_restart_teardown_handles_cleared_openvpn_variables(monkeypatch, hook, missing):
    routes = [];calls = simulator(monkeypatch, hook, {'tun7': '10.8.0.65'}, routes)
    hook.sync(event())
    down = event(action='route-pre-down')
    for key in missing:down.pop(key)
    hook.sync(down);hook.sync(down)
    assert routes == [] and len(calls) == 2


def test_other_identity_route_is_never_replaced_or_deleted(monkeypatch, hook):
    other = dict(dev='other', gateway='10.8.0.1', prefsrc='10.8.0.66', metric=50)
    routes = [other.copy()];calls = simulator(monkeypatch, hook, {'tun7': '10.8.0.65'}, routes)
    with pytest.raises(hook.RouteError):hook.sync(event())
    hook.sync(event(action='route-pre-down'))
    assert routes == [other] and calls == []


def test_higher_priority_subnet_route_fails_closed(monkeypatch, hook):
    routes = [dict(dev='management', gateway='10.8.0.1', metric=0)]
    calls = simulator(monkeypatch, hook, {'tun7': '10.8.0.65'}, routes)
    with pytest.raises(hook.RouteError):hook.sync(event())
    assert calls == [] and routes[0]['metric'] == 0


@pytest.mark.parametrize('update', [dict(dev='tun0;evil'), dict(dev='../tun0'),
    dict(ifconfig_local='10.8.0.67'), dict(ifconfig_local='10.0.1.2'),
    dict(route_vpn_gateway='1.1.1.1'), dict(script_type='up')])
def test_invalid_event_rejected_before_route_mutation(hook, monkeypatch, update):
    calls = simulator(monkeypatch, hook, {}, [])
    with pytest.raises(hook.RouteError):hook.sync({**event(), **update})
    assert calls == []


def test_device_must_own_certificate_assigned_source(monkeypatch, hook):
    calls = simulator(monkeypatch, hook, {'tun7': '10.8.0.66'}, [])
    with pytest.raises(hook.RouteError):hook.sync(event())
    assert calls == []


def test_physical_interface_is_not_an_eligible_vpn(monkeypatch, hook):
    calls = simulator(monkeypatch, hook, {'tun7': '10.8.0.65'}, [], kind='ether')
    with pytest.raises(hook.RouteError):hook.sync(event())
    assert calls == []


@pytest.mark.parametrize('cidr', ['0.0.0.0/0', '127.0.0.0/8', '8.8.8.0/24', '::/0', '10.0.1.1/24'])
def test_no_public_or_ambiguous_route_configuration(cidr):
    m = load('learner_routes')
    with pytest.raises(m.RouteError):m.network(cidr)


def test_config_must_be_root_owned(tmp_path, monkeypatch):
    m = load('learner_routes');p = tmp_path / 'config.json'
    p.write_text('{}');monkeypatch.setattr(m, 'CONFIG', p)
    assert p.stat().st_uid != 0
    with pytest.raises(m.RouteError):m.load_config()


def test_config_symlink_is_rejected(tmp_path, monkeypatch):
    m = load('learner_routes');target = tmp_path / 'target';target.write_text('{}')
    p = tmp_path / 'config';p.symlink_to(target);monkeypatch.setattr(m, 'CONFIG', p)
    with pytest.raises(OSError):m.load_config()


def test_standard_profile_unchanged_and_accepted_by_development_launcher():
    text = profile();load('learner_vpn').validate_profile(text)
    assert 'dev tun\n' in text
    assert 'route-up' not in text and 'script-security' not in text and 'pull-filter' not in text
    assert 'remote-cert-tls server\n' in text and '<tls-crypt>' in text


@pytest.mark.parametrize('extra', ['up /tmp/evil', 'plugin /tmp/evil', 'script-security 2', 'dev tun1'])
def test_launcher_rejects_executable_or_fixed_interface_profile_options(extra):
    with pytest.raises(ValueError):load('learner_vpn').validate_profile(profile()+'\n'+extra+'\n')


@pytest.mark.parametrize("filename", ["client.ovpn", "secforge-testlearner2004.ovpn", "secforge-renamed-user.ovpn"])
def test_launcher_uses_private_snapshot_and_automatic_route_hooks(tmp_path, monkeypatch, filename):
    import os
    import stat
    from types import SimpleNamespace
    m = load('learner_vpn');p = tmp_path / filename;p.write_text(profile());p.chmod(0o600)
    uid = p.stat().st_uid;real_stat = os.stat;captured = {}
    monkeypatch.setattr(m.os, 'geteuid', lambda: 0)
    monkeypatch.setenv('SUDO_UID', str(uid))
    monkeypatch.setattr(m.os, 'stat', lambda path, **kw: SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755) if str(path)==m.HOOK else real_stat(path, **kw))
    monkeypatch.setattr(m.sys, 'argv', ['learner_vpn', str(p)])
    def execute(executable, args):
        captured.update(executable=executable, args=args)
        fd = int(args[args.index('--config')+1].rsplit('/', 1)[1])
        captured['fd'] = fd
        assert os.pread(fd, 10000, 0).decode() == profile()
        assert os.get_inheritable(fd)
        assert os.fstat(fd).st_mode & 0o077 == 0
    monkeypatch.setattr(m.os, 'execv', execute)
    try:
        m.main()
        assert captured['executable'] == '/usr/sbin/openvpn'
        args = captured['args']
        assert '--route-noexec' in args and '--up-restart' in args
        assert '--disable-dco' in args
        assert args[args.index('--route-up')+1] == m.HOOK
        assert args[args.index('--route-pre-down')+1] == m.HOOK
        assert str(p) not in args
    finally:
        if 'fd' in captured:os.close(captured['fd'])
