import json
import os
import urllib.error
import urllib.request

NOTION_API_VERSION = "2022-06-28"


def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        return


def fetch_database(notion_token: str, database_id: str) -> dict:
    url = f"https://api.notion.com/v1/databases/{database_id}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {notion_token}")
    req.add_header("Notion-Version", NOTION_API_VERSION)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Notion API error: HTTP {exc.code}: {body}") from exc


def get_title(database: dict) -> str:
    title_parts = database.get("title", [])
    text = "".join(part.get("plain_text", "") for part in title_parts).strip()
    return text or "(untitled)"


def summarize_property(prop: dict) -> str:
    prop_type = prop.get("type")
    details = prop.get(prop_type, {}) if prop_type else {}

    if prop_type in {"select", "multi_select", "status"}:
        options = [opt.get("name", "") for opt in details.get("options", [])]
        options = [opt for opt in options if opt]
        if options:
            return f"options={', '.join(options)}"
        return "options=none"

    if prop_type == "relation":
        related_db = details.get("database_id", "")
        if related_db:
            return f"database_id={related_db}"
        return "database_id=unknown"

    if prop_type == "rollup":
        relation_prop = details.get("relation_property_name", "")
        rollup_prop = details.get("rollup_property_name", "")
        if relation_prop or rollup_prop:
            return f"relation={relation_prop}, rollup={rollup_prop}"

    if prop_type == "formula":
        expression = details.get("expression", "")
        if expression:
            return f"expression={expression}"

    return ""


def main() -> None:
    load_dotenv()
    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DB_ID")

    if not notion_token or not database_id:
        raise RuntimeError("NOTION_TOKEN and NOTION_DB_ID must be set (env or .env)")

    database = fetch_database(notion_token, database_id)
    print(f"Database: {get_title(database)}")
    print("Properties:")

    for name, prop in database.get("properties", {}).items():
        prop_type = prop.get("type", "unknown")
        summary = summarize_property(prop)
        if summary:
            print(f"- {name}: {prop_type} ({summary})")
        else:
            print(f"- {name}: {prop_type}")


if __name__ == "__main__":
    main()
