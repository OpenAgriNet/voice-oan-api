---
name: weather
description: Weather forecasts and district IMD agromet advisories.
tools:
- weather_forecast
sections:
  forecast:
    file: forecast/weather_{language}.md
  routing_weather_forecast:
    file: routing_weather_forecast/routing_weather_forecast_{language}.md
---

# Weather

Edit the localized files named above to change this workflow. Tool names refer to
the shared Python registry; listing a tool in another skill never duplicates its
schema or implementation. Loading policy is configured separately.
