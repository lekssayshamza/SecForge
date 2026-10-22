"""Offline builder internet regression: real model queries, mocked gateway."""
from datetime import timedelta
from unittest.mock import MagicMock
import pytest
from app.builder_egress import COMMENT, NON_PUBLIC, desired_builder_ips, render_policy
from app.config import Settings, settings
from app.lab_time import utc_now
from app.vpn_authz import (VpnAuthorizationError, authorize_builder_access,
                          find_filter_rule_handle, render_nft_transaction, sync_vpn_authz)
from test_vpn_authz import (engine, db, key_file, enabled_vpn, authz_on,
                           make_user, make_room, make_builder, make_lab, make_identity)

@pytest.fixture
def internet(authz_on, monkeypatch):
    monkeypatch.setattr(settings, 'builder_internet_enabled', True)
    monkeypatch.setattr(settings, 'builder_internet_gateway_interface', 'ens5')
    monkeypatch.setattr(settings, 'vpn_backend_authz_enabled', False)


def test_default_off():
    assert Settings(_env_file=None, jwt_secret="offline-test-signing-secret-not-real").builder_internet_enabled is False


def test_only_registered_running_creator_builders(db, internet):
    creator = make_user(db, 'creator', 'creator'); room = make_room(db, creator)
    builder = make_builder(db, creator, room)
    learner = make_user(db, 'learner'); make_lab(db, learner, room, private_ip='10.0.1.137')
    make_builder(db, learner, room, private_ip='10.0.1.202')
    for state, ip in [('stopped','203'),('failed','204'),('stopping','205')]:
        owner=make_user(db, 'other'+ip, 'creator')
        make_builder(db, owner, room, status=state, private_ip='10.0.1.'+ip)
    make_builder(db, make_user(db, 'missing', 'creator'), room, instance_id=None, private_ip='10.0.1.206')
    make_builder(db, make_user(db, 'expired', 'creator'), room, expires_at=utc_now()-timedelta(seconds=1), private_ip='10.0.1.207')
    assert desired_builder_ips(db) == {builder.private_ip}


@pytest.mark.parametrize('state', ['stopping', 'stopped', 'failed'])
def test_stop_failure_deletion_remove_egress(db, internet, state):
    creator = make_user(db, 'creator', 'creator'); room = make_room(db, creator)
    builder = make_builder(db, creator, room)
    assert desired_builder_ips(db) == {builder.private_ip}
    builder.status = state; db.commit()
    assert desired_builder_ips(db) == set()
    db.delete(builder); db.commit()
    assert desired_builder_ips(db) == set()


def test_transaction_keeps_vpn_and_exact_egress_separate(internet):
    transaction = render_nft_transaction({('10.8.0.65','10.0.1.137')}, 31,
                        builder_ips={'10.0.1.201'}, existing_egress_handle=32)
    assert 'delete rule inet filter forward handle 31' in transaction
    assert 'delete rule inet filter forward handle 32' in transaction
    vpn = next(line for line in transaction.splitlines() if 'comment "secforge-vpn-authz"' in line)
    assert 'iifname "tun0"' in vpn and '10.8.0.65 . 10.0.1.137' in vpn
    egress = next(line for line in transaction.splitlines() if f'comment "{COMMENT}"' in line)
    assert 'iifname "ens5" oifname "ens5"' in egress
    assert 'ip saddr { 10.0.1.201 }' in egress and 'tcp dport { 80, 443 }' in egress
    assert '10.0.1.137' not in egress and '10.8.0.65' not in egress
    assert all(net in egress for net in NON_PUBLIC)
    assert 'flush ruleset' not in transaction and 'policy drop' not in transaction
    assert transaction.count('masquerade') == 1
    nat = next(line for line in transaction.splitlines() if 'masquerade' in line)
    assert 'ip saddr { 10.0.1.201 }' in nat and 'iifname' not in nat and 'oifname "ens5"' in nat
    assert 'hook forward priority -10' in transaction
    assert 'counter drop' in transaction


def test_no_builder_means_no_accept_or_nat(internet):
    policy, accept = render_policy(set())
    assert not accept and 'masquerade' not in policy
    assert 'ip saddr 10.0.1.0/24' in policy and 'counter drop' in policy


@pytest.mark.parametrize('interface', ['', 'tun0', 'tap2', 'lo', 'ens5"; accept', 'ens5\n', 'x'*16])
def test_invalid_interface_fails_closed(internet, monkeypatch, interface):
    monkeypatch.setattr(settings, 'builder_internet_gateway_interface', interface)
    with pytest.raises(VpnAuthorizationError):render_policy({'10.0.1.201'})


@pytest.mark.parametrize('address', ['10.8.0.65', '10.0.2.5', '10.0.1.201;accept', '::1'])
def test_invalid_address_fails_closed(internet, address):
    with pytest.raises(VpnAuthorizationError):render_policy({address})


def test_duplicate_marker_refused():
    with pytest.raises(VpnAuthorizationError):
        find_filter_rule_handle('accept comment "secforge-builder-egress" # handle 3\naccept comment "secforge-builder-egress" # handle 4', COMMENT)


def test_rule_handle_collision_refused(internet):
    with pytest.raises(VpnAuthorizationError):
        render_nft_transaction(set(), 3, builder_ips=set(), existing_egress_handle=3)


def test_uncommitted_builder_registered_and_worker_restores(db, internet, monkeypatch):
    creator=make_user(db,'creator','creator');room=make_room(db,creator)
    make_identity(db,creator,'10.8.0.65')
    builder=make_builder(db,creator,room,status='pending',private_ip=None,instance_id=None)
    db.commit(); db.autoflush=False
    builder.status='running';builder.private_ip='10.0.1.201';builder.instance_id='i-offline-builder'
    manager=MagicMock();manager.list_forward_chain_rules.return_value=''
    monkeypatch.setattr('app.vpn_authz.VpnManager', lambda:manager)
    authorize_builder_access(db,builder)
    assert manager.sync_forward_rules.call_args.kwargs['builder_ips']=={'10.0.1.201'}
    assert ('10.8.0.65','10.0.1.201') in manager.sync_forward_rules.call_args.args[0]
    db.commit();sync_vpn_authz(db)
    assert manager.sync_forward_rules.call_args.kwargs['builder_ips']=={'10.0.1.201'}
    builder.status='stopping';db.commit();sync_vpn_authz(db)
    assert manager.sync_forward_rules.call_args.kwargs['builder_ips']==set()
    assert manager.sync_forward_rules.call_args.args[0]==set()


def test_disabled_preserves_existing_transport(db, authz_on, monkeypatch):
    monkeypatch.setattr(settings,'builder_internet_enabled',False)
    manager=MagicMock();manager.list_forward_chain_rules.return_value=''
    monkeypatch.setattr('app.vpn_authz.VpnManager', lambda:manager)
    sync_vpn_authz(db)
    manager.sync_forward_rules.assert_called_once_with(set(),existing_rule_handle=None)


def test_disabling_existing_policy_revokes_even_established_egress(db, internet, monkeypatch):
    monkeypatch.setattr(settings, 'builder_internet_enabled', False)
    manager=MagicMock()
    manager.list_forward_chain_rules.return_value='accept comment "secforge-builder-egress" # handle 42'
    monkeypatch.setattr('app.vpn_authz.VpnManager', lambda:manager)
    sync_vpn_authz(db)
    manager.sync_forward_rules.assert_called_once_with(
        set(), existing_rule_handle=None, builder_ips=set(), existing_egress_handle=42)
    transaction=render_nft_transaction(set(), builder_ips=set(), existing_egress_handle=42)
    assert 'counter drop' in transaction and 'masquerade' not in transaction


def test_empty_instance_identifier_is_not_registered(db, internet):
    creator=make_user(db,'creator','creator');room=make_room(db,creator)
    make_builder(db,creator,room,instance_id='')
    assert desired_builder_ips(db)==set()
