# -*- coding: utf-8 -*-
"""Manager app backend: scope (manager / supervisor), session checks, last
position, and chat messages pushed to the members' mobile streams."""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from odoo.addons.sales_rep_management.models import sse_dispatch


@tagged('post_install', '-at_install', 'sales_rep')
class TestManagerApp(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        base = env.ref('base.group_user') | env.ref('sales_rep_management.group_sales_rep_user')
        mk = lambda login, groups: env['res.users'].create({'name': login, 'login': login, 'groups_id': [(6, 0, groups.ids)]})
        cls.u_mgr = mk('ma_mgr', base | env.ref('sales_rep_management.group_sales_manager'))
        cls.u_sup = mk('ma_sup', base | env.ref('sales_rep_management.group_sales_supervisor'))
        cls.u_r1 = mk('ma_r1', base)
        cls.u_r2 = mk('ma_r2', base)
        Rep = env['sales.representative']
        cls.sup = Rep.create({'name': 'MA Sup', 'user_id': cls.u_sup.id, 'is_supervisor': True})
        cls.r1 = Rep.create({'name': 'MA R1', 'user_id': cls.u_r1.id, 'supervisor_id': cls.sup.id})
        cls.r2 = Rep.create({'name': 'MA R2', 'user_id': cls.u_r2.id})

    def test_scope(self):
        self.assertIn(self.r2, self.u_mgr._srm_scope_reps())
        self.assertEqual(self.u_sup._srm_scope_reps(), self.r1)
        self.assertFalse(self.u_r1._srm_scope_reps())
        self.assertTrue(self.u_sup._srm_is_manager_app_user())
        self.assertFalse(self.u_r1._srm_is_manager_app_user())
        with self.assertRaises(AccessError):
            self.u_sup._srm_check_rep(self.r2.id)

    def test_session_expiry_and_role(self):
        Session = self.env['sales.rep.manager.session']
        Session._bind(self.u_sup, 'sha256$x', 'dev-1', 'Pixel')
        s = Session.search([('user_id', '=', self.u_sup.id)])
        self.assertEqual(len(s), 1)
        self.assertIsNone(s._check('dev-1'))
        s.expiry = fields.Datetime.now() - timedelta(minutes=1)
        self.assertEqual(s._check('dev-1'), 'token_expired')
        # a new login closes the previous phone
        Session._bind(self.u_sup, 'sha256$y', 'dev-2', 'iPhone')
        self.assertEqual(Session.search_count([('user_id', '=', self.u_sup.id)]), 1)
        # losing the role ends the access
        s2 = Session.search([('user_id', '=', self.u_sup.id)])
        self.u_sup.groups_id = [(3, self.env.ref('sales_rep_management.group_sales_supervisor').id)]
        self.assertEqual(s2._check('dev-2'), 'not_manager')

    def test_position_keeps_newest(self):
        now = fields.Datetime.now()
        self.r1._set_position(30.0, 31.0, now, 'sync')
        self.r1._set_position(29.0, 30.0, now - timedelta(hours=2), 'sync')  # late offline point
        self.assertEqual((self.r1.last_latitude, self.r1.last_longitude), (30.0, 31.0))
        self.r1._set_position(0, 0)
        self.assertEqual(self.r1.last_latitude, 30.0)

    def test_chat_message_pushed_to_members(self):
        channel = self.env['discuss.channel'].with_user(self.u_sup).channel_get([self.u_r1.partner_id.id])
        captured = []
        original = sse_dispatch.schedule_users

        def spy(env, user_ids, reason, event_type='sync_required'):
            captured.append((set(user_ids), reason, event_type))
            return original(env, user_ids, reason, event_type)

        sse_dispatch.schedule_users = spy
        from odoo.addons.sales_rep_management.models import chat_push
        chat_push.schedule_users = spy
        try:
            channel.with_user(self.u_sup).message_post(body='Start with customer A', message_type='comment',
                                                      subtype_xmlid='mail.mt_comment')
        finally:
            sse_dispatch.schedule_users = original
            chat_push.schedule_users = original
        self.assertTrue(captured)
        users, reason, event_type = captured[-1]
        self.assertEqual(event_type, 'chat_message')
        self.assertEqual(reason, f'channel:{channel.id}')
        self.assertIn(self.u_r1.id, users)
        self.assertNotIn(self.u_sup.id, users)  # never to the author
        bucket = self.env.cr.postcommit.data.get(sse_dispatch._DATA_KEY)
        self.assertIn(self.r1.id, bucket['reps'])         # representative stream
        self.assertIn(f'u{self.u_r1.id}', bucket['reps'])  # manager-app stream key
