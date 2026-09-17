"""Render one installation; no team blueprint or mutable image tags."""

import yaml

from .nostr import public


def render(config, secrets):
    images = config["images"]
    project = config["project"]
    labels = {"io.team-builder.project": project}

    def service(image, **kwargs):
        return dict(
            image=image,
            labels=labels,
            restart="unless-stopped",
            networks=["community"],
            **kwargs,
        )

    def check(command):
        return {"test": command, "interval": "5s", "timeout": "3s", "retries": 60}

    endpoint = config["advertised_url"].rstrip("/")
    relay_env = {
        "BUZZ_BIND_ADDR": "0.0.0.0:3000",
        "BUZZ_HEALTH_PORT": "8080",
        "DATABASE_URL": "postgres://buzz:"
        + secrets["database"]
        + "@postgres:5432/buzz",
        "REDIS_URL": "redis://:" + secrets["redis"] + "@redis:6379",
        "BUZZ_S3_ENDPOINT": "http://minio:9000",
        "BUZZ_S3_ADDRESSING_STYLE": "path",
        "BUZZ_S3_ACCESS_KEY": "team-builder",
        "BUZZ_S3_SECRET_KEY": secrets["s3"],
        "BUZZ_S3_BUCKET": "buzz-media",
        "BUZZ_GIT_REPO_PATH": "/data/git",
        "BUZZ_AUTO_MIGRATE": "true",
        "BUZZ_GIT_CONFORMANCE_PROBE": "true",
        "BUZZ_REQUIRE_AUTH_TOKEN": "true",
        "BUZZ_REQUIRE_RELAY_MEMBERSHIP": "true",
        "BUZZ_ALLOW_NIP_OA_AUTH": "true",
        "RELAY_URL": endpoint.replace("http://", "ws://").replace("https://", "wss://"),
        "BUZZ_MEDIA_BASE_URL": endpoint + "/media",
        "BUZZ_MEDIA_SERVER_DOMAIN": endpoint.split("://", 1)[1],
        "BUZZ_CORS_ORIGINS": endpoint,
        "RELAY_OWNER_PUBKEY": config["owner"],
        "BUZZ_RELAY_PRIVATE_KEY": secrets["relay"],
        "BUZZ_GIT_HOOK_HMAC_SECRET": secrets["hmac"],
        "RELAY_OPERATOR_PUBKEYS": public(secrets["admin"]),
        "RELAY_OPERATOR_API_ORIGIN": "http://relay:3000",
        "BUZZ_COMMUNITY_NAME": config["name"],
        "BUZZ_COMMUNITY_DESCRIPTION": "Managed by Chief of Agents",
        "BUZZ_EXTERNAL_REPOS_ONLY": "true",
    }
    services = {
        "postgres": service(
            images["postgres"],
            environment={
                "POSTGRES_DB": "buzz",
                "POSTGRES_USER": "buzz",
                "POSTGRES_PASSWORD": secrets["database"],
            },
            volumes=["postgres:/var/lib/postgresql/data"],
            healthcheck=check(["CMD", "pg_isready", "-U", "buzz"]),
        ),
        "redis": service(
            images["redis"],
            command=[
                "redis-server",
                "--appendonly",
                "yes",
                "--requirepass",
                secrets["redis"],
            ],
            environment={"REDISCLI_AUTH": secrets["redis"]},
            volumes=["redis:/data"],
            healthcheck=check(["CMD", "redis-cli", "ping"]),
        ),
        "minio": service(
            images["minio"],
            command=["server", "/data"],
            volumes=["minio:/data"],
            environment={
                "MINIO_ROOT_USER": "team-builder",
                "MINIO_ROOT_PASSWORD": secrets["s3"],
            },
            healthcheck=check(
                ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
            ),
        ),
        "minio-init": {
            "image": images["mc"],
            "labels": labels,
            "networks": ["community"],
            "restart": "no",
            "depends_on": {"minio": {"condition": "service_healthy"}},
            "environment": {"S3_KEY": secrets["s3"]},
            "entrypoint": [
                "sh",
                "-ec",
                'mc alias set local http://minio:9000 team-builder "$$S3_KEY" >/dev/null; mc mb --ignore-existing local/buzz-media >/dev/null',
            ],
        },
        "relay": service(
            images["relay"],
            environment=relay_env,
            volumes=["relay:/data/git"],
            ports=[f"{config['bind']}:{config['port']}:3000"],
            depends_on={
                "postgres": {"condition": "service_healthy"},
                "redis": {"condition": "service_healthy"},
                "minio-init": {"condition": "service_completed_successfully"},
            },
            healthcheck=check(
                [
                    "CMD-SHELL",
                    'bash -ec \'exec 3<>/dev/tcp/127.0.0.1/8080; printf "GET /_readiness HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n" >&3; grep -q "200 OK" <&3\'',
                ]
            ),
        ),
        "manager": service(
            config.get("manager_image", config["runtime_image"]),
            user="0:0",
            entrypoint=["/opt/hermes/.venv/bin/team-builder-manager"],
            volumes=[
                config["host_root"] + ":/state",
                "/var/run/docker.sock:/var/run/docker.sock",
            ],
            depends_on={"relay": {"condition": "service_healthy"}},
            healthcheck=check(
                [
                    "CMD",
                    "/opt/hermes/.venv/bin/python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://localhost:8088/health')",
                ]
            ),
        ),
    }
    dashboard = config.get("dashboard", {})
    if dashboard.get("enabled"):
        services["manager"]["ports"] = [f"{dashboard['bind']}:{dashboard['port']}:8089"]
    return yaml.safe_dump(
        {
            "name": project,
            "services": services,
            "networks": {"community": {}},
            "volumes": {
                name: {"labels": labels}
                for name in ("postgres", "redis", "minio", "relay")
            },
        },
        sort_keys=False,
    )
