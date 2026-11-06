from odoo import models, fields


class MapLocation(models.Model):
    _name = 'map.location'

    name = fields.Char(string="Name")
    latitude = fields.Char(string="Latitude")
    longitude = fields.Char(string="Longitude")