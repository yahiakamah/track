from odoo import api, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.model
    def get_default_leaflet_position(self, model_name):
        current_partner = self.env.user.company_id.partner_id
        return {
            "lat": current_partner.partner_latitude,
            "lng": current_partner.partner_longitude,
        }
