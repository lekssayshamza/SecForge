"""Explicit local first-administrator provisioning. Never run automatically."""
import argparse
from pathlib import Path

def bootstrap(db,user_id,expected_username,confirmation):
    from sqlalchemy import select,func
    from app.models import User
    from app.administration import control,active_resources,audit
    try:
        control(db)
        if db.scalar(select(func.count()).select_from(User).where(User.role=='admin',User.suspended.is_(False))):raise ValueError('An active administrator already exists; use audited administration')
        user=db.scalar(select(User).where(User.id==user_id).with_for_update().execution_options(populate_existing=True))
        if not user or user.username!=expected_username or user.suspended or user.must_change_password:raise ValueError('Target identity/status verification failed')
        if confirmation!='PROMOTE '+str(user_id)+' '+expected_username:raise ValueError('Explicit target confirmation required')
        if active_resources(db,user.id):raise ValueError('Target has unresolved machine resources')
        old=user.role;user.creator_access=(old=='creator' or user.creator_access);user.role='admin';user.token_version+=1
        audit(db,None,'user',user.id,'first-admin-bootstrap','success',{'source':'explicit-local-operator','old_role':old,'new_role':'admin','creator_access_retained':user.creator_access});db.commit()
    except Exception:db.rollback();raise

def main():
    p=argparse.ArgumentParser(description='Requires explicit owner approval for this exact target ID before execution')
    p.add_argument('--user-id',type=int,required=True);p.add_argument('--expected-username',required=True);p.add_argument('--confirm',required=True)
    p.add_argument('--env-file',type=Path,required=True)
    a=p.parse_args()
    import os,sys
    from dotenv import dotenv_values
    if not a.env_file.is_absolute() or not a.env_file.is_file():p.error('An existing absolute service environment file is required')
    os.environ.update({k:v for k,v in dotenv_values(a.env_file).items() if v is not None})
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from app.database import SessionLocal
    with SessionLocal() as db:bootstrap(db,a.user_id,a.expected_username,a.confirm)
    print('Explicit first administrator promotion committed and audited; prior sessions invalidated.')
if __name__=='__main__':main()
