from odoo import fields, models


class IrUiView(models.Model):
    _inherit = 'ir.ui.view'

    type = fields.Selection(selection_add=[('leaflet_map', 'Leaflet Map')], ondelete={'leaflet_map': 'cascade'})

    def _get_view_info(self):
        result = super()._get_view_info()
        result['leaflet_map'] = {
            'icon': 'fa fa-map-marker',
        }
        return result