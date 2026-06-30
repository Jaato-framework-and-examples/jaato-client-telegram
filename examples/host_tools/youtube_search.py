"""YouTube search tool — returns top results as a formatted list."""
import json

TOOL_SCHEMA = {
    "name": "youtube_search",
    "description": "Search YouTube and return the top matching videos with titles, URLs, durations, channels, and view counts.",
    "timeout": 30000,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for YouTube."
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5).",
                "default": 5
            }
        },
        "required": ["query"]
    }
}


async def execute(args, ctx):
    from youtube_search import YoutubeSearch

    query = args["query"]
    max_results = args.get("max_results", 5)

    try:
        results = YoutubeSearch(query, max_results=max_results).to_dict()
    except Exception as e:
        return {"error": f"Search failed: {e}"}

    if not results:
        return {"result": "No results found."}

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        video_id = r.get("id", "")
        url = f"https://youtube.com/watch?v={video_id}" if video_id else "N/A"
        duration = r.get("duration", "N/A")
        channel = r.get("channel", "N/A")
        views = r.get("views", "N/A")
        published = r.get("publish_time", "N/A")
        lines.append(f"{i}. {title}")
        lines.append(f"   {url}")
        lines.append(f"   {duration} | {channel} | {views} | {published}")
        lines.append("")

    return {"result": "\n".join(lines)}
