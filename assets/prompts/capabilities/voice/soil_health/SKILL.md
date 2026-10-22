---
name: soil_health
description: Soil Health Card status and report lookup.
tools:
- check_shc_status
sections:
  results:
    file: results/soil_health_results_{language}.md
  crop_suitability:
    file: crop_suitability/crop_suitability_{language}.md
  routing_check_shc_status:
    file: routing_check_shc_status/routing_check_shc_status_{language}.md
---

# Soil Health

Edit the localized files named above to change this workflow. Tool names refer to
the shared Python registry; listing a tool in another skill never duplicates its
schema or implementation. Loading policy is configured separately.
