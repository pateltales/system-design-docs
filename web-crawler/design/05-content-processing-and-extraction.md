# 05 - Content Processing and Extraction

## Overview

Once the fetcher downloads a page, the raw bytes are useless until they are
parsed, cleaned, and stored. This document covers every stage of that pipeline:
decoding bytes into text, building a DOM, extracting links and metadata,
handling non-HTML content, storing raw and processed data at scale, and tracking
content freshness for efficient re-crawling.

---

## HTML Parsing Pipeline

Five stages transform raw HTTP response bytes into structured, usable data.

```
                         HTML PARSING PIPELINE
 ============================================================================

  +-----------+     +------------+     +-----------+     +-----------+
  | Raw Bytes |---->| Decode to  |---->| Parse     |---->| DOM Tree  |
  | (HTTP     |     | Text       |     | HTML      |     | (in-      |
  |  response)|     | (charset   |     | (lenient  |     |  memory)  |
  |           |     |  detect)   |     |  parser)  |     |           |
  +-----------+     +------------+     +-----------+     +-----+-----+
                                                               |
                          +------------------------------------+
                          |                  |                  |
                          v                  v                  v
                   +-------------+   +-------------+   +-------------+
                   | Extract     |   | Extract     |   | Extract     |
                   | Links       |   | Metadata    |   | Visible     |
                   | (URLs,      |   | (title,     |   | Text        |
                   |  resolve    |   |  robots,    |   | (strip      |
                   |  relative)  |   |  canonical, |   |  tags,      |
                   |             |   |  JSON-LD)   |   |  normalize) |
                   +------+------+   +------+------+   +------+------+
                          |                  |                  |
                          v                  v                  v
                   +-------------+   +-------------+   +-------------+
                   | URL         |   | Metadata    |   | Text for    |
                   | Frontier    |   | Store       |   | Dedup &     |
                   | (new URLs   |   | (index,     |   | Indexing    |
                   |  to crawl)  |   |  ranking)   |   |             |
                   +-------------+   +-------------+   +-------------+
```

---

### Stage 1: Decode (Raw Bytes to Text)

The HTTP response body is a stream of bytes. Before parsing, you must determine
the character encoding and decode those bytes into a Unicode string.

**Charset detection order (highest to lowest priority):**

1. `Content-Type` HTTP header: `Content-Type: text/html; charset=UTF-8`
2. BOM (Byte Order Mark) at the start of the byte stream
3. `<meta charset="UTF-8">` or `<meta http-equiv="Content-Type" content="text/html; charset=...">` in the first 1024 bytes
4. Heuristic detection (e.g., chardet in Python, ICU in Java)
5. Default fallback: UTF-8 (historically was Windows-1252 for HTTP)

**Common encodings encountered in the wild:**

| Encoding       | Prevalence (approx.) | Notes                                       |
|----------------|----------------------|---------------------------------------------|
| UTF-8          | ~97% of the web      | Dominant since ~2016                        |
| ISO-8859-1     | ~1-2%                | Latin-1, Western European legacy            |
| Windows-1252   | <1%                  | Microsoft's superset of ISO-8859-1          |
| GB2312 / GBK   | <1%                  | Chinese legacy sites                        |
| Shift_JIS      | <0.5%                | Japanese legacy sites                       |
| EUC-KR         | <0.5%                | Korean legacy sites                         |

**Implementation notes:**

- Always decode before parsing. Feeding raw bytes to an HTML parser with the
  wrong encoding produces mojibake (garbled text) or parse errors.
- If the declared charset is wrong (e.g., header says `ISO-8859-1` but content
  is UTF-8), heuristic detection is the last resort. Libraries like `chardet`
  (Python) or `juniversalchardet` (Java) analyze byte frequency distributions.
- Truncate excessively large pages before decoding. A 50 MB HTML file is almost
  certainly not a normal web page. A reasonable limit is 10-15 MB.

```python
# Python example
import chardet

def decode_response(raw_bytes: bytes, declared_charset: str | None) -> str:
    # 1. Try declared charset
    if declared_charset:
        try:
            return raw_bytes.decode(declared_charset)
        except (UnicodeDecodeError, LookupError):
            pass

    # 2. Try BOM detection
    if raw_bytes[:3] == b'\xef\xbb\xbf':
        return raw_bytes[3:].decode('utf-8')

    # 3. Heuristic detection
    detected = chardet.detect(raw_bytes)
    if detected['confidence'] > 0.7:
        try:
            return raw_bytes.decode(detected['encoding'])
        except (UnicodeDecodeError, LookupError):
            pass

    # 4. Fallback
    return raw_bytes.decode('utf-8', errors='replace')
```

---

### Stage 2: Parse HTML (Build DOM Tree)

Real-world HTML is messy. Pages have unclosed tags, mismatched nesting, invalid
attributes, and proprietary extensions. A production crawler **must** use a
lenient (error-tolerant) parser, not a strict XML parser.

**Parser libraries by language:**

| Language | Library                   | Notes                                              |
|----------|---------------------------|----------------------------------------------------|
| Java     | jsoup                     | CSS selector API, lenient, fast. Industry standard. |
| Python   | BeautifulSoup + lxml      | BS4 wraps lxml/html.parser. lxml is C-based, fast. |
| Python   | selectolax                | Modest/Lexbor based, faster than BS4 for parsing.  |
| Go       | golang.org/x/net/html     | Standard library extension, token-based.            |
| C/C++    | Gumbo (Google)            | Pure C, HTML5 spec compliant parser.               |
| Rust     | scraper + html5ever       | html5ever is servo's HTML5 parser.                 |

**Why lenient parsing matters:**

Consider this broken HTML (common in the wild):

```html
<html>
<body>
<p>First paragraph
<p>Second paragraph
<div><span>Unclosed span
<table><tr><td>Cell 1<td>Cell 2
</body>
```

A strict XML parser rejects this. A lenient HTML5 parser produces a valid DOM
tree by applying the HTML5 specification's error recovery rules:
- Implicit closing of `<p>` when another block element starts
- Auto-closing of `<td>` when a sibling `<td>` starts
- Unclosed `<span>` gets closed at the parent boundary

**Performance considerations:**

- Parsing is CPU-bound. At scale, it becomes a bottleneck.
- jsoup parses a typical page (~50 KB HTML) in ~2-5 ms on modern hardware.
- For a crawler processing 1,000 pages/second, that is 2-5 CPU-seconds of
  parse work per second — parallelizable across cores.
- SAX/streaming parsers (token-by-token) use less memory than DOM parsers
  (full tree in memory). For a crawler that only needs links and metadata,
  streaming is often sufficient.

---

### Stage 3: Extract Links

Link extraction is the mechanism that drives the crawler forward. Every
discovered URL is a candidate for the URL frontier.

**Source elements and attributes:**

| HTML Element        | Attribute | Example                                         |
|---------------------|-----------|--------------------------------------------------|
| `<a>`               | `href`    | `<a href="/page2">Link</a>`                      |
| `<link>`            | `href`    | `<link rel="stylesheet" href="/style.css">`       |
| `<img>`             | `src`     | `<img src="/image.jpg">`                          |
| `<script>`          | `src`     | `<script src="/app.js"></script>`                 |
| `<frame>`, `<iframe>` | `src`  | `<iframe src="/embed"></iframe>`                  |
| `<form>`            | `action`  | `<form action="/submit">`                         |
| `<area>`            | `href`    | Image map links                                  |
| `<base>`            | `href`    | Sets base URL for all relative URLs in the page  |
| `<source>`          | `src`     | `<video>` / `<audio>` media sources              |
| `<meta>` (refresh)  | `content` | `<meta http-equiv="refresh" content="0;url=...">` |

**Relative URL resolution:**

Extracted URLs are frequently relative. They must be resolved against a base URL.

Resolution order:
1. If the page has a `<base href="...">` tag, use that as the base.
2. Otherwise, use the URL of the page itself as the base.

```
Page URL:      https://example.com/blog/posts/2024/article.html
Relative URL:  ../images/photo.jpg
Resolved:      https://example.com/blog/posts/images/photo.jpg

Page URL:      https://example.com/blog/
Relative URL:  /about
Resolved:      https://example.com/about

Page URL:      https://example.com/page
Relative URL:  ?query=1
Resolved:      https://example.com/page?query=1
```

**URL filtering (what to discard):**

| Filter                     | Reason                                          |
|----------------------------|-------------------------------------------------|
| `javascript:` URIs         | Not real URLs, execute JS in browser context     |
| `mailto:` URIs             | Email addresses, not crawlable                   |
| `data:` URIs               | Inline data, not crawlable                       |
| `tel:` URIs                | Phone numbers                                    |
| Fragment-only (`#section`) | Same page, no new content to fetch               |
| URLs matching blocklists   | Trap detection (calendar, session, infinite URLs)|

**Respecting `rel="nofollow"`:**

```html
<a href="/paid-link" rel="nofollow">Sponsored</a>
```

- `rel="nofollow"` is an advisory signal. Google uses it to avoid passing
  PageRank through the link.
- A crawler for search may still crawl the URL (Google does), but should not
  pass link-juice.
- A polite research crawler may choose to honor it and skip the URL entirely.

**URL canonicalization before adding to frontier:**

- Lowercase the scheme and host: `HTTP://Example.COM/Page` -> `http://example.com/Page`
- Remove default ports: `http://example.com:80/` -> `http://example.com/`
- Remove trailing dot on hostname: `http://example.com./` -> `http://example.com/`
- Decode unreserved percent-encoded characters: `%41` -> `A`
- Remove fragment: `http://example.com/page#section` -> `http://example.com/page`
- Normalize path: `/a/b/../c` -> `/a/c`
- Sort query parameters (optional, aggressive): `?b=2&a=1` -> `?a=1&b=2`
- Remove tracking parameters (optional): `utm_source`, `utm_medium`, `fbclid`, etc.

---

### Stage 4: Extract Metadata

Metadata drives ranking, deduplication, and crawl policy decisions.

**Key metadata fields:**

| Source                           | Field             | Use                                       |
|----------------------------------|-------------------|-------------------------------------------|
| `<title>`                        | Page title        | Display in search results, ranking signal  |
| `<meta name="description">`     | Description       | Snippet in search results                  |
| `<meta name="robots">`          | Robot directives  | noindex, nofollow, noarchive, nosnippet    |
| `<link rel="canonical">`        | Canonical URL     | Dedup: multiple URLs same content          |
| `<html lang="...">`             | Language          | Language-specific search, geo-targeting    |
| `<meta name="keywords">`        | Keywords          | Largely ignored by modern search engines   |
| `X-Robots-Tag` HTTP header       | Robot directives  | Same as meta robots but via HTTP header    |
| `<link rel="alternate" hreflang="...">` | Alternate languages | i18n URL variants            |

**Meta robots directives:**

```html
<meta name="robots" content="noindex, nofollow">
```

| Directive    | Meaning                                           |
|--------------|---------------------------------------------------|
| `noindex`    | Do not include this page in the search index       |
| `nofollow`   | Do not follow any links on this page               |
| `noarchive`  | Do not show a cached copy                          |
| `nosnippet`  | Do not show a text snippet in search results       |
| `none`       | Equivalent to `noindex, nofollow`                  |

A crawler that respects `noindex` should still process the page (to extract
links, unless `nofollow` is also set) but must not add it to the search index.

**Canonical URL handling:**

```html
<!-- Page at https://example.com/products?sort=price&page=1 -->
<link rel="canonical" href="https://example.com/products">
```

This tells the crawler: "The authoritative version of this content lives at
`/products`. Don't index me separately; credit this URL instead." The crawler
should:
1. Record the canonical mapping in the metadata store.
2. If the canonical URL hasn't been crawled, add it to the frontier.
3. Consolidate signals (links, content) to the canonical URL.

**Structured data extraction (JSON-LD, Microdata, RDFa):**

Modern pages embed structured data for rich search results:

```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Article",
  "headline": "How Web Crawlers Work",
  "author": {"@type": "Person", "name": "Jane Doe"},
  "datePublished": "2024-03-15",
  "dateModified": "2024-06-01"
}
</script>
```

Extracting this gives the crawler explicit signals about content type, author,
dates, and entity relationships without NLP inference. Google's Knowledge Graph
heavily relies on structured data extraction.

---

### Stage 5: Extract Visible Text

The visible text is what a human reads on the page. It powers full-text search
indexing and content-based deduplication.

**Extraction process:**

1. Remove `<script>`, `<style>`, `<noscript>` elements entirely.
2. Remove HTML comments.
3. Process remaining elements: extract text content, respecting block vs. inline
   element boundaries (insert whitespace at block boundaries).
4. Decode HTML entities: `&amp;` -> `&`, `&#8212;` -> `--`, etc.
5. Normalize whitespace: collapse runs of whitespace into single spaces, trim.
6. Optionally: identify and extract the "main content" area, stripping
   navigation, headers, footers, sidebars (boilerplate removal).

**Boilerplate removal:**

Most pages are 60-80% boilerplate (navigation, ads, footers). Extracting only
the main content improves indexing quality and dedup accuracy.

Approaches:
- **DOM-density based**: Measure text-to-tag ratio in subtrees. The subtree
  with the highest text density is likely the main content. (Used by Readability,
  the algorithm behind Firefox Reader View.)
- **Machine learning**: Train a classifier on labeled data to identify content
  vs. boilerplate blocks.
- **Heuristic**: Look for `<article>`, `<main>`, `role="main"`, common content
  container class names (`content`, `article-body`, `post-content`).

**Text fingerprinting for dedup:**

After extracting visible text, compute a fingerprint for near-duplicate
detection:

- **Exact dedup**: SHA-256 of the full normalized text.
- **Near-dedup**: SimHash or MinHash over text shingles (sliding window of
  k words). Pages with Hamming distance < threshold on SimHash are near-
  duplicates. Google uses SimHash at scale for web dedup.

---

## Non-HTML Content Handling

The web is not only HTML. A crawler encounters XML sitemaps, RSS feeds, PDFs,
images, and many other content types.

### XML Sitemaps

Sitemaps are the most cooperative discovery mechanism: site owners explicitly
list URLs they want crawled.

**Structure of a sitemap:**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/page1</loc>
    <lastmod>2024-06-15</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://example.com/page2</loc>
    <lastmod>2024-05-01</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
</urlset>
```

| Element       | Description                                                         |
|---------------|---------------------------------------------------------------------|
| `<loc>`       | URL of the page (required)                                          |
| `<lastmod>`   | Last modification date (ISO 8601). Used for conditional re-crawl.   |
| `<changefreq>`| Hint: always, hourly, daily, weekly, monthly, yearly, never         |
| `<priority>`  | Hint: 0.0 to 1.0, relative importance within the site. Often abused.|

**Sitemap index files:**

Large sites split sitemaps across multiple files:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://example.com/sitemap-products.xml</loc>
    <lastmod>2024-06-15</lastmod>
  </sitemap>
  <sitemap>
    <loc>https://example.com/sitemap-blog.xml</loc>
    <lastmod>2024-06-10</lastmod>
  </sitemap>
</sitemapindex>
```

**Sitemap discovery:**

1. `robots.txt` directive: `Sitemap: https://example.com/sitemap.xml`
2. Convention: try `https://example.com/sitemap.xml`
3. Convention: try `https://example.com/sitemap_index.xml`

**Implementation notes:**

- Sitemaps can be gzip-compressed (`.xml.gz`). Always handle decompression.
- A single sitemap file can have at most 50,000 URLs or 50 MB uncompressed.
- `<changefreq>` and `<priority>` are hints, not guarantees. Many sites set
  all priorities to 1.0, rendering the field useless. `<lastmod>` is more
  reliable but still not always accurate.
- On re-crawl, compare the sitemap's `<lastmod>` timestamps against your
  records to identify changed pages without fetching them.

### RSS/Atom Feeds

Feeds are efficient for discovering new content on frequently updated sites.

```xml
<!-- RSS 2.0 -->
<rss version="2.0">
  <channel>
    <title>Example Blog</title>
    <item>
      <title>New Post</title>
      <link>https://example.com/blog/new-post</link>
      <pubDate>Mon, 15 Jun 2024 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
```

- Extract `<link>` from each `<item>` (RSS) or `<entry>` (Atom) and add to
  the URL frontier.
- `<pubDate>` (RSS) or `<updated>` (Atom) gives you the publication timestamp
  without fetching the page.
- Discover feeds via `<link rel="alternate" type="application/rss+xml" ...>`
  in HTML `<head>`.
- Polling feeds is much cheaper than re-crawling entire sites. A news crawler
  polls feeds every few minutes.

### PDF Documents

PDFs are the second most common document type on the web after HTML.

| Library          | Language | Notes                                     |
|------------------|----------|-------------------------------------------|
| Apache Tika      | Java     | Extracts text and metadata from 1000+ formats |
| pdfminer.six     | Python   | Pure Python, detailed text layout extraction  |
| PyMuPDF (fitz)   | Python   | C-based, very fast, text + image extraction   |
| poppler (pdftotext) | C/C++ | Fast CLI tool, widely available              |

**What to extract from PDFs:**

- Text content (for indexing and dedup)
- Embedded hyperlinks (for URL discovery)
- Metadata: title, author, creation date, page count
- Language detection on extracted text

**Challenges:**

- Scanned PDFs contain images, not text. OCR (Tesseract) is needed but slow.
- PDF text extraction can produce garbled output if the font encoding is
  non-standard.
- Large PDFs (100+ pages) are expensive to process. Set a page-count or
  file-size limit.

### Images and Media

Images, videos, and audio files are not parsed for text. The crawler records
metadata only.

| Field          | Source                                      |
|----------------|---------------------------------------------|
| URL            | Extracted from `<img src>`, `<video src>`, etc.|
| Content-Type   | HTTP `Content-Type` header                   |
| Content-Length  | HTTP `Content-Length` header                 |
| Alt text       | `<img alt="...">` from the referring HTML page|
| Dimensions     | Can be read from image headers without full decode |

For image search, additional processing is needed (EXIF extraction, perceptual
hashing, image classification), but that is outside the core crawler's scope.

---

## Content Storage

### Raw HTML Storage: WARC Format

**WARC (Web ARChive)** is the ISO 28500 standard for storing web crawl data.
First specified in 2008, it is used by the Internet Archive (Wayback Machine)
and Common Crawl. It is the de facto standard for archival-grade web storage.

**WARC file structure:**

A WARC file is a sequence of records, each consisting of a header and a content
block:

```
WARC/1.0
WARC-Type: request
WARC-Date: 2024-06-15T10:30:00Z
WARC-Target-URI: https://example.com/page
WARC-Record-ID: <urn:uuid:12345678-1234-1234-1234-123456789012>
Content-Type: application/http;msgtype=request
Content-Length: 156

GET /page HTTP/1.1
Host: example.com
User-Agent: MyCrawler/1.0
Accept: text/html


WARC/1.0
WARC-Type: response
WARC-Date: 2024-06-15T10:30:01Z
WARC-Target-URI: https://example.com/page
WARC-Record-ID: <urn:uuid:12345678-1234-1234-1234-123456789013>
Content-Type: application/http;msgtype=response
Content-Length: 12345

HTTP/1.1 200 OK
Content-Type: text/html; charset=UTF-8
Content-Length: 12000

<!DOCTYPE html>
<html>...
```

**WARC record types:**

| Type        | Purpose                                                  |
|-------------|----------------------------------------------------------|
| `warcinfo`  | Metadata about the WARC file itself (crawler version, etc.) |
| `request`   | The HTTP request sent by the crawler                     |
| `response`  | The HTTP response (headers + body) received              |
| `resource`  | A resource not obtained via HTTP (e.g., DNS lookup result)|
| `metadata`  | Additional metadata about a record                       |
| `revisit`   | Indicates content is identical to a previous crawl (dedup)|
| `conversion`| A transformed version of another record                  |

**Why WARC:**

- Self-contained: HTTP headers + body together, no external dependencies.
- Append-only: WARC files are written sequentially. No random access needed
  during writing. Efficient for high-throughput crawlers.
- Reproducible: Can replay the exact HTTP interaction.
- Ecosystem: Tools like `warcio` (Python), `jwat` (Java), `wget --warc-file`.
- The `revisit` record type enables cross-crawl dedup: if content hasn't
  changed, store a pointer instead of a copy.

**WARC file sizing:**

- Convention: ~1 GB per WARC file (gzip-compressed).
- Common Crawl uses ~1 GiB compressed WARC files.
- At 1 GB compressed per file, 100 TB of raw content = ~100,000 WARC files.

---

### Content Hash Dedup

Two different URLs can serve identical content (mirrors, syndication, URL
parameter variations). Storing the same content twice wastes space.

**Content-addressed storage:**

```
URL: https://example.com/page?ref=twitter
URL: https://example.com/page?ref=google
  |
  v
SHA-256(response body) = a1b2c3d4e5...
  |
  v
Stored once at: s3://crawl-data/content/a1/b2/c3d4e5...
```

- Compute SHA-256 (or SHA-1, or xxHash for speed) of the raw response body.
- Before writing to object storage, check if the hash already exists.
- If it exists, store only a metadata pointer (URL -> content hash).
- Savings: in practice, 15-30% of pages on the web are exact duplicates of
  another page.

**Hash collision risk:**

SHA-256 has 2^256 possible outputs. With 10 billion pages, the probability of
a collision is approximately 10^(-57). This is not a practical concern.

---

### Storage Backend Architecture

```
 +------------------+     +--------------------+     +-------------------+
 | Object Storage   |     | Metadata Store     |     | Dedup Index       |
 | (S3, GCS, HDFS)  |     | (HBase, Cassandra) |     | (Redis, RocksDB)  |
 |                  |     |                    |     |                   |
 | - Raw WARC files |     | - URL -> metadata  |     | - content hash -> |
 | - Extracted text |     |   - crawl timestamp|     |   object key      |
 | - PDF documents  |     |   - content hash   |     | - URL hash ->     |
 |                  |     |   - HTTP status     |     |   seen/not-seen   |
 |                  |     |   - redirect target |     |                   |
 |                  |     |   - ETag, Last-Mod  |     |                   |
 +------------------+     +--------------------+     +-------------------+
```

**Object storage (raw content):**

| Option  | Best for                               | Notes                          |
|---------|----------------------------------------|--------------------------------|
| S3/GCS  | Cloud-native crawlers                  | Cheap, durable, scalable       |
| HDFS    | On-prem Hadoop clusters                | Good throughput, rack-aware     |
| MinIO   | Self-hosted S3-compatible              | Good for development/small scale|

Object storage is ideal for raw content because:
- Append-heavy write pattern (WARC files are written sequentially).
- Read pattern is sequential (processing pipelines read full WARC files).
- No random access needed on the raw data.
- Cost: ~$0.02/GB/month on S3 Standard (as of 2024).

**Metadata store (structured data about each URL):**

Requirements:
1. Fast lookup by URL (point query): "What do we know about this URL?"
2. Range scans by domain or crawl timestamp: "All URLs from example.com" or
   "All URLs not crawled in the last 7 days."
3. High write throughput: bulk inserts from crawler workers.
4. Billions of rows (one per URL).

| Option        | Strengths                           | Weaknesses                    |
|---------------|-------------------------------------|-------------------------------|
| HBase         | Range scans, Hadoop integration     | Operational complexity        |
| Cassandra     | Write throughput, multi-DC replication | Weak range scans (needs careful partitioning) |
| ScyllaDB      | Cassandra-compatible, better latency| Newer, smaller community      |
| BigTable      | Managed HBase-like, Google-scale    | GCP-only                      |
| PostgreSQL    | ACID, rich queries                  | Doesn't scale past ~1B rows easily |
| FoundationDB  | ACID, ordered keys, scalable        | Less ecosystem tooling        |

**Metadata schema (conceptual):**

```
Row key: reversed domain + URL hash
  e.g., "com.example:sha256(url)" — reversed domain enables range scans by domain

Columns:
  url:              full URL string
  last_crawl_time:  timestamp of most recent crawl
  content_hash:     SHA-256 of response body at last crawl
  http_status:      200, 301, 404, etc.
  redirect_target:  URL if 3xx redirect
  etag:             HTTP ETag header value
  last_modified:    HTTP Last-Modified header value
  content_type:     MIME type from Content-Type header
  content_length:   size in bytes
  canonical_url:    <link rel="canonical"> value
  title:            <title> text
  language:         detected language
  robots_directives: noindex, nofollow, etc.
  change_count:     how many times content hash changed across crawls
  warc_file:        which WARC file contains the raw content
  warc_offset:      byte offset within the WARC file
```

The `warc_file` + `warc_offset` fields allow you to retrieve the raw content
for any URL without scanning entire WARC files. This is how the Wayback Machine
serves individual pages from its petabyte-scale WARC archive.

---

### Storage Scale

**Per-page storage breakdown:**

| Data Type         | Avg Size per Page | Notes                                  |
|-------------------|-------------------|----------------------------------------|
| Raw HTML          | 50-100 KB         | Uncompressed. Varies widely.           |
| Compressed HTML   | 10-20 KB          | gzip typically achieves 5:1 on HTML    |
| HTTP headers      | 1-2 KB            | Stored in WARC alongside body          |
| Extracted text    | 5-15 KB           | After boilerplate removal              |
| Metadata record   | 0.5-1 KB          | URL, timestamps, hashes, status, etc.  |
| Dedup index entry | 40-80 bytes       | Hash -> object pointer                 |
| Link graph edge   | 20-40 bytes       | Source URL hash -> target URL hash     |

**Scale projections:**

| Scale                | Raw (compressed) | Metadata     | Extracted Text | Total (approx.)  |
|----------------------|-------------------|-------------|----------------|-------------------|
| 10 million pages     | ~150 GB           | ~7 GB       | ~100 GB        | ~260 GB           |
| 100 million pages    | ~1.5 TB           | ~70 GB      | ~1 TB          | ~2.6 TB           |
| 1 billion pages      | ~15 TB            | ~700 GB     | ~10 TB         | ~26 TB            |
| 10 billion pages     | ~150 TB           | ~7 TB       | ~100 TB        | ~260 TB           |

**Common Crawl reference point (real-world data):**

| Metric                    | Value (typical monthly crawl, 2023-2024)    |
|---------------------------|---------------------------------------------|
| Pages crawled             | ~2.5-3.5 billion                            |
| WARC data (compressed)    | ~350-460 TiB                                |
| WAT (metadata) files      | ~30-40 TiB                                  |
| WET (extracted text) files| ~40-55 TiB                                  |
| Number of WARC files      | ~90,000-100,000 (each ~1 GiB compressed)    |
| Unique domains            | ~40-50 million                              |
| Unique URLs (all time)    | ~200+ billion                               |
| Storage location          | S3 (us-east-1), publicly accessible         |
| Hosting cost              | Sponsored by Amazon (AWS Open Data Program) |

---

## Content Freshness Tracking

Re-crawling every page on every cycle wastes bandwidth, compute, and storage.
Smart freshness tracking minimizes unnecessary re-fetches.

### Freshness State Per URL

| Field                    | Description                                      |
|--------------------------|--------------------------------------------------|
| `url`                    | The URL                                          |
| `last_crawl_time`        | When we last fetched this URL                    |
| `content_hash`           | SHA-256 of the response body at last crawl       |
| `http_last_modified`     | Value of `Last-Modified` response header         |
| `http_etag`              | Value of `ETag` response header                  |
| `change_count`           | Number of times content hash changed across crawls|
| `crawl_count`            | Total number of times we've crawled this URL     |
| `estimated_change_interval` | Computed: avg time between content changes    |

### Conditional HTTP Requests

On re-crawl, the crawler sends conditional headers:

```
GET /page HTTP/1.1
Host: example.com
If-Modified-Since: Sat, 15 Jun 2024 10:30:00 GMT
If-None-Match: "abc123etag"
```

**Server responses:**

| Response               | Meaning                                    | Crawler action               |
|------------------------|--------------------------------------------|------------------------------|
| `304 Not Modified`     | Content unchanged since last crawl         | Update `last_crawl_time`, skip storage. Saves bandwidth. |
| `200 OK` (same hash)  | Server didn't support conditional, but content is same | Update timestamp, no new storage needed. |
| `200 OK` (new hash)   | Content has changed                        | Store new version, update metadata. |
| `404 Not Found`        | Page removed                               | Mark as dead, reduce re-crawl priority. |

**Bandwidth savings from conditional requests:**

- A `304` response is typically ~200 bytes (just headers, no body).
- A full `200` response is ~50-100 KB.
- If 70% of pages are unchanged on re-crawl (common for non-news sites),
  conditional requests save ~70% of download bandwidth.
- Not all servers support conditional requests. In practice, ~40-60% of
  servers return proper `304` responses.

### Change Frequency Estimation

Track how often a URL's content changes to schedule re-crawl intervals
intelligently.

**Simple approach (Poisson model):**

```
change_rate = change_count / total_observation_period
expected_change_interval = 1 / change_rate

Re-crawl interval = min(expected_change_interval, MAX_INTERVAL)
                    capped at min MIN_INTERVAL
```

For example:
- A news homepage changes every 10 minutes -> re-crawl every 10 minutes.
- A company's "About" page changes once a year -> re-crawl once a month.
- A URL that has never changed across 10 crawls -> re-crawl very infrequently.

**Adaptive scheduling:**

| Page type          | Typical change frequency | Re-crawl interval       |
|--------------------|-------------------------|-------------------------|
| News homepages     | Minutes                 | 5-15 minutes            |
| Blog posts (new)   | Days (edits, comments)  | 1-7 days                |
| Blog posts (old)   | Rarely/never            | 30-90 days              |
| Product pages      | Days-weeks              | 3-14 days               |
| Government/legal   | Months-years            | 30-180 days             |
| Dead pages (404)   | Likely permanent        | 90-365 days (check if revived) |

---

## Contrasts

### Google's Indexing Pipeline vs. A Basic Crawler

A basic web crawler parses HTML, extracts links, and stores content. Google's
pipeline goes far beyond this:

| Stage                  | Basic Crawler                  | Google                                          |
|------------------------|--------------------------------|-------------------------------------------------|
| **Rendering**          | None (HTML only)               | Web Rendering Service (WRS): headless Chrome renders JS-heavy SPAs. Re-crawls after rendering to capture dynamic content. |
| **Text processing**    | Strip tags, extract text       | Language detection, tokenization, stemming, entity extraction, synonym expansion, spam content detection. |
| **Index**              | None or simple full-text       | Inverted index across hundreds of billions of pages. Distributed across many data centers. Supports millisecond query response. |
| **Link analysis**      | Extract links for frontier     | PageRank computation across the entire web graph. Link spam detection. Anchor text propagation. |
| **Entity extraction**  | None                           | Knowledge Graph: extract entities (people, places, organizations), relationships, facts from page content and structured data. |
| **Spam detection**     | None                           | SpamBrain (ML-based): detects link spam, cloaking, keyword stuffing, thin content, doorway pages. Manual actions team. |
| **Freshness**          | Simple re-crawl scheduling     | Caffeine (continuous indexing), real-time indexing for breaking news, Indexing API for site owners. |
| **Quality signals**    | None                           | Hundreds of ranking signals: content quality (Helpful Content System), E-E-A-T, Core Web Vitals, mobile-friendliness. |
| **Serving**            | None                           | Distributed serving infrastructure (GFS/Colossus, Bigtable, Spanner) returning ranked results in <200ms. |

### Common Crawl vs. A Private Crawler

| Aspect              | Common Crawl                                     | Private Crawler (e.g., Googlebot)                |
|---------------------|--------------------------------------------------|--------------------------------------------------|
| **Purpose**         | Open research archive                            | Building a search index / product                |
| **Scale**           | ~2.5-3.5B pages per monthly crawl                | Google: hundreds of billions of known URLs        |
| **Dedup**           | No dedup across monthly crawls                   | Continuous dedup, canonical URL resolution        |
| **Storage**         | WARC files on S3, publicly accessible             | Proprietary distributed storage (Colossus)       |
| **Processing**      | Raw archive; users process downstream             | Full indexing pipeline (rendering, ranking, etc.) |
| **Freshness**       | Monthly snapshots, no inter-crawl freshness       | Continuous crawling, seconds to days freshness    |
| **Cost model**      | AWS Open Data sponsorship, donations              | Billions of dollars in infrastructure annually    |
| **Access**          | Free, anyone can download and process             | Internal to the company                          |
| **Data format**     | WARC (raw), WAT (metadata), WET (extracted text) | Proprietary internal formats                     |

Common Crawl is invaluable for researchers, NLP dataset construction (e.g.,
C4 dataset used for training T5 and LLMs), academic web studies, and building
alternative search engines without the crawling infrastructure cost.

---

## Summary

Content processing is the bridge between raw HTTP responses and actionable,
searchable, storable data. The five-stage HTML parsing pipeline (decode, parse,
extract links, extract metadata, extract text) forms the core. Non-HTML content
(sitemaps, feeds, PDFs) extends discovery and coverage. WARC provides
archival-grade storage. Content hashing enables deduplication. And freshness
tracking with conditional HTTP requests ensures the crawler spends its bandwidth
on pages that have actually changed.

The key design principle: **separate raw storage from processed data**. Raw WARC
files are immutable, append-only, and cheap to store. Metadata, extracted text,
and link graphs are derived, queryable, and can be reprocessed from the raw
archive if the extraction logic improves.
