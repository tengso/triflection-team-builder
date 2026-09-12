import json

import httpx
import pytest

from team_builder.buzz import Buzz
from team_builder.nostr import key, public, reply_tags, sign


def test_internal_transport_signs_canonical_advertised_host():
    secret = key()
    client = Buzz(
        "http://relay:3000",
        secret,
        public(key()),
        canonical_origin="http://ubuntu.orb.local:3310",
    )

    def transport(request):
        import base64

        assert request.url.host == "relay"
        assert request.headers["Host"] == "ubuntu.orb.local:3310"
        event = json.loads(
            base64.b64decode(request.headers["Authorization"].split()[1])
        )
        assert ["u", "http://ubuntu.orb.local:3310/query"] in event["tags"]
        return httpx.Response(200, json=[])

    client.client = httpx.Client(transport=httpx.MockTransport(transport))
    assert client.query([{"kinds": [0]}]) == []


def test_query_rejects_forged_signature_and_wrong_coordinate():
    secret = key()
    client = Buzz("http://relay:3000", secret, public(key()))
    event = sign(secret, 0, [], "{}")
    event["content"] = "forged"
    client.call = lambda *args: [event]
    with pytest.raises(RuntimeError, match="invalid signed"):
        client.query([{"kinds": [0]}])
    event = sign(secret, 0, [], "{}")
    client.call = lambda *args: [event]
    with pytest.raises(RuntimeError, match="mismatched"):
        client.head(0, public(key()))


def test_reply_tags_keep_buzz_thread_ancestry():
    secret = key()
    root = sign(secret, 9, [["h", "office"]], "Build a team")
    direct = sign(secret, 9, [["h", "office"], *reply_tags(root)], "Proposal")
    assert reply_tags(root) == [["e", root["id"], "", "reply"]]
    assert reply_tags(direct) == [
        ["e", root["id"], "", "root"],
        ["e", direct["id"], "", "reply"],
    ]
    legacy = sign(
        secret, 9, [["h", "office"], ["e", root["id"], "", "root"]], "Legacy top level"
    )
    assert reply_tags(legacy) == [["e", legacy["id"], "", "reply"]]
