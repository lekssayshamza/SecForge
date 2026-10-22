"""Fixed guest-only public-key helper. Never executes on an application/gateway host.

Only the /run tmpfs file is an SSH trust source. A persisted, root-only binding
rebuilds that file after reboot only if IMDSv2 identifies the original builder.
Captured clones have a different instance ID and receive no creator key.
"""
import base64
import ipaddress
import json
import re

# This standalone program uses only Ubuntu's standard Python library.
BOOT_HELPER = r'''
import base64,json,os,re,stat,urllib.request,fcntl,time
from contextlib import contextmanager
from pathlib import Path
BINDING=Path('/var/lib/secforge/creator-key/binding.json')
RUNTIME=Path('/run/secforge-creator-key/authorized_keys')

def instance_identity():
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request=urllib.request.Request('http://169.254.169.254/latest/api/token',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'})
    with opener.open(request,timeout=3) as response:token=response.read(2049).decode('ascii')
    if not token or len(token)>2048 or '\n' in token or '\r' in token:raise ValueError()
    request=urllib.request.Request('http://169.254.169.254/latest/meta-data/instance-id',headers={'X-aws-ec2-metadata-token':token})
    with opener.open(request,timeout=3) as response:value=response.read(64).decode('ascii').strip()
    if not re.fullmatch(r'i-[a-f0-9]{8,17}',value):raise ValueError()
    return value

def directory(path,mode):
    if path.is_symlink():raise ValueError()
    path.mkdir(mode=mode,parents=True,exist_ok=True)
    info=path.stat()
    if info.st_uid!=0 or not stat.S_ISDIR(info.st_mode) or info.st_mode&0o022:raise ValueError()
    path.chmod(mode)

@contextmanager
def key_guard():
    directory(RUNTIME.parent,0o755)
    fd=os.open(RUNTIME.parent/'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=0 or not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise ValueError()
        deadline=time.monotonic()+8
        while True:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise ValueError()
                time.sleep(.05)
        yield
    finally:os.close(fd)

def atomic(path,body,mode):
    if path.is_symlink():raise ValueError()
    if path.exists():
        info=path.stat()
        if info.st_uid!=0 or not stat.S_ISREG(info.st_mode) or info.st_mode&0o022:raise ValueError()
    temp=path.with_name(path.name+'.sf-new')
    if temp.exists() or temp.is_symlink():
        info=temp.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0:raise ValueError()
        temp.unlink()
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(body);stream.flush();os.fsync(stream.fileno())
        os.chmod(temp,mode);os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)

def clear_runtime():
    if RUNTIME.is_symlink():raise ValueError()
    if RUNTIME.exists():
        info=RUNTIME.stat()
        if info.st_uid!=0 or not stat.S_ISREG(info.st_mode):raise ValueError()
        RUNTIME.unlink()

def restore_key():
    # Fail closed before metadata/network access. /run is never an image disk.
    directory(RUNTIME.parent,0o755);clear_runtime()
    mounts=Path('/proc/self/mountinfo').read_text().splitlines()
    if not any(line.split()[4]=='/run' and line.split(' - ',1)[1].split()[0]=='tmpfs' for line in mounts):raise ValueError()
    fd=os.open(BINDING,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if info.st_uid!=0 or info.st_mode&0o077 or not stat.S_ISREG(info.st_mode):raise ValueError()
        data=json.loads(stream.read(4097))
    if not data.get('enabled'):return False
    if data['instance_id']!=instance_identity():return False
    key=data['public_key']
    if not re.fullmatch(r'(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/]+={0,2}',key):raise ValueError()
    base64.b64decode(key.split()[1],validate=True)
    atomic(RUNTIME,(key+'\n').encode(),0o644)
    return True

if __name__=='__main__':
    try:
        with key_guard():restore_key()
    except Exception:
        # No keys, identities, metadata tokens or diagnostics in guest logs.
        raise SystemExit(1)
'''
UNIT = '''[Unit]
Description=SecForge instance-bound creator public key
Wants=network-online.target
After=network-online.target
Before=ssh.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/libexec/secforge-creator-key.py
TimeoutStartSec=15
RemainAfterExit=yes
UMask=0077
[Install]
WantedBy=multi-user.target
'''

INSTALLER = r'''
import base64,json,os,pwd,re,stat,subprocess
from pathlib import Path
DATA=json.loads(base64.b64decode(PAYLOAD))
HELPER=base64.b64decode(HELPER_BODY)
UNIT_BODY=base64.b64decode(SERVICE_BODY)
namespace={'__name__':'secforge_creator_key_guest'}
exec(compile(HELPER,'secforge-creator-key.py','exec'),namespace)
atomic=namespace['atomic'];directory=namespace['directory'];binding=namespace['BINDING'];runtime=namespace['RUNTIME']
def command(args):return subprocess.check_output(args,stderr=subprocess.DEVNULL,timeout=20,text=True)
def effective(user,address):return dict(line.split(' ',1) for line in command(['/usr/sbin/sshd','-T','-C','user='+user+',host=localhost,addr='+address]).splitlines() if ' ' in line)
def install():
    if os.geteuid()!=0 or namespace['instance_identity']()!=DATA['instance_id']:raise ValueError()
    account=pwd.getpwnam(DATA['admin'])
    if account.pw_uid<1000 or account.pw_name=='root':raise ValueError()
    directory(binding.parent,0o700)
    config=Path('/etc/ssh/sshd_config.d/01-secforge-creator-key.conf')
    helper=Path('/usr/local/libexec/secforge-creator-key.py');unit=Path('/etc/systemd/system/secforge-creator-key.service')
    directory(helper.parent,0o755)
    before={p:(p.read_bytes(),stat.S_IMODE(p.stat().st_mode)) if p.exists() and not p.is_symlink() else None for p in (binding,config,helper,unit,runtime)}
    if any(p.is_symlink() for p in before):raise ValueError()
    if binding.exists():
        old=json.loads(binding.read_bytes())
        if old.get('instance_id')==DATA['instance_id'] and old.get('revision',0)>DATA['revision']:raise ValueError()
    outside=effective(DATA['admin'],'192.0.2.1');inside=effective(DATA['admin'],DATA['probe'])
    source=inside['authorizedkeysfile'].split()
    original=[s for s in source if s!=str(runtime)]
    if not original or any(not re.fullmatch(r'[A-Za-z0-9_./%~-]+',s) for s in original):raise ValueError()
    block=('Match User '+DATA['admin']+' Address '+DATA['cidr']+'\n    AuthorizedKeysFile '+' '.join(original+[str(runtime)])+'\nMatch all\n').encode()
    try:
        atomic(helper,HELPER,0o755);atomic(unit,UNIT_BODY,0o644);atomic(binding,json.dumps(DATA).encode(),0o600);atomic(config,block,0o644)
        namespace['restore_key']()
        command(['/usr/sbin/sshd','-t'])
        actual=effective(DATA['admin'],DATA['probe'])
        if actual['pubkeyauthentication']!='yes' or str(runtime) not in actual['authorizedkeysfile'].split():raise ValueError()
        if effective(DATA['admin'],'192.0.2.1')!=outside:raise ValueError()
        if DATA['enabled'] and not runtime.exists():raise ValueError()
        if not DATA['enabled'] and runtime.exists():raise ValueError()
        command(['systemctl','daemon-reload']);command(['systemctl','enable','secforge-creator-key.service']);command(['systemctl','reload','ssh'])
    except Exception:
        for path,previous in before.items():
            if previous:atomic(path,previous[0],previous[1])
            elif path.exists() and not path.is_symlink():path.unlink()
        if before[unit] is None:
            try:command(['systemctl','disable','secforge-creator-key.service'])
            except Exception:pass
        try:command(['systemctl','daemon-reload']);command(['/usr/sbin/sshd','-t']);command(['systemctl','reload','ssh'])
        except Exception:pass
        raise
try:
    if Path('/usr/bin/cloud-init').exists():
        result=subprocess.run(['cloud-init','status','--wait'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
        if result.returncode not in (0,2):raise ValueError()
    with namespace['key_guard']():install()
    print('SF_CREATOR_KEY_CONFIGURED')
except Exception:raise SystemExit(1)
'''


def program(instance_id, creator_id, revision, public_key, enabled, admin, cidr):
    if not re.fullmatch(r'i-[a-f0-9]{8,17}',instance_id) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',admin) or admin=='root':raise ValueError('Invalid guest identity')
    network=ipaddress.ip_network(cidr,strict=True)
    if network.version!=4 or network.prefixlen>30 or network.num_addresses>65536:raise ValueError('Invalid creator VPN network')
    data={'instance_id':instance_id,'creator_id':creator_id,'revision':revision,'public_key':public_key,'enabled':bool(enabled),'admin':admin,'cidr':str(network),'probe':str(network.network_address+2)}
    encoded=lambda value:repr(base64.b64encode(value).decode())
    return 'PAYLOAD='+encoded(json.dumps(data).encode())+'\nHELPER_BODY='+encoded(BOOT_HELPER.encode())+'\nSERVICE_BODY='+encoded(UNIT.encode())+'\n'+INSTALLER
