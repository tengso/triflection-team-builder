"""Template acceptance entrypoint for an application repository (copy to acceptance.py).

The release policy runs it inside the deployed container in both environments:

    {"id": "acceptance", "service": "api",
     "command": ["python", "-m", "acceptance", "--environment", "{environment}"],
     "timeout": 120}

The development agent maintains the checks here through reviewed pull requests;
the policy only names the command. Keep every check read-only and repeatable,
print one line per check, and exit non-zero on the first failure. Output is kept
(secrets masked) for the release agents to read, so never print credentials.
"""

import argparse
import os
import sys
import urllib.request

CHECKS = []


def check(name):
    def register(function):
        CHECKS.append((name, function))
        return function

    return register


def http_ok(url, token_env=None):
    headers = {}
    if token_env:
        headers["Authorization"] = "Bearer " + os.environ[token_env]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=headers), timeout=5) as r:
        return r.status == 200


@check("ui-http")
def ui_http(environment):
    return http_ok("http://ui:8502/_stcore/health")


@check("authenticated-api")
def authenticated_api(environment):
    return http_ok("http://127.0.0.1:8002/health", token_env="CRM_REST_TOKEN")


@check("database-read")
def database_read(environment):
    # Replace with a read-only query through the application's own driver, e.g.
    # SELECT 1 FROM a small table. Use the credentials the application already has.
    return bool(os.environ.get("MYSQL_HOST"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--environment", choices=["staging", "production"], required=True
    )
    parser.add_argument("--only", nargs="*", help="Run a subset of checks by name")
    args = parser.parse_args()
    failed = False
    for name, function in CHECKS:
        if args.only and name not in args.only:
            continue
        try:
            ok = bool(function(args.environment))
            detail = ""
        except Exception as exc:  # noqa: BLE001 -- report the class, not application data
            ok, detail = False, f" ({type(exc).__name__})"
        print(f"{'PASS' if ok else 'FAIL'} {name} [{args.environment}]{detail}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
