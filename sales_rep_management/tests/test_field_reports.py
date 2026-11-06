# -*- coding: utf-8 -*-
"""Field reports: figures match the source documents, filters (area with
sub-areas, supervisor team) work, visibility follows the team rules, and the
PDF / Excel / analysis outputs build for every grouping."""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.sales_rep_management.wizards.field_report_wizard import GROUP_BY, SECTIONS


@tagged('post_install', '-at_install', 'sales_rep')
class TestFieldReports(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        base = (env.ref('base.group_user') | env.ref('sales_team.group_sale_salesman_all_leads')
                | env.ref('sales_rep_management.group_sales_rep_user'))
        cls.u_sup = env['res.users'].create({'name': 'FR Sup', 'login': 'fr_sup',
                                            'groups_id': [(6, 0, (base | env.ref('sales_rep_management.group_sales_supervisor')).ids)]})
        cls.u_rep1 = env['res.users'].create({'name': 'FR Rep1', 'login': 'fr_rep1', 'groups_id': [(6, 0, base.ids)]})
        cls.u_rep2 = env['res.users'].create({'name': 'FR Rep2', 'login': 'fr_rep2', 'groups_id': [(6, 0, base.ids)]})
        Rep = env['sales.representative']
        cls.sup = Rep.create({'name': 'FR Sup', 'user_id': cls.u_sup.id, 'is_supervisor': True})
        cls.rep1 = Rep.create({'name': 'FR Rep1', 'user_id': cls.u_rep1.id, 'supervisor_id': cls.sup.id})
        cls.rep2 = Rep.create({'name': 'FR Rep2', 'user_id': cls.u_rep2.id})
        Area = env['sales.area']
        cls.cairo = Area.create({'name': 'FR Cairo'})
        cls.nasr = Area.create({'name': 'FR Nasr City', 'parent_id': cls.cairo.id})
        cls.giza = Area.create({'name': 'FR Giza'})
        cls.c1 = env['res.partner'].create({'name': 'FR C1', 'sales_area_id': cls.nasr.id})
        cls.c2 = env['res.partner'].create({'name': 'FR C2', 'sales_area_id': cls.giza.id})
        cls.today = fields.Date.context_today(env['res.users'])
        Task = env['sales.rep.task']
        cls.t_done = Task.create({'name': 'FR done', 'sales_rep_id': cls.rep1.id, 'partner_id': cls.c1.id, 'date_deadline': cls.today})
        cls.t_done.action_done({})
        cls.t_late = Task.create({'name': 'FR late', 'sales_rep_id': cls.rep1.id, 'partner_id': cls.c1.id,
                                  'date_deadline': cls.today - timedelta(days=3)})
        cls.t_other = Task.create({'name': 'FR other', 'sales_rep_id': cls.rep2.id, 'partner_id': cls.c2.id, 'date_deadline': cls.today})
        route = env['sales.rep.route'].create({'name': 'FR R1', 'sales_rep_id': cls.rep1.id, 'date': cls.today - timedelta(days=1)})
        cls.rc_visited = env['sales.route.customer'].create({'route_id': route.id, 'partner_id': cls.c1.id, 'state': 'visited',
                                                             'visit_result': 'successful'})
        cls.rc_missed = env['sales.route.customer'].create({'route_id': route.id, 'partner_id': cls.c2.id})

    def _wizard(self, **vals):
        vals.setdefault('date_from', self.today - timedelta(days=30))
        vals.setdefault('date_to', self.today + timedelta(days=1))
        return self.env['sales.rep.field.report.wizard'].create(vals)

    def _section(self, data, key):
        return next(s for s in data['sections'] if s['key'] == key)

    def test_task_and_visit_figures(self):
        data = self._wizard(sales_rep_ids=[(6, 0, self.rep1.ids)]).get_report_data()
        t = self._section(data, 'tasks')['totals']
        self.assertEqual((t['task_count'], t['nbr_done'], t['nbr_overdue'], t['nbr_on_time']), (2, 1, 1, 1))
        v = self._section(data, 'visits')['totals']
        self.assertEqual((v['nbr_planned'], v['nbr_plan_done'], v['nbr_missed']), (2, 1, 1))
        kpis = {k[0]: k[1] for k in self._section(data, 'visits')['kpis']}
        self.assertEqual(kpis['Journey compliance'], 50.0)

    def test_area_includes_sub_areas(self):
        data = self._wizard(area_ids=[(6, 0, self.cairo.ids)]).get_report_data()
        self.assertEqual(self._section(data, 'tasks')['totals']['task_count'], 2)  # both rep1 tasks are in Nasr City
        data = self._wizard(area_ids=[(6, 0, self.giza.ids)]).get_report_data()
        self.assertEqual(self._section(data, 'tasks')['totals']['task_count'], 1)

    def test_supervisor_team_filter(self):
        data = self._wizard(supervisor_id=self.sup.id).get_report_data()
        self.assertEqual(self._section(data, 'tasks')['totals']['task_count'], 2)

    def test_visibility_rules(self):
        Report = self.env['sales.rep.report.task']
        mine = Report.with_user(self.u_rep1).search([('task_id', 'in', (self.t_done | self.t_late | self.t_other).ids)])
        self.assertEqual(mine.mapped('task_id'), self.t_done | self.t_late)
        team = Report.with_user(self.u_sup).search([('task_id', 'in', (self.t_done | self.t_late | self.t_other).ids)])
        self.assertEqual(team.mapped('task_id'), self.t_done | self.t_late)
        other = Report.with_user(self.u_rep2).search([('task_id', 'in', (self.t_done | self.t_late | self.t_other).ids)])
        self.assertEqual(other.mapped('task_id'), self.t_other)

    def test_outputs_for_every_grouping(self):
        wiz = self._wizard(show_details=True)
        for key, _label in GROUP_BY:
            wiz.group_by = key
            data = wiz.get_report_data()
            self.assertEqual(len(data['sections']), len(SECTIONS))
            for s in data['sections']:
                self.assertEqual(len(s['totals_cells']), len(s['measures']))
                for g in s['groups']:
                    self.assertEqual(len(g['cells']), len(s['measures']))
        html, _t = self.env['ir.actions.report']._render_qweb_html('sales_rep_management.action_report_field_activity', wiz.ids)
        self.assertIn(b'Field Activity Report', html)
        for key in SECTIONS:
            action = getattr(wiz, f'action_open_{key}')()
            self.env[action['res_model']].search(action['domain'])
            self.env[action['res_model']].get_views([(False, 'pivot'), (False, 'graph'), (False, 'list'), (False, 'search')])
