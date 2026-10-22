"""Small production-only login throttle for the single-process private beta."""
from collections import deque
import time
from starlette.responses import JSONResponse


class LoginRateLimitMiddleware:
    busy_detail = 'Login is busy; retry shortly'
    limited_detail = 'Too many login attempts; retry in a minute'
    def __init__(self, app, enabled=False, limit=20, window=60, clock=time.monotonic):
        self.app, self.enabled, self.limit, self.window, self.clock = app, enabled, limit, window, clock
        self.clients = {}

    def matches(self, scope):
        paths = {'/api/auth/login', '/api/auth/password', '/api/users', '/api/auth/google/link', '/api/auth/google/complete', '/api/auth/google/start', '/api/auth/google/callback', '/api/creator/invitations/accept'}
        return scope.get('path') in paths

    async def __call__(self, scope, receive, send):
        if self.enabled and scope['type'] == 'http' and self.matches(scope):
            now = self.clock()
            key = (scope.get('client') or ('unknown',))[0]
            # Bounded storage; also discard inactive clients. No credentials or
            # request bodies are inspected. Trusted proxy settings determine IP.
            for client in list(self.clients):
                if not self.clients[client] or self.clients[client][-1] <= now - self.window:
                    del self.clients[client]
            if key not in self.clients and len(self.clients) >= 1024:
                response = JSONResponse({'detail': self.busy_detail}, 429, headers={'Retry-After': '60'})
                await response(scope, receive, send)
                return
            attempts = self.clients.setdefault(key, deque())
            while attempts and attempts[0] <= now - self.window:
                attempts.popleft()
            if len(attempts) >= self.limit:
                response = JSONResponse({'detail': self.limited_detail}, 429, headers={'Retry-After': '60'})
                await response(scope, receive, send)
                return
            attempts.append(now)
        await self.app(scope, receive, send)


class PublicRoomRateLimitMiddleware(LoginRateLimitMiddleware):
    """Separate bounded read budget; public browsing cannot consume login quota."""
    busy_detail = 'Room browsing is busy; retry shortly'
    limited_detail = 'Too many room requests; retry in a minute'
    def __init__(self, app, enabled=False, limit=120, **kwargs):
        super().__init__(app, enabled=enabled, limit=limit, **kwargs)

    def matches(self, scope):
        path = scope.get('path', '')
        return scope.get('method') == 'GET' and (path == '/api/public/rooms' or path.startswith('/api/public/rooms/'))


class ProfileWriteRateLimitMiddleware(LoginRateLimitMiddleware):
    busy_detail = 'Profile editing is busy; retry shortly'
    limited_detail = 'Too many profile changes; retry in a minute'
    def __init__(self, app, enabled=False, limit=10, **kwargs):
        super().__init__(app, enabled=enabled, limit=limit, **kwargs)
    def matches(self, scope):
        return scope.get('path') in ('/api/users/me','/api/users/me/avatar') and scope.get('method') in ('PATCH','PUT','DELETE')


class ProfileReadRateLimitMiddleware(PublicRoomRateLimitMiddleware):
    def matches(self, scope):
        return scope.get('method') == 'GET' and scope.get('path') in ('/api/users/me/learning','/api/users/me/activity','/api/users/me/avatar','/api/users/me/creator-key')


class LeaderboardRateLimitMiddleware(PublicRoomRateLimitMiddleware):
    busy_detail = 'Rankings are busy; retry shortly'
    limited_detail = 'Too many ranking requests; retry in a minute'
    def matches(self, scope):
        path=scope.get('path','')
        return scope.get('method')=='GET' and (path in ('/api/public/leaderboard','/api/leaderboard/me') or path.startswith('/api/public/learners/'))


class TaskRateLimitMiddleware(PublicRoomRateLimitMiddleware):
    busy_detail = 'Task editing is busy; retry shortly'
    limited_detail = 'Too many task requests; retry in a minute'
    def matches(self, scope):
        import re
        return re.fullmatch(r'/api/rooms/[0-9]+/(?:tasks(?:/[0-9]+|/order)?|workspace(?:/publish)?)',scope.get('path','')) is not None


class BuilderAuthRateLimitMiddleware(LoginRateLimitMiddleware):
    busy_detail = 'Guest SSH configuration is busy; retry shortly'
    limited_detail = 'Too many guest SSH changes; retry in a minute'
    def __init__(self, app, enabled=False, limit=10, **kwargs):
        super().__init__(app, enabled=enabled, limit=limit, **kwargs)
    def matches(self, scope):
        import re
        return re.fullmatch(r'/api/rooms/[0-9]+/builder/ssh-auth',scope.get('path','')) is not None


class PathRateLimitMiddleware(PublicRoomRateLimitMiddleware):
    busy_detail = 'Learning paths are busy; retry shortly'
    limited_detail = 'Too many path requests; retry in a minute'
    def matches(self, scope):
        path = scope.get('path', '')
        return any(path == root or path.startswith(root + '/') for root in ('/api/paths', '/api/public/paths'))


class DiscoveryRateLimitMiddleware(PublicRoomRateLimitMiddleware):
    busy_detail = 'Discovery is busy; retry shortly'
    limited_detail = 'Too many discovery requests; retry in a minute'
    def matches(self,scope):
        path=scope.get('path','')
        return any(path==root or path.startswith(root+'/') for root in ('/api/discovery','/api/bookmarks','/api/saved'))


class OperationsRateLimitMiddleware(PublicRoomRateLimitMiddleware):
    busy_detail = 'Management requests are busy; retry shortly'
    limited_detail = 'Too many management requests; retry in a minute'
    def __init__(self, app, enabled=False, limit=60, **kwargs):
        super().__init__(app, enabled=enabled, limit=limit, **kwargs)
    def matches(self, scope):
        path=scope.get('path','')
        return any(path==root or path.startswith(root+'/') for root in ('/api/admin','/api/creator/analytics'))


class SocialRateLimitMiddleware(LoginRateLimitMiddleware):
    """Bound writes separately from public profile reads; stores no event stream."""
    limited_detail = 'Too many connection changes; retry in a minute'
    def __init__(self, app, enabled=False, limit=30, **kwargs):
        super().__init__(app, enabled=enabled, limit=limit, **kwargs)
    def matches(self, scope):
        return scope.get('path', '').startswith('/api/social/') and scope.get('method') in ('PUT','DELETE')


class RoomMediaRateLimitMiddleware(LoginRateLimitMiddleware):
    limited_detail = 'Too many lesson image requests; retry in a minute'
    def __init__(self, app, enabled=False, limit=60, **kwargs):
        super().__init__(app, enabled=enabled, limit=limit, **kwargs)
    def matches(self, scope):
        import re
        return re.fullmatch(r'/api/rooms/[0-9]+/media(?:/[a-f0-9]{32})?', scope.get('path','')) is not None


class CreatorKeyRateLimitMiddleware(LoginRateLimitMiddleware):
    limited_detail = 'Too many creator key changes; retry in a minute'
    def __init__(self, app, enabled=False, limit=10, **kwargs):
        super().__init__(app, enabled=enabled, limit=limit, **kwargs)
    def matches(self, scope):
        import re
        path=scope.get('path','')
        return scope.get('method') in ('PUT','POST','DELETE') and (path=='/api/users/me/creator-key' or re.fullmatch(r'/api/rooms/[0-9]+/builder/creator-key|/api/admin/users/[0-9]+/creator-key/revoke',path) is not None)
