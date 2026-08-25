import urllib.request, json

URL = "https://hqlqqthkiulmozgxbnxx.supabase.co/rest/v1"
KEY = "sb_publishable_V3VfpbZEjZUbvQ4DYilXWQ_x02ikkUs"
DEVICE = "6a0f049d-c856-4e77-947a-bd132717a1fe"
TODAY = "2026-08-24"

def fetch(table, params=""):
    url = f"{URL}/{table}?{params}"
    req = urllib.request.Request(url, headers={
        "apikey": KEY, "Authorization": f"Bearer {KEY}"
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# Check each table
print("=== tag_time_records ===")
d = fetch("tag_time_records_cloud", f"select=tag,seconds&device_id=eq.{DEVICE}&date=eq.{TODAY}")
print(f"  {len(d)} rows: {d}")

print("\n=== ai_token_daily ===")
d = fetch("ai_token_daily_cloud", f"select=source,input_tokens,output_tokens,cached_tokens,sessions,messages&device_id=eq.{DEVICE}&date=eq.{TODAY}")
print(f"  {len(d)} rows: {d}")

print("\n=== tool_call_daily ===")
d = fetch("tool_call_daily_cloud", f"select=category,name,count&device_id=eq.{DEVICE}&date=eq.{TODAY}")
print(f"  {len(d)} rows: {d[:5]}")

print("\n=== devin_activity_daily ===")
d = fetch("devin_activity_daily_cloud", f"select=data_json&device_id=eq.{DEVICE}&date=eq.{TODAY}")
print(f"  {len(d)} rows")
if d:
    act = json.loads(d[0]["data_json"]) if isinstance(d[0]["data_json"], str) else d[0]["data_json"]
    print(f"  projects: {len(act.get('projects',[]))}")
    print(f"  titles: {len(act.get('titles',[]))}")
    print(f"  tool_kinds: {act.get('tool_kinds',{})}")
