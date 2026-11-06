# -*- coding: utf-8 -*-
"""
SSE (Server-Sent Events) Controller for Trackly Mobile App.

Provides a streaming endpoint built directly into Odoo — no separate server needed.
Mobile clients connect to GET /api/mobile/sse?token=<bearer> and receive
real-time 'sync_required' events when data changes.
"""

import json
import logging
import os
import queue
import selectors
import threading
import time

import odoo
from odoo import http
from odoo.http import request
from odoo.modules.registry import Registry
from .utils import SalesRepUtils, token_lookup_values

_logger = logging.getLogger(__name__)

# ── Cross-process delivery ─────────────────────────────────────────────
# Odoo runs several processes in production (prefork HTTP workers, cron
# workers, the gevent server). A business write committed in one process
# must reach SSE streams held by another one, so events travel through
# PostgreSQL NOTIFY on the 'postgres' maintenance database (the same trick
# the bus module uses) and every process holding streams LISTENs to it.
#
#   publish(db, events)  ->  NOTIFY sales_rep_sse '{"db":..,"events":[..]}'
#   listener thread      ->  _deliver(payload)  ->  client queues
#
# { (dbname, sales_rep_id): [queue.Queue, ...] }  — one queue per stream
_sse_clients = {}
_sse_lock = threading.Lock()

CHANNEL = 'sales_rep_sse'
_MAX_PAYLOAD = 7500           # NOTIFY payload limit is 8000 bytes
_STREAM_LIFETIME = 30 * 60    # recycle streams; the app reconnects in ~5s
_HEARTBEAT = 25

_listener = {'thread': None, 'pid': None}
_listener_lock = threading.Lock()


def _current_db():
    try:
        if request and request.db:
            return request.db
    except RuntimeError:
        pass
    return getattr(threading.current_thread(), 'dbname', None)


def _deliver(payload):
    """Push a decoded NOTIFY payload into the local client queues."""
    db = payload.get('db')
    skip = set(payload.get('skip') or [])
    pushed = 0
    with _sse_lock:
        for ev in payload.get('events') or []:
            event_type = ev.get('type') or 'sync_required'
            msg = {'type': event_type, 'reason': ev.get('reason') or 'data_changed'}
            rep = ev.get('rep')
            if rep is None:
                targets = [(key, qs) for key, qs in _sse_clients.items() if key[0] == db]
            else:
                targets = [((db, rep), _sse_clients.get((db, rep), []))]
            for (_db, rep_id), queues in targets:
                # the rep whose own upload caused the change already has the data;
                # a forced logout is never skipped
                if event_type == 'sync_required' and rep_id in skip:
                    continue
                for q in queues:
                    try:
                        q.put_nowait(msg)
                        pushed += 1
                    except queue.Full:
                        pass
    if pushed:
        _logger.info("SSE: delivered %s event(s) to %s stream(s) on %s", len(payload.get('events') or []), pushed, db)


def publish(db, events, skip=None):
    """Send events to every Odoo process. Safe to call from post-commit hooks."""
    db = db or _current_db()
    if not db or not events:
        return
    payload = {'db': db, 'events': events, 'skip': sorted(skip or [])}
    data = json.dumps(payload)
    if len(data) > _MAX_PAYLOAD:
        # too many targeted reps: collapse syncs into one broadcast, keep logouts
        reasons = sorted({e['reason'] for e in events if e['type'] == 'sync_required'})
        payload['events'] = [e for e in events if e['type'] != 'sync_required'][:50] + [
            {'rep': None, 'type': 'sync_required', 'reason': ','.join(reasons)[:500]}]
        data = json.dumps(payload)
    try:
        with odoo.sql_db.db_connect('postgres').cursor() as cr:
            cr.execute("SELECT pg_notify(%s, %s)", (CHANNEL, data))
    except Exception as e:
        # PostgreSQL unreachable for NOTIFY: at least serve this process
        _logger.warning("SSE: NOTIFY failed (%s), delivering in-process only", e)
        _deliver(payload)


def _listen_loop():
    while True:
        try:
            with odoo.sql_db.db_connect('postgres').cursor() as cr:
                conn = cr._cnx
                cr.execute(f"LISTEN {CHANNEL}")
                cr.commit()
                _logger.info("SSE: listener started (pid %s)", os.getpid())
                with selectors.DefaultSelector() as sel:
                    sel.register(conn, selectors.EVENT_READ)
                    while True:
                        if sel.select(_HEARTBEAT * 2):
                            conn.poll()
                            while conn.notifies:
                                note = conn.notifies.pop(0)
                                try:
                                    _deliver(json.loads(note.payload))
                                except Exception as e:
                                    _logger.warning("SSE: bad payload ignored: %s", e)
        except Exception as e:
            _logger.warning("SSE: listener connection lost (%s), retrying in 5s", e)
            time.sleep(5)


def _ensure_listener():
    """One listener per OS process, started with the first stream (after fork)."""
    pid = os.getpid()
    with _listener_lock:
        thread = _listener['thread']
        if thread and thread.is_alive() and _listener['pid'] == pid:
            return
        thread = threading.Thread(target=_listen_loop, name=f'sales_rep_sse_listener_{pid}', daemon=True)
        _listener.update(thread=thread, pid=pid)
        thread.start()


def notify_sales_rep(sales_rep_id, reason='data_changed', event_type='sync_required', skip_sales_rep_id=None, db=None):
    """Immediate (non-transactional) push to one rep. Prefer sse_dispatch.schedule_reps
    from models so the event is sent only after the commit."""
    if not sales_rep_id or (skip_sales_rep_id and sales_rep_id == skip_sales_rep_id):
        return
    publish(db, [{'rep': sales_rep_id, 'type': event_type, 'reason': reason}])


def notify_all(reason='data_changed', event_type='sync_required', skip_sales_rep_id=None, db=None):
    """Immediate broadcast to every stream of the database."""
    publish(db, [{'rep': None, 'type': event_type, 'reason': reason}],
            skip=[skip_sales_rep_id] if skip_sales_rep_id else None)


class SSEController(http.Controller, SalesRepUtils):

    @http.route('/api/mobile/sse', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def sse_stream(self, **kwargs):
        """
        SSE streaming endpoint.
        GET /api/mobile/sse?token=<bearer_token>

        The client connects and receives events as they happen.
        A heartbeat comment is sent every 25s to keep the connection alive.
        """
        # ── Auth ───────────────────────────────────────────────────
        token = kwargs.get('token')
        if not token:
            auth_header = request.httprequest.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header.split(' ', 1)[1]

        if not token:
            return request.make_response(
                'data: {"error": "Missing token"}\n\n',
                headers={'Content-Type': 'text/event-stream'}, status=401
            )

        # ── Find and Bind Database ─────────────────────────────────
        db_name = kwargs.get('db') or request.params.get('db') or request.session.db
        
        if not db_name and token:
            import odoo
            from odoo.http import db_list
            try:
                dbs = db_list(force=True)
            except Exception:
                try:
                    from odoo.service.db import list_dbs
                    dbs = list_dbs()
                except Exception:
                    dbs = []
            
            for d in dbs:
                try:
                    registry = Registry(d)
                    with registry.cursor() as cr:
                        cr.execute(
                            "SELECT id FROM sales_representative WHERE mobile_access_token IN %s AND active = True LIMIT 1",
                            (tuple(token_lookup_values(token)),)
                        )
                        if cr.fetchone():
                            db_name = d
                            break
                except Exception:
                    continue

        if db_name:
            request.session.db = db_name
            request.db = db_name
            try:
                import odoo
                import threading
                registry = Registry(db_name)
                cr = registry.cursor()
                request.registry = registry
                request.env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                threading.current_thread().dbname = db_name
                threading.current_thread().uid = odoo.SUPERUSER_ID
            except Exception as e:
                _logger.error(f"SSE: Failed to initialize registry for db {db_name}: {e}")

        sales_rep = self._authenticate_token(token)
        if not sales_rep:
            return request.make_response(
                'data: {"error": "Invalid token"}\n\n',
                headers={'Content-Type': 'text/event-stream'}, status=401
            )

        rep_id = sales_rep.id
        rep_name = sales_rep.name
        key = (request.env.cr.dbname, rep_id)

        # ── Register client queue ──────────────────────────────────
        client_queue = queue.Queue(maxsize=50)

        with _sse_lock:
            _sse_clients.setdefault(key, []).append(client_queue)
            count = len(_sse_clients[key])
        _ensure_listener()

        _logger.info(f"SSE: Client connected — sales_rep {rep_id} ({rep_name}) on {key[0]}, "
                      f"{count} connection(s), pid {os.getpid()}")

        # ── Close the Odoo cursor before streaming ─────────────────
        # We don't need DB access during the stream, and holding the
        # cursor would block a DB connection for the entire session.
        try:
            request.env.cr.close()
        except Exception:
            pass

        # ── Stream generator ───────────────────────────────────────
        def event_stream():
            try:
                # Send initial connected event
                yield f"event: connected\ndata: {json.dumps({'sales_rep_id': rep_id, 'name': rep_name})}\n\n"

                deadline = time.monotonic() + _STREAM_LIFETIME
                while time.monotonic() < deadline:
                    try:
                        # Block for up to 25s waiting for an event
                        event = client_queue.get(timeout=_HEARTBEAT)
                        # the app listens to 'sync_required' and reads data.type
                        # (sync_required / force_logout) from the payload
                        yield f"event: sync_required\ndata: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        # Send heartbeat to keep connection alive
                        yield ": heartbeat\n\n"
                # Recycle long streams so dead TCP peers never pile up.
                yield "event: reconnect\ndata: {}\n\n"

            except GeneratorExit:
                _logger.info(f"SSE: Client disconnected (GeneratorExit) — sales_rep {rep_id}")
            except Exception as e:
                _logger.warning(f"SSE: Stream error for sales_rep {rep_id}: {e}")
            finally:
                # ── Cleanup ────────────────────────────────────────
                with _sse_lock:
                    if key in _sse_clients:
                        try:
                            _sse_clients[key].remove(client_queue)
                        except ValueError:
                            pass
                        if not _sse_clients[key]:
                            del _sse_clients[key]
                _logger.info(f"SSE: Cleaned up — sales_rep {rep_id}")

        # ── Return streaming response ──────────────────────────────
        import werkzeug.wrappers
        return werkzeug.wrappers.Response(
            event_stream(),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
            }
        )
