import asyncio
from starlette.responses import Response
from app.login_limit import LoginRateLimitMiddleware


def test_production_login_limit_does_not_read_secrets_or_affect_lab_calls():
    now = [0]
    async def downstream(scope, receive, send):
        await Response(status_code=204)(scope, receive, send)
    guard = LoginRateLimitMiddleware(downstream, enabled=True, limit=2, clock=lambda: now[0])
    async def request(path='/api/auth/login', client='test-client'):
        messages = []
        async def receive():
            raise AssertionError('Throttle must not read a password or request body')
        async def send(message):
            messages.append(message)
        await guard(dict(type='http', method='POST', path=path, client=(client, 123)), receive, send)
        return messages[0]['status']
    async def check():
        assert await request() == 204
        assert await request() == 204
        assert await request() == 429
        assert await request('/api/rooms/3171/lab/stop') == 204
        assert await request(client='another-client') == 204
        now[0] = 60
        assert await request() == 204
    asyncio.run(check())


def test_development_login_behavior_is_unchanged():
    async def downstream(scope, receive, send):
        await Response(status_code=204)(scope, receive, send)
    guard = LoginRateLimitMiddleware(downstream, enabled=False, limit=0)
    async def check():
        messages = []
        async def send(message): messages.append(message)
        await guard(dict(type='http', method='POST', path='/api/auth/login'), None, send)
        assert messages[0]['status'] == 204
    asyncio.run(check())


def test_workspace_and_guest_auth_limits_are_scoped_and_never_read_secrets():
    from app.login_limit import TaskRateLimitMiddleware, BuilderAuthRateLimitMiddleware
    async def downstream(scope,receive,send):await Response(status_code=204)(scope,receive,send)
    async def check(guard,path):
        async def request(target):
            messages=[]
            async def receive():raise AssertionError('Secret body must not be inspected')
            async def send(message):messages.append(message)
            await guard(dict(type='http',method='POST',path=target,client=('isolated',1)),receive,send)
            return messages[0]['status']
        assert await request(path)==204
        assert await request(path)==429
        assert await request('/api/rooms/42/lab/stop')==204
        assert await request('/api/auth/google/callback')==204
    asyncio.run(check(TaskRateLimitMiddleware(downstream,enabled=True,limit=1),'/api/rooms/42/workspace/publish'))
    asyncio.run(check(BuilderAuthRateLimitMiddleware(downstream,enabled=True,limit=1),'/api/rooms/42/builder/ssh-auth'))
