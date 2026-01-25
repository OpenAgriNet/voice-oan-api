# Bharat Vistaar API Documentation

## Overview

The Bharat Vistaar Chat Completion API provides an OpenAI-compatible interface for interacting with the Bharat Vistaar AI Voice Assistant. This API supports both streaming and non-streaming responses, making it easy to integrate with existing OpenAI-compatible clients.

**Base URL**: `https://dev-vistaar.da.gov.in`
**API Prefix**: `/api`
**Endpoint**: `/api/v1/chat/completions`

---

## Authentication

The API uses custom headers for authentication and session management. All requests must include the following headers:

| Header Name      | Type   | Required | Description                                                               |
| ---------------- | ------ | -------- | ------------------------------------------------------------------------- |
| `X-Tenant-ID`  | string | Yes      | Tenant identifier for multi-tenancy support                               |
| `X-User-ID`    | string | Yes      | Unique identifier for the user making the request                         |
| `X-Session-ID` | string | Yes      | Session identifier for maintaining conversation context                   |
| `X-Language`   | string | No       | Language code for the response. Supported:`en`, `hi`. Default: `hi` |

---

## Endpoint

### Create Chat Completion

**POST** `/api/v1/chat/completions`

Creates a completion for the chat message. Supports both streaming and non-streaming responses.

---

## Request

### Headers

```http
Content-Type: application/json
X-Tenant-ID: your-tenant-id
X-User-ID: your-user-id
X-Session-ID: your-session-id
X-Language: en
```

### Request Body

The request body is a JSON object with the following fields:

| Field        | Type    | Required | Default                 | Description                                            |
| ------------ | ------- | -------- | ----------------------- | ------------------------------------------------------ |
| `model`    | string  | No       | `bharatvistaar-voice` | The model to use for completion                        |
| `messages` | array   | Yes      | -                       | Array of message objects representing the conversation |
| `stream`   | boolean | No       | `true`                | Whether to stream the response                         |

### Message Object

Each message in the `messages` array must have the following structure:

| Field       | Type   | Required | Description                                                                        |
| ----------- | ------ | -------- | ---------------------------------------------------------------------------------- |
| `role`    | string | Yes      | The role of the message sender. Must be one of:`system`, `user`, `assistant` |
| `content` | string | Yes      | The content of the message                                                         |

### Example Request (Non-Streaming)

```bash
curl --location 'https://dev-vistaar.da.gov.in/api/v1/chat/completions' \
--header 'Content-Type: application/json' \
--header 'X-Tenant-ID: tenant-123' \
--header 'X-User-ID: user-456' \
--header 'X-Session-ID: 550e8400-e29b-41d4-a716-446655440001' \
--header 'X-Language: en' \
--data '{
    "model": "bharatvistaar-voice",
    "messages": [
        {
            "role": "user",
            "content": "What is PM-KISAN scheme?"
        }
    ],
    "stream": false
}'
```

### Example Request (Streaming)

```bash
curl --location 'https://dev-vistaar.da.gov.in/api/v1/chat/completions' \
--header 'Content-Type: application/json' \
--header 'X-Tenant-ID: tenant-123' \
--header 'X-User-ID: user-456' \
--header 'X-Session-ID: 550e8400-e29b-41d4-a716-446655440002' \
--header 'X-Language: en' \
--data '{
    "model": "bharatvistaar-voice",
    "messages": [
        {
            "role": "user",
            "content": "What is PM-KISAN scheme?"
        }
    ],
    "stream": true
}'
```

### Example Request (With Conversation History)

```bash
curl --location 'https://dev-vistaar.da.gov.in/api/v1/chat/completions' \
--header 'Content-Type: application/json' \
--header 'X-Tenant-ID: tenant-123' \
--header 'X-User-ID: user-456' \
--header 'X-Session-ID: 550e8400-e29b-41d4-a716-446655440003' \
--header 'X-Language: en' \
--data '{
    "model": "bharatvistaar-voice",
    "messages": [
        {
            "role": "user",
            "content": "What is PM-KISAN scheme?"
        },
        {
            "role": "assistant",
            "content": "PM-KISAN is a government scheme..."
        },
        {
            "role": "user",
            "content": "How do I apply for it?"
        }
    ],
    "stream": true
}'
```

### Example Request (Hindi)

```bash
curl --location 'https://dev-vistaar.da.gov.in/api/v1/chat/completions' \
--header 'Content-Type: application/json' \
--header 'X-Tenant-ID: tenant-123' \
--header 'X-User-ID: user-456' \
--header 'X-Session-ID: 550e8400-e29b-41d4-a716-446655440004' \
--header 'X-Language: hi' \
--data '{
    "model": "bharatvistaar-voice",
    "messages": [
        {
            "role": "user",
            "content": "PM-KISAN योजना क्या है?"
        }
    ],
    "stream": true
}'
```

---

## Response

### Non-Streaming Response

When `stream` is set to `false`, the API returns a complete JSON response:

**Status Code**: `200 OK`

**Response Body**:

```json
{
    "id": "chatcmpl-1234567890-abc123",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "bharatvistaar-voice",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "PM-KISAN (Pradhan Mantri Kisan Samman Nidhi) is a government scheme..."
            },
            "finish_reason": "stop"
        }
    ],
    "usage": {
        "prompt_tokens": 15,
        "completion_tokens": 120,
        "total_tokens": 135
    }
}
```

### Streaming Response

When `stream` is set to `true`, the API returns a Server-Sent Events (SSE) stream.

**Status Code**: `200 OK`
**Content-Type**: `text/event-stream`

**Response Format**:

The response is streamed as Server-Sent Events. Each event is a JSON object prefixed with `data: ` and followed by two newlines (`\n\n`).

**Stream Chunk Format**:

```json
data: {"id":"chatcmpl-1234567890-abc123","object":"chat.completion.chunk","created":1234567890,"model":"bharatvistaar-voice","choices":[{"index":0,"delta":{"content":"PM-KISAN"},"finish_reason":null}]}

data: {"id":"chatcmpl-1234567890-abc123","object":"chat.completion.chunk","created":1234567890,"model":"bharatvistaar-voice","choices":[{"index":0,"delta":{"content":" (Pradhan Mantri"},"finish_reason":null}]}

data: {"id":"chatcmpl-1234567890-abc123","object":"chat.completion.chunk","created":1234567890,"model":"bharatvistaar-voice","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

**Stream End Marker**:

The stream ends with:

```
data: [DONE]
```

---

## Error Responses

### 400 Bad Request

Returned when the request is invalid.

**Example**:

```json
{
    "detail": "messages field is required"
}
```

**Common Error Messages**:

- `"messages field is required"` - The messages array is missing or empty
- `"At least one user message is required"` - No user messages found in the messages array
- `"Invalid language code 'xx'. Supported languages: en, hi"` - Invalid language code provided

### 500 Internal Server Error

Returned when an unexpected server error occurs.

**Example**:

```json
{
    "detail": "Internal server error"
}
```

---

## Language Support

The API supports two languages:

- **English (`en`)** - English language support
- **Hindi (`hi`)** - Hindi language support (default)

Set the `X-Language` header to specify the desired response language. The API will automatically handle translation if needed.

---

## Rate Limits

The API enforces rate limits to ensure fair usage. Current limits:

- **Requests per minute**: 1000 (configurable)

Rate limit headers are included in responses:

```http
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 999
X-RateLimit-Reset: 1234567890
```

---

## Best Practices

### 1. Session Management

- Use a consistent `X-Session-ID` for a conversation thread
- Generate a new session ID for new conversations
- Session IDs should be UUIDs or unique identifiers

### 2. Error Handling

- Always check for error responses
- Implement retry logic for transient errors
- Handle streaming errors gracefully

### 3. Streaming vs Non-Streaming

- Use **streaming** (`stream: true`) for better user experience and faster time-to-first-token
- Use **non-streaming** (`stream: false`) when you need the complete response before processing

### 4. Message History

- Include conversation history in the `messages` array for context
- The API automatically maintains session history, but including recent messages can improve responses

### 5. Language Selection

- Set `X-Language` header based on user preference
- Ensure consistency across requests in the same session
