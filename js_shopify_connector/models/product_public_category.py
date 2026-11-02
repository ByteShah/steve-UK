from odoo import fields, models


class ProductPublicCategory(models.Model):
    _inherit = "product.public.category"

    shopify_taxonomy_id = fields.Char(
        string="Shopify Taxonomy ID",
        help="Shopify Standard Product Taxonomy category of products in this category, "
             "e.g. ap-2-3-2 for Animals & Pet Supplies > Pet Supplies > Dog Supplies > Dog Beds.")
