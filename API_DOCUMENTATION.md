# API Documentation - Bharat Vistaar AI Voice API

## Overview

Bharat Vistaar AI Voice API is an AI-powered voice assistant API that provides information about government agricultural schemes, helps check scheme status, and assists in raising grievances.

**Base URL**: `https://dev-vistaar.da.gov.in`
**API Prefix**: `/api`

## Endpoints

### Voice Chat Endpoint

#### Chat with Voice Assistant

**GET** `/api/voice/`

Streams AI-generated responses for queries related to government agricultural schemes, scheme status checks, and grievance management. Supports conversation context through session management.

**Query Parameters:**

| Parameter       | Type   | Required | Default        | Description                                     |
| --------------- | ------ | -------- | -------------- | ----------------------------------------------- |
| `query`       | string | Yes      | -              | The user's chat query/question                  |
| `session_id`  | string | No       | Auto-generated | Session ID for maintaining conversation context |
| `source_lang` | string | No       | `hi`         | Source language code (hi, en)                   |
| `target_lang` | string | No       | `hi`         | Target language code for response (hi, en)      |
| `user_id`     | string | No       | `anonymous`  | User identifier                                 |

**Response:**

Streaming response with `text/event-stream` media type. The response is streamed in real-time as the AI generates the answer.

**Example Request:**

```bash
curl --location 'https://dev-vistaar.da.gov.in/api/voice/?session_id=550e8400-e29b-41d4-a716-446655440004&query=what%20is%20kcc&source_lang=en&target_lang=en'
```

**Example Request (Hindi):**

```bash
curl --location 'https://dev-vistaar.da.gov.in/api/voice/?session_id=550e8400-e29b-41d4-a716-446655440004&query=PM-KISAN%20scheme%20kya%20hai&source_lang=hi&target_lang=hi'
```

**Example Request (Without Session ID):**

```bash
curl --location 'https://dev-vistaar.da.gov.in/api/voice/?query=what%20is%20PM-KISAN%20scheme&source_lang=en&target_lang=en'
```

**Example Response (Streaming):**

Streaming response with Server-Sent Events format containing AI-generated response chunks.

**Status Codes:**

- `200 OK` - Request processed successfully
- `400 Bad Request` - Invalid request parameters
- `500 Internal Server Error` - Server error

**Features:**

- **Streaming Responses**: Real-time streaming of AI-generated responses
- **Multi-language Support**: Supports Hindi (hi) and English (en)
- **Conversation History**: Automatically maintains and uses conversation history

---

## Use Cases

The API supports the following use cases through the voice assistant:

### Government Schemes

The system provides information about **8 government agricultural schemes**:

1. **KCC** - Kisan Credit Card
2. **PM-KISAN** - Pradhan Mantri Kisan Samman Nidhi
3. **PMFBY** - Pradhan Mantri Fasal Bima Yojana
4. **SHC** - Soil Health Card
5. **PMKSY** - Pradhan Mantri Krishi Sinchayee Yojana
6. **SATHI** - Seed Authentication, Traceability & Holistic Inventory
7. **PMASHA** - Pradhan Mantri Annadata Aay Sanrakshan Abhiyan
8. **AIF** - Agriculture Infrastructure Fund

**Use Case Examples:**

1. **Get Scheme Information**

   - Query: "What is PM-KISAN scheme?"
   - Query: "Tell me about KCC benefits"
   - Query: "What are the eligibility criteria for PMFBY?"
2. **General Scheme Inquiry**

   - Query: "What government schemes are available for farmers?"
   - Query: "List all agricultural schemes"

### Scheme Status Checks

The API supports status checks for specific schemes:

#### 1. PM-KISAN Status Check

**Two-step process:**

**Step 1: Initiate Status Check**

- Requires: Registration number (Aadhaar number or mobile number)
- Returns: Transaction ID and OTP sent to registered mobile

**Step 2: Verify with OTP**

- Requires: Transaction ID and OTP
- Returns: PM-KISAN status details including payment history

**Use Case Examples:**

- Query: "Check my PM-KISAN status"
- Query: "What is my PM-KISAN payment status?"
- Query: "When will I receive my next PM-KISAN payment?"

#### 2. PMFBY Status Check

- Requires: registered phone number
- Returns: PMFBY insurance status and policy details

**Use Case Examples:**

- Query: "Check my PMFBY insurance status"
- Query: "What is my crop insurance status?"

#### 3. SHC (Soil Health Card) Status Check

**Single-step process:**

- Requires: phone number registered with the Soil Health Card
- Returns: Soil Health Card status and details

**Use Case Examples:**

- Query: "Check my Soil Health Card status"
- Query: "What is my SHC status?"
- Query: "When will I receive my Soil Health Card?"

### Grievance Management

The API supports grievance submission and status tracking:

#### 1. Submit Grievance

**Process:**

- Requires: Identity number (Aadhaar/registration no), grievance description, grievance type
- Returns: Grievance reference number and confirmation

**Supported Grievance Types:**

- Payment issues
- Registration problems
- Scheme-related complaints
- Application issues

**Use Case Examples:**

- Query: "I want to file a complaint about PM-KISAN payment"
- Query: "Submit a grievance about my registration"
- Query: "I have an issue with my scheme application"

#### 2. Check Grievance Status

**Process:**

- Requires: Aadhaar/registration no
- Returns: List of grievances with their current status

**Use Case Examples:**

- Query: "Check my grievance status"
- Query: "What is the status of my complaint?"
- Query: "Show me all my grievances"

### Follow-up Questions

- The system maintains conversation context, allowing natural follow-up questions
- Example: After asking about PM-KISAN, user can ask "How do I apply?" without repeating context

---

## Request/Response Models

### ChatRequest

Request model containing the following fields:

**Field Descriptions:**

- `query`: The user's question or message (required)
- `session_id`: Unique identifier for maintaining conversation context. If not provided, a new UUID is generated (optional)
- `source_lang`: Language code of the input query (`hi`, `en`) (default: 'hi')
- `target_lang`: Language code for the response (`hi`, `en`) (default: 'hi')
- `user_id`: Identifier for the user making the request (default: 'anonymous')

### Response Format

The voice endpoint returns a streaming response with Server-Sent Events (SSE) format. Each data line contains a chunk of the AI-generated response.

---

## Error Handling

### Error Response Format

Error responses include status, message, error_code, and optional details fields.

### Common Error Codes

- `400 Bad Request` - Invalid request parameters
- `401 Unauthorized` - Missing or invalid authentication token
- `503 Service Unavailable` - Service dependencies unavailable (e.g., Redis cache)
- `500 Internal Server Error` - Unexpected server error

---

## Language Support

The API supports two languages:

- **Hindi (hi)** - Hindi language support
- **English (en)** - English language support

Users can query in one language and receive responses in another by setting different `source_lang` and `target_lang` parameters.
