# -*- coding: utf-8 -*-
"""Integrity guards for the mobile sync.

Three layers stop duplicate records when the network is slow, a sync is
retried, or the phone sends the same queue twice:

1. Per-rep serialization: /api/mobile/sync takes a transaction-level
   advisory lock on the representative, so two overlapping syncs of the
   same rep run one after the other and the second one sees what the first
   committed (controllers/sync.py).
2. Idempotency keys: every record created from the phone carries the
   phone's ``mobile_local_id``; uploads look it up before creating, and a
   partial UNIQUE index makes the database refuse a second row with the
   same key even if two code paths race.
3. Action ledger: every queued action (deliver, invoice, pay, return...)
   that succeeded is written to ``sales.rep.sync.ledger`` with its result.
   A resent action returns the stored result instead of running again, so
   a partial delivery or a payment can never be applied twice.
"""
import json
import logging
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


def ensure_unique_local_id(cr, table, column='mobile_local_id', scope=None):
    """Create a partial unique index on the phone's idempotency key.

    Skipped (with a warning) while duplicates from before the fix still exist,
    so an upgrade never fails on old data.
    """
    name = f'{table}_{column}_mobile_uniq'
    cr.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (name,))
    if cr.fetchone():
        return
    cols = ', '.join(([scope] if scope else []) + [column])
    cr.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT {cols} FROM {table}
             WHERE {column} IS NOT NULL AND {column} != ''
          GROUP BY {cols} HAVING COUNT(*) > 1) d
    """)
    dups = cr.fetchone()[0]
    if dups:
        _logger.warning("Sync integrity: %s has %s duplicated %s value(s); unique index not created. "
                        "Merge or clear the duplicates, then upgrade the module again.", table, dups, column)
        return
    cr.execute(f"CREATE UNIQUE INDEX {name} ON {table} ({cols}) WHERE {column} IS NOT NULL AND {column} != ''")


class SalesRepSyncLedger(models.Model):
    _name = 'sales.rep.sync.ledger'
    _description = 'Mobile Sync Action Ledger'
    _order = 'id desc'
    _rec_name = 'local_id'

    sales_rep_id = fields.Many2one('sales.representative', required=True, ondelete='cascade', index=True)
    local_id = fields.Char(required=True, index=True)
    action_type = fields.Char(required=True)
    result = fields.Text()

    _sql_constraints = [
        ('rep_local_uniq', 'unique(sales_rep_id, local_id)', 'This mobile action was already processed.'),
    ]

    @api.model
    def lookup(self, rep_id, local_id):
        if not local_id:
            return None
        row = self.sudo().search([('sales_rep_id', '=', rep_id), ('local_id', '=', local_id)], limit=1)
        if not row:
            return None
        try:
            return json.loads(row.result or 'null')
        except ValueError:
            return None

    @api.model
    def record(self, rep_id, local_id, action_type, result):
        if not local_id:
            return
        self.sudo().create({
            'sales_rep_id': rep_id,
            'local_id': local_id,
            'action_type': action_type,
            'result': json.dumps(result, default=str),
        })

    @api.model
    def _cron_purge(self, days=90):
        limit = fields.Datetime.now() - timedelta(days=days)
        self.sudo().search([('create_date', '<', limit)]).unlink()


class SaleOrderLocalId(models.Model):
    _inherit = 'sale.order'

    def init(self):
        super().init()
        ensure_unique_local_id(self.env.cr, self._table)


class AccountPaymentLocalId(models.Model):
    _inherit = 'account.payment'

    def init(self):
        super().init()
        ensure_unique_local_id(self.env.cr, self._table)


class SalesRepVisitLocalId(models.Model):
    _inherit = 'sales.rep.visit'

    def init(self):
        super().init()
        ensure_unique_local_id(self.env.cr, self._table)


class SalesRouteCustomerUnique(models.Model):
    _inherit = 'sales.route.customer'

    @api.constrains('route_id', 'partner_id')
    def _check_unique_customer_per_route(self):
        for rc in self.filtered(lambda r: r.route_id and r.partner_id and r.state != 'cancelled'):
            dup = self.search_count([
                ('id', '!=', rc.id), ('route_id', '=', rc.route_id.id),
                ('partner_id', '=', rc.partner_id.id), ('state', '!=', 'cancelled')])
            if dup:
                raise ValidationError(_("%(customer)s is already on route %(route)s.",
                                        customer=rc.partner_id.display_name, route=rc.route_id.display_name))
