# -*- coding: utf-8 -*-
from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestCustomerReturn(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        # A storable product valued with AVCO so the SVL reflects price unit.
        cls.product = cls.env['product.product'].create({
            'name': 'Return Test Product',
            'is_storable': True,
            'standard_price': 0.0,
            'list_price': 50.0,
            'categ_id': cls.env.ref('product.product_category_all').id,
        })
        cls.product.categ_id.property_cost_method = 'average'
        cls.product.categ_id.property_valuation = 'real_time'

        cls.partner = cls.env['res.partner'].create({'name': 'Return Customer'})
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1)
        cls.reason = cls.env['customer.return.reason'].create(
            {'name': 'Test reason'})

        # Users
        group_user = cls.env.ref(
            'customer_return_request.group_customer_return_user')
        group_manager = cls.env.ref(
            'customer_return_request.group_customer_return_manager')
        cls.user = cls.env['res.users'].create({
            'name': 'CR User', 'login': 'cr_user',
            'groups_id': [Command.set([group_user.id])],
        })
        cls.manager = cls.env['res.users'].create({
            'name': 'CR Manager', 'login': 'cr_manager',
            'groups_id': [Command.set([group_manager.id])],
        })

    def _create_request(self, user=None, cost=12.0, qty=5.0, credit=False):
        env = self.env(user=user) if user else self.env
        return env['customer.return.request'].create({
            'partner_id': self.partner.id,
            'location_dest_id': self.warehouse.lot_stock_id.id,
            'reason_id': self.reason.id,
            'create_credit_note': credit,
            'line_ids': [Command.create({
                'product_id': self.product.id,
                'product_uom_id': self.product.uom_id.id,
                'quantity': qty,
                'inventory_unit_cost': cost,
                'credit_note_unit_price': 40.0,
            })],
        })

    def test_01_sequence_assigned(self):
        request = self._create_request()
        self.assertNotEqual(request.name, 'New')
        self.assertTrue(request.name.startswith('CRR/'))

    def test_02_full_workflow_and_valuation(self):
        request = self._create_request(cost=12.0, qty=5.0)
        request.action_submit()
        request.action_request_approval()
        self.assertEqual(request.state, 'waiting_approval')

        request.with_user(self.manager).action_approve()
        self.assertEqual(request.state, 'approved')
        self.assertEqual(len(request.picking_ids), 1)

        picking = request.picking_ids
        for move in picking.move_ids:
            move.move_line_ids.quantity = move.product_uom_qty
        picking.button_validate()
        self.assertEqual(picking.state, 'done')

        # Request auto-set to Done when the incoming transfer is validated.
        self.assertEqual(request.state, 'done')

        # Stock valuation layer uses quantity * inventory_unit_cost (= 60).
        svls = self.env['stock.valuation.layer'].search(
            [('customer_return_request_id', '=', request.id)])
        self.assertTrue(svls)
        self.assertAlmostEqual(sum(svls.mapped('value')), 60.0, places=2)

    def test_03_credit_note_created(self):
        request = self._create_request(credit=True)
        request.action_submit()
        request.action_request_approval()
        request.with_user(self.manager).action_approve()
        self.assertEqual(len(request.credit_note_ids), 1)
        cn = request.credit_note_ids
        self.assertEqual(cn.move_type, 'out_refund')
        self.assertEqual(cn.partner_id, self.partner)

    def test_04_user_cannot_approve(self):
        request = self._create_request()
        request.action_submit()
        request.action_request_approval()
        with self.assertRaises(AccessError):
            request.with_user(self.user).action_approve()

    def test_05_approval_blocks_zero_cost(self):
        request = self._create_request(cost=0.0)
        request.action_submit()
        request.action_request_approval()
        with self.assertRaises(UserError):
            request.with_user(self.manager).action_approve()

    def test_06_user_edit_only_draft(self):
        request = self._create_request(user=self.user)
        request.action_submit()
        with self.assertRaises(UserError):
            request.with_user(self.user).write({'reference': 'X'})
