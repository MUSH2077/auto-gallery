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

---

## Extended scope: Any external tool behavior

This constraint applies to ALL situations where code depends on the behavior of an external tool or API, not just metadata fields.

### gallery-dl behavior

| Decision point | Verify by | Do NOT guess |
|---------------|-----------|--------------|
| Exit code meaning | Read gallery-dl source for the extractor | Assume exit 0 = files downloaded |
| Output file format | Read extractor source + postprocessor source | Assume ugoira setting produces GIF |
| CLI flag ↔ config equivalence | Read `option.py` to see exact config paths | Assume `--flag X` = `{"X": "value"}` in config |
| Config merge behavior | Test with multiple `--config` files | Assume deep merge |
| Download archive behavior | Test: re-run on same URL | Assume it skips duplicates |

### ffmpeg behavior

| Decision point | Verify by | Do NOT guess |
|---------------|-----------|--------------|
| Available codecs | `ffmpeg -codecs` in container | Assume libx264 available |
| Output format support | Test conversion with actual file | Assume GIF output works |
| Frame count in output | `ffprobe -show_streams` on output file | Count bytes manually |

### Pixiv API behavior

| Decision point | Verify by | Do NOT guess |
|---------------|-----------|--------------|
| Auth requirements | Read gallery-dl pixiv.py `_init_oauth` / `_login_impl` | Assume refresh_token is enough |
| R-18 access | Check if account has R-18 enabled in Pixiv settings | Assume all content is visible |
| Rate limiting | Check gallery-dl's `sleep-request` handling | Assume unlimited requests |

### PostgreSQL behavior

| Decision point | Verify by | Do NOT guess |
|---------------|-----------|--------------|
| `to_char()` on VARCHAR | Test query first | Assume it works on non-timestamp columns |
| `ILIKE` on UUID | Use `::text` cast | Assume ILIKE works on UUID type |
| JSONB operators | Check PostgreSQL version docs | Assume `->>'key'` always returns string |

## Workflow for any new detection/classification

```
1. Locate the authoritative source
   ├── gallery-dl extractor? → Read extractor .py in container
   ├── gallery-dl postprocessor? → Read postprocessor .py in container
   ├── gallery-dl CLI option? → Read option.py in container
   ├── ffmpeg codec/format? → Run ffmpeg -codecs / -formats in container
   └── Platform API? → Find official API docs

2. Write a test that exercises the real tool
   └── docker compose exec worker <test_command>

3. Inspect the actual output
   └── Check files, JSON fields, exit codes

4. Implement detection logic based on verified values
   └── Include source references in comments

5. Test BOTH positive and negative cases
   └── e.g., safe work AND R-18 work for NSFW detection

6. Commit with evidence in the commit message
   └── "From gallery-dl pixiv.py line 62: ratings = {0: General, 1: R-18, 2: R-18G}"
```
