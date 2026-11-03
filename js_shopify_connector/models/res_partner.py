from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    shopify_customer_id = fields.Char(string="Shopify Customer ID", copy=False, index="btree_not_null",
                                      help="Set when the contact is created or matched from a Shopify order.")
