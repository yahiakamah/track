from odoo import models


class Http(models.AbstractModel):
    _inherit = 'ir.http'

    def session_info(self):
        result = super(Http, self).session_info()
        config = self.env['ir.config_parameter'].sudo()
        result.update({
            "leaflet.tile_url": config.get_param('leaflet.tile_url', default=''),
            "leaflet.copyright": config.get_param('leaflet.copyright', default=''),
        })
        return result
