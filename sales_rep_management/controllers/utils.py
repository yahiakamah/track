from odoo.http import request
import hashlib
from datetime import datetime
import pytz
import hmac
import logging

_logger = logging.getLogger(__name__)

TOKEN_HASH_PREFIX = 'sha256$'


def hash_token(token):
    """Return the stored form of a mobile access token (never kept in clear)."""
    return TOKEN_HASH_PREFIX + hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def token_lookup_values(token):
    """Values to search for: the hash, plus the clear value for legacy rows."""
    return [hash_token(token), token]


def parse_mobile_datetime(value, user=None):
    """Datetime sent by the phone -> naive UTC datetime for Odoo.

    New app versions send ISO 8601 with 'Z' or an offset (unambiguous).
    Older versions sent the phone's LOCAL time without a zone, which Odoo
    stored as if it were UTC (visits looked 2-3 hours off in Cairo). A
    zone-less value is therefore read in the rep's time zone.
    """
    if not value:
        return False
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace(' ', 'T', 1)
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return False
    if dt.tzinfo is None:
        tz_name = (user and (user.tz or user.company_id.partner_id.tz)) or 'UTC'
        try:
            dt = pytz.timezone(tz_name).localize(dt)
        except pytz.UnknownTimeZoneError:
            dt = pytz.utc.localize(dt)
    return dt.astimezone(pytz.utc).replace(tzinfo=None, microsecond=0)


class SalesRepUtils:

    def _get_sales_rep(self, user_id=None):
        """Resolve the representative of the CURRENT caller only.

        Never trusts a `sales_rep_id` or `user_email` sent by the client (that
        allowed any logged-in user to read another rep's data). Order:
        1. Bearer token of the mobile app.
        2. The logged-in Odoo user.
        """
        sales_rep, _user = self._authenticate_request()
        if sales_rep:
            return sales_rep
        uid = user_id or request.uid
        if uid:
            return request.env['sales.representative'].sudo().search([('user_id', '=', uid)], limit=1)
        return request.env['sales.representative'].sudo()

    def _authenticate_token(self, token):
        """Find the active representative owning this mobile access token.

        Tokens are stored as SHA-256 hashes. A legacy clear-text token is still
        accepted once and immediately replaced by its hash, so existing devices
        keep working after the upgrade without a forced re-login.
        """
        if not token:
            return None
        Rep = request.env['sales.representative'].sudo()
        sales_rep = Rep.search([
            ('mobile_access_token', 'in', token_lookup_values(token)),
            ('active', '=', True),
        ], limit=1)
        if not sales_rep:
            _logger.warning("Auth: unknown or inactive mobile token")
            return None
        stored = sales_rep.mobile_access_token or ''
        if not hmac.compare_digest(stored, hash_token(token)):
            if not hmac.compare_digest(stored, token):
                return None
            # legacy clear-text token -> upgrade in place
            sales_rep.with_context(tracking_disable=True).write({'mobile_access_token': hash_token(token)})
        refusal = sales_rep._mobile_check(self._request_device_id())
        if refusal:
            _logger.warning("Auth: mobile request refused for rep %s (%s)", sales_rep.id, refusal)
            try:
                request.srm_auth_error = refusal
            except Exception:
                pass
            return None
        return sales_rep

    @staticmethod
    def _request_device_id():
        try:
            return (request.httprequest.headers.get('X-Device-Id')
                    or request.httprequest.args.get('device_id') or '').strip() or None
        except Exception:
            return None

    @staticmethod
    def auth_error_message():
        """Reason of the last refused token (for a clear message on the phone)."""
        code = getattr(request, 'srm_auth_error', None)
        return {
            'token_expired': 'Your mobile session has expired. Please log in again.',
            'other_device': 'This account is now signed in on another phone. Please log in again on this one.',
        }.get(code, 'Unauthorized'), code

    def _authenticate_request(self):
        """
        Authenticate the current request via Bearer token.
        Returns (sales_rep, user) or (None, None).
        """
        auth_header = request.httprequest.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return None, None

        token = auth_header.split(' ', 1)[1].strip()
        sales_rep = self._authenticate_token(token)

        if not sales_rep or not sales_rep.user_id:
            return None, None

        # Restrict the request environment to the rep's user and company so
        # record rules and multi-company checks apply.
        company = sales_rep.company_id
        if company:
            request.env = request.env(
                user=sales_rep.user_id,
                context=dict(request.env.context, allowed_company_ids=[company.id])
            )
        return sales_rep, sales_rep.user_id
