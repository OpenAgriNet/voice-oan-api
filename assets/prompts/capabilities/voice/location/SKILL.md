---
name: location
description: Resolve place names and coordinates for location-dependent workflows.
tools:
- forward_geocode
- reverse_geocode
sections:
  routing_reverse_geocode:
    file: routing_reverse_geocode/routing_reverse_geocode_{language}.md
---

# Location

Edit the localized files named above to change this workflow. Tool names refer to
the shared Python registry; listing a tool in another skill never duplicates its
schema or implementation. Loading policy is configured separately.
