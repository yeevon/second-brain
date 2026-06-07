# 03 · Gemini API config

## 1. Get your API key

1. Go to https://aistudio.google.com/apikey
2. Click **Create API key**
3. Copy it → save as `GEMINI_API_KEY` in your `.env`

## 2. Model to use

```init
GEMINI_MODEL=gemini-3.5-flash
```

`gemini-3.5-flash` is the correct model as of June 2026. Fast, cheap, strong enough for classification. Update this when newer stable models are released.

## 3. Test the API

You can test your key with a quick curl from your EC2:

```bash
curl -X POST \
  "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent" \
  -H "x-goog-api-key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{"parts": [{"text": "Reply with the word WORKING only."}]}]
  }'
```

Expected response contains `"WORKING"` in the output.

## 4. Pricing

At personal capture volume (20–50 messages/day) expect well under $1/month.
Gemini 3.5 Flash is priced per token — classification tasks are short inputs and outputs.

Next: [Vault and GitHub setup](04-vault.md)