{
    "name": "Shopify Connector",
    "summary": "Publish Odoo products and their variants to Shopify and import Shopify orders.",
    "description": """
Product data sent to Shopify, kept on Odoo products.

* Shopify tab on the product: Sell on Shopify, status, handle, SEO and product page data
* Handle = the product's standard SEO name, filled from the product name and locked once published
* Shopify IDs on the product and on each variant, written back by the connector
* Shopify taxonomy ID on eCommerce categories, Shopify location on warehouses
* "Check for Shopify" lists what is missing before a product can be published
* Publish to Shopify (product form or list): images, product, variants, metafields,
  stock per location and sales channels in one go; Shopify IDs written back
* Changes to published products mark them To Sync; a cron updates them every 15 minutes,
  archives them in Shopify when archived in Odoo, and sends stock changes every 5 minutes
* Shopify orders imported every 5 minutes as sales orders of the store's company: customer
  matched or created, lines matched by variant ID then SKU at the prices charged, discounts
  and shipping lines; paid orders confirmed, pending ones kept as quotations, anything that
  does not match held back as "Needs attention"
* Paid orders invoiced and their Shopify payments registered; shipping rates mapped to delivery methods
* Validated deliveries sent back as Shopify fulfilments with tracking (partial deliveries too)
* Shopify cancellations and refunds followed: deliveries reduced or cancelled, credit notes with
  refund payments, return receipts for restocked items
* Sales > Configuration > Shopify: store connection (Admin GraphQL API), locations,
  sales channels, metafield mapping and the sync log
""",
    "version": "19.0.1.9.0",
    "category": "Sales/Sales",
    "author": "Codecrafters",
    "license": "LGPL-3",
    "depends": ["js_product_sku", "website_sale", "website_sale_stock", "stock_delivery"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/shopify_security.xml",
        "security/ir.model.access.csv",
        "data/shopify_metafield_map_data.xml",
        "views/shopify_store_views.xml",
        "views/product_template_views.xml",
        "views/product_product_views.xml",
        "views/product_public_category_views.xml",
        "views/stock_warehouse_views.xml",
        "data/ir_actions_server.xml",
        "data/ir_cron.xml",
        "data/shopify_order_data.xml",
        "views/sale_order_views.xml",
        "views/res_partner_views.xml",
        "views/account_payment_views.xml",
        "views/stock_picking_views.xml",
    ],
    "installable": True,
}
