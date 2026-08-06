# Company Profile Generation

You receive a company name, a Wikipedia extract, and text from the company's
own website. Generate a compact, factual company profile.

## Input

- Company name: $company_name
- Wikipedia extract: $wikipedia_text
- Website text: $website_text

Respond with a single JSON object:

```json
{
  "description": "one paragraph (max 120 words) describing what the company does",
  "products_services": ["category", "..."]
}
```

Rules:
- `products_services` must be product or service **categories** (e.g.
  "smartphones", "cloud services", "commercial banking"), never specific
  product names, model numbers, or SKUs.
- Base everything strictly on the provided content. No outside knowledge.
- Max 12 categories. Prefer the most representative categories.
- When a section is empty, write "(no content available)" in its place and do
  not invent material.
