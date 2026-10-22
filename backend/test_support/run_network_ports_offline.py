"""Fresh SQLite with AWS/network blocked; only local SSH config checks may spawn."""
import os,sys,tempfile,socket,subprocess,selectors
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
args=sys.argv[1:]
with tempfile.TemporaryDirectory(prefix='secforge-ports-tests-') as folder:
    os.chdir(folder)
    os.environ.clear()
    os.environ.update(PATH='/usr/bin:/bin',HOME=folder,JWT_SECRET='offline-test-only-at-least-thirty-two-bytes',DATABASE_URL='sqlite://',TERRAFORM_DRY_RUN='false',VPN_ENABLED='false',VPN_AUTHZ_ENABLED='false',AWS_EC2_METADATA_DISABLED='true',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    sys.path.insert(0,str(ROOT))
    import boto3,botocore.session
    def forbidden(*a,**kw): raise RuntimeError('External network/AWS/process operation blocked by offline runner')
    original_popen = subprocess.Popen
    def local_ssh_check(command, *a, **kw):
        # Existing SSH regression tests generate a disposable host key and use
        # sshd -T (configuration inspection, no listener). Nothing else runs.
        if isinstance(command, list) and not kw.get('shell'):
            if (len(command) == 8 and command[:7] == ['ssh-keygen','-q','-t','ed25519','-N','','-f']
                    and Path(command[7]).is_relative_to(folder)):
                return original_popen(command, *a, **kw)
            if (len(command) == 6 and command[:3] == ['/usr/sbin/sshd','-T','-f']
                    and command[4] == '-C' and Path(command[3]).is_relative_to(folder)):
                return original_popen(command, *a, **kw)
        return forbidden()
    class PollingSelector(selectors.DefaultSelector):
        def select(self,timeout=None):return super().select(.01 if timeout is None else min(timeout,.01))
    with patch.object(socket.socket,'connect',forbidden),patch.object(socket.socket,'connect_ex',forbidden),patch.object(socket.socket,'sendto',forbidden),patch.object(subprocess,'Popen',local_ssh_check),patch.object(os,'system',forbidden),patch.object(boto3,'client',forbidden),patch.object(boto3,'resource',forbidden),patch.object(boto3.session.Session,'client',forbidden),patch.object(boto3.session.Session,'resource',forbidden),patch.object(botocore.session.Session,'create_client',forbidden),patch.object(selectors,'DefaultSelector',PollingSelector):
        import app.database as database
        from app.config import settings
        settings.reconciliation_enabled = False
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool
        database.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        database.SessionLocal.configure(bind=database.engine)
        import app.models
        database.Base.metadata.create_all(database.engine)
        import pytest
        sys.exit(pytest.main(['-p','no:cacheprovider','--basetemp',str(Path(folder)/'pytest'),'--tb=short','-q',*[str(ROOT/a) if (ROOT/a.split('::')[0]).is_file() else a for a in args]]))
