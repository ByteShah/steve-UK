"""Mark published products "To Sync" when data sent to Shopify changes."""
from odoo import _, api, models
from odoo.exceptions import UserError

# product.template fields sent to Shopify (metafield fields are added from the mapping).
TEMPLATE_FIELDS = {
    "name", "description_ecommerce", "seo_name", "categ_id", "product_tag_ids", "shopify_status",
    "shopify_enabled", "website_meta_title", "website_meta_description", "public_categ_ids",
    "list_price", "taxes_id", "allow_out_of_stock_order", "is_storable", "type", "hs_code",
    "country_of_origin", "image_1920", "product_template_image_ids", "attribute_line_ids", "active",
    "default_code", "barcode", "weight", "standard_price",
}
VARIANT_FIELDS = {
    "default_code", "barcode", "weight", "standard_price", "image_variant_1920", "active",
    "product_template_attribute_value_ids", "lst_price",
}


class ProductTemplate(models.Model):
    _inherit = "product.template"

    @api.model
    def _shopify_tracked_fields(self):
        maps = self.env["shopify.metafield.map"].sudo().search([])
        return TEMPLATE_FIELDS | set(maps.field_id.mapped("name")) | set(maps.condition_field_id.mapped("name"))

    def _shopify_mark_to_sync(self):
        templates = self.with_context(active_test=False).filtered(
            lambda t: t.shopify_product_id and t.shopify_sync_state != "to_sync")
        if templates:
            templates.sudo().write({"shopify_sync_state": "to_sync"})

    def write(self, vals):
        res = super().write(vals)
        if vals.keys() & self._shopify_tracked_fields():
            self._shopify_mark_to_sync()
        return res

    @api.ondelete(at_uninstall=False)
    def _unlink_except_in_shopify(self):
        published = self.filtered("shopify_product_id")
        if published:
            raise UserError(_(
                "%s is in Shopify. Archive it instead: it is then archived in Shopify too.",
                ", ".join(published.mapped("display_name"))))


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model_create_multi
    def create(self, vals_list):
        variants = super().create(vals_list)
        variants.product_tmpl_id._shopify_mark_to_sync()
        return variants

    def write(self, vals):
        res = super().write(vals)
        if vals.keys() & VARIANT_FIELDS:
            self.product_tmpl_id._shopify_mark_to_sync()
        return res

    def unlink(self):
        templates = self.product_tmpl_id
        res = super().unlink()
        templates.exists()._shopify_mark_to_sync()
        return res


class ProductImage(models.Model):
    _inherit = "product.image"

    def _shopify_templates(self):
        return self.product_tmpl_id | self.product_variant_id.product_tmpl_id

    @api.model_create_multi
    def create(self, vals_list):
        images = super().create(vals_list)
        images._shopify_templates()._shopify_mark_to_sync()
        return images

    def write(self, vals):
        res = super().write(vals)
        self._shopify_templates()._shopify_mark_to_sync()
        return res

    def unlink(self):
        templates = self._shopify_templates()
        res = super().unlink()
        templates.exists()._shopify_mark_to_sync()
        return res


class ProductTemplateAttributeValue(models.Model):
    _inherit = "product.template.attribute.value"

    def write(self, vals):
        res = super().write(vals)
        if "price_extra" in vals or "ptav_active" in vals:
            self.product_tmpl_id._shopify_mark_to_sync()
        return res


class ProductCategory(models.Model):
    _inherit = "product.category"

    def write(self, vals):
        res = super().write(vals)
        if "name" in vals:  # product type in Shopify
            self.env["product.template"].with_context(active_test=False).search(
                [("categ_id", "in", self.ids), ("shopify_product_id", "!=", False)])._shopify_mark_to_sync()
        return res


class ProductPublicCategory(models.Model):
    _inherit = "product.public.category"

    def write(self, vals):
        res = super().write(vals)
        if "shopify_taxonomy_id" in vals:
            self.env["product.template"].with_context(active_test=False).search(
                [("public_categ_ids", "in", self.ids), ("shopify_product_id", "!=", False)])._shopify_mark_to_sync()
        return res


class ProductTag(models.Model):
    _inherit = "product.tag"

    def write(self, vals):
        res = super().write(vals)
        if "name" in vals:
            self.product_template_ids._shopify_mark_to_sync()
        return res
