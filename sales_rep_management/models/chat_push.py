# -*- coding: utf-8 -*-
"""Push Discuss messages to the mobile apps (manager and representative).

After a message is posted in a conversation, every other member that uses a
mobile app receives ``{type: 'chat_message', reason: 'channel:<id>'}`` on his
SSE stream (sent after commit, see sse_dispatch.schedule_users).
"""
from odoo import fields, models

from .sse_dispatch import schedule_users


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    # team conversation created from the manager app (one per owner and team)
    srm_team_owner_id = fields.Many2one('res.users', index=True, copy=False, readonly=True)
    srm_team_key = fields.Char(copy=False, readonly=True)

    def _message_post_after_hook(self, message, msg_vals):
        res = super()._message_post_after_hook(message, msg_vals)
        if message.message_type in ('comment', 'email'):
            for channel in self:
                users = channel.sudo().channel_member_ids.mapped('partner_id.user_ids').filtered(
                    lambda u: u.partner_id != message.author_id)
                schedule_users(self.env, users.ids, f'channel:{channel.id}', event_type='chat_message')
        return res
