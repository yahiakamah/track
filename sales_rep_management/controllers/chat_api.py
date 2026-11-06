# -*- coding: utf-8 -*-
"""Operational chat for the mobile apps, built on Odoo Discuss.

The same conversations are visible in the Odoo web client (Discuss), the
manager app and the representative apps. Works with a manager token or a
representative token. Who may start a conversation with whom:

* a manager / supervisor: the representatives of his scope and any internal user;
* a representative: his supervisors (up the chain), the sales managers and
  his team mates.

New messages are pushed to the members' mobile streams (SSE) as
``{type: 'chat_message', reason: 'channel:<id>'}`` (models/chat_push.py).
"""
import base64
import json
import logging
import re

from odoo import _, fields, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.tools import html2plaintext, plaintext2html

from .utils import SalesRepUtils, hash_token

_logger = logging.getLogger(__name__)
MAX_ATTACHMENT = 15 * 1024 * 1024


class ChatApiHelpers:

    @staticmethod
    def members_of(user):
        return request.env['discuss.channel.member'].sudo().search([('partner_id', '=', user.partner_id.id)])

    @staticmethod
    def unread_total(user):
        members = ChatApiHelpers.members_of(user).filtered(lambda m: m.channel_id.channel_type in ('chat', 'group', 'channel'))
        return sum(members.mapped('message_unread_counter'))


class ChatApi(http.Controller, SalesRepUtils):

    def _json(self, data, status=200):
        return request.make_response(json.dumps(data, default=str), status=status,
                                     headers={'Content-Type': 'application/json'})

    def _body(self):
        try:
            return json.loads(request.httprequest.data or '{}')
        except ValueError:
            return {}

    # ------------------------------------------------------------------ auth
    def _auth_user(self):
        """(user, rep) from a manager token or a representative token."""
        header = request.httprequest.headers.get('Authorization', '')
        token = header.split(' ', 1)[1].strip() if header.startswith('Bearer ') else None
        if not token:
            return None, None
        session = request.env['sales.rep.manager.session'].sudo().search(
            [('token_hash', '=', hash_token(token)), ('active', '=', True)], limit=1)
        if session and not session._check(self._request_device_id()):
            user = session.user_id
            rep = request.env['sales.representative'].sudo().search([('user_id', '=', user.id)], limit=1)
        else:
            rep, user = self._authenticate_request()
            if not rep or not user:
                return None, None
        request.env = request.env(user=user, context=dict(
            request.env.context, allowed_company_ids=(user.company_ids or user.company_id).ids, lang=user.lang))
        return user, rep

    def _run(self, fn, *args):
        user, rep = self._auth_user()
        if not user:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        try:
            return self._json(dict({'success': True}, **(fn(user, rep, *args) or {})))
        except (UserError, AccessError) as e:
            request.env.cr.rollback()
            return self._json({'success': False, 'message': str(e.args[0] if e.args else e)})
        except Exception as e:
            request.env.cr.rollback()
            _logger.exception("Chat API error")
            return self._json({'success': False, 'message': _('Server error'), 'error': str(e)}, 500)

    # --------------------------------------------------------------- helpers
    def _member(self, user, channel_id):
        member = request.env['discuss.channel.member'].sudo().search(
            [('channel_id', '=', int(channel_id)), ('partner_id', '=', user.partner_id.id)], limit=1)
        if not member:
            raise AccessError(_('You are not a member of this conversation.'))
        return member

    def _allowed_partners(self, user, rep):
        """Partners this user may open a conversation with."""
        Users = request.env['res.users'].sudo()
        if user._srm_is_manager_app_user():
            reps_users = user._srm_scope_reps().mapped('user_id')
            internal = Users.search([('share', '=', False), ('id', '!=', user.id)])
            return (reps_users | internal).mapped('partner_id')
        partners = request.env['res.partner']
        if rep:
            sup = rep.supervisor_id
            while sup:
                partners |= sup.user_id.partner_id
                sup = sup.supervisor_id
            mates = request.env['sales.representative'].sudo().search(
                [('supervisor_id', '=', rep.supervisor_id.id), ('id', '!=', rep.id)]) if rep.supervisor_id else []
            for m in mates:
                partners |= m.user_id.partner_id
        managers = request.env.ref('sales_rep_management.group_sales_manager').sudo().users
        partners |= managers.filtered(lambda u: u != user).mapped('partner_id')
        return partners

    @staticmethod
    def _attachment_payload(a):
        return {'id': a.id, 'name': a.name, 'mimetype': a.mimetype or '', 'size': a.file_size,
                'url': f'/api/chat/attachment/{a.id}', 'is_image': (a.mimetype or '').startswith('image/')}

    def _message_payload(self, m, user):
        return {
            'id': m.id, 'body': html2plaintext(m.body or '').strip(), 'date': fields.Datetime.to_string(m.date),
            'author_id': m.author_id.id, 'author_name': m.author_id.name or m.email_from or '',
            'is_me': m.author_id == user.partner_id, 'message_type': m.message_type,
            'attachments': [self._attachment_payload(a) for a in m.attachment_ids],
        }

    def _channel_payload(self, member, user):
        ch = member.channel_id
        others = ch.channel_member_ids.mapped('partner_id') - user.partner_id
        if ch.channel_type == 'chat':
            name = others[:1].name or ch.name
        else:
            name = ch.name or ', '.join(others.mapped('name')[:4])
        last = request.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', ch.id), ('message_type', 'in', ('comment', 'email'))],
            order='id desc', limit=1)
        rep_of = {r.user_id.partner_id.id: r.id for r in request.env['sales.representative'].sudo().search(
            [('user_id.partner_id', 'in', others.ids)])}
        return {
            'id': ch.id, 'name': name, 'type': ch.channel_type, 'unread': member.message_unread_counter,
            'members': [{'partner_id': p.id, 'name': p.name, 'rep_id': rep_of.get(p.id, False)} for p in others],
            'last_message': self._message_payload(last, user) if last else None,
            'last_date': fields.Datetime.to_string(last.date) if last else fields.Datetime.to_string(ch.create_date),
            'image_url': (f'/web/image/res.partner/{others[:1].id}/avatar_128' if ch.channel_type == 'chat' and others
                          else f'/web/image/discuss.channel/{ch.id}/avatar_128'),
        }

    # ----------------------------------------------------------------- routes
    @http.route('/api/chat/channels', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def channels(self, **kw):
        def run(user, rep):
            members = ChatApiHelpers.members_of(user).filtered(
                lambda m: m.channel_id.channel_type in ('chat', 'group', 'channel'))
            data = [self._channel_payload(m, user) for m in members]
            data.sort(key=lambda c: c['last_date'] or '', reverse=True)
            return {'channels': data, 'unread_total': sum(c['unread'] for c in data)}
        return self._run(run)

    @http.route('/api/chat/unread', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def unread(self, **kw):
        return self._run(lambda user, rep: {'unread_total': ChatApiHelpers.unread_total(user)})

    @http.route('/api/chat/channels/<int:channel_id>/messages', type='http', auth='none', methods=['GET'],
                cors='*', csrf=False)
    def messages(self, channel_id, **kw):
        def run(user, rep):
            member = self._member(user, channel_id)
            domain = [('model', '=', 'discuss.channel'), ('res_id', '=', channel_id),
                      ('message_type', 'in', ('comment', 'email', 'notification'))]
            if kw.get('before_id'):
                domain.append(('id', '<', int(kw['before_id'])))
            if kw.get('after_id'):
                domain.append(('id', '>', int(kw['after_id'])))
            msgs = request.env['mail.message'].sudo().search(domain, order='id desc', limit=int(kw.get('limit') or 40))
            return {'channel': self._channel_payload(member, user),
                    'messages': [self._message_payload(m, user) for m in reversed(msgs)]}
        return self._run(run)

    @http.route('/api/chat/channels/<int:channel_id>/post', type='http', auth='none', methods=['POST'],
                cors='*', csrf=False)
    def post(self, channel_id, **kw):
        def run(user, rep):
            self._member(user, channel_id)
            data = self._body()
            body = (data.get('body') or '').strip()
            files = data.get('attachments') or []
            if not body and not files:
                raise UserError(_('Write a message first.'))
            channel = request.env['discuss.channel'].with_user(user).browse(channel_id)
            attachment_ids = []
            for f in files[:10]:
                raw = f.get('data') or ''
                if raw.startswith('data:'):
                    raw = raw.split(',', 1)[1]
                if len(raw) * 3 / 4 > MAX_ATTACHMENT:
                    raise UserError(_('Attachment too large (max 15 MB).'))
                att = request.env['ir.attachment'].with_user(user).sudo().create({
                    'name': re.sub(r'[\\/]', '_', f.get('name') or 'file'), 'datas': raw,
                    'mimetype': f.get('mimetype') or False, 'res_model': 'discuss.channel', 'res_id': channel_id})
                attachment_ids.append(att.id)
            msg = channel.message_post(body=plaintext2html(body) if body else '', message_type='comment',
                                       subtype_xmlid='mail.mt_comment', attachment_ids=attachment_ids)
            member = self._member(user, channel_id)
            member.sudo()._mark_as_read(msg.id)
            return {'message': self._message_payload(msg.sudo(), user)}
        return self._run(run)

    @http.route('/api/chat/channels/<int:channel_id>/seen', type='http', auth='none', methods=['POST'],
                cors='*', csrf=False)
    def seen(self, channel_id, **kw):
        def run(user, rep):
            member = self._member(user, channel_id)
            last_id = self._body().get('message_id')
            if not last_id:
                last = request.env['mail.message'].sudo().search(
                    [('model', '=', 'discuss.channel'), ('res_id', '=', channel_id)], order='id desc', limit=1)
                last_id = last.id
            if last_id:
                member.with_user(user).sudo()._mark_as_read(int(last_id))
            return {'unread_total': ChatApiHelpers.unread_total(user)}
        return self._run(run)

    @http.route('/api/chat/contacts', type='http', auth='none', methods=['GET'], cors='*', csrf=False)
    def contacts(self, **kw):
        def run(user, rep):
            partners = self._allowed_partners(user, rep)
            q = (kw.get('q') or '').strip().lower()
            if q:
                partners = partners.filtered(lambda p: q in (p.name or '').lower())
            rep_of = {r.user_id.partner_id.id: r for r in request.env['sales.representative'].sudo().search(
                [('user_id.partner_id', 'in', partners.ids)])}
            out = []
            for p in partners.sorted('name')[:300]:
                r = rep_of.get(p.id)
                out.append({'partner_id': p.id, 'name': p.name, 'rep_id': r.id if r else False,
                            'role': ('supervisor' if r and r.is_supervisor else 'rep') if r else 'staff',
                            'image_url': f'/web/image/res.partner/{p.id}/avatar_128'})
            return {'contacts': out}
        return self._run(run)

    @http.route('/api/chat/direct', type='http', auth='none', methods=['POST'], cors='*', csrf=False)
    def direct(self, **kw):
        """Open (or create) the private conversation with one person."""
        def run(user, rep):
            data = self._body()
            partner_id = data.get('partner_id')
            if not partner_id and data.get('rep_id'):
                other = request.env['sales.representative'].sudo().browse(int(data['rep_id'])).exists()
                partner_id = other.user_id.partner_id.id if other.user_id else False
            if not partner_id:
                raise UserError(_('This person has no Odoo user to chat with.'))
            if int(partner_id) not in self._allowed_partners(user, rep).ids:
                raise AccessError(_('You cannot start a conversation with this person.'))
            channel = request.env['discuss.channel'].with_user(user).channel_get([int(partner_id)])
            return {'channel': self._channel_payload(self._member(user, channel.id), user)}
        return self._run(run)

    @http.route('/api/chat/team', type='http', auth='none', methods=['POST'], cors='*', csrf=False)
    def team_channel(self, **kw):
        """Group conversation of a manager / supervisor with his whole team
        (created once, members kept in step with the team)."""
        def run(user, rep):
            if not user._srm_is_manager_app_user():
                raise AccessError(_('Only managers and supervisors have a team conversation.'))
            data = self._body()
            reps = user._srm_scope_reps()
            if data.get('supervisor_id'):
                sup = reps.browse(int(data['supervisor_id'])).exists()
                reps = (sup._team_reps() & reps) | sup if sup else reps
            partners = reps.mapped('user_id.partner_id') | user.partner_id
            name = data.get('name') or _('Team — %s', user.name)
            Channel = request.env['discuss.channel'].sudo()
            channel = Channel.search([('srm_team_owner_id', '=', user.id), ('srm_team_key', '=', str(data.get('supervisor_id') or 'all'))], limit=1)
            if not channel:
                channel = request.env['discuss.channel'].with_user(user).create_group(partners_to=partners.ids, name=name)
                channel.sudo().write({'srm_team_owner_id': user.id, 'srm_team_key': str(data.get('supervisor_id') or 'all')})
            else:
                missing = partners - channel.channel_member_ids.mapped('partner_id')
                if missing:
                    channel.with_user(user).sudo().add_members(partner_ids=missing.ids, post_joined_message=False)
            return {'channel': self._channel_payload(self._member(user, channel.id), user)}
        return self._run(run)

    @http.route('/api/chat/attachment/<int:attachment_id>', type='http', auth='none', methods=['GET'],
                cors='*', csrf=False)
    def attachment(self, attachment_id, **kw):
        user, rep = self._auth_user()
        if not user:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        att = request.env['ir.attachment'].sudo().browse(attachment_id).exists()
        if not att or att.res_model != 'discuss.channel':
            return request.not_found()
        try:
            self._member(user, att.res_id)
        except AccessError:
            return request.not_found()
        return request.make_response(base64.b64decode(att.datas or b''), headers=[
            ('Content-Type', att.mimetype or 'application/octet-stream'),
            ('Content-Disposition', f'inline; filename="{att.name}"')])
