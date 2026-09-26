"""Generate an HTI example policy; does not contact the host or deploy anything."""

import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument(
    "--enable",
    action="store_true",
    help="Authorize automatic UAT and production releases",
)
args = parser.parse_args()
ui = """import urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
assert opener.open('http://127.0.0.1:8502/_stcore/health',timeout=5).status==200
"""
api = """import os,urllib.request
request=urllib.request.Request('http://127.0.0.1:8002/health',headers={'Authorization':'Bearer '+os.environ['CRM_REST_TOKEN']})
assert urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5).status==200
"""
database = """import os,pymysql
connection=pymysql.connect(host=os.environ['MYSQL_HOST'],port=int(os.environ['MYSQL_PORT']),user=os.environ['MYSQL_USER'],password=os.environ['MYSQL_PASSWORD'],connect_timeout=5,read_timeout=5,write_timeout=5)
try:
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1 FROM eq_crm_data.interaction_type_overrides LIMIT 1')
        cursor.fetchone()
finally:
    connection.close()
"""
login = """import os,yaml
from pathlib import Path
config=yaml.safe_load(Path(os.environ['AIME_USER_DB_PATH']).read_text())
users=config['credentials']['usernames']
assert users and config['cookie']['key']
assert all(isinstance(u.get('password'),str) and u['password'].startswith(('$2a$','$2b$','$2y$')) for u in users.values())
"""
policy = {
    "application": "hti-research-admin",
    "enabled": args.enable,
    "staging_agent": "cody",
    "production_agent": "oppo",
    "staging_profile": "standard-v1",
    "production_profile": "standard-v1",
    "notification_channel": "production-operations",
    "checks": [
        {
            "id": name,
            "service": service,
            "command": ["python", "-c", code],
            "timeout": 20,
        }
        for name, service, code in [
            ("ui-http", "ui", ui),
            ("authenticated-api", "api", api),
            ("database-read", "api", database),
            ("login-config", "ui", login),
        ]
    ],
}
print(json.dumps({"action": "automation-policy", "policy": policy}, indent=2))
