"""Google authorization-code OIDC. Browser-bound state, PKCE and nonce.

Provider tokens/authorization codes are never stored or returned to the browser.
Account association uses Google sub, never email alone. All local handoffs expire.
"""
import base64
from datetime import timedelta
import hashlib
import hmac
import logging
import secrets
from urllib.parse import urlencode
import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy import delete, select, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.auth import account_allowed, get_current_user, token_response
from app.config import settings
from app.database import get_db
from app.lab_time import as_utc, utc_now
from app.models import GoogleIdentity, OAuthAttempt, User
from app.schemas import GoogleCompleteRequest, GoogleLinkRequest
from app.security import hash_password, verify_password

router = APIRouter(prefix='/auth/google', tags=['Authentication'])
_jwks = jwt.PyJWKClient('https://www.googleapis.com/oauth2/v3/certs', timeout=5, lifespan=300)

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def cookie_name():
    return '__Host-secforge-oauth' if settings.production else 'secforge-oauth'

def configured():
    if not settings.google_oauth_enabled:
        raise HTTPException(503, 'Google sign-in is not configured')

def require_origin(request):
    if request.headers.get('origin') != settings.app_public_url.rstrip('/'):
        raise HTTPException(403, 'Invalid authentication request origin')

def set_cookie(response, value):
    response.set_cookie(cookie_name(), value, max_age=600, httponly=True,
                        secure=settings.production, samesite='lax', path='/')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'

def begin(db, link_user=None):
    configured()
    state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
    db.execute(delete(OAuthAttempt).where(OAuthAttempt.expires_at <= utc_now()))
    db.add(OAuthAttempt(state_digest=digest(state), browser_digest=digest(browser),
        nonce_digest=digest(nonce), verifier=verifier, expires_at=utc_now()+timedelta(minutes=10),
        link_user_id=link_user.id if link_user else None,
        link_token_version=link_user.token_version if link_user else None))
    db.commit()
    query = dict(client_id=settings.google_client_id, redirect_uri=settings.google_redirect_uri,
                 response_type='code', scope='openid email', state=state, nonce=nonce,
                 code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),
                 code_challenge_method='S256', prompt='select_account')
    return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(query), browser

@router.get('/start')
def start(db: Session = Depends(get_db)):
    url, browser = begin(db)
    response = RedirectResponse(url, status_code=303);set_cookie(response,browser)
    return response

@router.post('/link')
def link(data: GoogleLinkRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    configured();require_origin(request)
    if not user.password_enabled or not verify_password(data.password.get_secret_value(), user.password_hash):
        raise HTTPException(401, 'Reauthenticate with your current password')
    if db.scalar(select(GoogleIdentity.id).where(GoogleIdentity.user_id == user.id)):
        raise HTTPException(409, 'Google is already linked to this account')
    url,browser=begin(db,user)
    response=JSONResponse({'authorization_url':url});set_cookie(response,browser)
    return response

def verify_google_token(encoded, nonce_digest):
    key = _jwks.get_signing_key_from_jwt(encoded)
    claims = jwt.decode(encoded, key.key, algorithms=['RS256'], audience=settings.google_client_id,
                        options={'require':['exp','iat','iss','aud','sub','nonce','email','email_verified']})
    if claims['iss'] not in ['https://accounts.google.com','accounts.google.com']:
        raise ValueError('Invalid issuer')
    if (isinstance(claims['aud'],list) and len(claims['aud'])>1 and 'azp' not in claims) or claims.get('azp', settings.google_client_id) != settings.google_client_id:
        raise ValueError('Invalid authorized party')
    if not isinstance(claims['sub'],str) or not 1 <= len(claims['sub']) <= 255:
        raise ValueError('Invalid subject')
    if not isinstance(claims['nonce'],str) or not hmac.compare_digest(digest(claims['nonce']),nonce_digest):
        raise ValueError('Invalid nonce')
    from pydantic import TypeAdapter, EmailStr
    email=str(TypeAdapter(EmailStr).validate_python(claims['email'])).lower()
    if claims['email_verified'] is not True:
        raise ValueError('Unverified email')
    return claims['sub'],email

def finish_redirect(error=None):
    target=settings.app_public_url.rstrip('/')+('/login?oauth='+error if error else '/auth/google')
    response=RedirectResponse(target,status_code=303)
    response.headers['Cache-Control']='no-store';response.headers['Referrer-Policy']='no-referrer'
    if error:response.delete_cookie(cookie_name(),path='/',secure=settings.production,httponly=True,samesite='lax')
    return response

@router.get('/callback')
def callback(request: Request, state: str = '', code: str = '', error: str = '', db: Session = Depends(get_db)):
    configured()
    browser=request.cookies.get(cookie_name(),'')
    if not state or not browser or len(state)>128 or len(browser)>128:
        return finish_redirect('invalid')
    attempt=db.scalar(select(OAuthAttempt).where(OAuthAttempt.state_digest==digest(state)).with_for_update())
    if not attempt or attempt.callback_consumed or as_utc(attempt.expires_at)<=utc_now() or not hmac.compare_digest(attempt.browser_digest,digest(browser)):
        db.rollback();return finish_redirect('invalid')
    verifier,nonce=attempt.verifier,attempt.nonce_digest
    attempt.callback_consumed=True;attempt.verifier=None;attempt_id=attempt.id
    db.commit() # Single use before the external exchange: uncertain provider calls are not retried.
    if error:return finish_redirect('cancelled')
    if not code or len(code)>4096:return finish_redirect('invalid')
    try:
        response=requests.post('https://oauth2.googleapis.com/token',data={
            'code':code,'client_id':settings.google_client_id,
            'client_secret':settings.google_client_secret.get_secret_value(),
            'redirect_uri':settings.google_redirect_uri,'grant_type':'authorization_code','code_verifier':verifier
        },timeout=(3,10),allow_redirects=False)
        response.raise_for_status()
        subject,email=verify_google_token(response.json()['id_token'],nonce)
    except Exception:
        # No provider diagnostic, code, JWT or secret is logged/rendered.
        return finish_redirect('provider')
    attempt=db.get(OAuthAttempt,attempt_id)
    if not attempt or as_utc(attempt.expires_at)<=utc_now():
        return finish_redirect('invalid')
    attempt.subject=subject;attempt.email=email;db.commit()
    return finish_redirect()

def verified_attempt(request, db):
    configured();browser=request.cookies.get(cookie_name(),'')
    if not browser or len(browser)>128:raise HTTPException(401,'Restart Google sign-in')
    attempt=db.scalar(select(OAuthAttempt).where(OAuthAttempt.browser_digest==digest(browser)).with_for_update())
    if not attempt or attempt.completed or not attempt.subject or as_utc(attempt.expires_at)<=utc_now():
        raise HTTPException(401,'Restart Google sign-in')
    return attempt

@router.get('/status')
def handoff_status(request: Request, db: Session = Depends(get_db)):
    attempt=verified_attempt(request,db)
    known=db.scalar(select(GoogleIdentity).where(GoogleIdentity.subject==attempt.subject))
    result={'choose_username':not known and not attempt.link_user_id,'linking':bool(attempt.link_user_id)}
    db.rollback()
    return JSONResponse(result,headers={'Cache-Control':'no-store'})

@router.post('/complete')
def complete(data: GoogleCompleteRequest, request: Request, db: Session = Depends(get_db)):
    require_origin(request);attempt=verified_attempt(request,db)
    identity=db.scalar(select(GoogleIdentity).where(GoogleIdentity.subject==attempt.subject))
    if attempt.link_user_id:
        user=db.scalar(select(User).where(User.id==attempt.link_user_id).with_for_update())
        if not user or user.token_version!=attempt.link_token_version or user.must_change_password or not account_allowed(user):
            raise HTTPException(401,'Reauthenticate before linking')
        if identity or db.scalar(select(GoogleIdentity.id).where(GoogleIdentity.user_id==user.id)):
            raise HTTPException(409,'Google identity is already linked')
        db.add(GoogleIdentity(user_id=user.id,subject=attempt.subject))
    elif identity:
        user=db.get(User,identity.user_id)
        if not user or not account_allowed(user):raise HTTPException(403,'Account access is unavailable')
    else:
        if not settings.registration_enabled:raise HTTPException(403,'Registration is currently closed')
        if not data.username:raise HTTPException(422,'Choose a username')
        if db.scalar(select(User.id).where(func.lower(User.email)==attempt.email)):
            raise HTTPException(409,'Sign in to your existing account and explicitly link Google with your password')
        user=User(username=data.username,email=attempt.email,role='learner',password_enabled=False,
                  password_hash=hash_password(secrets.token_urlsafe(48)))
        db.add(user)
        try:db.flush()
        except IntegrityError:
            db.rollback();raise HTTPException(409,'That username or email is already in use') from None
        db.add(GoogleIdentity(user_id=user.id,subject=attempt.subject))
    attempt.completed=True
    try:db.commit()
    except IntegrityError:
        db.rollback();raise HTTPException(409,'Account association changed; restart sign-in') from None
    db.refresh(user)
    response=JSONResponse(token_response(user));response.delete_cookie(cookie_name(),path='/',secure=settings.production,httponly=True,samesite='lax')
    response.headers['Cache-Control']='no-store';return response

class OAuthAccessLogFilter(logging.Filter):
    """Uvicorn logs paths, but never callback codes or OAuth query values."""
    def filter(self,record):
        if isinstance(record.args,tuple) and len(record.args)>=3 and isinstance(record.args[2],str) and record.args[2].startswith('/api/auth/google/'):
            parts=list(record.args);parts[2]=parts[2].split('?')[0];record.args=tuple(parts)
        return True
logging.getLogger('uvicorn.access').addFilter(OAuthAccessLogFilter())
