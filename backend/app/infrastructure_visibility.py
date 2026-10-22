"""Bounded, cached, read-only EC2 inventory. Pricing deliberately unavailable."""
import threading,time
from datetime import datetime,timezone
from fastapi import APIRouter,Depends,Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.auth import require_role
from app.database import get_db
from app.models import LabInstance,BuilderEnvironment
from app.config import settings
router=APIRouter(prefix='/admin',tags=['Infrastructure'],dependencies=[Depends(require_role('admin'))])
class Instance(BaseModel):id:str;role:str;instance_type:str;state:str
class Inventory(BaseModel):
    available:bool;observed_at:datetime;instances:list[Instance];running_count:int|None
    truncated:bool=False;hourly_estimate:float|None=None;monthly_baseline_estimate:float|None=None
    pricing_note:str='Verified current pricing and actual billing are unavailable. Inventory is not a bill. Storage, IPv4, transfer, credits and taxes are not estimated.'
    issue:str|None=None
_lock=threading.Lock();_cache=None;_expires=0

def read_inventory(db):
    import boto3
    from botocore.config import Config
    now=datetime.now(timezone.utc);result=[]
    known={i:'registered-lab' for i in db.scalars(select(LabInstance.instance_id).where(LabInstance.instance_id.is_not(None)))}
    known.update({i:'registered-builder' for i in db.scalars(select(BuilderEnvironment.instance_id).where(BuilderEnvironment.instance_id.is_not(None)))})
    try:
        client=boto3.client('ec2',region_name=settings.aws_region,config=Config(connect_timeout=3,read_timeout=5,retries={'max_attempts':0}))
        token=None
        for _ in range(10):
            args={'MaxResults':100}
            if token:args['NextToken']=token
            data=client.describe_instances(**args)
            for reservation in data['Reservations']:
                for i in reservation['Instances']:
                    if i['State']['Name']=='terminated':continue
                    name=' '.join(t['Value'] for t in i.get('Tags',[]) if t['Key']=='Name').lower()
                    role=known.get(i['InstanceId'],'unclassified')
                    if role=='unclassified' and 'secforge' in name:
                        if 'vpn' in name or 'gateway' in name:role='vpn-gateway (tag identified)'
                        elif 'app' in name or 'production' in name:role='application (tag identified)'
                    result.append(Instance(id=i['InstanceId'],role=role,instance_type=i['InstanceType'],state=i['State']['Name']))
            token=data.get('NextToken')
            if not token:break
        return Inventory(available=True,observed_at=now,instances=sorted(result,key=lambda v:v.id),running_count=sum(i.state=='running' for i in result),truncated=bool(token))
    except Exception:
        return Inventory(available=False,observed_at=now,instances=[],running_count=None,issue='Live EC2 inventory unavailable. Existing read permissions or connectivity may be insufficient; no permission changes attempted.')
@router.get('/infrastructure',response_model=Inventory)
def infrastructure(response:Response,user=Depends(require_role('admin')),db:Session=Depends(get_db)):
    global _cache,_expires
    response.headers['Cache-Control']='private, no-store'
    with _lock:
        if _cache is None or time.monotonic()>=_expires:
            _cache=read_inventory(db);_expires=time.monotonic()+60
        return _cache
