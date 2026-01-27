# Hindi Language Support Documentation

## Overview

The Voice API supports Hindi (`hi`) as both source and target language, allowing users to interact with the agricultural voice assistant (Vasudha) in Hindi. 

```
GET /api/voice/
```

## Request Structure

### Example Request

```bash
curl --location 'https://vistaar-dev.mahapocra.gov.in/api/voice/?session_id=a1b2c3d4e5f6&query=नासिक में मौसम कैसा है&source_lang=hi&target_lang=hi'
```

### URL Decoded Parameters

- **session_id**: `a1b2c3d4e5f6`
- **query**: `नासिक में मौसम कैसा है` (What's the weather in Nashik?)
- **source_lang**: `hi` (Hindi)
- **target_lang**: `hi` (Hindi)

## Request Parameters

| Parameter       | Type   | Required | Valid Values                                                         |
| --------------- | ------ | -------- | -------------------------------------------------------------------- |
| `query`       | string | Yes      | Any text                                                             |
| `session_id`  | string | No       | Any string (auto-generated if not provided)                          |
| `source_lang` | string | No       | `hi` (Hindi), `mr` (Marathi), `en` (English) - Default: `mr` |
| `target_lang` | string | No       | Any language code - Default:`mr`                                   |
| `user_id`     | string | No       | Any string - Default:`anonymous`                                   |
| `provider`    | string | No       | `RAYA` or `None`                                     |
| `process_id`  | string | No       | Any string                                                           |

## Hindi Language Support Details

### Language Code

- **Code**: `hi`
- **Language**: Hindi (हिंदी)

### Response Format

- **Streaming Response**: The API returns a Server-Sent Events (SSE) stream
- **Content Type**: `text/event-stream`
- **Response Language**: Hindi (when `target_lang=hi`)

## Example Hindi Questions

Common agricultural queries in Hindi:

- **Weather Query**: `नासिक में मौसम कैसा है` (What's the weather in Nashik?)
- **Pest Control Query**: `सोयाबीन में कीट नियंत्रण कैसे करें` (How to control pests in soyabean?)

## Example Usage

### Weather Query Example

```bash
curl --location 'https://vistaar-dev.mahapocra.gov.in/api/voice/?session_id=a1b2c3d4e5f6&query=नासिक में मौसम कैसा है&source_lang=hi&target_lang=hi'
```

**URL Decoded:**

- **query**: `नासिक में मौसम कैसा है`
- **session_id**: `a1b2c3d4e5f6`

### Pest Control Query Example

```bash
curl --location 'https://vistaar-dev.mahapocra.gov.in/api/voice/?session_id=a1b2c3d4e5f6&query=सोयाबीन में कीट नियंत्रण कैसे करें&source_lang=hi&target_lang=hi'
```

**URL Decoded:**

- **query**: `सोयाबीन में कीट नियंत्रण कैसे करें`
- **session_id**: `a1b2c3d4e5f6`

## How It Works

1. **Request Processing**: The API receives the request with language parameters
2. **Language Selection**: Based on `target_lang`, the system loads the appropriate prompt file
3. **Context Management**: The `session_id` maintains conversation history across multiple requests
4. **Agent Processing**: The voice agent processes the query using Hindi-specific prompts and tools
5. **Streaming Response**: The response is streamed back in Hindi as Server-Sent Events

## Supported Agricultural Queries in Hindi

The system can handle various agricultural queries in Hindi:

- **Weather**: `नासिक में मौसम कैसा है` (What's the weather in Nashik?)
- **Pest Control**: `सोयाबीन में कीट नियंत्रण कैसे करें` (How to control pests in soyabean?)
- **Market Prices**: बाजार भाव, मंडी दरें
- **Crop Advice**: फसल सलाह, खेती के तरीके
- **Government Schemes**: सरकारी योजनाएं, सब्सिडी
- **Fertilizers**: उर्वरक, खाद की जानकारी
- **Warehouses**: गोदाम, भंडारण सुविधाएं
