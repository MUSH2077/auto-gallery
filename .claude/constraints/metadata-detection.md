# Metadata Detection Constraint

## Rule

When implementing any detection, classification, or filtering logic that reads raw metadata fields from gallery-dl output, **NEVER guess field names or value ranges**. Always verify against authoritative sources first.

## Authoritative sources (in priority order)

1. **gallery-dl source code** — `/usr/local/lib/python3.12/site-packages/gallery_dl/extractor/<source>.py` inside the Docker worker container. This is the definitive reference for what fields gallery-dl emits and what values they contain.

2. **gallery-dl official documentation** — https://github.com/mikf/gallery-dl/tree/master/docs

3. **Source platform API documentation** — Pixiv API, Danbooru API, etc. Only needed when gallery-dl's documentation is insufficient.

## Required verification steps

Before writing any detection function:

### 1. Read the extractor source

```bash
docker compose exec worker cat /usr/local/lib/python3.12/site-packages/gallery_dl/extractor/<source>.py
```

Look for:
- Field mappings (API field name → gallery-dl metadata key)
- Default values
- Computed/derived fields
- Enum/constant definitions

### 2. Download a test work with known properties

```bash
docker compose exec worker gallery-dl --config /gallerydl-config/config.json \
  --destination /tmp/test --range 1-1 "<test_url>" 2>&1
cat /tmp/test/pixiv/*/<id>*.json | python3 -m json.tool | less
```

Verify the actual field names and values present in the metadata JSON.

### 3. For classification fields, test BOTH positive and negative cases

If detecting NSFW, test with both a safe work and an R-18 work to confirm the field values differ as expected.

### 4. Document the field semantics

In the code comments, reference the exact source file and line where the field is defined:

```python
# From gallery-dl pixiv.py line 62:
#   ratings = {0: "General", 1: "R-18", 2: "R-18G"}
#   work["rating"] = ratings.get(work["x_restrict"])
rating = raw.get("rating", "")
```

## Known field reference (verified)

### Pixiv (from gallery-dl `pixiv.py`)

| Field | Source | Type | Values | Notes |
|-------|--------|------|--------|-------|
| `rating` | gallery-dl computed | str | `"General"`, `"R-18"`, `"R-18G"` | Derived from `x_restrict`. **Primary NSFW indicator.** |
| `x_restrict` | Pixiv API `xRestrict` | int | `0`=General, `1`=R-18, `2`=R-18G | Raw API value |
| `restrict` | Pixiv API `restrict` | int | `0`=public, `1`=private | **Visibility scope — NOT content rating. Do not use for NSFW.** |
| `sanity_level` | Pixiv API `sl` | int | `0`–`11` | Content safety score. `6` appears for both safe and R-18 works. **Unreliable for NSFW.** |
| `illust_ai_type` | Pixiv API | int | `1`=human, `2`=AI-generated | Only `== 2` means AI |
| `type` | Pixiv API | str | `"illust"`, `"manga"`, `"ugoira"`, `"novel"` | Work type |
| `page_count` | Pixiv API | int | | Number of pages |
| `tags` | Pixiv API | list | | Gallery-dl extracts to separate `tags` array |

### Danbooru (from gallery-dl `danbooru.py`)

| Field | Source | Type | Values | Notes |
|-------|--------|------|--------|-------|
| `rating` | Danbooru API | str | `"s"`=safe, `"q"`=questionable, `"e"`=explicit | **Primary NSFW indicator** |
| `tag_string_artist` | Danbooru API | str | space-separated artist tags | |
| `tag_string_character` | Danbooru API | str | | |
| `tag_string_copyright` | Danbooru API | str | | |
| `tag_string_general` | Danbooru API | str | | |
| `tag_string_meta` | Danbooru API | str | includes `ai_generated` if applicable | |

## Anti-patterns

- ❌ Guessing that `restrict` means content restriction (it means visibility)
- ❌ Assuming `sanity_level >= 7` means NSFW (value 6 is ambiguous)
- ❌ Not testing with actual metadata JSON files before committing
- ❌ Using field names that "look right" without checking gallery-dl source
- ❌ Copying detection logic from other projects without verifying against THIS gallery-dl version

## When adding new detection logic

1. Read the extractor source in the running container
2. Download test works and inspect the JSON
3. Test both positive and negative cases
4. Document field semantics in code comments with source references
5. Add the verified fields to the reference table above
