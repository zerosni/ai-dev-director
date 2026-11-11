"""Upgraded local console on 8081. Frozen 8080 is deliberately untouched."""
import json
import logging
import re
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import dashboard_server as d
from core.solomon_bridge import SolomonBridge

ROOT = Path(__file__).resolve().parent
# The console is a fixed allowlist of local files: nothing outside console/ is ever served.
STATIC_FILES = {'/': 'index.html', '/console.js': 'console.js', '/console.css': 'console.css',
                '/stage.css': 'stage.css', '/trinity.js': 'trinity.js', '/stage.js': 'stage.js'}


_PICK_LOCK = threading.Lock()


def pick_folder(initial: str = "", timeout: int = 300) -> dict:
    """Open the native folder dialog on this PC's desktop and return {'path': str} ('' when cancelled). One at a time."""
    if not _PICK_LOCK.acquire(blocking=False):
        return {"error": "폴더 선택 창이 이미 열려 있습니다."}
    try:
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "pick_folder.py"), initial or ""],
                             capture_output=True, timeout=timeout)
        if out.returncode != 0:
            return {"error": "폴더 선택 창을 열지 못했습니다."}
        return {"path": str(json.loads(out.stdout.decode("ascii") or "{}").get("path", ""))}
    except subprocess.TimeoutExpired:
        return {"error": "폴더 선택 시간이 초과됐습니다."}
    except (OSError, ValueError):
        return {"error": "폴더 선택 창을 열지 못했습니다."}
    finally:
        _PICK_LOCK.release()


def solomon_target_ok(path: str) -> tuple:
    """Solomon may commission work on any git project under config solomon.allowed_roots, except the denied ones
    (the Director itself, Solomon itself, the shared vault). Returns (ok, resolved_or_reason)."""
    cfg = (d.load_config().get('solomon') or {})
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return False, '경로를 해석할 수 없습니다'
    roots = [Path(r).resolve() for r in (cfg.get('allowed_roots') or [])]
    denied = [Path(x).resolve() for x in (cfg.get('denied') or [])] + [ROOT.resolve()]
    if not any(target == r or r in target.parents for r in roots):
        return False, '솔로몬에 허용된 폴더 밖입니다'
    if any(target == x or x in target.parents for x in denied):
        return False, '이 폴더는 솔로몬이 지시할 수 없습니다'
    return True, str(target)


def project_registry() -> list:
    """Projects Solomon can target: config `projects:` entries, every project a mission ever ran on, and git folders
    one level under the allowed roots. Each entry carries the latest mission and whether a deliverable awaits."""
    from core.mission import MissionStore
    cfg = d.load_config()
    seen = {}

    def add(path, origin):
        ok, resolved = solomon_target_ok(path)
        if not ok or not Path(resolved).is_dir():
            return
        key = resolved.casefold()
        entry = seen.setdefault(key, {'path': resolved, 'name': Path(resolved).name, 'origins': [], 'git': (Path(resolved) / '.git').exists(),
                                      'missions': 0, 'last_mission': None})
        if origin not in entry['origins']:
            entry['origins'].append(origin)
    for key in (cfg.get('projects') or {}):
        add(key, 'config')
    for root in (cfg.get('solomon') or {}).get('allowed_roots') or []:
        try:
            for child in sorted(Path(root).iterdir()):
                if child.is_dir() and (child / '.git').exists():
                    add(child, 'root')
        except OSError:
            pass
    missions = sorted(MissionStore().all(), key=lambda m: m.get('updated_at', ''))
    for m in missions:
        add(m.get('project_path', ''), 'mission')
        ok, resolved = solomon_target_ok(m.get('project_path', ''))
        if ok and resolved.casefold() in seen:
            e = seen[resolved.casefold()]
            e['missions'] += 1
            e['last_mission'] = {'mission_id': m['mission_id'], 'status': m.get('status'), 'goal': (m.get('goal') or '')[:160],
                                 'updated_at': m.get('updated_at'), 'applied': (m.get('applied') or {}).get('status')}
    return sorted(seen.values(), key=lambda e: (e['last_mission'] is None, -(e['missions']), e['name']))


class CompanionHandler(d.DashboardHandler):
    def do_GET(self):
        if not d.is_request_origin_allowed(self.headers, self.server.server_address[1])[0]:
            return self._send_json({'error': 'local requests only'}, 403)
        path = urlsplit(self.path).path
        try:                                    # a GET that carries a body: read it, or closing the socket with unread
            unread = int(self.headers.get('Content-Length') or 0)       # data resets the connection mid-response
        except ValueError:
            unread = 0
        if 0 < unread <= 65536:
            self.rfile.read(unread)
        if path == '/ops/roles':
            from core.role_settings import current
            return self._send_json({'roles': current(d.load_config()), 'csrf': self.server.bridge_token})
        if path == '/ops/local':
            from core.local_status import local_status
            return self._send_json(local_status(d.load_config()))
        if path in STATIC_FILES:
            name = STATIC_FILES[path]
            body = (ROOT / 'console' / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css'}[name.rsplit('.', 1)[1]] + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers(); self.wfile.write(body); return
        if path == '/classic':
            self.path = '/'
        elif path == '/ops/state':
            report = ROOT / 'data' / 'cli_diagnostics.json'
            return self._send_json({'csrf': self.server.bridge_token,
                'orders': self.server.bridge.list(), 'allowed_projects': sorted(self.server.bridge.allowed),
                'diagnostics': json.loads(report.read_text(encoding='utf-8')) if report.exists() else None,
                'status': d.cached_status_payload(), 'solomon_url': 'http://127.0.0.1:8710'})
        elif path.startswith('/bridge/'):
            if not self.authorized():
                return self._send_json({'error': 'bridge authentication required'}, 403)
            try:
                if path == '/bridge/status':
                    return self._send_json({'ok': True, 'version': '1.0', 'orders': self.server.bridge.list()})
                if path == '/bridge/order':
                    ident = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                    return self._send_json(self.server.bridge.status(ident))
                if path == '/bridge/projects':
                    return self._send_json({'projects': project_registry()})
                if path == '/bridge/digest':
                    return self._send_json(d.build_digest_payload())
                if path == '/bridge/missions':
                    from core.mission import MissionStore
                    rows = sorted(MissionStore().all(), key=lambda m: m.get('updated_at', ''), reverse=True)[:30]
                    return self._send_json({'missions': [d.mission_view(m) for m in rows]})
                if path.startswith('/bridge/missions/'):
                    from core.mission import MissionStore
                    ident = path.split('/')[3][:80]
                    m = MissionStore().load(ident)
                    if not m:
                        return self._send_json({'error': '알 수 없는 미션입니다'}, 404)
                    return self._send_json(d.mission_view(m))
            except ValueError as exc:
                return self._send_json({'error': str(exc)}, 400)
        return super().do_GET()

    def authorized(self):
        return secrets.compare_digest(self.headers.get('X-Solomon-Bridge', ''), self.server.bridge_token)

    def do_POST(self):
        if self.path == '/ops/roles':
            if not d.is_request_origin_allowed(self.headers, self.server.server_address[1])[0] or not self.authorized():
                return self._send_json({'error': 'local authenticated request required'}, 403)
            try:
                from core.role_settings import save
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Invalid role request')
                roles = save(json.loads(self.rfile.read(size)))
                try:
                    # Make the provider cards show the new HEAD/BUILDER/REVIEWER labels right away instead of
                    # after the next run (resolve_roles rewrites provider_health.current_role; can_proceed is ignored here).
                    from core.provider_manager import ProviderManager
                    ProviderManager().resolve_roles(roles['head'], roles['builder'], roles['reviewer'], strict=True)
                    d.invalidate_status_cache()
                except Exception as exc:  # bookkeeping only; the saved file is already the source of truth
                    logging.getLogger('director.companion').warning('role bookkeeping refresh failed: %s', exc)
                return self._send_json({'roles': roles, 'message': '저장됨 · 다음 대화/작업부터 적용됩니다. 진행 중인 실행은 유지됩니다.'})
            except (ValueError, TypeError, KeyError) as exc:
                return self._send_json({'error': str(exc)}, 400)
            except OSError:
                return self._send_json({'error': '역할 설정을 저장하지 못했습니다.'}, 500)
        if self.path == '/ops/missions/stop':
            # 작업 정지 (2026-11-03): stop running missions + kill their CLI processes; the dashboard stays up.
            if not d.is_request_origin_allowed(self.headers, self.server.server_address[1])[0] or not self.authorized():
                return self._send_json({'error': 'local authenticated request required'}, 403)
            try:
                size = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(size)) if 0 < size <= 4096 else {}
                if not isinstance(payload, dict):
                    payload = {}
                return self._send_json(d.stop_missions(str(payload.get('reason', ''))[:300], payload.get('mission_id') or None, by=str(payload.get('by') or '사용자')[:40]))
            except (ValueError, TypeError) as exc:
                return self._send_json({'error': str(exc)}, 400)
        if self.path == '/ops/shutdown':
            # "시스템 중지" (2026-11-02): stop the companion from the console or from Solomon. Records, worktrees and
            # mission state stay on disk; resume_on_start picks interrupted missions up again when it is restarted.
            if not d.is_request_origin_allowed(self.headers, self.server.server_address[1])[0] or not self.authorized():
                return self._send_json({'error': 'local authenticated request required'}, 403)
            try:
                unread = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                unread = 0
            if 0 < unread <= 65536:
                self.rfile.read(unread)
            self.server.stopping = True
            logging.getLogger('director.companion').warning('shutdown requested from %s', self.headers.get('Referer') or 'local client')
            import threading
            threading.Thread(target=self.server.shutdown, name='director-shutdown', daemon=True).start()
            return self._send_json({'stopping': True, 'message': 'AI DEV를 멈춥니다. 기록과 작업본은 남고, 다시 켜면 중단된 미션을 이어갑니다.'})
        if self.path == '/ops/pick-folder':
            if not d.is_request_origin_allowed(self.headers, self.server.server_address[1])[0] or not self.authorized():
                return self._send_json({'error': 'local authenticated request required'}, 403)
            try:
                current = (d.cached_status_payload() or {}).get('project_path') or ''
            except Exception:
                current = ''
            result = pick_folder(current)
            return self._send_json(result, 409 if 'error' in result else 200)
        if self.path == '/bridge/missions' or (self.path.startswith('/bridge/missions/') and self.path.endswith('/decide')):
            if not d.is_request_origin_allowed(self.headers, self.server.server_address[1])[0] or not self.authorized():
                return self._send_json({'error': 'bridge authentication required'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32000 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('JSON request required')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('object required')
                if self.path == '/bridge/missions':
                    ident = str(payload.get('request_id', ''))
                    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', ident):
                        raise ValueError('안정적인 request_id가 필요합니다')
                    from core.mission import MissionStore
                    known = [m for m in MissionStore().all() if m.get('request_id') == ident]
                    if known:                                  # retries never start a second mission
                        return self._send_json({'status': 'duplicate', 'mission_id': known[0]['mission_id'], 'mission': d.mission_view(known[0])})
                    ok, target = solomon_target_ok(str(payload.get('project_path', '')))
                    if not ok:
                        raise ValueError(target)
                    goal = str(payload.get('goal', '')).strip()
                    if not 5 <= len(goal) <= 6000:
                        raise ValueError('작업 목표는 5~6000자여야 합니다')
                    result = d.start_goal_mission(target, goal, profile=payload.get('profile'), source='solomon', request_id=ident,
                                                  create_project=bool(payload.get('create_project')), auto_apply=payload.get('auto_apply'))
                    code = {'started': 202, 'busy': 409, 'question': 200, 'escalated': 200}.get(result.get('status'), 400)
                    return self._send_json(result, code)
                ident = self.path.split('/')[3][:80]
                result = d.decide_mission(ident, str(payload.get('action', '')), reason=str(payload.get('reason', ''))[:500],
                                          max_steps=payload.get('max_steps'), on_conflict=str(payload.get('on_conflict', 'block')), by='SOLOMON')
                return self._send_json(result, 200 if result.get('status') != 'error' else 400)
            except (ValueError, TypeError, KeyError) as exc:
                return self._send_json({'error': str(exc)}, 400)
            except Exception as exc:
                return self._send_json({'error': str(exc)[:500]}, 500)
        if self.path == '/bridge/orders':
            if not d.is_request_origin_allowed(self.headers, self.server.server_address[1])[0] or not self.authorized():
                return self._send_json({'error': 'bridge authentication required'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32000:
                    raise ValueError('request size rejected')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('JSON required')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('object required')
                return self._send_json(self.server.bridge.submit(payload), 202)
            except (ValueError, TypeError, KeyError) as exc:
                return self._send_json({'error': str(exc)}, 400)
            except Exception as exc:
                return self._send_json({'error': str(exc)[:500]}, 500)
        return super().do_POST()


def run():
    config = json.loads((ROOT / 'companion.json').read_text(encoding='utf-8'))
    d._ensure_schema()
    server = d.DashboardServer(('127.0.0.1', config.get('port', 8081)), CompanionHandler)
    token_path = ROOT / 'data' / 'solomon-bridge.token'
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(48), encoding='utf-8')
    server.bridge_token = token_path.read_text(encoding='utf-8').strip()
    server.bridge = SolomonBridge(d, config.get('allowed_projects', []))
    if (d.load_config().get('autonomy') or {}).get('resume_on_start'):
        import threading
        threading.Thread(target=d.resume_interrupted_missions, name='director-mission-resume', daemon=True).start()
    server.stopping = False
    print('AI DEV × SOLOMON http://127.0.0.1:' + str(server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        print('AI DEV companion stopped (system stop)', flush=True)


if __name__ == '__main__':
    run()
