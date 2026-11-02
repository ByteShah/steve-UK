"""Odoo HTML -> Shopify rich_text_field JSON (paragraphs, headings, lists, bold, italic, links)."""
import json
from copy import deepcopy

from lxml import html as lxml_html

HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCKS = {"p", "div", "blockquote", "section"} | set(HEADINGS)


def _inline(element, bold=False, italic=False):
    """Text nodes of an element and its inline children."""
    nodes = []

    def text(value, b, i):
        if value and value.strip("\n"):
            node = {"type": "text", "value": value}
            if b:
                node["bold"] = True
            if i:
                node["italic"] = True
            nodes.append(node)

    text(element.text, bold, italic)
    for child in element:
        tag = child.tag if isinstance(child.tag, str) else ""
        if tag == "br":
            text("\n", bold, italic)
        elif tag == "a" and child.get("href"):
            nodes.append({"type": "link", "url": child.get("href"),
                          "children": _inline(child, bold, italic) or [{"type": "text", "value": child.get("href")}]})
        else:
            nodes.extend(_inline(child, bold or tag in ("b", "strong"), italic or tag in ("i", "em")))
        text(child.tail, bold, italic)
    return nodes


def _blocks(element):
    blocks = []
    loose = []  # inline content sitting directly in a container

    def flush():
        if loose:
            blocks.append({"type": "paragraph", "children": loose.copy()})
            loose.clear()

    if element.text and element.text.strip():
        loose.append({"type": "text", "value": element.text})
    for child in element:
        tag = child.tag if isinstance(child.tag, str) else ""
        if tag in HEADINGS:
            flush()
            children = _inline(child)
            if children:
                blocks.append({"type": "heading", "level": HEADINGS[tag], "children": children})
        elif tag in ("ul", "ol"):
            flush()
            items = [{"type": "list-item", "children": _inline(li) or [{"type": "text", "value": ""}]}
                     for li in child if li.tag == "li"]
            if items:
                blocks.append({"type": "list", "listType": "ordered" if tag == "ol" else "unordered",
                               "children": items})
        elif tag in BLOCKS:
            flush()
            nested = [c for c in child if isinstance(c.tag, str) and c.tag in BLOCKS | {"ul", "ol"}]
            if nested:
                blocks.extend(_blocks(child))
            else:
                children = _inline(child)
                if children:
                    blocks.append({"type": "paragraph", "children": children})
        else:
            # Inline element (b, i, a, span...) directly in the container.
            wrapper = lxml_html.Element("span")
            copy = deepcopy(child)
            copy.tail = None
            wrapper.append(copy)
            loose.extend(_inline(wrapper))
        if child.tail and child.tail.strip():
            loose.append({"type": "text", "value": child.tail})
    flush()
    return blocks


def html_to_rich_text(html):
    """Return the Shopify rich text JSON string, or False when the HTML has no text."""
    if not html or not str(html).strip():
        return False
    root = lxml_html.fragment_fromstring(str(html), create_parent="div")
    blocks = _blocks(root)
    if not blocks:
        return False
    return json.dumps({"type": "root", "children": blocks})
