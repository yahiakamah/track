# -*- coding: utf-8 -*-

from odoo import models

class IrActionsAccountReportDownload(models.AbstractModel):

    _name = 'ir_actions_account_report_download'
    _description = 'Technical model for accounting report downloads'

    def _get_readable_fields(self):

        return self.env['ir.actions.actions']._get_readable_fields() | {'data'}
