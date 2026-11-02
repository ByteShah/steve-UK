"""Odoo product.template -> Shopify productSet input."""
import json

from odoo.tools import float_round, html2plaintext, is_html_empty

from .rich_text import html_to_rich_text

DEFAULT_OPTION = "Title"
DEFAULT_VALUE = "Default Title"
TAXONOMY_PREFIX = "gid://shopify/TaxonomyCategory/"
STATUS = {"draft": "DRAFT", "active": "ACTIVE", "archived": "ARCHIVED"}


class ProductPayloadBuilder:
    """Build the productSet input of one product. `media_files` maps
    (res_model, res_id, res_field) to Shopify file IDs of the images already uploaded."""

    def __init__(self, template, store, media_files=None):
        self.store = store
        self.company = store.company_id
        self.template = template.with_company(self.company)
        self.media_files = media_files or {}

    # ------------------------------------------------------------------
    # Product
    # ------------------------------------------------------------------
    def build(self):
        tmpl = self.template
        options, variants = self._options_and_variants()
        payload = {
            "title": tmpl.name,
            "handle": tmpl.seo_name,
            "descriptionHtml": "" if is_html_empty(tmpl.description_ecommerce) else str(tmpl.description_ecommerce),
            "vendor": self.store.vendor or self.company.name,
            "productType": tmpl.categ_id.name or "",
            "tags": tmpl.product_tag_ids.mapped("name"),
            "status": STATUS[tmpl.shopify_status] if tmpl.active else "ARCHIVED",
            "seo": {
                "title": tmpl.website_meta_title or tmpl.name,
                "description": tmpl.website_meta_description or "",
            },
            "productOptions": options,
            "variants": variants,
            "metafields": self.metafields()[0],
        }
        category = self.taxonomy_category()
        if category:
            payload["category"] = category
        files = self.product_files()
        if files:
            payload["files"] = files
        return payload

    def taxonomy_category(self):
        taxonomy = self.template.public_categ_ids[:1].shopify_taxonomy_id
        if not taxonomy:
            return False
        taxonomy = taxonomy.strip()
        return taxonomy if taxonomy.startswith("gid://") else TAXONOMY_PREFIX + taxonomy

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------
    def images(self):
        """Ordered images as (res_model, record, res_field, alt): main image, extra images, variant images."""
        tmpl = self.template
        images = []
        if tmpl.image_1920:
            images.append(("product.template", tmpl, "image_1920", tmpl.name))
        for image in tmpl.product_template_image_ids.sorted(lambda i: (i.sequence, i.id)):
            if image.image_1920:
                images.append(("product.image", image, "image_1920", image.name or tmpl.name))
        for variant in self.variants():
            if variant.image_variant_1920:
                images.append(("product.product", variant, "image_variant_1920", variant.display_name))
        return images

    def _file_id(self, model, record, field):
        return self.media_files.get((model, record.id, field))

    def product_files(self):
        files = []
        for model, record, field, alt in self.images():
            file_id = self._file_id(model, record, field)
            if file_id:
                files.append({"id": file_id, "alt": alt, "contentType": "IMAGE"})
        return files

    # ------------------------------------------------------------------
    # Options and variants
    # ------------------------------------------------------------------
    def variants(self):
        return self.template.product_variant_ids.sorted(lambda v: v.id)

    @staticmethod
    def variant_options(variant):
        """[(option name, value name)] of a variant, in attribute line order."""
        ptavs = variant.product_template_attribute_value_ids.sorted(
            lambda v: (v.attribute_line_id.sequence, v.attribute_line_id.id))
        return [(ptav.attribute_id.name, ptav.name) for ptav in ptavs]

    def _options_and_variants(self):
        variants = self.variants()
        option_values = {}  # option name -> ordered values used by the variants
        for variant in variants:
            for name, value in self.variant_options(variant):
                values = option_values.setdefault(name, [])
                if value not in values:
                    values.append(value)
        if option_values:
            options = [{"name": name, "position": position, "values": [{"name": v} for v in values]}
                       for position, (name, values) in enumerate(option_values.items(), start=1)]
        else:
            options = [{"name": DEFAULT_OPTION, "position": 1, "values": [{"name": DEFAULT_VALUE}]}]
        return options, [self.variant_input(variant, position)
                         for position, variant in enumerate(variants, start=1)]

    def variant_input(self, variant, position=1):
        tmpl = self.template
        variant = variant.with_company(self.company)
        option_values = [{"optionName": name, "name": value} for name, value in self.variant_options(variant)]
        vals = {
            "position": position,
            "optionValues": option_values or [{"optionName": DEFAULT_OPTION, "name": DEFAULT_VALUE}],
            "sku": variant.default_code or "",
            "barcode": variant.barcode or "",
            "price": self.price(variant),
            "compareAtPrice": None,
            "taxable": self.taxable(),
            "inventoryPolicy": "CONTINUE" if tmpl.allow_out_of_stock_order else "DENY",
            "inventoryItem": {
                "tracked": bool(tmpl.is_storable),
                "requiresShipping": tmpl.type == "consu",
                "cost": self._decimal(variant.standard_price),
                "harmonizedSystemCode": (tmpl.hs_code or "").replace(".", "").replace(" ", "") or None,
                "countryCodeOfOrigin": tmpl.country_of_origin.code or None,
                "measurement": {"weight": {"value": variant.weight or 0.0, "unit": "KILOGRAMS"}},
            },
        }
        if variant.shopify_variant_id:
            vals["id"] = variant.shopify_variant_id
        file_id = self._file_id("product.product", variant, "image_variant_1920")
        if file_id:
            vals["file"] = {"id": file_id, "alt": variant.display_name, "contentType": "IMAGE"}
        return vals

    def taxes(self):
        return self.template.taxes_id._filter_taxes_by_company(self.company)

    def taxable(self):
        return any(tax.amount for tax in self.taxes())

    def price(self, variant):
        """Retail price including VAT, as Shopify expects for the UK."""
        price = variant.with_company(self.company).lst_price
        taxes = self.taxes()
        if taxes:
            price = taxes.compute_all(price, currency=self.company.currency_id, product=variant)["total_included"]
        return self._decimal(price)

    def _decimal(self, value):
        return str(float_round(value or 0.0, precision_rounding=self.company.currency_id.rounding))

    # ------------------------------------------------------------------
    # Metafields
    # ------------------------------------------------------------------
    def metafields(self):
        """Return (metafields to set, (namespace, key) of mapped metafields that are empty)."""
        to_set, empty = [], []
        maps = self.template.env["shopify.metafield.map"].sudo().search([])
        for mapping in maps:
            value = self.metafield_value(mapping)
            if value in (None, False, ""):
                empty.append((mapping.namespace, mapping.key))
            else:
                to_set.append({"namespace": mapping.namespace, "key": mapping.key,
                               "type": mapping.metafield_type, "value": value})
        return to_set, empty

    def metafield_value(self, mapping):
        tmpl = self.template
        if mapping.condition_field_id and not tmpl[mapping.condition_field_id.name]:
            return False
        field = tmpl._fields.get(mapping.field_id.name)
        if not field:
            return False
        raw = tmpl[field.name]
        kind = mapping.metafield_type

        if kind == "list.product_reference":
            gids = [p.shopify_product_id for p in raw if p.shopify_product_id] if field.type.endswith("2many") else []
            return json.dumps(gids) if gids else False
        if kind == "boolean":
            return "true" if raw else "false"
        if field.type in ("many2one", "one2many", "many2many"):
            if not raw:
                return False
            if raw._name == "res.partner":
                text = "\n".join(self.partner_lines(p) for p in raw)
            else:
                text = ", ".join(raw.mapped("display_name"))
        elif field.type == "html":
            if is_html_empty(raw):
                return False
            if kind == "rich_text_field":
                return html_to_rich_text(raw)
            text = html2plaintext(raw)
        else:
            text = raw

        if kind == "dimension":
            return json.dumps({"value": float(raw), "unit": mapping.dimension_unit or "cm"}) if raw else False
        if kind == "number_integer":
            return str(int(raw)) if raw else False
        if kind == "number_decimal":
            return str(float(raw)) if raw else False
        if kind == "json":
            return json.dumps(raw) if raw else False
        if kind == "rich_text_field":
            return json.dumps({"type": "root", "children": [
                {"type": "paragraph", "children": [{"type": "text", "value": str(text)}]}]}) if text else False
        if not text:
            return False
        text = str(text).strip()
        if kind == "single_line_text_field":
            text = " ".join(text.split())
        return text or False

    @staticmethod
    def partner_lines(partner):
        address = partner._display_address(without_company=True) if partner else ""
        lines = [partner.name] + [line for line in address.splitlines() if line.strip()] + [partner.email]
        return "\n".join(line.strip() for line in lines if line)
