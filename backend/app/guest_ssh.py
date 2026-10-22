"""Fixed guest-only SSH configuration program, sent over pinned SSH stdin.

Never execute this program on the application server or VPN gateway. The caller
verifies the owned builder's EC2 tags, state, address and console host key first.
"""
import base64
import ipaddress
import json
import re
from pydantic import BaseModel, SecretStr, StrictBool, field_validator, model_validator
from typing import Literal


class SshAuthWrite(BaseModel):
    mode: Literal['key', 'password']
    username: str
    password: SecretStr | None = None
    create_account: StrictBool = False
    model_config = {'extra': 'forbid'}

    @field_validator('username')
    @classmethod
    def username_valid(cls, value):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', value) or value in {'root', 'ubuntu', 'admin', 'challenge_admin', 'secforge', 'postgres', 'openvpn'}:
            raise ValueError('Choose a nonprivileged training account')
        return value

    @model_validator(mode='after')
    def creation_requires_password(self):
        if self.create_account and (self.mode != 'password' or self.password is None):
            raise ValueError('Account creation requires an explicit training password')
        return self

    @field_validator('password')
    @classmethod
    def password_valid(cls, value):
        if value is not None:
            raw = value.get_secret_value()
            if not 12 <= len(raw) <= 128 or any(ord(c) < 32 or ord(c) == 127 or c == ':' for c in raw):
                raise ValueError('Use 12–128 characters without colons or control characters')
        return value


PROGRAM = r'''
import base64,json,os,pwd,grp,subprocess,tempfile,ipaddress,shlex
from pathlib import Path

def command(args,input=None):
    return subprocess.run(args,input=input,text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True,timeout=20).stdout

def effective(user,address):
    return dict(line.split(' ',1) for line in command(['/usr/sbin/sshd','-T','-C','user='+user+',host=localhost,addr='+address]).splitlines() if ' ' in line)

def write(path,content):
    fd,name=tempfile.mkstemp(prefix='.secforge-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as stream: stream.write(content);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

def apply(data):
    # A running EC2 instance can still be executing cloud-init's password module.
    # Wait for it before configuring the selected account; never enable global
    # ssh_pwauth or put a plaintext password into cloud-init metadata.
    if Path('/usr/bin/cloud-init').exists():
        result=subprocess.run(['cloud-init','status','--wait'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
        if result.returncode not in (0,2):raise ValueError()
    user=data['username'];created=False
    try:account=pwd.getpwnam(user)
    except KeyError:
        if not data.get('create_account') or data['mode']!='password' or not data.get('password'):raise ValueError()
        if any(p.exists() or p.is_symlink() for p in (Path('/home/'+user),Path('/var/mail/'+user))):raise ValueError()
        command(['useradd','--create-home','--user-group','--shell','/bin/bash',user])
        created=True;account=pwd.getpwnam(user)
    try:
        configure_account(data,account)
    except Exception:
        # Only remove an account created by this invocation, never an existing
        # training user or their work. useradd has not started any user process.
        if created:command(['userdel','--remove',user])
        raise

def configure_account(data,account):
    user=data['username']
    if account.pw_uid<1000 or account.pw_uid==65534 or user==data['admin'] or account.pw_shell.endswith(('nologin','false')):raise ValueError()
    if not Path(account.pw_shell).is_file() or not os.access(account.pw_shell,os.X_OK):raise ValueError()
    groups={g.gr_name for g in grp.getgrall() if user in g.gr_mem or g.gr_gid==account.pw_gid}
    if groups.intersection({'sudo','wheel','adm','root','disk','docker','lxd','shadow'}):raise ValueError()
    # A direct sudoers grant is privileged even when no privileged group exists.
    sudo=subprocess.run(['sudo','-n','-l','-U',user],env=dict(os.environ,LC_ALL='C'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=20)
    # Ubuntu sudo can return zero for a successful *listing* that explicitly
    # denies every command. Accept only that definitive denial, not a status
    # code alone or an unknown policy/error. No sudo policy is changed here.
    if not any(line.startswith('User '+user+' is not allowed to run sudo on ') for line in (sudo.stdout+sudo.stderr).splitlines()):raise ValueError()
    path=Path('/etc/ssh/sshd_config.d/00-secforge-training.conf')
    if path.is_symlink() or path.parent.is_symlink():raise ValueError()
    original=path.read_bytes() if path.exists() else None
    if original is not None and not original.startswith(b'# SecForge managed training account\n'):raise ValueError()
    main=Path('/etc/ssh/sshd_config').read_text()
    directives=[shlex.split(line,comments=True) for line in main.splitlines()]
    if not any(len(words)>1 and words[0].lower()=='include' and '/etc/ssh/sshd_config.d/*.conf' in words[1:] for words in directives):raise ValueError()
    network=ipaddress.ip_network(data['cidr']);address=str(next(network.hosts()))
    admin_before=effective(data['admin'],address)
    outside_before=effective(user,'192.0.2.10')
    if data['mode']=='password' and outside_before.get('passwordauthentication')=='yes':raise ValueError()
    old_shadow=None;password_changed=False
    try:
        content=('# SecForge managed training account\nMatch User root\n    PermitRootLogin no\nMatch User '+user+' Address '+str(network)+'\n    PasswordAuthentication '+('yes' if data['mode']=='password' else 'no')+'\n    PubkeyAuthentication yes\n    KbdInteractiveAuthentication no\n    AuthenticationMethods any\nMatch all\n').encode()
        write(path,content)
        command(['/usr/sbin/sshd','-t'])
        after=effective(user,address);admin_after=effective(data['admin'],address);outside_after=effective(user,'192.0.2.10')
        if after.get('passwordauthentication')!=('yes' if data['mode']=='password' else 'no') or after.get('pubkeyauthentication')!='yes' or after.get('kbdinteractiveauthentication')!='no':raise ValueError()
        if after.get('authenticationmethods')!='any' or effective('root',address).get('permitrootlogin')!='no':raise ValueError()
        for field in ('passwordauthentication','pubkeyauthentication','authenticationmethods'):
            if admin_before.get(field)!=admin_after.get(field) or outside_before.get(field)!=outside_after.get(field):raise ValueError()
        if data['mode']=='password':
            old_shadow=command(['getent','shadow',user]).strip().split(':')
            password_changed=True
            command(['chpasswd'],user+':'+data['password']+'\n')
            command(['chage','-E','-1','-I','-1','-M','99999',user])
            shadow=command(['getent','shadow',user]).strip().split(':')
            if not shadow[1] or shadow[1].startswith(('!','*')) or shadow[7] not in ('','-1'):raise ValueError()
        command(['systemctl','reload','ssh'])
    except Exception:
        if original is None:path.unlink(missing_ok=True)
        else:write(path,original)
        if password_changed:
            command(['chpasswd','-e'],user+':'+old_shadow[1]+'\n')
            command(['chage','-d',old_shadow[2] or '-1','-m',old_shadow[3] or '-1','-M',old_shadow[4] or '-1','-W',old_shadow[5] or '-1','-I',old_shadow[6] or '-1','-E',old_shadow[7] or '-1',user])
        command(['/usr/sbin/sshd','-t']);command(['systemctl','reload','ssh'])
        raise

try:
    apply(DATA)
except Exception:
    print('Training SSH configuration could not be validated; previous settings restored where applicable.')
    raise SystemExit(1)
print('Training SSH configuration validated and applied.')
'''


def script(data: SshAuthWrite, cidr: str, admin: str) -> str:
    network = ipaddress.ip_network(cidr)
    if network.version != 4 or network.prefixlen < 16:
        raise ValueError('Invalid training VPN network')
    if admin == 'root' or data.username == admin or (data.mode == 'password' and data.password is None):
        raise ValueError('An explicit training account and password are required')
    payload = {'mode': data.mode, 'username': data.username, 'password': data.password.get_secret_value() if data.password else None, 'create_account': data.create_account, 'cidr': str(network), 'admin': admin}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return 'import base64,json\nDATA=json.loads(base64.b64decode(' + repr(encoded) + '))\n' + PROGRAM
