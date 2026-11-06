# -*- coding: utf-8 -*-
"""Core regression tests: mobile auth tokens, debt snapshot, SSE dispatch,
targets, tasks, home tiles and department requests.

Run: odoo-bin -d <db> -u sales_rep_management --test-tags /sales_rep_management --stop-after-init
"""
import queue
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.sales_rep_management.controllers import sse
from odoo.addons.sales_rep_management.controllers.utils import hash_token, token_lookup_values
from odoo.addons.sales_rep_management.models import sse_dispatch


@tagged('post_install', '-at_install', 'sales_rep')
class TestSalesRepCore(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups = (cls.env.ref('base.group_user') | cls.env.ref('sales_team.group_sale_salesman_all_leads')
                  | cls.env.ref('stock.group_stock_user') | cls.env.ref('sales_rep_management.group_sales_rep_user'))
        cls.sup_user = cls.env['res.users'].create({'name': 'T Supervisor', 'login': 't_sup', 'groups_id': [(6, 0, groups.ids)]})
        cls.user = cls.env['res.users'].create({'name': 'T Rep', 'login': 't_rep', 'groups_id': [(6, 0, groups.ids)]})
        cls.sup = cls.env['sales.representative'].create({'name': 'T Supervisor', 'user_id': cls.sup_user.id, 'is_supervisor': True})
        cls.rep = cls.env['sales.representative'].create({'name': 'T Rep', 'user_id': cls.user.id, 'supervisor_id': cls.sup.id})
        cls.customer = cls.env['res.partner'].create({'name': 'T Customer', 'is_cash': False})
        cls.product = cls.env['product.product'].create({'name': 'T Product', 'list_price': 100.0, 'type': 'consu'})

    # ------------------------------------------------------------------ auth
    def test_token_hashing(self):
        hashed = hash_token('secret-token')
        self.assertTrue(hashed.startswith('sha256$'))
        self.assertNotIn('secret-token', hashed)
        self.assertEqual(hash_token('secret-token'), hashed, 'hash must be deterministic for lookups')
        values = token_lookup_values('secret-token')
        self.assertIn(hashed, values)
        self.assertIn('secret-token', values, 'legacy clear tokens stay readable for the upgrade path')

    # ------------------------------------------------------------------ debt
    def _post_invoice(self, amount):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.customer.id,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1, 'price_unit': amount, 'tax_ids': [(5, 0, 0)]})],
        })
        move.action_post()
        return move

    def test_due_map_matches_single_partner_rule(self):
        other = self.env['res.partner'].create({'name': 'T No Debt'})
        self._post_invoice(250.0)
        self._post_invoice(50.0)
        due = self.env['res.partner']._sales_rep_due_map([self.customer.id, other.id], self.env.company.id)
        self.assertAlmostEqual(due[self.customer.id], 300.0)
        self.assertAlmostEqual(due[self.customer.id], self.customer._get_total_due())
        self.assertEqual(due[other.id], 0.0, 'partners without moves are still reported (as zero)')
        self.assertEqual(self.env['res.partner']._sales_rep_due_map([]), {})

    def test_debt_snapshot_includes_customers_sold_to(self):
        from odoo.addons.sales_rep_management.controllers.sync import SyncController
        self._post_invoice(120.0)
        self.env['sale.order'].create({'partner_id': self.customer.id, 'user_id': self.user.id,
                                       'order_line': [(0, 0, {'product_id': self.product.id, 'product_uom_qty': 1})]}).action_confirm()
        with patch('odoo.addons.sales_rep_management.controllers.sync_download.request', new=SimpleNamespace(env=self.env)):
            debts = SyncController()._rep_partner_debts(self.rep, [])
        row = next((d for d in debts if d['id'] == self.customer.id), None)
        self.assertTrue(row, 'a customer the rep sold to is in the snapshot even off-route')
        self.assertAlmostEqual(row['total_due'], 120.0)

    # ------------------------------------------------------------------ SSE
    def test_dispatch_coalesces_one_event_per_rep(self):
        bucket = {'all': {}, 'reps': {}, 'skip': set()}
        with patch.object(sse_dispatch, '_bucket', return_value=bucket):
            sse_dispatch.schedule_reps(self.env, [self.rep.id, self.rep.id], 'order_updated')
            sse_dispatch.schedule_reps(self.env, [self.rep.id], 'partner_updated')
            sse_dispatch.schedule_reps(self.env, [self.rep.id], 'archived', event_type='force_logout')
        with patch.object(sse, 'publish') as publish:
            sse_dispatch._flush('dbx', bucket)
        db, events = publish.call_args.args[:2]
        self.assertEqual(db, 'dbx')
        syncs = [e for e in events if e['type'] == 'sync_required']
        self.assertEqual(len(syncs), 1)
        self.assertEqual(syncs[0]['reason'], 'order_updated,partner_updated')
        self.assertTrue(any(e['type'] == 'force_logout' for e in events))

    def test_dispatch_broadcast_supersedes_targeted_syncs(self):
        bucket = {'all': {}, 'reps': {}, 'skip': set()}
        with patch.object(sse_dispatch, '_bucket', return_value=bucket):
            sse_dispatch.schedule_reps(self.env, [self.rep.id], 'order_updated')
            sse_dispatch.schedule_all(self.env, 'product_updated')
        with patch.object(sse, 'publish') as publish:
            sse_dispatch._flush('dbx', bucket)
        events = publish.call_args.args[1]
        self.assertEqual(events, [{'rep': None, 'type': 'sync_required', 'reason': 'product_updated'}])

    def test_deliver_routes_by_database_and_skips_origin(self):
        q_rep, q_sup, q_other_db = queue.Queue(), queue.Queue(), queue.Queue()
        clients = {('dbx', self.rep.id): [q_rep], ('dbx', self.sup.id): [q_sup], ('dby', self.rep.id): [q_other_db]}
        with patch.dict(sse._sse_clients, clients, clear=True):
            sse._deliver({'db': 'dbx', 'skip': [self.sup.id],
                          'events': [{'rep': None, 'type': 'sync_required', 'reason': 'x'}]})
            self.assertEqual(q_rep.qsize(), 1)
            self.assertEqual(q_sup.qsize(), 0, 'the rep that caused the change is not told to re-sync')
            self.assertEqual(q_other_db.qsize(), 0, 'events never cross databases')
            sse._deliver({'db': 'dbx', 'skip': [self.sup.id],
                          'events': [{'rep': self.sup.id, 'type': 'force_logout', 'reason': 'archived'}]})
            self.assertEqual(q_sup.get_nowait()['type'], 'force_logout', 'a forced logout is never skipped')

    def test_publish_falls_back_in_process(self):
        q_rep = queue.Queue()
        with patch.dict(sse._sse_clients, {('dbx', self.rep.id): [q_rep]}, clear=True), \
                patch('odoo.sql_db.db_connect', side_effect=Exception('pg down')):
            sse.publish('dbx', [{'rep': self.rep.id, 'type': 'sync_required', 'reason': 'x'}])
        self.assertEqual(q_rep.qsize(), 1)

    # ------------------------------------------------------------------ targets
    def test_target_weighted_score_is_capped(self):
        today = fields.Date.context_today(self.rep)
        target = self.env['sales.rep.target'].create({
            'name': 'T', 'sales_rep_id': self.rep.id, 'date_from': today.replace(day=1), 'date_to': today,
            'line_ids': [(0, 0, {'kpi_type': 'sales_value', 'target_value': 1000, 'weight': 3}),
                         (0, 0, {'kpi_type': 'returns_pct', 'target_value': 5, 'weight': 1})]})
        for line in target.line_ids:
            self.assertGreaterEqual(line.achievement, 0.0)
        capped = sum(min(l.achievement, 120.0) * l.weight for l in target.line_ids) / 4
        self.assertAlmostEqual(target.score, capped, places=1)
        with self.assertRaises(ValidationError):
            self.env['sales.rep.target.line'].create({'target_id': target.id, 'kpi_type': 'category_sales', 'target_value': 5})

    def test_team_contains_supervisor_and_reports(self):
        self.assertEqual(set(self.sup._team_reps().ids), {self.sup.id, self.rep.id})

    # ------------------------------------------------------------------ tasks
    def test_task_lifecycle(self):
        task = self.env['sales.rep.task'].create({
            'name': 'Collect', 'sales_rep_id': self.rep.id, 'partner_id': self.customer.id,
            'task_type': 'collect', 'proof_type': 'note', 'date_deadline': fields.Date.today() - timedelta(days=1)})
        self.assertTrue(task.is_overdue)
        task.action_start()
        self.assertEqual(task.state, 'in_progress')
        with self.assertRaises(ValidationError):
            task.action_done({})  # a note is required as proof
        task.action_done({'result_note': 'paid'})
        self.assertEqual(task.state, 'done')

    # ------------------------------------------------------------------ home / requests
    def test_home_tiles_follow_access_rights(self):
        self.rep.access_storage = False
        self.rep.action_reset_home_tiles()
        self.assertNotIn('stock', self.rep._get_home_tiles())
        self.rep.access_storage = True
        self.assertIn('stock', self.rep._get_home_tiles())

    def test_dept_request_flow(self):
        adv = self.env.ref('sales_rep_management.dept_request_type_advance')
        with self.assertRaises(ValidationError):
            self.env['sales.rep.dept.request'].create({'type_id': adv.id, 'sales_rep_id': self.rep.id, 'description': 'x'})
        req = self.env['sales.rep.dept.request'].create(
            {'type_id': adv.id, 'sales_rep_id': self.rep.id, 'description': 'fuel', 'amount': 300})
        self.assertEqual(req.approver_id, self.sup_user)
        with self.assertRaises(UserError):
            req.action_reject()
        req.response = 'receipt?'
        req.action_request_info()
        req.rep_reply('attached')
        self.assertEqual(req.state, 'submitted')
        req.action_approve()
        self.assertEqual(req.state, 'approved')

    # ------------------------------------------------------------------ offline queue
    def _sync_ctrl(self):
        from odoo.addons.sales_rep_management.controllers.sync import SyncController
        return SyncController()

    def _run_action(self, action_type, payload):
        ns = SimpleNamespace(env=self.env)
        with patch('odoo.addons.sales_rep_management.controllers.sync_actions.request', new=ns):
            return self._sync_ctrl()._execute_action(action_type, payload, self.user, self.rep)

    def test_queued_task_update_is_replay_safe(self):
        task = self.env['sales.rep.task'].create({
            'name': 'Shelf', 'sales_rep_id': self.rep.id, 'task_type': 'merchandising',
            'proof_type': 'note', 'date_deadline': fields.Date.today()})
        first = self._run_action('task_update', {'task_id': task.id, 'action': 'done', 'result_note': 'ok'})
        self.assertEqual(first['state'], 'done')
        again = self._run_action('task_update', {'task_id': task.id, 'action': 'done', 'result_note': 'ok'})
        self.assertTrue(again.get('replayed'), 'a resent action does not fail nor redo the work')
        other_rep_task = self.env['sales.rep.task'].create({
            'name': 'Not mine', 'sales_rep_id': self.sup.id, 'task_type': 'other', 'date_deadline': fields.Date.today()})
        with self.assertRaises(UserError):
            self._run_action('task_update', {'task_id': other_rep_task.id, 'action': 'done'})

    def test_queued_request_create_and_reply(self):
        adv = self.env.ref('sales_rep_management.dept_request_type_advance')
        payload = {'local_id': 'REQ-test-1', 'type_id': adv.id, 'description': 'fuel', 'amount': 200}
        r1 = self._run_action('dept_request_create', dict(payload))
        r2 = self._run_action('dept_request_create', dict(payload))
        self.assertEqual(r1['id'], r2['id'], 'same local id creates one request only')
        req = self.env['sales.rep.dept.request'].browse(r1['id'])
        req.response = 'receipt?'
        req.action_request_info()
        # the phone may only know the local id of a request it created offline
        out = self._run_action('dept_request_reply', {'request_local_id': 'REQ-test-1', 'text': 'attached'})
        self.assertEqual(out['state'], 'submitted')
        replay = self._run_action('dept_request_reply', {'request_local_id': 'REQ-test-1', 'text': 'attached'})
        self.assertTrue(replay.get('replayed'))
        self.assertEqual(req.description.count('attached'), 1, 'the answer is not appended twice')

    # ------------------------------------------------------------------ sync integrity
    def test_mobile_datetime_is_stored_in_utc(self):
        from odoo.addons.sales_rep_management.controllers.utils import parse_mobile_datetime
        self.user.tz = 'Africa/Cairo'
        utc = parse_mobile_datetime('2026-11-05T08:30:00.000Z', self.user)
        self.assertEqual(fields.Datetime.to_string(utc), '2026-11-05 08:30:00')
        # old app: naive local Cairo time (UTC+2 in November)
        local = parse_mobile_datetime('2026-11-05 10:30:00', self.user)
        self.assertEqual(fields.Datetime.to_string(local), '2026-11-05 08:30:00')
        self.assertFalse(parse_mobile_datetime('', self.user))
        self.assertFalse(parse_mobile_datetime('not a date', self.user))

    def test_ledger_returns_stored_result_on_replay(self):
        Ledger = self.env['sales.rep.sync.ledger']
        self.assertIsNone(Ledger.lookup(self.rep.id, 'action_x'))
        Ledger.record(self.rep.id, 'action_x', 'process_delivery', {'picking': 'WH/OUT/1'})
        self.assertEqual(Ledger.lookup(self.rep.id, 'action_x'), {'picking': 'WH/OUT/1'})
        self.assertIsNone(Ledger.lookup(self.sup.id, 'action_x'), 'keys are per representative')

    def test_unique_local_id_indexes_exist(self):
        for table in ('sale_order', 'account_payment', 'sales_rep_visit'):
            self.env.cr.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (f'{table}_mobile_local_id_mobile_uniq',))
            self.assertTrue(self.env.cr.fetchone(), f'unique idempotency index on {table}')

    def test_customer_only_once_per_route(self):
        route = self.env['sales.rep.route'].create({'name': 'T Route', 'sales_rep_id': self.rep.id, 'date': fields.Date.today()})
        self.env['sales.route.customer'].create({'route_id': route.id, 'partner_id': self.customer.id})
        with self.assertRaises(ValidationError):
            self.env['sales.route.customer'].create({'route_id': route.id, 'partner_id': self.customer.id})

    def test_fake_location_notifies_supervisor_once(self):
        Alert = self.env['sales.rep.security.alert']
        Alert.create({'sales_rep_id': self.rep.id, 'alert_type': 'mock_location', 'latitude': 30.1, 'longitude': 31.3})
        Alert.create({'sales_rep_id': self.rep.id, 'alert_type': 'mock_location', 'latitude': 30.1, 'longitude': 31.3})
        acts = self.rep.activity_ids.filtered(lambda a: a.user_id == self.sup_user)
        self.assertEqual(len(acts), 1, 'one to-do for the supervisor, not one per report')

    # ------------------------------------------------------------------ commissions
    def test_commission_most_specific_rule_and_refunds(self):
        categ = self.env['product.category'].create({'name': 'T Categ'})
        special = self.env['product.product'].create({'name': 'T Special', 'list_price': 10, 'categ_id': categ.id})
        plan = self.env['sales.rep.commission.plan'].create({'name': 'T Plan', 'base': 'invoiced', 'rule_ids': [
            (0, 0, {'rate': 2.0}),
            (0, 0, {'categ_id': categ.id, 'rate': 5.0}),
        ]})
        self.rep.commission_plan_id = plan.id
        self.assertEqual(plan._rule_for(special).rate, 5.0)
        self.assertEqual(plan._rule_for(self.product).rate, 2.0)

        def invoice(product, amount, move_type='out_invoice'):
            m = self.env['account.move'].create({'move_type': move_type, 'partner_id': self.customer.id,
                'invoice_line_ids': [(0, 0, {'product_id': product.id, 'quantity': 1, 'price_unit': amount, 'tax_ids': [(5, 0, 0)]})]})
            m.sales_rep_id = self.rep.id
            m.action_post()
            return m
        invoice(self.product, 1000)
        invoice(special, 200)
        invoice(self.product, 100, 'out_refund')
        sheet = self.env['sales.rep.commission.sheet'].create({'sales_rep_id': self.rep.id, 'plan_id': plan.id})
        sheet.action_compute()
        # (1000 - 100) * 2 % + 200 * 5 % = 18 + 10
        self.assertAlmostEqual(sheet.commission_amount, 28.0)
        self.assertAlmostEqual(self.env['sales.rep.commission.sheet'].estimate_for(self.rep, sheet.date_from, sheet.date_to), 28.0)
        with self.assertRaises(ValidationError):
            self.env['sales.rep.commission.sheet'].create({'sales_rep_id': self.rep.id, 'plan_id': plan.id})

    def test_commission_collected_basis_and_target_gate(self):
        plan = self.env['sales.rep.commission.plan'].create({'name': 'T Cash', 'base': 'collected', 'target_gate': 80,
                                                             'rule_ids': [(0, 0, {'rate': 10.0})]})
        move = self._post_invoice(400.0)
        move.sales_rep_id = self.rep.id
        self.env['account.payment.register'].with_context(active_model='account.move', active_ids=move.ids).create(
            {'amount': 100.0})._create_payments()
        sheet = self.env['sales.rep.commission.sheet'].create({'sales_rep_id': self.rep.id, 'plan_id': plan.id})
        sheet.action_compute()
        self.assertAlmostEqual(sum(sheet.line_ids.mapped('commission_amount')), 10.0, msg='10 % of the paid quarter')
        self.assertTrue(sheet.gate_blocked, 'no approved target -> gate closed')
        self.assertEqual(sheet.total_amount, 0.0)

    # ------------------------------------------------------------------ free products
    def test_one_shared_free_product_per_base_product(self):
        def program(name):
            return self.env['loyalty.program'].create({
                'name': name, 'program_type': 'promotion', 'applies_on': 'current', 'trigger': 'auto',
                'rule_ids': [(0, 0, {'minimum_qty': 3})],
                'reward_ids': [(0, 0, {'reward_type': 'product', 'reward_product_id': self.product.id, 'reward_product_qty': 1})]})
        p1, p2 = program('Buy 3 get 1 - Jan'), program('Buy 3 get 1 - Feb')
        twin = p1.reward_ids.discount_line_product_id
        self.assertEqual(twin, p2.reward_ids.discount_line_product_id, 'second promotion reuses the same free product')
        self.assertEqual(twin.free_of_product_id, self.product)
        self.assertEqual(twin.uom_id, self.product.uom_id)
        self.assertEqual(twin.categ_id, self.product.categ_id)
        p1.reward_ids.write({'description': 'Renamed reward', 'active': False})
        self.assertTrue(twin.active and twin.name != 'Renamed reward', 'one promotion cannot rename or archive the shared product')

    # ------------------------------------------------------------------ working day
    def test_workday_start_end_replay_safe(self):
        from odoo.addons.sales_rep_management.controllers import mobile_ops
        self.assertFalse(mobile_ops.workday_op(self.env, self.rep, 'status', {})['started'])
        self.assertFalse(self.env['sales.rep.workday'].search_count([('sales_rep_id', '=', self.rep.id)]),
                         'asking for the status creates nothing')
        with self.assertRaises(UserError):
            mobile_ops.workday_op(self.env, self.rep, 'end', {})
        a = mobile_ops.workday_op(self.env, self.rep, 'start', {'time': '2026-11-05T06:00:00Z'})
        b = mobile_ops.workday_op(self.env, self.rep, 'start', {'time': '2026-11-05T07:00:00Z'})
        self.assertEqual(a['start_time'], b['start_time'], 'a second start keeps the first')
        end = mobile_ops.workday_op(self.env, self.rep, 'end', {'time': '2026-11-05T14:30:00Z'})
        self.assertAlmostEqual(end['duration_hours'], 8.5)

    # ------------------------------------------------------------------ command center
    def test_command_center_scopes_and_sections(self):
        CC = self.env['sales.rep.command.center']
        data = CC.get_data(rep_ids=[self.rep.id])
        self.assertFalse(data['empty'])
        keys = [s['key'] for s in data['sections']]
        for k in ('visits', 'sales', 'invoices', 'delivery', 'collections', 'tasks', 'targets', 'commissions', 'workdays', 'risk'):
            self.assertIn(k, keys)
        team = CC.get_data(supervisor_id=self.sup.id)
        self.assertEqual(team['rep_count'], 2, 'team scope = supervisor and his reps')
        self.assertIn('reps', CC.get_filters())

    # ------------------------------------------------------------------ mobile access policy
    def test_mobile_token_expiry_and_device_binding(self):
        from datetime import timedelta as td
        self.env['ir.config_parameter'].sudo().set_param('sales_rep_management.mobile_token_days', '30')
        expiry = self.rep._mobile_bind(hash_token('tok'), 'phone-A', 'Pixel')
        self.assertTrue(expiry and expiry > fields.Datetime.now() + td(days=29))
        self.assertIsNone(self.rep._mobile_check('phone-A'))
        self.assertIsNone(self.rep._mobile_check(None), 'old app without the header still works')
        self.assertEqual(self.rep._mobile_check('phone-B'), 'other_device')
        self.rep.mobile_token_expiry = fields.Datetime.now() - td(minutes=1)
        self.assertEqual(self.rep._mobile_check('phone-A'), 'token_expired')
        # the master server's expiry is never exceeded
        master = fields.Datetime.now() + td(days=2)
        self.assertEqual(self.rep._mobile_bind(hash_token('tok2'), 'phone-A', None, master), master)
        self.rep.action_revoke_mobile_access()
        self.assertFalse(self.rep.sudo().mobile_access_token)

    def test_technical_rep_writes_do_not_reset_catalog(self):
        self.rep.catalog_changed_at = False
        self.rep._mobile_bind(hash_token('t'), 'p')
        self.assertFalse(self.rep.catalog_changed_at)
        self.rep.product_category_ids = [(4, self.product.categ_id.id)]
        self.assertTrue(self.rep.catalog_changed_at, 'a catalog setting change forces a full catalog')
