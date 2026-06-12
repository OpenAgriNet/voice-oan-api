from pydantic_ai import Agent

# OpenTelemetry-based instrumentation. Adds pydantic-ai–level OTel spans on top
# of the manual Langfuse spans. This is what makes tool calls appear in
# Langfuse automatically during streaming, without needing
# event_stream_handler (which would conflict with stream_text()).
Agent.instrument_all()
