# -*- coding: utf-8 -*-
import logging
from odoo import models, api

from .sse_dispatch import schedule_reps, reps_for_locations

_logger = logging.getLogger(__name__)


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    def _notify_product_update(self, location_ids=None):
        """Wake up only the reps whose van/default locations hold these quants."""
        location_ids = location_ids if location_ids is not None else self.mapped('location_id').ids
        schedule_reps(self.env, reps_for_locations(self.env, location_ids), 'stock_updated')

    @api.model_create_multi
    def create(self, vals_list):
        quants = super().create(vals_list)
        quants._notify_product_update()
        return quants

    def write(self, vals):
        res = super().write(vals)
        if 'quantity' in vals or 'reserved_quantity' in vals or 'inventory_quantity' in vals:
            self._notify_product_update()
        return res

    def unlink(self):
        location_ids = self.mapped('location_id').ids
        res = super().unlink()
        self._notify_product_update(location_ids)
        return res
