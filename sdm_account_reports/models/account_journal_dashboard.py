# -*- coding: utf-8 -*-
from odoo import models

import ast


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    def action_open_bank_balance_in_gl(self):

        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("sdm_account_reports.action_account_report_general_ledger")

        action['context'] = dict(ast.literal_eval(action['context']), default_filter_accounts=self.default_account_id.code)

        return action
