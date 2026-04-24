#!/usr/bin/env python3
"""MCP server for searching Hong Kong property listings in the Causeway Bay Fashion Walk area."""

import asyncio
import json
import re
import urllib.parse
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

server = Server("causeway-bay-property")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# 28hse district IDs for Causeway Bay area
# data-value="5-23" → district_id=23 (Causeway Bay)
# district_id=22 = Happy Valley (adjacent, part of the Causeway Bay / Happy Valley grouping)
CAUSEWAY_BAY_DISTRICT_IDS = ["23"]  # 23 = Causeway Bay on 28hse

DEFAULT_MIN_M = 10   # HK$10M
DEFAULT_MAX_M = 20   # HK$20M


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_fashion_walk_properties",
            description=(
                "Search for residential properties for sale in the Fashion Walk / Causeway Bay "
                "area of Hong Kong. Defaults to HK$10M–20M. Returns live listings from 28hse.com."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "min_price_hkd_millions": {
                        "type": "number",
                        "description": "Minimum asking price in HK$ millions (e.g. 10 = HK$10M). Default: 10",
                    },
                    "max_price_hkd_millions": {
                        "type": "number",
                        "description": "Maximum asking price in HK$ millions (e.g. 20 = HK$20M). Default: 20",
                    },
                    "bedrooms": {
                        "type": "integer",
                        "description": "Filter by number of bedrooms (optional, 1–5)",
                    },
                    "min_sqft": {
                        "type": "number",
                        "description": "Minimum saleable area in sq ft (optional)",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number for pagination (default: 1, 15 results per page)",
                        "default": 1,
                    },
                    "include_happy_valley": {
                        "type": "boolean",
                        "description": "Also include Happy Valley (adjacent to Causeway Bay / Fashion Walk). Default: false",
                        "default": False,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_property_details",
            description="Fetch full details for a specific 28hse property listing by its URL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL of the 28hse property listing page",
                    }
                },
                "required": ["url"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    if name == "search_fashion_walk_properties":
        return await handle_search(arguments)
    elif name == "get_property_details":
        return await handle_details(arguments["url"])
    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Search handler
# ---------------------------------------------------------------------------

async def handle_search(args: dict) -> list[types.TextContent]:
    min_m = float(args.get("min_price_hkd_millions", DEFAULT_MIN_M))
    max_m = float(args.get("max_price_hkd_millions", DEFAULT_MAX_M))
    bedrooms = args.get("bedrooms")
    min_sqft = args.get("min_sqft")
    page = int(args.get("page", 1))
    include_hv = args.get("include_happy_valley", False)

    district_ids = list(CAUSEWAY_BAY_DISTRICT_IDS)
    if include_hv:
        district_ids.append("22")  # Happy Valley

    listings, total_count = await search_28hse(
        min_m, max_m, district_ids, bedrooms, min_sqft, page
    )

    total_pages = max(1, (total_count + 14) // 15)
    area_label = "Causeway Bay + Happy Valley" if include_hv else "Causeway Bay"

    lines: list[str] = [
        f"Properties for sale — {area_label}, Hong Kong (Fashion Walk area)",
        f"Price: HK${min_m:.0f}M – HK${max_m:.0f}M  |  Page {page}/{total_pages}  |  {total_count} total listing(s)",
        "",
    ]

    if not listings:
        lines.append("No listings found for the selected criteria.")
        lines.append("")
        lines.append("Try browsing directly:")
        lines.append(f"  https://www.28hse.com/en/buy?district_ids[]=23&price_low={int(min_m)}&price_high={int(max_m)}")
    else:
        for i, prop in enumerate(listings, start=(page - 1) * 15 + 1):
            lines.append(f"{i}. {prop.get('name', 'Unnamed')}")
            if prop.get("price"):
                lines.append(f"   Price:    {prop['price']}")
            if prop.get("area_saleable"):
                lines.append(f"   Size:     {prop['area_saleable']} (saleable)")
            if prop.get("area_gross"):
                lines.append(f"   Gross:    {prop['area_gross']}")
            if prop.get("price_per_sqft"):
                lines.append(f"   $/sqft:   {prop['price_per_sqft']}")
            if prop.get("address"):
                lines.append(f"   Address:  {prop['address']}")
            if prop.get("floor"):
                lines.append(f"   Floor:    {prop['floor']}")
            if prop.get("bedrooms"):
                lines.append(f"   Rooms:    {prop['bedrooms']}")
            if prop.get("features"):
                lines.append(f"   Features: {prop['features']}")
            if prop.get("url"):
                lines.append(f"   Link:     {prop['url']}")
            lines.append("")

        if total_pages > page:
            lines.append(f"(Use page={page+1} to see more listings)")

    return [types.TextContent(type="text", text="\n".join(lines))]


# ---------------------------------------------------------------------------
# 28hse API search
# ---------------------------------------------------------------------------

async def search_28hse(
    min_m: float,
    max_m: float,
    district_ids: list[str],
    bedrooms: int | None,
    min_sqft: float | None,
    page: int,
) -> tuple[list[dict], int]:
    """Call 28hse's internal search API and return (listings, total_count)."""

    api_headers = {
        **HEADERS,
        "Referer": "https://www.28hse.com/en/buy",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Build form payload
    payload: dict[str, Any] = {
        "buyRent": "buy",
        "page": str(page),
        "price_low": str(int(min_m)),
        "price_high": str(int(max_m)),
        "lang": "en",
    }
    # district_ids[] is multi-value
    district_payload = [("district_ids[]", did) for did in district_ids]

    if bedrooms:
        payload["noOfRoom[]"] = str(bedrooms)

    if min_sqft:
        payload["area_low"] = str(int(min_sqft))

    async with httpx.AsyncClient(headers=api_headers, follow_redirects=True, timeout=30) as client:
        # httpx handles lists via the params tuple approach
        form_data: list[tuple[str, str]] = list(payload.items()) + district_payload
        resp = await client.post(
            "https://www.28hse.com/en/property/dosearch",
            content=urllib.parse.urlencode(form_data),
        )
        resp.raise_for_status()

    result = resp.json()
    if result.get("status") != 1:
        raise RuntimeError(f"28hse API error: {result.get('msg', 'unknown error')}")

    html = result["data"]["results"]["resultContentHtml"]
    total_str = re.search(r"([\d,]+)\s+result", html)
    total_count = int(total_str.group(1).replace(",", "")) if total_str else 0

    soup = BeautifulSoup(html, "lxml")
    listings = [_parse_28hse_card(card) for card in soup.select(".property_item")]
    return [l for l in listings if l], total_count


def _parse_28hse_card(card: Tag) -> dict | None:
    prop: dict[str, str] = {"source": "28hse.com"}

    # Price
    price_el = card.select_one(".ui.right.floated.red.large.label, .ui.red.label")
    if price_el:
        prop["price"] = price_el.get_text(strip=True)

    # Building name + district from .district_area
    district_el = card.select_one(".district_area")
    if district_el:
        links = district_el.find_all("a")
        if len(links) >= 2:
            prop["address"] = links[0].get_text(strip=True)  # district
            prop["name"] = links[1].get_text(strip=True)     # estate/building
        elif links:
            prop["name"] = links[0].get_text(strip=True)
        unit_el = district_el.select_one(".unit_desc")
        if unit_el:
            prop["floor"] = unit_el.get_text(strip=True)

    # Saleable / gross area and price per sqft
    area_el = card.select_one(".areaUnitPrice")
    if area_el:
        area_text = area_el.get_text(" ", strip=True)
        # Saleable area
        m_s = re.search(r"Saleable Area[:\s]*([\d,]+\s*ft[²2]?)", area_text)
        if m_s:
            prop["area_saleable"] = m_s.group(1).strip()
        # Gross area
        m_g = re.search(r"Gross Area[:\s]*([\d,]+\s*ft[²2]?)", area_text)
        if m_g:
            prop["area_gross"] = m_g.group(1).strip()
        # Price per sqft (after @)
        m_p = re.search(r"@\s*([\d,]+)", area_text)
        if m_p:
            prop["price_per_sqft"] = f"HK${m_p.group(1)}/ft²"

    # Bedrooms / features from tagLabels
    tags_el = card.select_one(".tagLabels")
    if tags_el:
        tag_texts = [t.get_text(strip=True) for t in tags_el.select(".ui.label")]
        if tag_texts:
            prop["bedrooms"] = tag_texts[0]  # first tag is usually room count
            if len(tag_texts) > 1:
                prop["features"] = ", ".join(tag_texts[1:3])

    # Detail page URL
    link = card.select_one("a.detail_page")
    if link and link.get("href"):
        href = str(link["href"])
        if href.startswith("/"):
            href = "https://www.28hse.com" + href
        prop["url"] = href

    return prop if (prop.get("name") or prop.get("price")) else None


# ---------------------------------------------------------------------------
# Property detail page
# ---------------------------------------------------------------------------

async def handle_details(url: str) -> list[types.TextContent]:
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    lines = [f"Property Details\nSource: {url}\n"]

    h1 = soup.find("h1")
    if h1:
        lines.append(f"Title: {h1.get_text(strip=True)}\n")

    # Price
    price_el = soup.select_one(".ui.red.label, [class*='price']")
    if price_el:
        lines.append(f"Price: {price_el.get_text(strip=True)}")

    # Area info
    area_el = soup.select_one(".areaUnitPrice, [class*='area']")
    if area_el:
        lines.append(f"Area: {area_el.get_text(' ', strip=True)}")

    # Key-value pairs from detail tables
    for dt in soup.select("dt, th"):
        label = dt.get_text(strip=True)
        dd = dt.find_next_sibling(["dd", "td"])
        if dd and label and len(label) < 60:
            lines.append(f"{label}: {dd.get_text(strip=True)}")

    # Tags
    tags = [t.get_text(strip=True) for t in soup.select(".tagLabels .ui.label")]
    if tags:
        lines.append(f"Features: {', '.join(tags)}")

    # Description / remarks
    desc = soup.select_one(".description, [class*='remark'], [class*='detail-desc']")
    if desc:
        lines.append(f"\nRemarks: {desc.get_text(' ', strip=True)[:600]}")

    # Meta description fallback
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):  # type: ignore[union-attr]
        lines.append(f"\nMeta: {meta['content'][:400]}")  # type: ignore[index]

    return [types.TextContent(type="text", text="\n".join(lines))]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
