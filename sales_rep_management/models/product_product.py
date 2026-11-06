# -*- coding: utf-8 -*-
from odoo import models, api

from .sse_dispatch import schedule_all

# Product fields the app does not use: writing them must not wake every device.
_SILENT_PRODUCT_FIELDS = {'standard_price', 'message_main_attachment_id', 'write_date'}


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def _notify_sse(self, reason):
        # Coalesced: one broadcast per transaction, after commit.
        schedule_all(self.env, reason)

    @api.model_create_multi
    def create(self, vals_list):
        products = super(ProductProduct, self).create(vals_list)
        self._notify_sse('product_created')
        return products

    def write(self, vals):
        res = super(ProductProduct, self).write(vals)
        if set(vals) - _SILENT_PRODUCT_FIELDS:
            self._notify_sse('product_updated')
        return res

    def unlink(self):
        res = super(ProductProduct, self).unlink()
        self._notify_sse('product_deleted')
        return res
