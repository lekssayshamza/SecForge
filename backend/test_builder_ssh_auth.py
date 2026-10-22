"""Guest-only SSH controls: owner checks, secret transport and rollback."""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import subprocess
import shutil
import pytest
from pydantic import ValidationError
from test_challenges import env
from app.models import BuilderEnvironment
from app.guest_ssh import SshAuthWrite, script, PROGRAM
from app.config import settings
from app.ops_lock import single_flight, builder_lock_key


def builder(e, **values):
    with e.maker() as db:
        b=BuilderEnvironment(room_id=e.room,creator_id=e.users['creator'],status='running',instance_id='i-isolated-builder',private_ip='10.0.1.50',expires_at=datetime.utcnow()+timedelta(hours=1),terraform_state_path='isolated',**values)
        db.add(b);db.commit();return b.id


def post(e, role='creator', **data):
    return e.client.post(f'/api/rooms/{e.room}/builder/ssh-auth',headers=e.headers[role],json={'mode':'key','username':'training',**data})


@pytest.mark.parametrize('mode', ['key','password'])
def test_mode_saved_only_after_verified_guest_success(env,mode):
    bid=builder(env)
    data={'mode':mode,**({'password':'synthetic-training-only'} if mode=='password' else {})}
    with patch('app.builder_ssh.configure') as apply:
        r=post(env,**data);assert r.status_code==200
        assert r.json()['ssh_auth_mode']==mode and r.json()['ssh_guest_username']=='training'
        assert 'synthetic-training-only' not in r.text and 'password_hash' not in r.text
        assert apply.call_count==1
    with env.maker() as db:assert db.get(BuilderEnvironment,bid).ssh_auth_mode==mode


@pytest.mark.parametrize('data', [{'mode':'password'},{'mode':'password','password':'short'}, {'mode':'password','password':'synthetic\nnewline'}, {'username':'root'},{'username':'ubuntu'},{'username':'training;id'},{'mode':'anything'},{'creator_id':99}])
def test_bad_inputs_do_not_echo_password_or_execute(env,data):
    builder(env)
    with patch('app.builder_ssh.configure') as apply:
        r=post(env,**data);assert r.status_code==422 and 'synthetic' not in r.text
        apply.assert_not_called()


@pytest.mark.parametrize('role,status',[('learner',403),('admin',404),('other',404)])
def test_only_owned_builder_can_be_configured(env,role,status):
    builder(env)
    with patch('app.builder_ssh.configure') as apply:
        assert post(env,role).status_code==status;apply.assert_not_called()


def test_stopped_expired_theory_and_capture_conflicts(env):
    bid=builder(env)
    with patch('app.builder_ssh.configure') as apply:
        with single_flight(builder_lock_key(bid)): assert post(env).status_code==409
        with env.maker() as db:
            b=db.get(BuilderEnvironment,bid);b.expires_at=datetime.utcnow()-timedelta(seconds=1);db.commit()
        assert post(env).status_code==409
        with env.maker() as db:
            b=db.get(BuilderEnvironment,bid);b.status='stopped';db.commit()
        assert post(env).status_code==409
        from app.models import Room
        with env.maker() as db:db.get(Room,env.room).room_type='THEORY';db.commit()
        assert post(env).status_code==409
        apply.assert_not_called()


def test_remote_failure_is_generic_and_preserves_record(env):
    bid=builder(env)
    with patch('app.builder_ssh.configure',side_effect=RuntimeError('synthetic-secret-do-not-return')):
        r=post(env,mode='password',password='synthetic-training-only')
        assert r.status_code==409 and 'synthetic' not in r.text
    with env.maker() as db:assert db.get(BuilderEnvironment,bid).ssh_auth_mode=='key'


def test_script_transport_and_guest_scope():
    program=script(SshAuthWrite(mode='password',username='training',password='synthetic-training-only'),'10.8.0.0/24','challenge_admin')
    assert 'synthetic-training-only' not in program # encoded stdin, never argv/logs
    assert 'Match User ' in program and ' Address ' in program
    assert "['/usr/sbin/sshd','-t']" in program and "['systemctl','reload','ssh']" in program
    assert "['chpasswd','-e']" in program and 'admin_before' in program and 'outside_before' in program
    with pytest.raises(ValueError):script(SshAuthWrite(mode='password',username='training'),'10.8.0.0/24','ubuntu')


@pytest.mark.parametrize('mismatch',[None,'CreatorId','RoomId','BuilderId','state','address'])
def test_live_target_requires_owned_ec2_identity_and_console_pin(mismatch):
    from contextlib import contextmanager
    from app.builder_ssh import configure
    b=SimpleNamespace(id=4,room_id=42,creator_id=7,instance_id='i-fixture',private_ip='10.0.1.50')
    tags={'Project':'SecForge','ResourceType':'builder','BuilderId':'bld-4','RoomId':'42','CreatorId':'7'}
    if mismatch in tags:tags[mismatch]='wrong'
    instance={'InstanceId':'i-fixture','PrivateIpAddress':'10.0.1.51' if mismatch=='address' else b.private_ip,'State':{'Name':'stopped' if mismatch=='state' else 'running'},'Tags':[{'Key':k,'Value':v} for k,v in tags.items()]}
    events=[]
    class Remote:
        @contextmanager
        def verified_builder_host(self,host,console):
            assert host==b.private_ip and console=='trusted-fixture-console';events.append('pin');yield
        def run_builder_command(self,host,args,**kw):
            assert events==['pin'] and args==['sudo','-n','python3','-']
            assert 'synthetic-training-only' not in str(args) and kw['stdin_data'].startswith('import base64,json')
            events.append('execute')
    ec2=SimpleNamespace(describe_instances=lambda **kw:{'Reservations':[{'Instances':[instance]}]},get_console_output=lambda **kw:{'Output':'trusted-fixture-console'})
    manager=SimpleNamespace(validate_source_instance=lambda instance_id:b.private_ip,ec2_client=ec2)
    with patch('app.ami_manager.AMIManager',return_value=manager),patch('app.vpn.VpnManager',return_value=Remote()):
        if mismatch:
            with pytest.raises(ValueError):configure(b,SshAuthWrite(mode='password',username='training',password='synthetic-training-only'))
            assert not events
        else:
            configure(b,SshAuthWrite(mode='password',username='training',password='synthetic-training-only'))
            assert events==['pin','execute']


def guest(tmp_path, mode, *, fail_reload=False, privileged=False):
    folder=tmp_path/'ssh';folder.mkdir();(folder/'sshd_config.d').mkdir()
    (folder/'sshd_config').write_text('Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication no\n')
    ns={};exec(PROGRAM[:PROGRAM.index('\ntry:\n')],ns)
    original_path=Path
    ns['Path']=lambda p:original_path(str(p).replace('/etc/ssh',str(folder)))
    ns['pwd']=SimpleNamespace(getpwnam=lambda u:SimpleNamespace(pw_uid=1001,pw_gid=1001,pw_shell='/bin/bash'))
    ns['grp']=SimpleNamespace(getgrall=lambda:[SimpleNamespace(gr_name='sudo' if privileged else 'training',gr_mem=['training'],gr_gid=1001)])
    calls=[]
    shadow=['training','synthetic-previous-hash','0','0','0','0','0','0','0']
    ns['subprocess']=SimpleNamespace(run=lambda *a,**kw:SimpleNamespace(returncode=0,stdout='User training is not allowed to run sudo on fixture.\n',stderr=''),DEVNULL=subprocess.DEVNULL,PIPE=subprocess.PIPE)
    def command(args,input=None):
        calls.append((args,input))
        if args[:2]==['getent','shadow']:return ':'.join(shadow)
        if args==['chpasswd']:
            shadow[1]='synthetic-new-hash';shadow[2]='20000'
        if args[:2]==['chage','-E']:shadow[7]=''
        if args==['systemctl','reload','ssh'] and fail_reload and sum(a==args for a,_ in calls)==1:raise ValueError()
        return ''
    def effective(user,address):
        path=folder/'sshd_config.d/00-secforge-training.conf'
        return {'passwordauthentication':'yes' if user=='training' and address=='10.8.0.1' and path.exists() and 'PasswordAuthentication yes' in path.read_text() else 'no','pubkeyauthentication':'yes','kbdinteractiveauthentication':'no','authenticationmethods':'any','permitrootlogin':'no'}
    ns['command']=command;ns['effective']=effective
    return ns,folder,calls,{'mode':mode,'username':'training','password':'synthetic-training-only','cidr':'10.8.0.0/24','admin':'challenge_admin'}


@pytest.mark.parametrize('mode',['key','password'])
def test_guest_program_validates_and_reloads_without_global_password_access(tmp_path,mode):
    ns,folder,calls,data=guest(tmp_path,mode)
    ns['apply'](data)
    dropin=(folder/'sshd_config.d/00-secforge-training.conf').read_text()
    assert 'Match User training Address 10.8.0.0/24' in dropin
    assert (folder/'sshd_config').read_text().endswith('PasswordAuthentication no\n')
    assert [a for a,_ in calls].index(['/usr/sbin/sshd','-t']) < [a for a,_ in calls].index(['systemctl','reload','ssh'])
    assert bool([a for a,_ in calls if a==['chpasswd']]) == (mode=='password')


def test_guest_reload_failure_restores_config_and_password(tmp_path):
    ns,folder,calls,data=guest(tmp_path,'password',fail_reload=True)
    with pytest.raises(ValueError):ns['apply'](data)
    assert not (folder/'sshd_config.d/00-secforge-training.conf').exists()
    assert (['chpasswd','-e'],'training:synthetic-previous-hash\n') in calls


def test_guest_privileged_account_rejected_before_changes(tmp_path):
    ns,folder,calls,data=guest(tmp_path,'password',privileged=True)
    with pytest.raises(ValueError):ns['apply'](data)
    assert not calls and not (folder/'sshd_config.d/00-secforge-training.conf').exists()


def test_explicit_password_creation_and_no_implicit_account_creation(tmp_path):
    ns,folder,calls,data=guest(tmp_path,'password')
    account=ns['pwd'].getpwnam('training');present=False
    def lookup(user):
        if not present:raise KeyError(user)
        return account
    ns['pwd'].getpwnam=lookup
    original=ns['command']
    def command(args,input=None):
        nonlocal present
        if args[0]=='useradd':present=True
        return original(args,input)
    ns['command']=command
    with pytest.raises(ValueError):ns['apply'](data)
    assert not calls
    ns['apply']({**data,'create_account':True})
    assert present and calls[0][0]==['useradd','--create-home','--user-group','--shell','/bin/bash','training']
    assert 'Match User root\n    PermitRootLogin no' in (folder/'sshd_config.d/00-secforge-training.conf').read_text()
    assert (['chage','-E','-1','-I','-1','-M','99999','training'],None) in calls


def test_new_account_removed_on_configuration_failure_only(tmp_path):
    ns,folder,calls,data=guest(tmp_path,'password')
    account=ns['pwd'].getpwnam('training');lookups=0
    def lookup(user):
        nonlocal lookups
        lookups+=1
        if lookups==1:raise KeyError(user)
        return account
    ns['pwd'].getpwnam=lookup
    def fail(*args):raise ValueError()
    ns['configure_account']=fail
    with pytest.raises(ValueError):ns['apply']({**data,'create_account':True})
    assert [a for a,_ in calls]==[['useradd','--create-home','--user-group','--shell','/bin/bash','training'],['userdel','--remove','training']]


def test_direct_sudo_grant_rejected_even_without_privileged_group(tmp_path):
    ns,folder,calls,data=guest(tmp_path,'password')
    ns['subprocess'].run=lambda *a,**kw:SimpleNamespace(returncode=0,stdout='User training may run the following commands: (ALL) ALL',stderr='')
    with pytest.raises(ValueError):ns['apply'](data)
    assert not calls


def test_valid_ubuntu_include_whitespace_and_comments_are_preserved(tmp_path):
    ns,folder,calls,data=guest(tmp_path,'password')
    main='  Include\t/etc/ssh/sshd_config.d/*.conf # cloud image\nPasswordAuthentication no\n'
    (folder/'sshd_config').write_text(main)
    ns['apply'](data)
    assert (folder/'sshd_config').read_text()==main


@pytest.mark.parametrize('status',[0,1])
def test_waits_for_cloud_init_before_account_or_ssh_changes(tmp_path,status):
    ns,folder,calls,data=guest(tmp_path,'password')
    cloud=tmp_path/'cloud-init';cloud.touch();original=ns['Path'];invocations=[]
    ns['Path']=lambda p:cloud if p=='/usr/bin/cloud-init' else original(p)
    def run(args,**kw):
        invocations.append(args)
        return SimpleNamespace(returncode=status if args[0]=='cloud-init' else 0,stdout='User training is not allowed to run sudo on fixture.\n',stderr='')
    ns['subprocess'].run=run
    if status:
        with pytest.raises(ValueError):ns['apply'](data)
        assert not calls
    else:ns['apply'](data)
    assert invocations[0]==['cloud-init','status','--wait']


@pytest.mark.parametrize('values',[{'mode':'key','create_account':True},{'mode':'password','create_account':True},{'mode':'password','password':'synthetic-training-only','create_account':'yes'}])
def test_creation_is_explicit_strict_and_requires_password(values):
    with pytest.raises(ValidationError):SshAuthWrite(username='training',**values)


@pytest.mark.parametrize('mode',['yes','no'])
def test_real_sshd_effective_match_and_cloud_init_precedence(tmp_path,mode):
    sshd=shutil.which('sshd') or '/usr/sbin/sshd'
    if not Path(sshd).exists(): pytest.skip('OpenSSH server binary unavailable; guest operations otherwise simulated')
    config=tmp_path/'sshd_config';dropin=tmp_path/'00-secforge-training.conf';cloud=tmp_path/'60-cloudimg-settings.conf'
    hostkey=tmp_path/'hostkey'
    subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(hostkey)],check=True,capture_output=True)
    dropin.write_text('Match User training Address 10.8.0.0/24\n PasswordAuthentication '+mode+'\n PubkeyAuthentication yes\n KbdInteractiveAuthentication no\n AuthenticationMethods any\nMatch all\n')
    cloud.write_text('PasswordAuthentication no\n')
    config.write_text(f'HostKey {hostkey}\nInclude {dropin}\nInclude {cloud}\nPubkeyAuthentication yes\nUsePAM yes\n')
    for user,address,expected in [('training','10.8.0.55',mode),('training','192.0.2.10','no'),('challenge_admin','10.8.0.55','no')]:
        result=subprocess.run([sshd,'-T','-f',str(config),'-C',f'user={user},host=localhost,addr={address}'],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        assert 'passwordauthentication '+expected in result.stdout and 'pubkeyauthentication yes' in result.stdout
