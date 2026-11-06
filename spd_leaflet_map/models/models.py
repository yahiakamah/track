from odoo import _, api, models
from lxml.builder import E
from odoo.exceptions import UserError


class Base(models.AbstractModel):
    _inherit = 'base'

    @api.model
    def _get_default_leaflet_map_view(self):
        return E.leaflet_map(string=self._description)
