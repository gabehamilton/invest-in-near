"""Explore your own thesis against this one, on NEAR AI Cloud, with the enclave checked rather than trusted.

    make explore THESIS=mine.md              # one answer
    make explore THESIS=mine.md ARGS=--chat  # then ask follow-ups; every answer is verified

What it does, in order:
  1. Reads your thesis from the file you name. It is never written anywhere unless you pass --out.
  2. Builds the context from this repo's published site files (site/THESIS.md, verdicts, tripwires and a compact
     list of the registry numbers), plus the same prompt the site's agent module hands out (thesis/copy.toml,
     agent_prompt), so a NEAR AI model can answer without opening any link.
  3. Refuses any model that does not run on NEAR AI's own confidential (TEE) fleet. /v1/models marks those with
     owned_by == "nearai"; the "Incognito" models (Claude, GPT, Gemini and others) are forwarded outside a TEE.
  4. Before sending a word, fetches a fresh attestation for the gateway and the model with a nonce of your own and
     checks it (see verify_attestation). Any failed check stops the run before your thesis leaves the machine.
  5. Sends the request, then fetches the signature NEAR AI Cloud made over the exact request and response bytes and
     checks it against the attested signing address (see verify_signature).

What the checks establish, and what they do not. They show that the answer came from software whose hardware
measurements Intel's DCAP service vouches for, on a TDX machine that was not in debug mode, inside a compose file
whose hash the quote commits to, and that this enclave signed this exact request and reply. They do not show that
the published compose file does what NEAR AI says (read it: its hash is printed), and they do not pin a measurement
you trust. The measurements are printed so that you can pin them yourself; the repo does not decide for you.

Needs the `explore` dependency group (uv sync installs it) and NEAR_AI_API_KEY from cloud.near.ai (in .env or the
environment). The key is sent only to cloud-api.near.ai and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from pipeline import config

API = "https://cloud-api.near.ai/v1"
NRAS = "https://nras.attestation.nvidia.com/v3/attest/gpu"
DEFAULT_MODEL = "z-ai/glm-5.3-flash"
CONFIDENTIAL_OWNER = "nearai"       # /v1/models owned_by for NEAR AI's own TEE fleet
TIMEOUT = 60
CHAT_TIMEOUT = 600
OK_TCB = {"UpToDate"}               # Intel TCB status accepted without --accept-tcb-status

# The line that turns the site's interview prompt into a one-shot run; the thesis follows it.
ONE_SHOT = ("My thesis is below, so do not interview me. Work from it. Where you would have asked me a question, "
            "list the question and say how each answer would change the comparison.")


class ExploreError(RuntimeError):
    """Stop with a message; nothing has been sent that should not have been."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return f"  [{'ok' if self.ok else 'FAIL'}] {self.name}" + (f": {self.detail}" if self.detail else "")


@dataclass
class Report:
    title: str
    checks: list[Check] = field(default_factory=list)
    measurements: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, ok, detail))
        return ok

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def text(self) -> str:
        out = [f"{self.title}: {'verified' if self.ok else 'NOT verified'}"] + [c.line() for c in self.checks]
        out += [f"      {k} {v}" for k, v in self.measurements.items()]
        return "\n".join(out)


# =========================================================================== attestation

QuoteVerifier = Callable[[bytes], dict]


def dcap_verify(quote: bytes) -> dict:
    """Verify an Intel TDX quote against Intel's collateral (through the Phala PCCS cache that dcap-qvl defaults
    to; the collateral is signed by Intel, so the cache is not trusted). Returns dcap-qvl's verified report as a
    dict: status, advisory_ids, report.TD10 with the measurements and report_data."""
    try:
        import dcap_qvl
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ExploreError("dcap-qvl is missing: run `uv sync` (the explore dependency group), or pass --no-verify "
                           "to skip the checks and trust the gateway") from exc
    try:
        verified = asyncio.run(dcap_qvl.get_collateral_and_verify(quote))
    except Exception as exc:  # the library raises ValueError and RuntimeError; either way the quote did not verify
        raise ExploreError(f"Intel quote verification failed: {exc}") from exc
    return json.loads(verified.to_json())


def _hex_bytes(value: str, name: str) -> bytes:
    try:
        return bytes.fromhex(value[2:] if value.startswith("0x") else value)
    except (ValueError, TypeError) as exc:
        raise ExploreError(f"{name} is not hex") from exc


def replay_rtmr3(event_log: list[dict]) -> str:
    """RTMR3 as the runtime events extend it. An event whose logged digest is empty is hashed the way dstack does:
    SHA-384 over event_type (4 bytes, little endian), ":", the event name, ":", then the payload bytes."""
    rtmr = bytes(48)
    for ev in event_log:
        if ev.get("imr") != 3:
            continue
        if ev.get("digest"):
            digest = _hex_bytes(ev["digest"], "event digest")
        else:
            payload = _hex_bytes(ev.get("event_payload") or "", "event payload")
            digest = hashlib.sha384(int(ev["event_type"]).to_bytes(4, "little") + b":"
                                    + str(ev["event"]).encode() + b":" + payload).digest()
        rtmr = hashlib.sha384(rtmr + digest).digest()
    return rtmr.hex()


def _as_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def verify_attestation(att: dict, nonce: str, verify_quote: QuoteVerifier, *, title: str,
                       accept_tcb: frozenset[str] = frozenset()) -> Report:
    """Check one attestation entry (the gateway's, or one of model_attestations).

    The values read are the ones inside the verified quote, never the unsigned HTTP fields next to it:
      - the quote verifies under Intel's collateral and its TCB status is acceptable;
      - the TD is not in debug mode;
      - report_data = signing address (zero padded to 32 bytes) then the nonce you sent, so the key that signs
        the replies lives in this enclave and the quote is fresh, not replayed;
      - RTMR3 replays from the event log, so the log is the one the hardware measured;
      - MRCONFIGID = 0x01 + SHA-256 of the compose file, so the compose file shown is the one that booted.
    """
    rep = Report(title)
    try:
        verified = verify_quote(_hex_bytes(att["intel_quote"], "intel_quote"))
        td = verified["report"]["TD10"]
    except (ExploreError, KeyError, TypeError) as exc:
        rep.add("Intel quote verifies", False, str(exc))
        return rep
    status = verified.get("status")
    rep.add("Intel quote verifies, TCB status acceptable", status in OK_TCB | accept_tcb,
            f"status {status}" + (f", advisories {', '.join(verified.get('advisory_ids') or [])}"
                                   if verified.get("advisory_ids") else ""))
    debug = _hex_bytes(td["td_attributes"], "td_attributes")[0] & 1
    rep.add("not a debug TD", not debug)

    report_data = _hex_bytes(td["report_data"], "report_data")
    address = _hex_bytes(att.get("signing_address", ""), "signing_address")
    rep.add("signing key is bound to the quote", len(report_data) == 64 and report_data[:32] == address.ljust(32, b"\0"),
            f"signing address {att.get('signing_address')}")
    rep.add("quote carries your nonce", len(report_data) == 64 and report_data[32:].hex() == nonce.lower()
            and att.get("request_nonce", "").lower() == nonce.lower())

    try:
        events = _as_json(att["event_log"])
        rep.add("RTMR3 replays from the event log", replay_rtmr3(events) == td["rt_mr3"])
    except (KeyError, TypeError, ValueError, ExploreError) as exc:
        rep.add("RTMR3 replays from the event log", False, f"event log unreadable: {exc}")
    try:
        info = att.get("info") or {}
        tcb = _as_json(info["tcb_info"])
        compose = tcb["app_compose"]
        compose_hash = hashlib.sha256(compose.encode("utf-8")).hexdigest()
        expected = "01" + compose_hash + "00" * 15
        rep.add("compose file matches MRCONFIGID", td["mr_config_id"] == expected)
        rep.measurements["compose sha256"] = compose_hash
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        rep.add("compose file matches MRCONFIGID", False, f"compose file missing from the report: {exc}")
    for key, label in (("mr_td", "MRTD"), ("rt_mr0", "RTMR0"), ("rt_mr1", "RTMR1"), ("rt_mr2", "RTMR2"),
                       ("rt_mr3", "RTMR3")):
        rep.measurements[label] = td[key]
    return rep


def check_gpu(payload: str | None, nonce: str, rep: Report, post: Callable[..., Any] = requests.post) -> None:
    """NVIDIA's attestation service (NRAS) must accept the GPU evidence the model's report carries."""
    if not payload:
        rep.add("GPU evidence accepted by NVIDIA", False, "the model's report has no nvidia_payload")
        return
    try:
        evidence = json.loads(payload)
        if str(evidence.get("nonce", "")).lower() != nonce.lower():
            rep.add("GPU evidence accepted by NVIDIA", False, "the GPU evidence does not carry your nonce")
            return
        resp = post(NRAS, data=payload.encode("utf-8"), headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        token = next(item[1] for item in body if isinstance(item, list) and item and item[0] == "JWT")
        claims = json.loads(_b64url(token.split(".")[1]))
        rep.add("GPU evidence accepted by NVIDIA", claims.get("x-nvidia-overall-att-result") is True,
                "NRAS overall result; the JWT signature itself is not re-verified here")
    except (ValueError, StopIteration, IndexError, KeyError, requests.RequestException) as exc:
        rep.add("GPU evidence accepted by NVIDIA", False, f"{type(exc).__name__}: {exc}")


def _b64url(part: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# =========================================================================== signatures

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def recover_ecdsa(text: str, signature: str) -> str:
    """The address that EIP-191 personal-signed `text`."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ExploreError("eth-account is missing: run `uv sync` (the explore dependency group)") from exc
    return Account.recover_message(encode_defunct(text=text), signature=signature if signature.startswith("0x")
                                   else "0x" + signature)


def verify_signature(sig: dict, request_bytes: bytes, response_bytes: bytes, model: str,
                     attested: dict[str, str]) -> Report:
    """The signature NEAR AI Cloud made over this exchange. `attested` maps a signature kind to the signing address
    a verified attestation bound to a quote: "gateway" and "provider_tee"."""
    rep = Report("Reply signature")
    req_hash, resp_hash = sha256_hex(request_bytes), sha256_hex(response_bytes)
    kind = sig.get("signature_kind") or "gateway"
    expected = f"{model}:{req_hash}:{resp_hash}" if kind == "provider_tee" else f"{req_hash}:{resp_hash}"
    rep.add("signed text is the hash of your request and the reply you got", sig.get("text") == expected,
            f"{kind} signature")
    if sig.get("signing_algo") != "ecdsa":
        rep.add("signature verifies", False, f"unsupported signing_algo {sig.get('signing_algo')}")
        return rep
    try:
        recovered = recover_ecdsa(sig["text"], sig["signature"]).lower()
    except (KeyError, ValueError, TypeError, ExploreError) as exc:
        rep.add("signature verifies", False, str(exc))
        return rep
    rep.add("signature verifies against the signing address", recovered == str(sig.get("signing_address", "")).lower(),
            recovered)
    rep.add("signing address is the one the verified quote bound", recovered == attested.get(kind, "").lower(),
            f"{kind} key {attested.get(kind) or 'not attested'}")
    return rep


# =========================================================================== context and prompt

def load_prompt() -> str:
    """The prompt the site's agent module hands out, with the site URL filled in."""
    copy = tomllib.loads((config.ROOT / "thesis" / "copy.toml").read_text(encoding="utf-8"))
    return copy["agent_prompt"]["variants"][-1]["text"].replace("SITE/", config.SITE_URL)


def read_site(site_dir: Path | None, site_url: str | None, name: str) -> str:
    if site_url:
        resp = requests.get(site_url.rstrip("/") + "/" + name, timeout=TIMEOUT,
                            headers={"User-Agent": config.USER_AGENT})
        resp.raise_for_status()
        return resp.text
    path = (site_dir or config.SITE_DIR) / name
    if not path.exists():
        raise ExploreError(f"{path} is missing: run `make charts` first, or pass --site-url")
    return path.read_text(encoding="utf-8")


def numbers_digest(numbers: dict) -> str:
    """Every registry figure with a value, one line each: key = display (unit). The formulas stay in numbers.json."""
    lines = []
    for key in sorted(numbers):
        n = numbers[key]
        if not isinstance(n, dict) or n.get("value") is None:
            continue
        lines.append(f"{key} = {n.get('display')}" + (f" ({n['unit']})" if n.get("unit") else ""))
    return "\n".join(lines)


def build_context(site_dir: Path | None = None, site_url: str | None = None) -> str:
    def block(title: str, body: str) -> str:
        return f"=== {title} ===\n\n{body.strip()}\n\n=== end of {title} ===\n"

    def compact(name: str) -> str:
        return json.dumps(json.loads(read_site(site_dir, site_url, name)), sort_keys=True, separators=(",", ":"))

    numbers = json.loads(read_site(site_dir, site_url, "data/numbers.json"))
    return "\n".join([
        block("THESIS.md", read_site(site_dir, site_url, "THESIS.md")),
        block("data/verdicts.json", compact("data/verdicts.json")),
        block("data/tripwires.json", compact("data/tripwires.json")),
        block("registry figures (key = value; the formula and inputs for each are in data/numbers.json)",
              numbers_digest(numbers)),
    ])


def build_first_message(thesis: str, context: str, prompt: str) -> str:
    return f"{context}\n{prompt.rstrip()}\n{ONE_SHOT}\n\n{thesis.strip()}\n"


# =========================================================================== NEAR AI Cloud

class Client:
    def __init__(self, key: str, base: str = API, session: requests.Session | None = None):
        self.base, self.http = base, session or requests.Session()
        self.http.headers.update({"User-Agent": config.USER_AGENT})
        self._auth = {"Authorization": f"Bearer {key}"}

    def models(self) -> list[dict]:
        r = self.http.get(f"{self.base}/models", timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("data", [])

    def attestation(self, model: str, nonce: str) -> dict:
        r = self.http.get(f"{self.base}/attestation/report", timeout=TIMEOUT, headers={"x-no-aliasing": "true"},
                          params={"model": model, "provider": "near", "signing_algo": "ecdsa", "nonce": nonce})
        r.raise_for_status()
        return r.json()

    def chat(self, body: bytes) -> tuple[bytes, dict]:
        """POST the exact bytes given; return the exact bytes received and the parsed reply."""
        r = self.http.post(f"{self.base}/chat/completions", data=body, timeout=CHAT_TIMEOUT,
                           headers={**self._auth, "Content-Type": "application/json"})
        if r.status_code in (401, 403):
            raise ExploreError("NEAR AI Cloud rejected the API key (check NEAR_AI_API_KEY)")
        if not r.ok:
            raise ExploreError(f"chat request failed: HTTP {r.status_code}: {r.text[:300]}")
        return r.content, r.json()

    def signature(self, chat_id: str, model: str, tries: int = 6) -> dict:
        """The signature is written just after the reply, so a 404 straight away is retried."""
        for attempt in range(tries):
            r = self.http.get(f"{self.base}/signature/{chat_id}", timeout=TIMEOUT, headers=self._auth,
                              params={"model": model, "signing_algo": "ecdsa"})
            if r.status_code != 404:
                r.raise_for_status()
                return r.json()
            time.sleep(1 + attempt)
        raise ExploreError(f"no signature for {chat_id} after {tries} tries")


def pick_model(models: list[dict], wanted: str, needed_tokens: int) -> dict:
    by_id = {m["id"]: m for m in models}
    eligible = sorted(m["id"] for m in models if m.get("owned_by") == CONFIDENTIAL_OWNER
                      and "text" in (m.get("output_modalities") or ["text"])
                      and "embedding" not in (m.get("output_modalities") or []))
    m = by_id.get(wanted)
    if m is None:
        raise ExploreError(f"model {wanted!r} is not offered. Confidential models now: {', '.join(eligible)}")
    if m.get("owned_by") != CONFIDENTIAL_OWNER:
        raise ExploreError(f"model {wanted!r} does not run on NEAR AI's confidential fleet (owned_by "
                           f"{m.get('owned_by')!r}); your thesis would leave the enclave. Confidential models "
                           f"now: {', '.join(eligible)}")
    if (m.get("context_length") or 0) < needed_tokens:
        raise ExploreError(f"model {wanted!r} has a context of {m.get('context_length')} tokens; this run needs "
                           f"about {needed_tokens}")
    return m


def estimate_tokens(text: str) -> int:
    return len(text) // 3 + 4000      # a pessimistic characters-per-token guess plus room for the answer


def request_body(model: str, messages: list[dict], max_tokens: int) -> bytes:
    return json.dumps({"model": model, "messages": messages, "stream": False, "max_tokens": max_tokens},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def preflight(client: Client, model: str, *, verify_quote: QuoteVerifier, skip_gpu: bool,
              accept_tcb: frozenset[str]) -> tuple[dict[str, str], list[Report]]:
    """Attest the gateway and the model under one fresh nonce. Returns {kind: signing address} and the reports."""
    nonce = secrets.token_hex(32)
    att = client.attestation(model, nonce)
    reports = [verify_attestation(att["gateway_attestation"], nonce, verify_quote, title="Gateway enclave",
                                  accept_tcb=accept_tcb)]
    attested = {"gateway": att["gateway_attestation"].get("signing_address", "")}
    entries = [e for e in att.get("model_attestations") or [] if e.get("model_name") == model]
    if not entries:
        rep = Report("Model enclave")
        rep.add("the attestation covers the model", False, f"no entry for {model}")
        reports.append(rep)
    for entry in entries[:1]:
        rep = verify_attestation(entry, nonce, verify_quote, title=f"Model enclave ({model})", accept_tcb=accept_tcb)
        if not skip_gpu:
            check_gpu(entry.get("nvidia_payload"), nonce, rep)
        reports.append(rep)
        attested["provider_tee"] = entry.get("signing_address", "")
    return attested, reports


def answer_of(reply: dict) -> str:
    try:
        return reply["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def run(args: argparse.Namespace) -> int:
    thesis_path = Path(args.thesis)
    if not thesis_path.is_file():
        raise ExploreError(f"{thesis_path} is not a file: pass the path to your thesis, e.g. THESIS=mine.md")
    thesis = thesis_path.read_text(encoding="utf-8")
    if not thesis.strip():
        raise ExploreError(f"{thesis_path} is empty")
    key = config.env("NEAR_AI_API_KEY")
    if not key:
        raise ExploreError("NEAR_AI_API_KEY is not set: create a key at https://cloud.near.ai and put it in .env")

    context = build_context(args.site_dir, args.site_url)
    first = build_first_message(thesis, context, load_prompt())
    client = Client(key)
    model = pick_model(client.models(), args.model, estimate_tokens(first))
    max_tokens = min(16000, int(model.get("max_output_length") or 16000))
    print(f"model {model['id']} (confidential fleet, context {model.get('context_length')} tokens); "
          f"sending about {estimate_tokens(first) - 4000} tokens of context, thesis and prompt", file=sys.stderr)

    verifier = dcap_verify
    accept = frozenset(args.accept_tcb_status or [])

    def attest() -> dict[str, str]:
        if args.no_verify:
            print("WARNING: --no-verify: the enclave is not being checked, only trusted.", file=sys.stderr)
            return {}
        attested, reports = preflight(client, model["id"], verify_quote=verifier, skip_gpu=args.skip_gpu,
                                      accept_tcb=accept)
        for r in reports:
            print(r.text(), file=sys.stderr)
        if not all(r.ok for r in reports):
            raise ExploreError("attestation failed; nothing was sent. Pass --no-verify only if you accept trusting "
                               "the gateway without checks.")
        return attested

    attested = attest()
    messages = [{"role": "user", "content": first}]
    transcript: list[str] = []
    while True:
        body = request_body(model["id"], messages, max_tokens)
        raw, reply = client.chat(body)
        text = answer_of(reply)
        if not text:
            raise ExploreError(f"the reply had no content (finish_reason "
                               f"{(reply.get('choices') or [{}])[0].get('finish_reason')})")
        print(text)
        transcript.append(text)
        if not args.no_verify:
            sig = client.signature(reply["id"], model["id"])
            rep = verify_signature(sig, body, raw, model["id"], attested)
            print(rep.text(), file=sys.stderr)
            if not rep.ok:
                raise ExploreError("the reply's signature did not verify; do not rely on that answer")
        if not args.chat:
            break
        messages.append({"role": "assistant", "content": text})
        try:
            follow = input("\nfollow-up (blank to finish)> ").strip()
        except EOFError:
            break
        if not follow:
            break
        messages.append({"role": "user", "content": follow})
        transcript.append(f"\n> {follow}\n")
    if args.out:
        Path(args.out).write_text("\n\n".join(transcript) + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare your thesis with this one on NEAR AI Cloud, attestation checked.")
    p.add_argument("thesis", help="path to a text or Markdown file holding your thesis")
    p.add_argument("--model", default=os.environ.get("NEAR_AI_MODEL") or DEFAULT_MODEL,
                   help=f"a confidential-fleet model id (default {DEFAULT_MODEL}; NEAR_AI_MODEL overrides)")
    p.add_argument("--chat", action="store_true", help="keep going: ask follow-ups, each answer verified")
    p.add_argument("--out", help="also write the answers to this file (your thesis is not written)")
    p.add_argument("--site-url", help="read THESIS.md and data from the published site instead of site/")
    p.add_argument("--site-dir", type=Path, help="read from this site directory (default: site/)")
    p.add_argument("--skip-gpu", action="store_true", help="do not ask NVIDIA's service to check the GPU evidence")
    p.add_argument("--accept-tcb-status", action="append", metavar="STATUS",
                   help="also accept this Intel TCB status, e.g. SWHardeningNeeded (repeatable)")
    p.add_argument("--no-verify", action="store_true", help="skip every attestation and signature check (not advised)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except ExploreError as exc:
        print(f"explore: {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"explore: network error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
