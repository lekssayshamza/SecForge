"""Opt-in real OpenSSH fixture. No host mounts, network, AWS or real credentials.

IMDS identity and systemd reload are test adapters. A fresh container hostname
represents a fresh EC2 instance ID, so filesystem clones must reject old keys.
"""
import argparse,base64,hashlib,importlib.util,json,re,secrets,subprocess,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import creator_key_guest
spec=importlib.util.spec_from_file_location('guest_fixture',Path(__file__).with_name('guest_ssh_ubuntu.py'));fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
FAKE_ID="""def instance_identity():
    import socket,hashlib
    return 'i-'+hashlib.sha256(socket.gethostname().encode()).hexdigest()[:17]

"""
SSH_CHECK=r'''
import subprocess,tempfile,json
from pathlib import Path
subprocess.run(['ip','address','replace','10.8.0.2/32','dev','lo'],check=True,capture_output=True)
with tempfile.TemporaryDirectory(dir='/run') as d:
    root=Path(d);key=root/'key';key.write_text(DATA['private']);key.chmod(0o600)
    known=root/'known';known.write_text('127.0.0.1 '+Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text())
    result=subprocess.run(['ssh','-o','ConnectTimeout=3','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(known),'-o','BatchMode=yes','-o','IdentitiesOnly=yes','-b',DATA['source'],'-i',str(key),'ubuntu@127.0.0.1','id -un'],capture_output=True,text=True,timeout=8)
    print(json.dumps({'allowed':result.returncode==0 and result.stdout.strip()=='ubuntu'}))
'''

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--allow-isolated-container',action='store_true',required=True);parser.add_argument('--image',required=True);args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.:/-]{0,150}',args.image):raise SystemExit('Invalid fixture image')
    name='secforge-creator-key-test-'+secrets.token_hex(5);clone=name+'-clone';snapshot=name+'-snapshot';created=[];snapshot_created=False
    def command(argv,body=None,expected=0):
        result=subprocess.run(argv,input=body,text=True,capture_output=True,timeout=110)
        if (result.returncode==0)!=(expected==0):raise RuntimeError('Isolated creator-key check failed: '+str(argv[:3]))
        return result.stdout
    def guest(container,body,expected=0):return command(['docker','exec','-i',container,'python3','-'],body,expected)
    def launch(container,image):
        command(['docker','run','--detach','--name',container,'--network','none','--cap-add','NET_ADMIN','--tmpfs','/run:rw,nosuid,nodev,size=16m',image,'/bin/sh','-c','mkdir -p /run/sshd; exec /usr/sbin/sshd -D -e']);created.append(container)
    def identity(container):
        hostname=command(['docker','exec',container,'hostname']).strip()
        return 'i-'+hashlib.sha256(hostname.encode()).hexdigest()[:17]
    def ssh(container,private,allowed,source='10.8.0.2'):
        result=json.loads(guest(container,'DATA='+repr({'private':private,'source':source})+'\n'+SSH_CHECK));assert result['allowed']==allowed
    with tempfile.TemporaryDirectory(prefix='secforge-personal-key-fixture-') as folder:
        Path(folder).chmod(0o700);pairs=[]
        for i in range(3):
            key=Path(folder)/('key-'+str(i));command(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)]);pairs.append((key.read_text(),key.with_suffix('.pub').read_text().strip().split()[:2]))
        management,personal,replacement=pairs
        creator_key_guest.BOOT_HELPER=creator_key_guest.BOOT_HELPER.replace("if __name__=='__main__':",FAKE_ID+"if __name__=='__main__':")
        try:
            launch(name,args.image);guest(name,fixture.SETUP.replace('__PUBLIC__',repr(' '.join(management[1]))))
            password=secrets.token_urlsafe(24)
            guest(name,fixture.script(fixture.SshAuthWrite(mode='password',username='training',password=password,create_account=True),'10.8.0.0/24','ubuntu'))
            def apply(revision,enabled,pub=personal[1],expected=0):
                return guest(name,creator_key_guest.program(identity(name),7,revision,' '.join(pub),enabled,'ubuntu','10.8.0.0/24'),expected)
            apply(1,True)
            assert json.loads(guest(name,'DATA='+repr({'password':password,'private':management[0],'mode':'password'})+'\n'+fixture.CHECK))['passed']
            ssh(name,personal[0],True);ssh(name,personal[0],False,'127.0.0.1');ssh(name,management[0],True);ssh(name,management[0],True,'127.0.0.1')
            command(['docker','restart',name]);command(['docker','exec',name,'python3','/usr/local/libexec/secforge-creator-key.py']);ssh(name,personal[0],True)
            command(['docker','commit',name,snapshot]);snapshot_created=True;launch(clone,snapshot)
            command(['docker','exec',clone,'python3','/usr/local/libexec/secforge-creator-key.py']);ssh(clone,personal[0],False);ssh(clone,management[0],True)
            apply(2,False);ssh(name,personal[0],False);ssh(name,management[0],True)
            command(['docker','restart',name]);command(['docker','exec',name,'python3','/usr/local/libexec/secforge-creator-key.py']);ssh(name,personal[0],False)
            apply(3,True,replacement[1]);ssh(name,replacement[0],True);ssh(name,personal[0],False)
            apply(1,True,expected=1);ssh(name,replacement[0],True)
            print(json.dumps({'ubuntu':'24.04','real_personal_key_login':True,'vpn_source_scope':True,'management_key_preserved':True,'learner_password_authentication_preserved':True,'container_restart_restores_same_instance_key':True,'filesystem_clone_rejects_creator_key':True,'revocation_survives_restart':True,'rotation_and_stale_replay_protection':True,'private_keys_used_only_in_client_fixture_tmpfs':True,'private_keys_persisted_in_cloned_image':False,'real_aws_or_systemd_boot_test':False,'identity_and_service_reload':'isolated adapters'}))
        finally:
            for container in reversed(created):subprocess.run(['docker','rm','--force',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if snapshot_created:subprocess.run(['docker','image','rm',snapshot],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

if __name__=='__main__':main()
