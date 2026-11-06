# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
import secrets
from .utils import SalesRepUtils, hash_token

_logger = logging.getLogger(__name__)


class AuthController(http.Controller, SalesRepUtils):

    def _json(self, data, status=200):
        return request.make_response(json.dumps(data), headers={'Content-Type': 'application/json'}, status=status)

    def _service_logins(self):
        raw = request.env['ir.config_parameter'].sudo().get_param('sales_rep_management.mobile_service_logins', '') or ''
        return {login.strip().lower() for login in raw.split(',') if login.strip()}

    def _resolve_login_rep(self, user, email):
        """Return (sales_rep, via_service_user) for the authenticated user.

        * A user linked to a representative is ALWAYS bound to that
          representative; the e-mail sent by the client is ignored.
        * Only the configured shared service user(s) of the master-server
          flow may identify a representative by e-mail.
        * Legacy mode (no service login configured): e-mail binding is still
          accepted for users not linked to any representative, with a warning,
          so existing deployments keep working until the setting is filled.
        """
        Rep = request.env['sales.representative'].sudo()
        own_rep = Rep.search([('user_id', '=', user.id), ('active', '=', True)], limit=1)
        if own_rep:
            return own_rep, False
        if not email:
            return Rep, False
        service_logins = self._service_logins()
        login = (user.login or '').lower()
        if service_logins and login not in service_logins:
            _logger.warning("Auth: user %s tried to bind to rep e-mail %s but is not a mobile service user", user.login, email)
            return Rep, False
        if not service_logins:
            _logger.warning("Auth: e-mail binding used without 'Mobile Service Logins' configured (legacy mode). "
                            "Set it in Sales Rep Management settings.")
        rep = Rep.search([('email', '=ilike', email.strip()), ('active', '=', True)], limit=1)
        return rep, True

    @http.route('/api/mobile/login', type='http', auth='none', methods=['POST'], cors='*', csrf=False)
    def login(self, **post):
        try:
            data = json.loads(request.httprequest.data or '{}')
            db = data.get('db') or request.db
            login = data.get('login')
            password = data.get('password')
            email = data.get('email')
            _logger.info("Auth: mobile login attempt for '%s' in database '%s'", login, db)

            if db:
                request.session.db = db
            auth_info = request.session.authenticate(db, {'type': 'password', 'login': login, 'password': password})
            uid = auth_info and auth_info.get('uid')
            if not uid:
                # 200 + success=False: the app shows `message` (an HTTP error would hide it)
                return self._json({'success': False, 'message': 'Invalid credentials'})

            user = request.env['res.users'].sudo().browse(uid)
            sales_rep, via_service = self._resolve_login_rep(user, email)
            if not sales_rep:
                return self._json({'success': False, 'message': 'No active sales representative is linked to this account'})

            # The master server issues the token in the service-user flow; in
            # every other case the tenant generates it. The client can never
            # choose the token of a representative it is not bound to.
            client_token = data.get('access_token') if via_service else None
            access_token = client_token or secrets.token_urlsafe(32)

            # Force the previous device out before rotating the token.
            from .sse import notify_sales_rep
            notify_sales_rep(sales_rep.id, reason='new_device_login', event_type='force_logout')

            last_log = request.env['sales.representative.session.log'].sudo().search([
                ('sales_rep_id', '=', sales_rep.id),
                ('session_identifier', '!=', False)
            ], order='timestamp desc', limit=1)
            if last_log and last_log.action_type == 'login':
                request.env['sales.representative.session.log'].sudo().create({
                    'sales_rep_id': sales_rep.id,
                    'action_type': 'forced_logout',
                    'timestamp': fields.Datetime.now(),
                    'device_model': 'System (New Login)',
                    'mac_address': 'System',
                    'session_identifier': last_log.session_identifier,
                })

            master_expiry = None
            if via_service and data.get('token_expiration'):
                from .utils import parse_mobile_datetime
                master_expiry = parse_mobile_datetime(data.get('token_expiration'), user) or None
            expiry = sales_rep._mobile_bind(hash_token(access_token), (data.get('device_id') or '').strip() or None,
                                            data.get('device_model'), master_expiry)

            return self._json({
                'success': True,
                'uid': uid,
                'session_id': request.session.sid,
                'access_token': access_token,
                'token_expiration': fields.Datetime.to_string(expiry) if expiry else False,
            })
        except Exception as e:
            _logger.exception("Error in mobile login")
            return self._json({'success': False, 'message': 'Login failed', 'error': str(e)})

    @http.route('/api/mobile/logout', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def logout(self, **kwargs):
        try:
            request.session.logout()
            return self._json({'success': True})
        except Exception as e:
            _logger.error("Error in logout: %s", e)
            return self._json({'success': False, 'error': str(e)})

    @http.route('/api/mobile/check_connection', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def check_connection(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                message, code = self.auth_error_message()
                return self._json({'success': False, 'message': message, 'code': code}, status=401)
            return self._json({'success': True, 'message': 'Connection successful'})
        except Exception as e:
            return self._json({'success': False, 'error': str(e)})
