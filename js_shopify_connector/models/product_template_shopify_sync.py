import base64
import hashlib
import mimetypes

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.mimetypes import guess_mimetype

from ..services.product_payload import ProductPayloadBuilder
from ..services.shopify_client import ShopifyError


PRODUCT_SET_MUTATION = """
mutation ($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(input: $input, identifier: $identifier, synchronous: true) {
    product {
      id
      handle
      variants(first: 250) { nodes { id sku inventoryItem { id } } }
    }
    userErrors { field message code }
  }
}
"""

STAGED_UPLOADS_MUTATION = """
mutation ($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
"""

FILE_CREATE_MUTATION = """
mutation ($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files { id fileStatus }
    userErrors { field message code }
  }
}
"""

METAFIELDS_DELETE_MUTATION = """
mutation ($metafields: [MetafieldIdentifierInput!]!) {
  metafieldsDelete(metafields: $metafields) {
    deletedMetafields { key namespace }
    userErrors { field message }
  }
}
"""

INVENTORY_SET_MUTATION = """
mutation ($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { id }
    userErrors { field message code }
  }
}
"""

INVENTORY_BATCH = 250  # Shopify limit of quantities per inventorySetQuantities call

INVENTORY_ACTIVATE_MUTATION = """
mutation ($inventoryItemId: ID!, $locationId: ID!) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId) {
    inventoryLevel { id }
    userErrors { field message }
  }
}
"""

ARCHIVE_MUTATION = """
mutation ($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id status }
    userErrors { field message }
  }
}
"""

PUBLISH_MUTATION = """
mutation ($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}
"""


class ProductTemplate(models.Model):
    _inherit = "product.template"

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_shopify_publish(self):
        """Create or update the products in Shopify, with all their variants."""
        self.check_access("write")
        store = self.env["shopify.store"].sudo()._get_active_store()
        if not store:
            raise UserError(_("Set up the Shopify store first (Sales > Configuration > Shopify > Store)."))
        if len(self) == 1 and self.active and self.shopify_enabled:
            issues = self._get_shopify_publish_issues()
            if issues:
                raise UserError(_("Fix these before publishing to Shopify:\n\n%s",
                                  "\n".join("• " + issue for issue in issues)))
        done = failed = 0
        for template in self:
            ok = template._shopify_sync(store) if template.shopify_product_id else template._shopify_publish(store)
            if ok:
                done += 1
            else:
                failed += 1
        if len(self) == 1:
            if failed:
                return self._shopify_notify(_("Shopify: %s", self.shopify_sync_error), "danger")
            return self._shopify_notify(_("%s is in Shopify.", self.display_name))
        if failed:
            return self._shopify_notify(_(
                "%(done)s product(s) sent to Shopify, %(failed)s failed. "
                "Use the Shopify Sync Error filter to see why.", done=done, failed=failed), "warning")
        return self._shopify_notify(_("%s product(s) sent to Shopify.", done))

    def action_shopify_open(self):
        self.ensure_one()
        store = self.env["shopify.store"].sudo()._get_active_store()
        if not (store and self.shopify_product_id):
            raise UserError(_("This product is not in Shopify yet."))
        shop = store.shop_domain.split(".")[0]
        numeric_id = self.shopify_product_id.rsplit("/", 1)[-1]
        return {"type": "ir.actions.act_url", "target": "new",
                "url": f"https://admin.shopify.com/store/{shop}/products/{numeric_id}"}

    def _shopify_notify(self, message, kind="success"):
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": kind, "message": message, "sticky": kind != "success",
                           "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    # ------------------------------------------------------------------
    # Publish one product
    # ------------------------------------------------------------------
    def _shopify_publish(self, store):
        """Send this product to Shopify. Returns True when done; on failure the error is
        stored on the product and in the sync log, and False is returned."""
        self.ensure_one()
        issues = self._get_shopify_publish_issues()
        if issues:
            self._shopify_set_error("\n".join(issues))
            return False
        try:
            self._shopify_send(store)
        except ShopifyError as error:
            self._shopify_set_error(str(error))
            return False
        self.sudo().write({"shopify_sync_state": "synced", "shopify_sync_error": False,
                           "shopify_last_synced": fields.Datetime.now()})
        return True

    def _shopify_set_error(self, message):
        self.sudo().write({"shopify_sync_state": "error", "shopify_sync_error": message})

    def _shopify_sync(self, store):
        """Bring a published product up to date: full update, or archive in Shopify when the
        product is archived in Odoo or Sell on Shopify was switched off."""
        self.ensure_one()
        if self.active and self.shopify_enabled:
            return self._shopify_publish(store)
        try:
            store._call("sync_product", ARCHIVE_MUTATION,
                        {"product": {"id": self.shopify_product_id, "status": "ARCHIVED"}},
                        product=self, mutation="productUpdate")
        except ShopifyError as error:
            self._shopify_set_error(str(error))
            return False
        self.sudo().write({"shopify_sync_state": "synced", "shopify_sync_error": False,
                           "shopify_last_synced": fields.Datetime.now()})
        return True

    def _shopify_send(self, store):
        is_new = not self.shopify_product_id
        media_files = self._shopify_upload_images(store)
        builder = ProductPayloadBuilder(self, store, media_files)
        payload = builder.build()
        # Variants new in Shopify get their stock on creation; existing ones are updated after.
        existing_variants = builder.variants().filtered("shopify_variant_id")
        locations = self._shopify_location_quantities(store, builder.variants() - existing_variants)
        for variant, variant_vals in zip(builder.variants(), payload["variants"]):
            if "id" not in variant_vals and locations.get(variant.id):
                variant_vals["inventoryQuantities"] = [
                    {"locationId": location_id, "name": "available", "quantity": quantity}
                    for location_id, quantity in locations[variant.id].items()]

        identifier = {"id": self.shopify_product_id} if self.shopify_product_id else {"handle": self.seo_name}
        result = store._call("publish_product" if is_new else "sync_product", PRODUCT_SET_MUTATION,
                             {"input": payload, "identifier": identifier}, product=self, mutation="productSet")
        self._shopify_write_ids(result["product"])

        _set, empty = builder.metafields()
        if empty and not is_new:
            self._shopify_delete_metafields(store, empty)
        existing_variants._shopify_push_stock(store)
        self._shopify_publish_on_channels(store)

    def _shopify_write_ids(self, product):
        by_sku = {node["sku"]: node for node in product["variants"]["nodes"] if node.get("sku")}
        self.sudo().shopify_product_id = product["id"]
        for variant in self.product_variant_ids:
            node = by_sku.get(variant.default_code)
            if node:
                variant.sudo().write({"shopify_variant_id": node["id"],
                                      "shopify_inventory_item_id": (node.get("inventoryItem") or {}).get("id")})

    def _shopify_delete_metafields(self, store, keys):
        store._call("sync_product", METAFIELDS_DELETE_MUTATION, {"metafields": [
            {"ownerId": self.shopify_product_id, "namespace": namespace, "key": key}
            for namespace, key in keys]}, product=self, mutation="metafieldsDelete")

    def _shopify_publish_on_channels(self, store):
        publications = store.publication_ids.filtered("publish_products")
        if publications:
            store._call("publish_product", PUBLISH_MUTATION, {
                "id": self.shopify_product_id,
                "input": [{"publicationId": pub.shopify_id} for pub in publications]},
                product=self, mutation="publishablePublish")

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------
    def _shopify_upload_images(self, store):
        """Upload new or changed images. Returns {(res_model, res_id, res_field): Shopify file ID}."""
        Media = self.env["shopify.media"].sudo()
        builder = ProductPayloadBuilder(self, store)
        files, to_upload = {}, []
        for model, record, field, alt in builder.images():
            content = base64.b64decode(record[field])
            checksum = hashlib.sha1(content).hexdigest()
            media = Media.search([("store_id", "=", store.id), ("res_model", "=", model),
                                  ("res_id", "=", record.id), ("res_field", "=", field)], limit=1)
            if media and media.checksum == checksum:
                files[(model, record.id, field)] = media.shopify_file_id
            else:
                to_upload.append((model, record, field, alt, content, checksum, media))
        if not to_upload:
            return files

        uploads = []
        for position, (model, record, field, alt, content, checksum, media) in enumerate(to_upload, start=1):
            mimetype = guess_mimetype(content, default="image/png")
            extension = mimetypes.guess_extension(mimetype) or ".png"
            uploads.append({"filename": f"{self.seo_name or 'product'}-{model.split('.')[-1]}-{record.id}"
                                        f"-{position}{extension}", "mimetype": mimetype})
        data = store._call("upload_media", STAGED_UPLOADS_MUTATION, {"input": [
            {"resource": "IMAGE", "filename": u["filename"], "mimeType": u["mimetype"], "httpMethod": "POST"}
            for u in uploads]}, product=self, mutation="stagedUploadsCreate")
        targets = data["stagedTargets"]

        client = store._client()
        sources = []
        for target, upload, item in zip(targets, uploads, to_upload):
            sources.append(client.upload_staged(target, upload["filename"], item[4], upload["mimetype"]))

        data = store._call("upload_media", FILE_CREATE_MUTATION, {"files": [
            {"originalSource": source, "alt": item[3], "contentType": "IMAGE", "filename": upload["filename"]}
            for source, item, upload in zip(sources, to_upload, uploads)]}, product=self, mutation="fileCreate")
        created = data["files"]

        for node, (model, record, field, alt, content, checksum, media) in zip(created, to_upload):
            vals = {"checksum": checksum, "shopify_file_id": node["id"]}
            if media:
                media.write(vals)
            else:
                Media.create(dict(vals, store_id=store.id, res_model=model, res_id=record.id, res_field=field))
            files[(model, record.id, field)] = node["id"]
        return files

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------
    def _shopify_location_quantities(self, store, variants):
        """{variant id: {Shopify location ID: available quantity}} for mapped warehouses."""
        warehouses = self.env["stock.warehouse"].sudo().search([
            ("shopify_location_id.store_id", "=", store.id), ("shopify_location_id.active", "=", True),
            ("company_id", "=", store.company_id.id)])
        result = {}
        for variant in variants.filtered("is_storable"):
            per_location = {}
            for warehouse in warehouses:
                qty = variant.with_context(warehouse_id=warehouse.id).free_qty
                location_id = warehouse.shopify_location_id.shopify_id
                per_location[location_id] = per_location.get(location_id, 0) + max(0, int(qty))
            result[variant.id] = per_location
        return result


class ProductProduct(models.Model):
    _inherit = "product.product"

    def _shopify_push_stock(self, store):
        """Send the available quantity of these variants to every mapped Shopify location."""
        variants = self.filtered(lambda v: v.is_storable and v.shopify_inventory_item_id)
        if not variants:
            return
        quantities = variants.product_tmpl_id._shopify_location_quantities(store, variants)
        rows = [{"inventoryItemId": variant.shopify_inventory_item_id, "locationId": location_id,
                 "quantity": quantity}
                for variant in variants for location_id, quantity in quantities.get(variant.id, {}).items()]
        product = variants.product_tmpl_id[:1] if len(variants.product_tmpl_id) == 1 else False
        for start in range(0, len(rows), INVENTORY_BATCH):
            chunk = rows[start:start + INVENTORY_BATCH]
            try:
                self._shopify_set_quantities(store, chunk, product)
            except ShopifyError as error:
                if "not stocked" not in str(error).lower():
                    raise
                # A location linked after the variant was created: stock the items there first.
                for row in chunk:
                    store._call("sync_stock", INVENTORY_ACTIVATE_MUTATION, {
                        "inventoryItemId": row["inventoryItemId"], "locationId": row["locationId"]},
                        product=product, mutation="inventoryActivate")
                self._shopify_set_quantities(store, chunk, product)

    @api.model
    def _shopify_set_quantities(self, store, rows, product=False):
        # Newer API versions compare-and-set with changeFromQuantity (null = no check);
        # older ones use ignoreCompareQuantity.
        variables = {"input": {"name": "available", "reason": "correction",
                               "quantities": [dict(row, changeFromQuantity=None) for row in rows]}}
        try:
            store._call("sync_stock", INVENTORY_SET_MUTATION, variables, product=product,
                        mutation="inventorySetQuantities")
        except ShopifyError as error:
            if "changeFromQuantity" not in str(error):
                raise
            variables = {"input": {"name": "available", "reason": "correction", "ignoreCompareQuantity": True,
                                   "quantities": rows}}
            store._call("sync_stock", INVENTORY_SET_MUTATION, variables, product=product,
                        mutation="inventorySetQuantities")
