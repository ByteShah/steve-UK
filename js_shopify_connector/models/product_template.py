from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import is_html_empty

SHOPIFY_MAX_OPTIONS = 3
SHOPIFY_MAX_VARIANTS = 2048


class ProductTemplate(models.Model):
    _inherit = "product.template"

    # Shopify: DENY by default, CONTINUE only for pre-order / made-to-order products.
    allow_out_of_stock_order = fields.Boolean(default=False)

    # Publishing. The Shopify handle is the standard seo_name (website.seo.metadata).
    shopify_enabled = fields.Boolean(
        string="Sell on Shopify", copy=False, tracking=True,
        help="Only products with this switch on are created and published in Shopify.")
    shopify_status = fields.Selection(
        [("draft", "Draft"), ("active", "Active"), ("archived", "Archived")],
        string="Shopify Status", default="draft", required=True, copy=False, tracking=True,
        help="New products arrive in Shopify as Draft. Set Active once content and images are checked.")

    # Product page data, sent as metafields.
    width = fields.Float(string="Width (cm)", digits=(10, 1))
    depth = fields.Float(string="Depth (cm)", digits=(10, 1))
    height = fields.Float(string="Height (cm)", digits=(10, 1))
    materials = fields.Char(help="e.g. Rattan cane, solid oak, cotton cover")
    care_instructions = fields.Html()
    enable_age_safety = fields.Boolean(string="Age Suitability & Warnings")
    age_safety_text = fields.Text(
        string="Age & Safety Text", help="Age range and required warnings shown on the product page.")
    gpsr_manufacturer_id = fields.Many2one(
        "res.partner", string="Manufacturer (GPSR)",
        help="Name, postal and email address shown for the General Product Safety Regulation.")
    gpsr_eu_responsible_id = fields.Many2one(
        "res.partner", string="EU Responsible Person (GPSR)")
    enable_google_shopping = fields.Boolean(string="Google Shopping")
    google_age_group = fields.Selection(
        [("newborn", "Newborn"), ("infant", "Infant"), ("toddler", "Toddler"), ("kids", "Kids"), ("adult", "Adult")],
        string="Age Group")
    google_gender = fields.Selection(
        [("male", "Male"), ("female", "Female"), ("unisex", "Unisex")], string="Gender")
    google_condition = fields.Selection(
        [("new", "New"), ("refurbished", "Refurbished"), ("used", "Used")], string="Condition", default="new")
    product_trade_note = fields.Text(
        string="Trade Note", help="Only when this product needs a non-default trade note or promises message.")

    # Written by the connector.
    shopify_product_id = fields.Char(string="Shopify Product ID", readonly=True, copy=False, index="btree_not_null")
    shopify_last_synced = fields.Datetime(string="Last Synced", readonly=True, copy=False)
    shopify_sync_state = fields.Selection(
        [("not_published", "Not Published"), ("to_sync", "To Sync"), ("synced", "Synced"), ("error", "Error")],
        string="Sync State", default="not_published", required=True, readonly=True, copy=False)
    shopify_sync_error = fields.Text(string="Sync Error", readonly=True, copy=False)

    @api.model
    def _shopify_slugify(self, value):
        return self.env["ir.http"]._slugify(value or "") or False

    def _shopify_handle_follows_name(self):
        """Handles that are still the default one (empty or slug of the name) follow the name."""
        return self.filtered(lambda t: not t.shopify_product_id
                             and (not t.seo_name or t.seo_name == self._shopify_slugify(t.name)))

    @api.onchange("name")
    def _onchange_name_shopify_handle(self):
        if not self.shopify_product_id and (
            not self.seo_name or self.seo_name == self._shopify_slugify(self._origin.name)
        ):
            self.seo_name = self._shopify_slugify(self.name)

    @api.onchange("seo_name")
    def _onchange_seo_name_shopify_handle(self):
        if self.seo_name:
            self.seo_name = self._shopify_slugify(self.seo_name)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("seo_name"):
                vals["seo_name"] = self._shopify_slugify(vals["seo_name"])
            elif vals.get("name"):
                vals["seo_name"] = self._shopify_slugify(vals["name"])
        return super().create(vals_list)

    def copy_data(self, default=None):
        # The copy gets its own handle from its new name.
        vals_list = super().copy_data(default=default)
        if not (default and "seo_name" in default):
            for vals in vals_list:
                vals.pop("seo_name", None)
        return vals_list

    def write(self, vals):
        if "seo_name" in vals:
            vals["seo_name"] = self._shopify_slugify(vals["seo_name"])
            if self.filtered(lambda t: t.shopify_product_id and t.seo_name != vals["seo_name"]):
                raise ValidationError(_(
                    "The handle of a product published in Shopify cannot be changed: "
                    "it is the product URL, and changing it breaks links, ads and SEO."))
        elif vals.get("name"):
            following = self._shopify_handle_follows_name()
            if following:
                res = super(ProductTemplate, self - following).write(vals) if self - following else True
                super(ProductTemplate, following).write(dict(vals, seo_name=self._shopify_slugify(vals["name"])))
                return res
        return super().write(vals)

    @api.constrains("shopify_enabled", "territory_id")
    def _check_shopify_territory(self):
        uk = self.env.ref("js_product_sku.territory_uk", raise_if_not_found=False)
        for template in self.filtered("shopify_enabled"):
            if not uk or template.territory_id != uk:
                raise ValidationError(_(
                    "%(product)s: only products of territory %(territory)s can be sold on Shopify.",
                    product=template.display_name, territory=uk.display_name if uk else "01"))

    @api.constrains("shopify_enabled", "seo_name")
    def _check_shopify_handle_unique(self):
        for template in self.filtered(lambda t: t.shopify_enabled and t.seo_name):
            other = self.with_context(active_test=False).search([
                ("id", "!=", template.id), ("shopify_enabled", "=", True), ("seo_name", "=", template.seo_name),
            ], limit=1)
            if other:
                raise ValidationError(_(
                    "The Shopify handle %(handle)s is already used by %(product)s.",
                    handle=template.seo_name, product=other.display_name))

    def _get_shopify_publish_issues(self):
        """Return what stops this product from being published to Shopify, as a list of messages."""
        self.ensure_one()
        issues = []
        if not self.shopify_enabled:
            issues.append(_("Sell on Shopify is off."))
        if self.type != "consu":
            issues.append(_("Only goods can be sold on Shopify."))
        if not self.seo_name:
            issues.append(_("Set a Shopify handle."))
        if is_html_empty(self.description_ecommerce):
            issues.append(_("Write the eCommerce description."))
        if not self.product_tag_ids:
            issues.append(_("Add at least one tag: department collections are built on tags."))
        if not self.image_1920:
            issues.append(_("Add a main image."))
        category = self.public_categ_ids[:1]
        if not category:
            issues.append(_("Add an eCommerce category: it gives the Shopify product category."))
        elif not category.shopify_taxonomy_id:
            issues.append(_("eCommerce category %s has no Shopify taxonomy ID.", category.display_name))
        if not self.hs_code:
            issues.append(_("Set the HS code: duties outside the UK cannot be calculated without it."))
        if not self.country_of_origin:
            issues.append(_("Set the country of origin."))

        attributes = self.attribute_line_ids.attribute_id
        dynamic = attributes.filtered(lambda a: a.create_variant == "dynamic")
        if dynamic:
            issues.append(_(
                "Attributes %s create variants dynamically: Shopify needs every variant to exist.",
                ", ".join(dynamic.mapped("name"))))
        if len(attributes.filtered(lambda a: a.create_variant != "no_variant")) > SHOPIFY_MAX_OPTIONS:
            issues.append(_("Shopify allows at most %s variant attributes.", SHOPIFY_MAX_OPTIONS))
        variants = self.product_variant_ids
        if len(variants) > SHOPIFY_MAX_VARIANTS:
            issues.append(_("Shopify allows at most %s variants.", SHOPIFY_MAX_VARIANTS))
        for what, missing in [
            (_("an SKU"), variants.filtered(lambda v: not v.default_code)),
            (_("a barcode"), variants.filtered(lambda v: not v.barcode)),
            (_("a sales price"), variants.filtered(lambda v: v.lst_price <= 0)),
            (_("a weight"), variants.filtered(lambda v: v.weight <= 0)),
        ]:
            if missing:
                issues.append(_(
                    "Variants without %(what)s: %(variants)s",
                    what=what, variants=", ".join(missing.mapped("display_name"))))

        if self.enable_age_safety and not self.age_safety_text:
            issues.append(_("Write the age suitability and safety text."))
        if self.enable_google_shopping and not (self.google_age_group and self.google_gender and self.google_condition):
            issues.append(_("Google Shopping needs the age group, gender and condition."))
        return issues

    def action_shopify_check(self):
        problems = []
        for template in self:
            issues = template._get_shopify_publish_issues()
            if issues:
                problems.append("%s\n%s" % (template.display_name, "\n".join("  • " + issue for issue in issues)))
        if problems:
            raise UserError(_("Fix these before publishing to Shopify:\n\n%s", "\n\n".join(problems)))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _("%s product(s) ready for Shopify.", len(self)),
            },
        }
