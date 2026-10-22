---
name: mandi
description: Commodity lookup and mandi prices for a confirmed place and date.
tools:
- get_mandi_prices
- search_commodity
sections:
  prices:
    file: prices/mandi_{language}.md
  routing_get_mandi_prices:
    file: routing_get_mandi_prices/routing_get_mandi_prices_{language}.md
---

# Mandi

Edit the localized files named above to change this workflow. Tool names refer to
the shared Python registry; listing a tool in another skill never duplicates its
schema or implementation. Loading policy is configured separately.
