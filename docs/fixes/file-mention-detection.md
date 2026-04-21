# File Mention Detection Fix

## Problem

The file mention detection system in `AgentResponseTracker` was failing to identify filenames in agent responses. Specifically, it could not match:

1. **Filenames with underscores** (e.g., `docker_containers_status.txt`)
2. **Filenames with hidden Unicode characters** (zero-width joiners)
3. **Filenames in backticks** (e.g., `` `file.txt` ``)

### Example of the Issue

```
Done! I've created `docker_containers_status.txt` with the status of all your Docker containers.
```

**Before the fix:** The filename was not detected, so the file was not offered to the user.

**After the fix:** The filename is correctly detected and queued for sending.

## Root Causes

1. **Regex pattern limitation:** The pattern `'created\s+\w+\.?\w+'` only matched `\w` (word characters), which includes letters, digits, and underscore, but the pattern structure was too restrictive for complex filenames.

2. **No Unicode normalization:** Hidden Unicode characters (like zero-width joiners U+200B-U+200D and BOM U+FEFF) could break regex matching.

3. **Missing backtick pattern:** Agents often wrap filenames in backticks for clarity, but there was no dedicated pattern for this.

4. **Limited character support:** Filenames with hyphens, multiple dots, or mixed case extensions were not properly handled.

## Solution

Updated the `_extract_file_names()` method in `src/jaato_client_telegram/agent_response_tracker.py`:

### Key Changes

1. **Unicode Normalization:**
   ```python
   # Remove zero-width joiner and other invisible Unicode characters
   normalized_text = re.sub(r'[\u200B-\u200D\uFEFF]', '', normalized_text)
   ```

2. **Improved Regex Patterns:**
   ```python
   patterns = [
       # Backtick-wrapped filenames (most precise): `file.txt`
       r'`([^`]+?\.(?:csv|json|yaml|yml|txt|py|js|ts|md|html|css|xml))`',

       # "created file.txt" style - match created + filename
       r'(?:created|saved|wrote|generated)\s+([a-zA-Z0-9_\-./]+\.(?:csv|json|yaml|yml|txt|py|js|ts|md|html|css|xml))',

       # Direct filename mentions with known extensions (with word boundaries)
       r'(?:^|\s)([a-zA-Z0-9_\-./]+\.(?:csv|json|yaml|yml|txt|py|js|ts|md|html|css|xml))(?:\s|[,.;!?]|$)',

       # Absolute/relative paths with directories
       r'(?:[~./][a-zA-Z0-9_\-./]*/)+[a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+',
   ]
   ```

3. **Match Cleaning:**
   ```python
   # Strip leading/trailing whitespace and quotes from matches
   cleaned_matches = [m.strip().strip('\'"') for m in matches if m.strip()]
   ```

4. **Deduplication with Order Preservation:**
   ```python
   seen = set()
   unique_files = []
   for f in files_found:
       if f not in seen:
           seen.add(f)
           unique_files.append(f)
   ```

## Testing

The fix was verified against 10 edge cases:

| Test Case | Input | Result |
|-----------|-------|--------|
| Underscores in filename | `docker_containers_status.txt` | ✅ Detected |
| Multiple files | `data.csv` and `summary.json` | ✅ Both detected |
| File without backticks | output.yaml | ✅ Detected |
| Generated keyword | `metrics.txt` | ✅ Detected |
| Path with directories | /home/user/results/data.txt | ✅ Detected |
| Multiple mentions | `data.csv` mentioned 3 times | ✅ Deduplicated to 1 |
| Mixed case extensions | output.TXT and data.CSV | ✅ Both detected |
| False positive | "The CSV format is..." | ✅ Not detected |
| Hyphens in filename | `my-file-name.txt` | ✅ Detected |
| Multiple dots | `my.file.name.json` | ✅ Detected |

## Impact

- **Fixed:** Filenames with underscores, hyphens, and multiple dots are now correctly detected
- **Fixed:** Hidden Unicode characters no longer break filename matching
- **Improved:** Backtick-wrapped filenames are now detected with highest precision
- **Improved:** Path-based filenames (absolute/relative) are now supported
- **Improved:** Better false positive prevention with word boundaries

## Files Modified

- `src/jaato_client_telegram/agent_response_tracker.py` - Updated `_extract_file_names()` method

## Related Components

This fix affects:
- `AgentResponseTracker` - Detects file mentions in agent responses
- `ResponseRenderer` - Uses detected mentions to queue files for sending
- `FileHandler` - Sends the mentioned files to Telegram users

The complete flow:
1. Agent outputs text with filename mention
2. `AgentResponseTracker._extract_file_names()` extracts the filename
3. Filename is queued for the current turn
4. On `turn.completed`, `ResponseRenderer._send_mentioned_files()` sends the file
