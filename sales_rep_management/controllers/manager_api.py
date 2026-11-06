# -*- coding: utf-8 -*-
"""REST API of the manager / supervisor mobile app (trackly_manager).

Authentication follows the representative app: master server -> Odoo session
-> ``POST /api/manager/login`` which returns a manager token (Bearer). A Sales
Manager follows every representative of his companies, a supervisor his whole
team (``res.users._srm_scope_reps``). Every query below is filtered by that
scope; nothing trusts a representative id sent by the phone without checking
it against the scope.

Live updates: ``GET /api/manager/sse`` (same channel as the representative
stream; chat messages arrive as ``{type: 'chat_message', reason: 'channel:<id>'}``).
"""
import json
import logging
import secrets
from datetime import timedelta

from odoo import _, fields, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from .utils import SalesRepUtils, hash_token, parse_mobile_datetime

_logger = logging.getLogger(__name__)

OPEN_TASK = ('new', 'in_progress')


def _dt(value):
    return fields.Datetime.to_string(value) if value else False


class ManagerApiBase(http.Controller, SalesRepUtils):

    # ----------------------------------------------------------------- helpers
    def _json(self, data, status=200):
        return request.make_response(json.dumps(data, default=str), status=status,
                                     headers={'Content-Type': 'application/json'})

    def _body(self):
        try:
            return json.loads(request.httprequest.data or '{}')
        except ValueError:
            return {}

    def _token(self):
        header = request.httprequest.headers.get('Authorization', '')
        if header.startswith('Bearer '):
            return header.split(' ', 1)[1].strip()
        return (request.httprequest.args.get('token') or '').strip() or None

    def _manager_session(self, token):
        if not token:
            return None
        return request.env['sales.rep.manager.session'].sudo().search(
            [('token_hash', '=', hash_token(token)), ('active', '=', True)], limit=1)

    def _auth_manager(self):
        """Return the manager user (request.env switched to him) or None."""
        session = self._manager_session(self._token())
        if not session:
            return None
        refusal = session._check(self._request_device_id())
        if refusal:
            request.srm_auth_error = refusal
            return None
        user = session.user_id
        request.env = request.env(user=user, context=dict(
            request.env.context, allowed_company_ids=(user.company_ids or user.company_id).ids, lang=user.lang))
        return user

    def _unauthorized(self):
        code = getattr(request, 'srm_auth_error', None)
        message = {
            'token_expired': _('Your session has expired. Please log in again.'),
            'other_device': _('This account is now signed in on another phone.'),
            'not_manager': _('Your account is no longer a manager or supervisor.'),
        }.get(code, _('Unauthorized'))
        return self._json({'success': False, 'message': message, 'code': code}, 401)

    def _run(self, fn, *args, **kwargs):
        """Authenticate, run, and turn business errors into {success: false}."""
        user = self._auth_manager()
        if not user:
            return self._unauthorized()
        try:
            return self._json(dict({'success': True}, **(fn(user, *args, **kwargs) or {})))
        except (UserError, ValidationError, AccessError) as e:
            request.env.cr.rollback()
            return self._json({'success': False, 'message': str(e.args[0] if e.args else e)})
        except Exception as e:
            request.env.cr.rollback()
            _logger.exception("Manager API error")
            return self._json({'success': False, 'message': _('Server error'), 'error': str(e)}, 500)

    # -------------------------------------------------------------- scoping
    def _scope(self, user, args=None):
        """Scope reps, narrowed by the optional filters supervisor_id / area_id / rep_ids."""
        reps = user._srm_scope_reps()
        args = args or {}
        if args.get('supervisor_id'):
            sup = reps.browse(int(args['supervisor_id'])).exists()
            reps = reps & (sup._team_reps() - sup) if sup else reps.browse()
        if args.get('area_id'):
            areas = request.env['sales.area'].sudo().search([('id', 'child_of', int(args['area_id']))])
            reps = reps & areas.mapped('sales_rep_ids')
        if args.get('rep_ids'):
            ids = [int(x) for x in str(args['rep_ids']).split(',') if x.strip().isdigit()]
            reps = reps.filtered(lambda r: r.id in ids)
        return reps

    def _today(self, user):
        return fields.Date.context_today(user)

    @staticmethod
    def _rep_brief(rep):
        return {'id': rep.id, 'name': rep.name, 'code': rep.code or '', 'phone': rep.phone or '',
                'email': rep.email or '', 'user_id': rep.user_id.id, 'partner_id': rep.user_id.partner_id.id,
                'supervisor': rep.supervisor_id.name or '', 'is_supervisor': rep.is_supervisor,
                'areas': rep.sales_area_ids.mapped('name'),
                'image_url': f'/web/image/res.users/{rep.user_id.id}/avatar_128' if rep.user_id else ''}

    @staticmethod
    def _position(rep):
        if not rep.last_location_at:
            return None
        return {'latitude': rep.last_latitude, 'longitude': rep.last_longitude,
                'at': _dt(rep.last_location_at), 'source': rep.last_location_source or ''}


class ManagerApi(ManagerApiBase):

    # ================================================================= auth
    @http.route('/api/manager/login', type='http', auth='none', methods=['POST'], cors='*', csrf=False)
    def login(self, **kw):
        data = self._body()
        try:
            db = data.get('db') or request.db
            if db:
                request.session.db = db
            info = request.session.authenticate(db, {'type': 'password', 'login': data.get('login'),
                                                     'password': data.get('password')})
            uid = info and info.get('uid')
            if not uid:
                return self._json({'success': False, 'message': _('Invalid credentials')})
            Users = request.env['res.users'].sudo()
            user = Users.browse(uid)
            via_service = False
            if not user._srm_is_manager_app_user() and data.get('email'):
                # shared service user of the master-server flow: identify the manager by e-mail
                service = AuthServiceLogins.logins()
                if service and (user.login or '').lower() in service:
                    email = data['email'].strip()
                    user = Users.search(['|', ('login', '=ilike', email), ('email', '=ilike', email)], limit=1)
                    via_service = True
            if not user or not user._srm_is_manager_app_user():
                return self._json({'success': False, 'message': _(
                    'This account is not a sales manager or supervisor. Use the representative app.')})
            token = (data.get('access_token') if via_service else None) or secrets.token_urlsafe(32)
            master_expiry = parse_mobile_datetime(data.get('token_expiration'), user) if data.get('token_expiration') else None
            expiry = request.env['sales.rep.manager.session'].sudo()._bind(
                user, hash_token(token), (data.get('device_id') or '').strip() or None, data.get('device_model'), master_expiry)
            return self._json({
                'success': True, 'access_token': token, 'token_expiration': _dt(expiry),
                'session_id': request.session.sid, 'user': self._me(user),
            })
        except Exception as e:
            _logger.exception("Manager login failed")
            return self._json({'success': False, 'message': _('Login failed'), 'error': str(e)})

    def _me(self, user):
        own = request.env['sales.representative'].sudo().search([('user_id', '=', user.id)], limit=1)
        reps = user._srm_scope_reps()
        return {
            'id': user.id, 'name': user.name, 'partner_id': user.partner_id.id, 'lang': user.lang,
            'company': user.company_id.name, 'currency_symbol': user.company_id.currency_id.symbol,
            'is_manager': user.has_group('sales_rep_management.group_sales_manager'),
            'is_supervisor': user.has_group('sales_rep_management.group_sales_supervisor'),
            'rep_id': own.id or False, 'team_size': len(reps),
            'image_url': f'/web/image/res.users/{user.id}/avatar_128',
        }

    @http.route('/api/manager/logout', type='http', auth='none', methods=['POST'], cors='*', csrf=False)
    def logout(self, **kw):
        session = self._manager_session(self._token())
        if session:
            session.write({'active': False})
        return self._json({'success': True})

    @http.route('/api/manager/me', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def me(self, **kw):
        return self._run(lambda user: {'user': self._me(user)})

    @http.route('/api/manager/sse', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def sse(self, **kw):
        user = self._auth_manager()
        if not user:
            return request.make_response('data: {"error": "Invalid token"}\n\n',
                                         headers={'Content-Type': 'text/event-stream'}, status=401)
        from .sse import stream_response, user_key
        return stream_response((request.env.cr.dbname, user_key(user.id)),
                               {'user_id': user.id, 'name': user.name}, f'manager {user.id}')

    # ============================================================= overview
    @http.route('/api/manager/filters', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def filters(self, **kw):
        def run(user):
            reps = user._srm_scope_reps().sorted('name')
            areas = request.env['sales.area'].sudo().search([('sales_rep_ids', 'in', reps.ids)])
            areas |= areas.mapped('parent_id')
            return {
                'reps': [{'id': r.id, 'name': r.name} for r in reps],
                'supervisors': [{'id': r.id, 'name': r.name} for r in reps.filtered('is_supervisor')],
                'areas': [{'id': a.id, 'name': a.complete_name} for a in areas.sorted('complete_name')],
                'task_types': request.env['sales.rep.task']._fields['task_type']._description_selection(request.env),
                'proof_types': request.env['sales.rep.task']._fields['proof_type']._description_selection(request.env),
            }
        return self._run(run)

    @http.route('/api/manager/overview', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def overview(self, **kw):
        def run(user):
            today = self._today(user)
            date_from = fields.Date.to_date(kw.get('date_from')) if kw.get('date_from') else today
            date_to = fields.Date.to_date(kw.get('date_to')) if kw.get('date_to') else today
            reps = self._scope(user, kw)
            cc = request.env['sales.rep.command.center'].sudo().with_context(
                allowed_company_ids=(user.company_ids or user.company_id).ids)
            data = cc.get_data(fields.Date.to_string(date_from), fields.Date.to_string(date_to),
                               rep_ids=reps.ids or [0]) if reps else {'empty': True}
            Workday = request.env['sales.rep.workday'].sudo()
            working = Workday.search_count([('sales_rep_id', 'in', reps.ids), ('date', '=', today),
                                            ('start_time', '!=', False), ('end_time', '=', False)])
            started = Workday.search_count([('sales_rep_id', 'in', reps.ids), ('date', '=', today),
                                            ('start_time', '!=', False)])
            data['today'] = {
                'date': today, 'reps': len(reps), 'working_now': working, 'not_started': len(reps) - started,
                'pending_approvals': self._approval_count(user, reps),
                'alerts': request.env['sales.rep.security.alert'].sudo().search_count(
                    [('sales_rep_id', 'in', reps.ids), ('detected_at', '>=', fields.Datetime.to_string(
                        fields.Datetime.now() - timedelta(days=1)))]),
                'overdue_tasks': request.env['sales.rep.task'].sudo().search_count(
                    [('sales_rep_id', 'in', reps.ids), ('state', 'in', OPEN_TASK), ('date_deadline', '<', today)]),
                'unread_messages': ChatApiHelpers.unread_total(user),
            }
            # sections carry an Odoo domain for the web client; the phone does not need it
            for s in data.get('sections', []):
                s.pop('domain', None)
                s['extra'] = [list(x) for x in s.get('extra') or []]
            return {'overview': data}
        return self._run(run)

    def _approval_count(self, user, reps):
        Req = request.env['sales.rep.dept.request'].sudo()
        n = Req.search_count(['&', ('state', '=', 'submitted'),
                              '|', ('approver_id', '=', user.id), ('sales_rep_id', 'in', reps.ids)])
        n += request.env['sales.rep.day.close'].sudo().search_count(
            [('sales_rep_id', 'in', reps.ids), ('state', '=', 'submitted')])
        return n

    # ================================================================= team
    def _route_progress(self, reps, day):
        """{rep_id: (done, planned)} for the routes of the day."""
        stops = request.env['sales.route.customer'].sudo().read_group(
            [('route_id.sales_rep_id', 'in', reps.ids), ('route_id.date', '=', day), ('state', '!=', 'cancelled')],
            ['route_id', 'state'], ['route_id', 'state'], lazy=False)
        route_rep = {r.id: r.sales_rep_id.id for r in request.env['sales.rep.route'].sudo().search(
            [('sales_rep_id', 'in', reps.ids), ('date', '=', day)])}
        out = {}
        for g in stops:
            rep_id = route_rep.get(g['route_id'][0])
            done, planned = out.get(rep_id, (0, 0))
            planned += g['__count']
            if g['state'] == 'visited':
                done += g['__count']
            out[rep_id] = (done, planned)
        return out

    @http.route('/api/manager/team', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def team(self, **kw):
        def run(user):
            today = self._today(user)
            reps = self._scope(user, kw).sorted('name')
            q = (kw.get('q') or '').strip().lower()
            if q:
                reps = reps.filtered(lambda r: q in (r.name or '').lower() or q in (r.code or '').lower())
            progress = self._route_progress(reps, today)
            wds = {w.sales_rep_id.id: w for w in request.env['sales.rep.workday'].sudo().search(
                [('sales_rep_id', 'in', reps.ids), ('date', '=', today)])}
            Task = request.env['sales.rep.task'].sudo()
            open_tasks = {g['sales_rep_id'][0]: g['sales_rep_id_count'] for g in Task.read_group(
                [('sales_rep_id', 'in', reps.ids), ('state', 'in', OPEN_TASK)], ['sales_rep_id'], ['sales_rep_id'])}
            members = []
            for rep in reps:
                k = rep._kpi_compute(today, today)
                done, planned = progress.get(rep.id, (0, 0))
                wd = wds.get(rep.id)
                members.append(dict(self._rep_brief(rep), **{
                    'today': {'sales': k['sales_value'], 'collections': k['collection_value'], 'orders': k['orders'],
                              'visited': done, 'planned': planned, 'strike_rate': k['strike_rate']},
                    'workday': wd.payload() if wd else {'started': False, 'ended': False},
                    'open_tasks': open_tasks.get(rep.id, 0),
                    'position': self._position(rep),
                    'last_seen': _dt(rep.mobile_last_seen),
                }))
            return {'members': members, 'currency_symbol': user.company_id.currency_id.symbol}
        return self._run(run)

    @http.route('/api/manager/rep/<int:rep_id>', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def rep_detail(self, rep_id, **kw):
        def run(user):
            rep = user._srm_check_rep(rep_id)
            today = self._today(user)
            month = today.replace(day=1)
            routes = request.env['sales.rep.route'].sudo().search([('sales_rep_id', '=', rep.id), ('date', '=', today)])
            targets = request.env['sales.rep.target'].sudo().search([
                ('sales_rep_id', '=', rep.id), ('state', 'in', ('approved', 'closed')),
                ('date_from', '<=', today), ('date_to', '>=', today)])
            tasks = request.env['sales.rep.task'].sudo().search(
                [('sales_rep_id', '=', rep.id), ('state', 'in', OPEN_TASK)], order='date_deadline', limit=30)
            wd = request.env['sales.rep.workday'].sudo().search([('sales_rep_id', '=', rep.id), ('date', '=', today)], limit=1)
            team = rep._team_reps() - rep
            return {
                'rep': dict(self._rep_brief(rep), position=self._position(rep), last_seen=_dt(rep.mobile_last_seen),
                            team=[{'id': r.id, 'name': r.name} for r in team]),
                'today': rep._kpi_compute(today, today),
                'month': rep._kpi_compute(month, today),
                'commission_estimate': request.env['sales.rep.commission.sheet'].sudo().estimate_for(rep, month, today),
                'workday': wd.payload() if wd else {'started': False, 'ended': False},
                'routes': [self._route_payload(r) for r in routes],
                'targets': [{'id': t.id, 'name': t.display_name, 'score': t.score,
                             'lines': [{'label': ln.display_name, 'target': ln.target_value, 'actual': ln.actual_value,
                                        'achievement': ln.achievement} for ln in t.line_ids]} for t in targets],
                'tasks': [self._task_payload(t) for t in tasks],
                'currency_symbol': rep.company_id.currency_id.symbol,
            }
        return self._run(run)

    # =============================================================== routes
    @staticmethod
    def _route_payload(route, with_stops=True):
        stops = route.route_customer_ids.sorted('sequence')
        visited = len(stops.filtered(lambda s: s.state == 'visited'))
        data = {'id': route.id, 'name': route.name, 'date': route.date, 'state': route.state,
                'rep_id': route.sales_rep_id.id, 'rep_name': route.sales_rep_id.name,
                'area': route.sales_area_id.name or '', 'planned': len(stops.filtered(lambda s: s.state != 'cancelled')),
                'visited': visited}
        if with_stops:
            data['stops'] = [{
                'id': s.id, 'sequence': s.sequence, 'partner_id': s.partner_id.id, 'name': s.partner_id.name,
                'address': s.partner_id.contact_address_complete or '', 'phone': s.partner_id.phone or s.partner_id.mobile or '',
                'state': s.state, 'visit_result': s.visit_result or '',
                'start': _dt(s.visit_start_time), 'end': _dt(s.visit_end_time),
                'latitude': s.partner_id.visit_latitude or s.partner_id['partner_latitude'] if 'partner_latitude' in s.partner_id._fields
                else s.partner_id.visit_latitude,
                'longitude': s.partner_id.visit_longitude or s.partner_id['partner_longitude'] if 'partner_longitude' in s.partner_id._fields
                else s.partner_id.visit_longitude,
            } for s in stops]
        return data

    @http.route('/api/manager/routes', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def routes(self, **kw):
        def run(user):
            day = fields.Date.to_date(kw.get('date')) if kw.get('date') else self._today(user)
            reps = self._scope(user, kw)
            routes = request.env['sales.rep.route'].sudo().search(
                [('sales_rep_id', 'in', reps.ids), ('date', '=', day)], order='sales_rep_id, name')
            return {'date': day, 'routes': [self._route_payload(r, with_stops=False) for r in routes]}
        return self._run(run)

    @http.route('/api/manager/routes/<int:route_id>', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def route_detail(self, route_id, **kw):
        def run(user):
            route = request.env['sales.rep.route'].sudo().browse(route_id).exists()
            if not route:
                raise UserError(_('Route not found'))
            user._srm_check_rep(route.sales_rep_id.id)
            return {'route': self._route_payload(route)}
        return self._run(run)

    @http.route('/api/manager/map', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def live_map(self, **kw):
        def run(user):
            today = self._today(user)
            reps = self._scope(user, kw)
            routes = request.env['sales.rep.route'].sudo().search([('sales_rep_id', 'in', reps.ids), ('date', '=', today)])
            stops_by_rep = {}
            for r in routes:
                stops_by_rep.setdefault(r.sales_rep_id.id, []).extend(
                    s for s in self._route_payload(r)['stops'] if s['latitude'] or s['longitude'])
            wds = {w.sales_rep_id.id: w for w in request.env['sales.rep.workday'].sudo().search(
                [('sales_rep_id', 'in', reps.ids), ('date', '=', today)])}
            return {'reps': [{
                'id': rep.id, 'name': rep.name, 'phone': rep.phone or '', 'position': self._position(rep),
                'working': bool(wds.get(rep.id) and wds[rep.id].start_time and not wds[rep.id].end_time),
                'stops': stops_by_rep.get(rep.id, []),
            } for rep in reps]}
        return self._run(run)

    # ================================================================ tasks
    @staticmethod
    def _task_payload(t):
        return {'id': t.id, 'name': t.name, 'task_type': t.task_type, 'state': t.state, 'priority': t.priority,
                'date_deadline': t.date_deadline, 'is_overdue': t.is_overdue, 'rep_id': t.sales_rep_id.id,
                'rep_name': t.sales_rep_id.name, 'partner_id': t.partner_id.id, 'partner_name': t.partner_id.name or '',
                'description': t.description or '', 'proof_type': t.proof_type, 'result_note': t.result_note or '',
                'done_date': _dt(t.done_date), 'assigned_by': t.assigned_by.name or '', 'source': t.source,
                'has_photo': bool(t.proof_image), 'amount_due': t.amount_due,
                'photo_url': f'/api/manager/tasks/{t.id}/photo' if t.proof_image else ''}

    @http.route('/api/manager/tasks', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def tasks(self, **kw):
        def run(user):
            reps = self._scope(user, kw)
            today = self._today(user)
            domain = [('sales_rep_id', 'in', reps.ids)]
            state = kw.get('state') or 'open'
            if state == 'open':
                domain.append(('state', 'in', OPEN_TASK))
            elif state == 'overdue':
                domain += [('state', 'in', OPEN_TASK), ('date_deadline', '<', today)]
            elif state == 'closed':
                domain += [('state', 'in', ('done', 'failed')), ('done_date', '>=', fields.Datetime.to_string(
                    fields.Datetime.now() - timedelta(days=int(kw.get('days') or 7))))]
            tasks = request.env['sales.rep.task'].sudo().search(domain, order='date_deadline, priority desc', limit=300)
            return {'tasks': [self._task_payload(t) for t in tasks]}
        return self._run(run)

    @http.route('/api/manager/tasks/create', type='http', auth='none', methods=['POST'], cors='*', csrf=False)
    def task_create(self, **kw):
        def run(user):
            data = self._body()
            rep_ids = data.get('rep_ids') or ([data['rep_id']] if data.get('rep_id') else [])
            if not rep_ids:
                raise UserError(_('Choose at least one representative.'))
            if not (data.get('name') or '').strip():
                raise UserError(_('Give the task a title.'))
            Task = request.env['sales.rep.task'].with_user(user).sudo()
            created = Task.browse()
            for rid in rep_ids:
                rep = user._srm_check_rep(rid)
                vals = {
                    'name': data['name'].strip(), 'sales_rep_id': rep.id,
                    'task_type': data.get('task_type') or 'other', 'priority': str(data.get('priority') or '0'),
                    'date_deadline': data.get('date_deadline') or fields.Date.context_today(user),
                    'description': data.get('description') or False, 'assigned_by': user.id,
                }
                if data.get('proof_type'):
                    vals['proof_type'] = data['proof_type']
                if data.get('partner_id'):
                    vals['partner_id'] = int(data['partner_id'])
                created |= Task.create(vals)
            return {'tasks': [self._task_payload(t) for t in created]}
        return self._run(run)

    @http.route('/api/manager/tasks/<int:task_id>/<string:op>', type='http', auth='none', methods=['POST'],
                cors='*', csrf=False)
    def task_op(self, task_id, op, **kw):
        def run(user):
            task = request.env['sales.rep.task'].sudo().browse(task_id).exists()
            if not task:
                raise UserError(_('Task not found'))
            user._srm_check_rep(task.sales_rep_id.id)
            data = self._body()
            if op == 'cancel':
                task.action_cancel()
            elif op == 'reopen':
                task.action_reset()
            elif op == 'reassign':
                task.write({'sales_rep_id': user._srm_check_rep(data.get('rep_id')).id})
            elif op == 'update':
                vals = {k: data[k] for k in ('name', 'description', 'date_deadline', 'priority') if data.get(k)}
                task.write(vals)
            else:
                raise UserError(_('Unknown operation'))
            return {'task': self._task_payload(task)}
        return self._run(run)

    @http.route('/api/manager/tasks/<int:task_id>/photo', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def task_photo(self, task_id, **kw):
        user = self._auth_manager()
        if not user:
            return self._unauthorized()
        task = request.env['sales.rep.task'].sudo().browse(task_id).exists()
        if not task or task.sales_rep_id not in user._srm_scope_reps() or not task.proof_image:
            return request.not_found()
        import base64
        return request.make_response(base64.b64decode(task.proof_image), headers=[('Content-Type', 'image/jpeg')])

    # ============================================================ approvals
    @staticmethod
    def _request_payload(r):
        return {'id': r.id, 'name': r.name, 'type_name': r.type_id.name, 'category': r.category, 'state': r.state,
                'rep_id': r.sales_rep_id.id, 'rep_name': r.sales_rep_id.name, 'description': r.description or '',
                'amount': r.amount, 'currency_symbol': r.currency_id.symbol, 'date_from': r.date_from,
                'date_to': r.date_to, 'partner_name': r.partner_id.name or '', 'response': r.response or '',
                'create_date': _dt(r.create_date), 'approver': r.approver_id.name or '',
                'can_decide': r.state in ('submitted', 'info'),
                'has_attachment': bool(r.attachment),
                'attachment_url': f'/api/manager/requests/{r.id}/attachment' if r.attachment else ''}

    @staticmethod
    def _day_close_payload(d):
        return {'id': d.id, 'name': d.name, 'date': d.date, 'state': d.state, 'rep_id': d.sales_rep_id.id,
                'rep_name': d.sales_rep_id.name, 'system_cash': d.system_cash, 'counted_cash': d.counted_cash,
                'cash_difference': d.cash_difference, 'stock_differences': d.stock_difference_count,
                'currency_symbol': d.currency_id.symbol, 'submitted': _dt(d.submitted_date), 'note': d.note or '',
                'lines': [{'product': ln.product_id.display_name, 'system': ln.system_qty, 'counted': ln.counted_qty,
                           'difference': ln.counted_qty - ln.system_qty}
                          for ln in d.line_ids if abs(ln.counted_qty - ln.system_qty) > 0.0001]}

    @http.route('/api/manager/approvals', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def approvals(self, **kw):
        def run(user):
            reps = self._scope(user, kw)
            Req = request.env['sales.rep.dept.request'].sudo()
            pending = Req.search(['&', ('state', 'in', ('submitted', 'info')),
                                  '|', ('approver_id', '=', user.id), ('sales_rep_id', 'in', reps.ids)],
                                 order='create_date desc', limit=200)
            recent = Req.search(['&', ('state', 'in', ('approved', 'rejected', 'done')), ('sales_rep_id', 'in', reps.ids),
                                 ('decided_date', '>=', fields.Datetime.to_string(fields.Datetime.now() - timedelta(days=7)))],
                                order='decided_date desc', limit=50)
            closes = request.env['sales.rep.day.close'].sudo().search(
                [('sales_rep_id', 'in', reps.ids), ('state', '=', 'submitted')], order='date desc')
            loads = request.env['stock.picking'].sudo().search(
                [('sales_rep_id', 'in', reps.ids), ('load_acceptance_state', '=', 'disputed'),
                 ('write_date', '>=', fields.Datetime.to_string(fields.Datetime.now() - timedelta(days=7)))], limit=50)
            alerts = request.env['sales.rep.security.alert'].sudo().search(
                [('sales_rep_id', 'in', reps.ids)], order='detected_at desc', limit=50)
            return {
                'requests': [self._request_payload(r) for r in pending],
                'recent_requests': [self._request_payload(r) for r in recent],
                'day_closes': [self._day_close_payload(d) for d in closes],
                'disputed_loads': [{'id': p.id, 'name': p.name, 'rep_name': p.sales_rep_id.name,
                                    'date': _dt(p.date_done or p.scheduled_date), 'note': p.load_acceptance_note or ''}
                               for p in loads],
                'alerts': [{'id': a.id, 'rep_id': a.sales_rep_id.id, 'rep_name': a.sales_rep_id.name,
                            'type': a.alert_type, 'detected_at': _dt(a.detected_at), 'latitude': a.latitude,
                            'longitude': a.longitude, 'device': a.device_identifier or ''} for a in alerts],
            }
        return self._run(run)

    def _check_request(self, user, req_id):
        req = request.env['sales.rep.dept.request'].sudo().browse(req_id).exists()
        if not req:
            raise UserError(_('Request not found'))
        if req.approver_id != user:
            user._srm_check_rep(req.sales_rep_id.id)
        return req

    @http.route('/api/manager/requests/<int:req_id>/<string:op>', type='http', auth='none', methods=['POST'],
                cors='*', csrf=False)
    def request_op(self, req_id, op, **kw):
        def run(user):
            req = self._check_request(user, req_id)
            data = self._body()
            if data.get('response'):
                req.write({'response': data['response']})
            req = req.with_user(user).sudo()  # decided_by = the manager
            actions = {'approve': req.action_approve, 'reject': req.action_reject,
                       'info': req.action_request_info, 'done': req.action_done}
            if op not in actions:
                raise UserError(_('Unknown operation'))
            actions[op]()
            return {'request': self._request_payload(req)}
        return self._run(run)

    @http.route('/api/manager/requests/<int:req_id>/attachment', type='http', auth='none', methods=['GET'],
                cors='*', csrf=False)
    def request_attachment(self, req_id, **kw):
        user = self._auth_manager()
        if not user:
            return self._unauthorized()
        try:
            req = self._check_request(user, req_id)
        except (UserError, AccessError):
            return request.not_found()
        if not req.attachment:
            return request.not_found()
        import base64
        return request.make_response(base64.b64decode(req.attachment), headers=[('Content-Type', 'image/jpeg')])

    @http.route('/api/manager/day_close/<int:close_id>/<string:op>', type='http', auth='none', methods=['POST'],
                cors='*', csrf=False)
    def day_close_op(self, close_id, op, **kw):
        def run(user):
            close = request.env['sales.rep.day.close'].sudo().browse(close_id).exists()
            if not close:
                raise UserError(_('Day close not found'))
            user._srm_check_rep(close.sales_rep_id.id)
            close = close.with_user(user).sudo()
            if op == 'approve':
                close.action_approve()
            elif op == 'reset':
                close.action_reset_draft()
            else:
                raise UserError(_('Unknown operation'))
            return {'day_close': self._day_close_payload(close)}
        return self._run(run)

    @http.route('/api/manager/alerts/<int:alert_id>/dismiss', type='http', auth='none', methods=['POST'],
                cors='*', csrf=False)
    def alert_dismiss(self, alert_id, **kw):
        def run(user):
            alert = request.env['sales.rep.security.alert'].sudo().browse(alert_id).exists()
            if not alert:
                raise UserError(_('Alert not found'))
            user._srm_check_rep(alert.sales_rep_id.id)
            alert.action_dismiss()
            return {}
        return self._run(run)

    # ======================================================= people / lookup
    @http.route('/api/manager/employees', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def employees(self, **kw):
        def run(user):
            Emp = request.env['hr.employee.public']
            domain = [('company_id', 'in', (user.company_ids or user.company_id).ids)]
            if kw.get('q'):
                q = kw['q'].strip()
                domain += ['|', '|', ('name', 'ilike', q), ('job_title', 'ilike', q), ('work_phone', 'ilike', q)]
            if kw.get('department_id'):
                domain.append(('department_id', 'child_of', int(kw['department_id'])))
            emps = Emp.search(domain, order='name', limit=int(kw.get('limit') or 200))
            rep_by_user = {r.user_id.id: r.id for r in user._srm_scope_reps() if r.user_id}
            departments = request.env['hr.department'].search(
                [('company_id', 'in', (user.company_ids or user.company_id).ids)], order='complete_name')
            return {
                'employees': [{
                    'id': e.id, 'name': e.name, 'job_title': e.job_title or '', 'department': e.department_id.name or '',
                    'department_id': e.department_id.id, 'manager': e.parent_id.name or '',
                    'work_phone': e.work_phone or '', 'mobile_phone': e.mobile_phone or '', 'email': e.work_email or '',
                    'user_id': e.user_id.id, 'partner_id': e.user_id.partner_id.id, 'rep_id': rep_by_user.get(e.user_id.id, False),
                    'image_url': f'/web/image/hr.employee.public/{e.id}/avatar_128',
                } for e in emps],
                'departments': [{'id': d.id, 'name': d.complete_name} for d in departments],
            }
        return self._run(run)

    @http.route('/api/manager/customers', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def customers(self, **kw):
        def run(user):
            q = (kw.get('q') or '').strip()
            domain = [('customer_rank', '>', 0)] + (['|', ('name', 'ilike', q), ('phone', 'ilike', q)] if q else [])
            partners = request.env['res.partner'].sudo().search(domain, limit=40, order='name')
            return {'customers': [{'id': p.id, 'name': p.name, 'phone': p.phone or p.mobile or '',
                                   'area': p.sales_area_id.name or ''} for p in partners]}
        return self._run(run)

    @http.route('/api/manager/kpi', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def kpi(self, **kw):
        """KPI engine for one rep or the scope, any period (leaderboard included)."""
        def run(user):
            today = self._today(user)
            date_from = fields.Date.to_date(kw.get('date_from')) if kw.get('date_from') else today.replace(day=1)
            date_to = fields.Date.to_date(kw.get('date_to')) if kw.get('date_to') else today
            reps = self._scope(user, kw)
            board = []
            for rep in reps:
                k = rep._kpi_compute(date_from, date_to)
                board.append({'id': rep.id, 'name': rep.name, **k})
            board.sort(key=lambda r: r['sales_value'], reverse=True)
            total = reps._kpi_compute(date_from, date_to) if reps else {}
            return {'date_from': date_from, 'date_to': date_to, 'total': total, 'leaderboard': board,
                    'currency_symbol': user.company_id.currency_id.symbol}
        return self._run(run)


class AuthServiceLogins:
    """Shared service login(s) of the master-server flow (Sales Rep settings)."""

    @staticmethod
    def logins():
        raw = request.env['ir.config_parameter'].sudo().get_param('sales_rep_management.mobile_service_logins', '') or ''
        return {x.strip().lower() for x in raw.split(',') if x.strip()}


from .chat_api import ChatApiHelpers  # noqa: E402  (shared unread counter)
