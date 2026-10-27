from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ProductProduct(models.Model):
    _inherit = "product.product"

    # SKU = TT D UUUU CCC SS. Products without an SKU base keep what was typed in.
    default_code = fields.Char(compute="_compute_sku_code", store=True, readonly=False)

    @api.depends(
        "product_tmpl_id.sku_base",
        "product_template_attribute_value_ids.product_attribute_value_id.sku_code",
        "product_template_attribute_value_ids.attribute_id.sku_part",
    )
    def _compute_sku_code(self):
        for product in self.filtered("product_tmpl_id.sku_base"):
            codes = {
                ptav.attribute_id.sku_part: ptav.product_attribute_value_id.sku_code
                for ptav in product.product_template_attribute_value_ids
                if ptav.attribute_id.sku_part
            }
            product.default_code = (
                f"{product.product_tmpl_id.sku_base}{codes.get('colour') or '000'}{codes.get('size') or '00'}")

    @api.constrains("default_code")
    def _check_sku_unique(self):
        # An SKU cannot be shared with any other product, SKU or typed-in reference.
        products = self.filtered("default_code")
        sku_codes = products.filtered("product_tmpl_id.sku_base").mapped("default_code")
        duplicate = next((code for code in sku_codes if sku_codes.count(code) > 1), None)
        if not duplicate and products:
            others = self.with_context(active_test=False).search(
                [("default_code", "in", products.mapped("default_code")), ("id", "not in", products.ids)])
            duplicate = next((other.default_code for other in others
                              if other.product_tmpl_id.sku_base or other.default_code in sku_codes), None)
        if duplicate:
            raise ValidationError(_("The SKU %s is already used by another product.", duplicate))
