"""Local-operator deployment configuration; secret files never enter Buzz."""

import json
from pathlib import Path


def configure_parser(sub):
    parser = sub.add_parser(
        "deployment",
        help="Register applications/releases and operate approved host deployments",
    )
    parser.add_argument(
        "request_file",
        help="JSON request: register, release, grant, credential, profile, profiles, preflight, configure, plan, execute, inspect or logs",
    )
    parser.add_argument(
        "--secrets-file",
        help="For register only: private JSON map of application environment values",
    )
    parser.add_argument(
        "--users-file", help="For register only: private users.yaml login file"
    )
    parser.add_argument(
        "--secret-file",
        help="For credential only: import a private value or configuration file",
    )
    parser.add_argument("--state-dir", default="~/.local/state/team-builder/default")


def command(args):
    request = json.loads(Path(args.request_file).read_text())
    if getattr(args, "secret_file", None):
        if request.get("action") != "credential":
            raise ValueError(
                "Secret values are accepted only for credential provisioning"
            )
        request["value"] = Path(args.secret_file).read_text().rstrip("\n")
    if args.secrets_file:
        if request.get("action") != "register":
            raise ValueError(
                "Secret files are accepted only during application registration"
            )
        request["secrets"] = json.loads(Path(args.secrets_file).read_text())
    if args.users_file:
        if request.get("action") != "register":
            raise ValueError(
                "Login files are accepted only during application registration"
            )
        request["files"] = {"users.yaml": Path(args.users_file).read_text()}
    print(
        json.dumps(
            operator_request(Path(args.state_dir).expanduser().resolve(), request),
            indent=2,
        )
    )


def operator_request(root, request):
    from .cli import run

    script = """import json,sys,httpx
from pathlib import Path
from team_builder.deployments import token
secrets=json.loads(Path('/state/secrets.json').read_text())
with httpx.Client(trust_env=False,timeout=60) as client:
    response=client.post('http://127.0.0.1:8088/operator/deployments',headers={'Authorization':'Bearer '+token(secrets)},json=json.load(sys.stdin))
    if response.status_code!=200:
        raise SystemExit('Deployment request rejected; verify request and manager health')
    print(json.dumps(response.json(),indent=2))
"""
    return json.loads(
        run(
            [
                "docker",
                "compose",
                "-f",
                str(root / "compose.yaml"),
                "exec",
                "-T",
                "manager",
                "/opt/hermes/.venv/bin/python",
                "-c",
                script,
            ],
            input=json.dumps(request),
            timeout=70,
        )
    )
