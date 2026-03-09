# Feedback Flow — Voice OAN API

```mermaid
sequenceDiagram
    participant Farmer
    participant Agent (LLM)
    participant submit_feedback Tool
    participant Telemetry API

    Farmer->>Agent (LLM): Signals end of call<br/>("No more questions", "Thank you", "Goodbye")
    Agent (LLM)->>Farmer: Farewell message<br/>"Thank you for calling Bharat VISTAAR…<br/>I hope the information was useful for you."
    Note over Agent (LLM): end_interaction = false

    Agent (LLM)->>Farmer: Feedback prompt<br/>"Before we end the call, could you please share your feedback?<br/>Did you find this conversation helpful?<br/>If yes or no, please tell me briefly why."
    Farmer->>Agent (LLM): Gives feedback (yes/no + optional reason)

    Agent (LLM)->>submit_feedback Tool: submit_feedback(feedback_type, feedback_text)<br/>feedback_type = "like" (helpful) or "dislike" (not helpful)<br/>feedback_text = farmer's reason (optional)
    submit_feedback Tool->>submit_feedback Tool: Build Telemetry Event<br/>with session_id, feedbackType, feedbackText
    submit_feedback Tool->>Telemetry API: POST /telemetry<br/>{ session_id, feedbackType, feedbackText }
    Telemetry API-->>submit_feedback Tool: 200 OK / error (logged, non-fatal)
    submit_feedback Tool-->>Agent (LLM): "Feedback recorded."

    Agent (LLM)->>Farmer: Mandatory closing line (verbatim, never altered)
    Note over Agent (LLM): end_interaction = true
```
