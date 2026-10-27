from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

SKU_CODE_LENGTH = {"colour": 3, "size": 2}


class ProductAttribute(models.Model):
    _inherit = "product.attribute"

    sku_part = fields.Selection(
        [("colour", "Colour (CCC)"), ("size", "Size (SS)")],
        string="SKU Part", copy=False,
        help="Part of the SKU filled by the codes of this attribute's values.")

    _sku_part_uniq = models.Constraint("unique(sku_part)", "Only one attribute can fill each SKU part.")

    @api.constrains("sku_part", "create_variant")
    def _check_sku_part(self):
        if self.filtered(lambda a: a.sku_part and a.create_variant == "no_variant"):
            raise ValidationError(_("An attribute used in the SKU must create variants."))
        self.value_ids._check_sku_code()

    def write(self, vals):
        if "sku_part" in vals and self.filtered(lambda a: a.sku_part != vals["sku_part"]).value_ids._is_used_in_sku():
            raise ValidationError(_("This attribute is used in product SKUs, so its SKU part cannot be changed."))
        return super().write(vals)


class ProductAttributeValue(models.Model):
    _inherit = "product.attribute.value"

    sku_code = fields.Char(string="SKU Code", copy=False)
    sku_part = fields.Selection(related="attribute_id.sku_part")

    _sku_code_uniq = models.Constraint(
        "unique(attribute_id, sku_code)", "Each SKU code can only be used once per attribute.")

    @api.depends("sku_code")
    @api.depends_context("show_sku_code")
    def _compute_display_name(self):
        super()._compute_display_name()
        if self.env.context.get("show_sku_code"):
            for value in self.filtered("sku_code"):
                value.display_name = f"{value.sku_code} - {value.display_name}"

    @api.constrains("sku_code", "attribute_id")
    def _check_sku_code(self):
        for value in self:
            length = SKU_CODE_LENGTH.get(value.attribute_id.sku_part)
            if length and not (value.sku_code and len(value.sku_code) == length and value.sku_code.isdigit()):
                raise ValidationError(_(
                    "Value '%(value)s' of %(attribute)s needs an SKU code of exactly %(length)s digits.",
                    value=value.name, attribute=value.attribute_id.name, length=length))

    def write(self, vals):
        if "sku_code" in vals and self.filtered(lambda v: v.sku_code != vals["sku_code"])._is_used_in_sku():
            raise ValidationError(_("This value is used in product SKUs, so its SKU code cannot be changed."))
        return super().write(vals)

    def _is_used_in_sku(self):
        return bool(self) and bool(self.env["product.template.attribute.value"].with_context(active_test=False).search_count(
            [("product_attribute_value_id", "in", self.ids), ("product_tmpl_id.sku_base", "!=", False)], limit=1))
