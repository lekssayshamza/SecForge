"""Explicit local-only Ubuntu guest integration; no AWS or production API calls.

Requires a local test image with Ubuntu 24.04, python3, sudo, iproute2 and
OpenSSH client/server. Run using backend/.venv/bin/python -B with
--allow-isolated-container --image IMAGE. Containers have no network, no host
mounts, no published ports, and NET_ADMIN only for private loopback addresses.
The service adapter reloads a real sshd; Docker restart/commit test persistence,
not an EC2 reboot or real AMI capture. No production credential is used.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import textwrap
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.guest_ssh import SshAuthWrite, script


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--allow-isolated-container',action='store_true',required=True)
    parser.add_argument('--image',required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.:/-]{0,150}',args.image):raise SystemExit('Invalid test image')
    name='secforge-ssh-test-'+secrets.token_hex(6)
    clone=name+'-clone';snapshot=name+'-snapshot'
    created=[];snapshot_created=False
    def command(arguments,input=None,timeout=60):
        result=subprocess.run(arguments,input=input,text=True,capture_output=True,timeout=timeout)
        if result.returncode:
            if result.stdout.startswith('{"safe_error"'):print(result.stdout)
            raise RuntimeError('Isolated guest verification failed; no credentials printed')
        return result.stdout
    def guest(container,program):
        if program.startswith('import base64,json\nDATA='):
            program=program[:program.rindex('\ntry:\n')]+"\nimport traceback\ntry:apply(DATA)\nexcept Exception as e:\n print(json.dumps({'safe_error':type(e).__name__,'frames':[(f.name,f.lineno) for f in traceback.extract_tb(e.__traceback__)],'command':getattr(e,'cmd',None)}));raise SystemExit(1)\n"
        else:
            program='import traceback,json\ntry:\n'+textwrap.indent(program,' ')+"\nexcept Exception as e:\n print(json.dumps({'safe_error':type(e).__name__,'frames':[(f.name,f.lineno) for f in traceback.extract_tb(e.__traceback__)],'command':getattr(e,'cmd',None),'stage':globals().get('stage','setup'),'ssh_error':getattr(globals().get('trained'),'stderr','')[-300:]}));raise SystemExit(1)\n"
        return command(['docker','exec','-i',container,'python3','-'],program,100)
    def launch(container,image):
        command(['docker','run','--detach','--name',container,'--network','none','--cap-add','NET_ADMIN','--tmpfs','/run:rw,nosuid,nodev,size=16m',image,'/bin/sh','-c','mkdir -p /run/sshd; exec /usr/sbin/sshd -D -e'])
        created.append(container)
    with tempfile.TemporaryDirectory(prefix='secforge-ssh-keys-') as folder:
        Path(folder).chmod(0o700);key=Path(folder)/'key'
        command(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)])
        public=key.with_suffix('.pub').read_text();private=key.read_text()
        password=secrets.token_urlsafe(24)
        try:
            launch(name,args.image)
            guest(name,SETUP.replace('__PUBLIC__',repr(public)))
            payload=SshAuthWrite(mode='password',username='training',password=password,create_account=True)
            guest(name,script(payload,'10.8.0.0/24','ubuntu'))
            def check(container,mode):
                result=guest(container,'DATA='+repr({'password':password,'private':private,'mode':mode})+'\n'+CHECK)
                assert json.loads(result)['passed']
            check(name,'password')
            # Expired/locked existing accounts must become usable on explicit apply.
            guest(name,"import subprocess\nsubprocess.run(['passwd','-l','training'],check=True,capture_output=True)\nsubprocess.run(['chage','-E','0','training'],check=True)")
            guest(name,script(payload,'10.8.0.0/24','ubuntu'));check(name,'password')
            command(['docker','restart',name]);check(name,'password')
            command(['docker','commit',name,snapshot],timeout=90);snapshot_created=True
            launch(clone,snapshot)
            guest(clone,"import subprocess,glob,os,signal\nfor p in glob.glob('/etc/ssh/ssh_host_*_key*'):os.unlink(p)\nsubprocess.run(['ssh-keygen','-A'],check=True,capture_output=True)\nos.kill(1,signal.SIGHUP)")
            check(clone,'password')
            guest(clone,script(SshAuthWrite(mode='key',username='training'),'10.8.0.0/24','ubuntu'))
            check(clone,'key')
            print(json.dumps({'ubuntu':'24.04','real_password_authentication':True,'admin_key_preserved':True,'training_key_authentication':True,'non_vpn_password_denied':True,'root_denied':True,'expired_locked_account_repaired':True,'container_restart_persistence':True,'cloned_filesystem_persistence':True,'fresh_host_keys':True,'real_ec2_or_ami_test':False}))
        finally:
            for container in reversed(created):subprocess.run(['docker','rm','--force',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if snapshot_created:subprocess.run(['docker','image','rm',snapshot],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


SETUP=r'''import os,pwd,subprocess
from pathlib import Path
assert Path('/etc/os-release').read_text().find('24.04')>=0
try:account=pwd.getpwnam('ubuntu')
except KeyError:
 subprocess.run(['useradd','-m','-s','/bin/bash','ubuntu'],check=True);account=pwd.getpwnam('ubuntu')
folder=Path(account.pw_dir)/'.ssh';folder.mkdir(mode=0o700,exist_ok=True)
key=folder/'authorized_keys';key.write_text(__PUBLIC__);key.chmod(0o600)
os.chown(folder,account.pw_uid,account.pw_gid);os.chown(key,account.pw_uid,account.pw_gid)
Path('/etc/ssh/sshd_config.d/60-cloudimg-settings.conf').write_text('PasswordAuthentication no\n')
adapter=Path('/usr/local/bin/systemctl')
adapter.write_text('#!/usr/bin/python3\nimport os,signal,sys\nif sys.argv[1] in ("reload","restart"):os.kill(1,signal.SIGHUP)\n')
adapter.chmod(0o755)
'''

CHECK=r'''import os,pwd,subprocess,tempfile,time,json
from pathlib import Path
stage='loopback address'
subprocess.run(['ip','address','replace','10.8.0.2/32','dev','lo'],check=True,capture_output=True)
account=pwd.getpwnam('training');folder=Path(account.pw_dir)/'.ssh';folder.mkdir(mode=0o700,exist_ok=True)
# Only the public half is installed persistently. Test private keys use tmpfs.
private=DATA['private']
with tempfile.TemporaryDirectory(dir='/run') as d:
 root=Path(d);key=root/'key';key.write_text(private);key.chmod(0o600)
 public=subprocess.check_output(['ssh-keygen','-y','-f',str(key)],text=True)
 authorized=folder/'authorized_keys';authorized.write_text(public);authorized.chmod(0o600)
 os.chown(folder,account.pw_uid,account.pw_gid);os.chown(authorized,account.pw_uid,account.pw_gid)
 known=root/'known';known.write_text('127.0.0.1 '+Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text())
 secret=root/'password';secret.write_text(DATA['password']);secret.chmod(0o600)
 # Docker mounts /run noexec. Only this nonsecret helper uses executable /tmp;
 # passwords and private keys stay in the private tmpfs directory.
 ask=Path('/tmp')/('secforge-test-askpass-'+root.name)
 ask.write_text('#!/bin/sh\ncat '+str(secret)+'\n');ask.chmod(0o700)
 environment=dict(os.environ,SSH_ASKPASS=str(ask),SSH_ASKPASS_REQUIRE='force',DISPLAY='isolated:0')
 base=['ssh','-o','ConnectTimeout=3','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(known),'-o','NumberOfPasswordPrompts=1']
 def login(user,password=False,address='10.8.0.2'):
  method=['-o','PubkeyAuthentication=no','-o','PreferredAuthentications=password'] if password else ['-o','BatchMode=yes','-o','IdentitiesOnly=yes','-i',str(key)]
  return subprocess.run(base+method+['-b',address,user+'@127.0.0.1','whoami'],env=environment,start_new_session=True,capture_output=True,text=True,timeout=12)
 # Reload and Docker startup can take a moment to accept a new connection.
 for attempt in range(15):
  admin=login('ubuntu')
  if admin.returncode==0:break
  time.sleep(.2)
 stage='admin key login'
 assert admin.returncode==0 and admin.stdout.strip()=='ubuntu'
 trained=login('training',DATA['mode']=='password')
 stage='training '+DATA['mode']+' login'
 assert trained.returncode==0 and trained.stdout.strip()=='training'
 stage='outside VPN denial'
 assert login('training',True,address='127.0.0.1').returncode!=0
 assert login('root').returncode!=0
 if DATA['mode']=='key':assert login('training',True).returncode!=0
 sudo=subprocess.run(['sudo','-n','-l','-U','training'],capture_output=True,text=True,env=dict(os.environ,LC_ALL='C'))
 assert 'User training is not allowed to run sudo on ' in sudo.stdout+sudo.stderr
 ask.unlink()
 print(json.dumps({'passed':True}))
'''

if __name__=='__main__':main()
