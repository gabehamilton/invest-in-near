"""pipeline/explore.py: the attestation and signature checks, and the guarantee that a failed check sends nothing.

Offline: quotes are checked by a fake verifier that returns what dcap-qvl would for a quote we built ourselves, so
each check can be shown to fail when its one input is tampered with.
"""

from __future__ import annotations

import argparse
import hashlib
import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from pipeline import explore as ex

NONCE = "ab" * 32
KEY = Account.from_key("0x" + "11" * 32)
GATEWAY_ADDR = KEY.address.lower()
EVENTS = [
    {"imr": 3, "event_type": 134217729, "digest": "", "event": "key-provider", "event_payload": "7b226e616d65223a226b6d73227d"},
    {"imr": 3, "event_type": 134217729, "digest": "", "event": "system-ready", "event_payload": ""},
    {"imr": 0, "event_type": 1, "digest": "00" * 48, "event": "other-register", "event_payload": ""},
]
COMPOSE = '{"manifest_version":2,"name":"fixture"}'


def independent_rtmr3() -> str:
    rt = bytes(48)
    for ev in EVENTS:
        if ev["imr"] != 3:
            continue
        digest = hashlib.sha384(ev["event_type"].to_bytes(4, "little") + b":" + ev["event"].encode() + b":"
                                + bytes.fromhex(ev["event_payload"])).digest()
        rt = hashlib.sha384(rt + digest).digest()
    return rt.hex()


def attestation(address: str = GATEWAY_ADDR, nonce: str = NONCE) -> dict:
    return {"signing_address": address, "signing_algo": "ecdsa", "intel_quote": "00", "request_nonce": nonce,
            "event_log": json.dumps(EVENTS), "info": {"tcb_info": {"app_compose": COMPOSE}}}


def verifier(**override):
    td = {"td_attributes": "0000001000000000",
          "report_data": bytes.fromhex(GATEWAY_ADDR[2:]).ljust(32, b"\0").hex() + NONCE,
          "rt_mr3": independent_rtmr3(), "mr_config_id": "01" + hashlib.sha256(COMPOSE.encode()).hexdigest() + "00" * 15,
          "mr_td": "aa", "rt_mr0": "bb", "rt_mr1": "cc", "rt_mr2": "dd"}
    td.update(override)
    status = td.pop("status", "UpToDate")
    return lambda quote: {"status": status, "advisory_ids": [], "report": {"TD10": td}}


def failed(rep: ex.Report) -> list[str]:
    return [c.name for c in rep.checks if not c.ok]


def test_rtmr3_replay_matches_an_independent_hash_chain():
    assert ex.replay_rtmr3(EVENTS) == independent_rtmr3()


def test_a_good_attestation_passes_every_check():
    rep = ex.verify_attestation(attestation(), NONCE, verifier(), title="t")
    assert rep.ok and len(rep.checks) == 6, rep.text()
    assert rep.measurements["compose sha256"] == hashlib.sha256(COMPOSE.encode()).hexdigest()


@pytest.mark.parametrize("what, att, override, expected", [
    ("another nonce in the quote", {}, {"report_data": bytes.fromhex(GATEWAY_ADDR[2:]).ljust(32, b"\0").hex() + "cd" * 32}, "quote carries your nonce"),
    ("the gateway echoes another nonce", {"request_nonce": "cd" * 32}, {}, "quote carries your nonce"),
    ("a signing key the quote does not bind", {"signing_address": "0x" + "22" * 20}, {},
     "signing key is bound to the quote"),
    ("debug mode", {}, {"td_attributes": "0100001000000000"}, "not a debug TD"),
    ("an out-of-date TCB", {}, {"status": "OutOfDate"}, "Intel quote verifies, TCB status acceptable"),
    ("a different RTMR3", {}, {"rt_mr3": "00" * 48}, "RTMR3 replays from the event log"),
    ("a different compose file", {"info": {"tcb_info": {"app_compose": COMPOSE + " "}}}, {},
     "compose file matches MRCONFIGID"),
])
def test_each_tamper_fails_exactly_its_check(what, att, override, expected):
    rep = ex.verify_attestation({**attestation(), **att}, NONCE, verifier(**override), title="t")
    assert failed(rep) == [expected], f"{what}: {rep.text()}"


def test_a_tampered_event_log_fails_the_replay():
    att = attestation()
    att["event_log"] = json.dumps([{**EVENTS[0], "event_payload": "00"}] + EVENTS[1:])
    assert failed(ex.verify_attestation(att, NONCE, verifier(), title="t")) == ["RTMR3 replays from the event log"]


def test_a_quote_that_does_not_verify_fails_closed():
    def bad(quote):
        raise ex.ExploreError("Intel quote verification failed: bad signature")
    rep = ex.verify_attestation(attestation(), NONCE, bad, title="t")
    assert not rep.ok and failed(rep) == ["Intel quote verifies"]


def test_accept_tcb_status_is_an_explicit_choice():
    ok = ex.verify_attestation(attestation(), NONCE, verifier(status="SWHardeningNeeded"), title="t",
                               accept_tcb=frozenset({"SWHardeningNeeded"}))
    assert ok.ok


# ---------------------------------------------------------------- signatures

def signed(request: bytes, response: bytes, *, kind="gateway", model="m"):
    h = ex.sha256_hex
    text = f"{model}:{h(request)}:{h(response)}" if kind == "provider_tee" else f"{h(request)}:{h(response)}"
    sig = Account.sign_message(encode_defunct(text=text), KEY.key).signature.hex()
    return {"text": text, "signature": "0x" + sig.removeprefix("0x"), "signing_address": KEY.address,
            "signing_algo": "ecdsa", "signature_kind": kind}


@pytest.mark.parametrize("kind", ["gateway", "provider_tee"])
def test_a_genuine_signature_verifies(kind):
    rep = ex.verify_signature(signed(b"req", b"resp", kind=kind), b"req", b"resp", "m", {kind: KEY.address})
    assert rep.ok, rep.text()


def test_a_reply_changed_after_signing_is_caught():
    rep = ex.verify_signature(signed(b"req", b"resp"), b"req", b"resp tampered", "m", {"gateway": KEY.address})
    assert failed(rep) == ["signed text is the hash of your request and the reply you got"]


def test_a_key_the_quote_never_bound_is_caught():
    rep = ex.verify_signature(signed(b"req", b"resp"), b"req", b"resp", "m", {"gateway": "0x" + "33" * 20})
    assert failed(rep) == ["signing address is the one the verified quote bound"]


def test_a_forged_signature_is_caught():
    sig = signed(b"req", b"resp")
    sig["signature"] = "0x" + "ab" * 65
    assert not ex.verify_signature(sig, b"req", b"resp", "m", {"gateway": KEY.address}).ok


# ---------------------------------------------------------------- models and prompt

MODELS = [{"id": "ok/model", "owned_by": "nearai", "context_length": 100000, "output_modalities": ["text"]},
          {"id": "anthropic/claude-x", "owned_by": "anthropic", "context_length": 100000, "output_modalities": ["text"]},
          {"id": "small/model", "owned_by": "nearai", "context_length": 1000, "output_modalities": ["text"]}]


def test_only_the_confidential_fleet_is_allowed_and_the_error_lists_it():
    assert ex.pick_model(MODELS, "ok/model", 5000)["id"] == "ok/model"
    with pytest.raises(ex.ExploreError, match="leave the enclave.*ok/model"):
        ex.pick_model(MODELS, "anthropic/claude-x", 5000)
    with pytest.raises(ex.ExploreError, match="context"):
        ex.pick_model(MODELS, "small/model", 5000)
    with pytest.raises(ex.ExploreError, match="not offered"):
        ex.pick_model(MODELS, "nope", 5000)


def test_the_prompt_names_the_published_site_and_the_context_carries_the_registry(tmp_path):
    assert "SITE/" not in ex.load_prompt() and ex.config.SITE_URL in ex.load_prompt()
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    (site / "THESIS.md").write_text("# T\n")
    (site / "data" / "verdicts.json").write_text('{"H1": {"verdict": "Refuted"}}')
    (site / "data" / "tripwires.json").write_text("{}")
    (site / "data" / "numbers.json").write_text(json.dumps({"g_365": {"value": 1.0, "display": "2.48%", "unit": "pct"},
                                                           "gone": {"value": None, "display": "n/a", "unit": "pct"}}))
    ctx = ex.build_context(site)
    assert "g_365 = 2.48% (pct)" in ctx and "gone" not in ctx and '"verdict":"Refuted"' in ctx
    msg = ex.build_first_message("I hold NEAR.", ctx, ex.load_prompt())
    assert msg.index("=== THESIS.md ===") < msg.index("My thesis") < msg.index("I hold NEAR.")


# ---------------------------------------------------------------- the whole run

class FakeClient:
    sent: list[bytes] = []
    good_quote = True

    def __init__(self, key):
        pass

    def models(self):
        return MODELS

    def attestation(self, model, nonce):
        att = {**attestation(nonce=nonce), "model_name": model}
        return {"gateway_attestation": att, "model_attestations": [att]}

    def chat(self, body):
        FakeClient.sent.append(body)
        raw = json.dumps({"id": "chat-1", "choices": [{"message": {"content": "the comparison"}}]}).encode()
        return raw, json.loads(raw)

    def signature(self, chat_id, model):
        body, raw = FakeClient.sent[-1], json.dumps({"id": "chat-1", "choices": [{"message": {"content": "the comparison"}}]}).encode()
        return signed(body, raw)


def run_args(tmp_path, **kw):
    thesis = tmp_path / "mine.md"
    thesis.write_text("I hold NEAR for the staking yield.")
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True, exist_ok=True)
    (site / "THESIS.md").write_text("# T\n")
    for name in ("verdicts", "tripwires", "numbers"):
        (site / "data" / f"{name}.json").write_text("{}")
    base = dict(thesis=str(thesis), model="ok/model", chat=False, out=None, site_url=None, site_dir=site,
                skip_gpu=True, accept_tcb_status=None, no_verify=False)
    return argparse.Namespace(**{**base, **kw})


@pytest.fixture
def fake(monkeypatch):
    FakeClient.sent = []
    monkeypatch.setattr(ex, "Client", FakeClient)
    monkeypatch.setattr(ex.config, "env", lambda name, default=None: "sk-test" if name == "NEAR_AI_API_KEY" else default)
    monkeypatch.setattr(ex, "dcap_verify", verifier())
    monkeypatch.setattr(ex.secrets, "token_hex", lambda n: NONCE)
    return FakeClient


def test_a_verified_run_answers(fake, tmp_path, capsys):
    assert ex.run(run_args(tmp_path)) == 0
    out, err = capsys.readouterr()
    assert "the comparison" in out and "Reply signature: verified" in err and "Gateway enclave: verified" in err
    body = json.loads(fake.sent[0])
    assert body["model"] == "ok/model" and "I hold NEAR for the staking yield." in body["messages"][0]["content"]


def test_a_failed_attestation_sends_nothing(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "dcap_verify", verifier(td_attributes="0100001000000000"))
    with pytest.raises(ex.ExploreError, match="nothing was sent"):
        ex.run(run_args(tmp_path))
    assert fake.sent == [], "the thesis must not leave the machine when the enclave does not verify"


def test_a_non_confidential_model_sends_nothing(fake, tmp_path):
    with pytest.raises(ex.ExploreError, match="leave the enclave"):
        ex.run(run_args(tmp_path, model="anthropic/claude-x"))
    assert fake.sent == []


def test_missing_key_and_missing_thesis_are_plain_errors(fake, tmp_path, monkeypatch):
    with pytest.raises(ex.ExploreError, match="is not a file"):
        ex.run(run_args(tmp_path, thesis=str(tmp_path / "nope.md")))
    monkeypatch.setattr(ex.config, "env", lambda name, default=None: default)
    with pytest.raises(ex.ExploreError, match="NEAR_AI_API_KEY"):
        ex.run(run_args(tmp_path))
