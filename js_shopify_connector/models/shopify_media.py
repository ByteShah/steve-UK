from odoo import fields, models


class ShopifyMedia(models.Model):
    """Shopify file of an Odoo image, so unchanged images are not uploaded again."""
    _name = "shopify.media"
    _description = "Shopify Media"

    store_id = fields.Many2one("shopify.store", required=True, ondelete="cascade")
    res_model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    res_field = fields.Char(required=True)
    checksum = fields.Char(required=True, help="SHA-1 of the image sent to Shopify.")
    shopify_file_id = fields.Char(string="Shopify File ID", required=True)

    _image_uniq = models.Constraint(
        "unique(store_id, res_model, res_id, res_field)", "Each image has one Shopify file per store.")
