"""
FastAPI reverse proxy + local engine API.

Routes
------
LLM proxy (anonymize out, deanonymize back):
    POST /v1/messages            -> Anthropic Messages API
    POST /v1/messages/count_tokens -> Anthropic token counting (anonymized too —
                                    SDKs send the full payload here)
    POST /v1/chat/completions    -> OpenAI-compatible chat completions
    POST /v1/completions         -> OpenAI-compatible legacy completions
    (any other path is passed through, routed by auth header; in strict mode,
     unknown POST/PUT/PATCH under /v1/* is refused instead of forwarded raw)

Local engine API (used by the Burp extension and tooling):
    POST /anonproxy/anonymize     {text, engagement?, is_tool_output?}
    POST /anonproxy/deanonymize   {text, engagement?}
    GET  /anonproxy/stats         ?engagement=
    GET  /anonproxy/export        ?engagement=
    GET  /anonproxy/health
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse

from ..config import Settings
from ..engine import Engine
from .. import audit
from . import transform, streaming

log = logging.getLogger("anonproxy.app")

_HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive",
               "transfer-encoding", "accept-encoding", "content-encoding"}

# Cap on concurrently live per-engagement engines. The engagement id comes from
# a client-controlled header, so without a cap any caller could mint unbounded
# engines (memory) and unbounded vault files under ANONPROXY_VAULT_DIR.
_MAX_ENGINES = 64


def _strip_v1(upstream: str) -> str:
    """Drop a trailing ``/v1`` from a configured upstream base URL.

    Our own routes are fixed at ``/v1/messages``, ``/v1/chat/completions``, etc.,
    and the real request URL is built as ``upstream + request.url.path`` — so
    ``upstream`` must NOT already include ``/v1``, or the path doubles up
    (``.../v1/v1/chat/completions``, a 404). But every OpenAI-compatible
    provider (OpenRouter, Groq, Together, ...) documents its ``base_url``
    *including* ``/v1`` — that's the copy-pasteable value users will reach for.
    Stripping it here means both forms work: ``https://openrouter.ai/api`` and
    ``https://openrouter.ai/api/v1``.
    """
    upstream = upstream.rstrip("/")
    if upstream.endswith("/v1"):
        upstream = upstream[:-len("/v1")]
    return upstream


def create_app(settings: Settings | None = None,
               client: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or Settings()
    engines: dict[str, Engine] = {}
    # ``client`` may be injected for testing (e.g. with a mock transport)
    if client is None:
        timeout = httpx.Timeout(600.0, connect=15.0)
        try:
            client = httpx.AsyncClient(timeout=timeout)
        except ImportError:
            # a SOCKS proxy is configured in the env but `socksio` isn't
            # installed — fall back to ignoring env proxies (install
            # `httpx[socks]` if you actually need that proxy).
            log.warning("SOCKS proxy in env but socksio missing; ignoring env proxies")
            client = httpx.AsyncClient(timeout=timeout, trust_env=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await client.aclose()

    app = FastAPI(title="Anonproxy", version="0.1.5", lifespan=lifespan)
    # exposed for tests/introspection: live engagement -> Engine cache
    app.state.engines = engines

    def get_engine(engagement: str | None) -> Engine:
        eid = (engagement or settings.engagement_id).strip()[:128]
        if not eid:
            eid = settings.engagement_id
        eng = engines.get(eid)
        if eng is None:
            if len(engines) >= _MAX_ENGINES:
                # FIFO eviction; disk-backed vaults reload on next use, and
                # ephemeral vaults simply drop — either way bounded.
                oldest = next(iter(engines))
                del engines[oldest]
                log.warning("engine cache full (%d); evicted engagement %r",
                            _MAX_ENGINES, oldest)
            s = Settings()
            s.__dict__.update(settings.__dict__)
            s.engagement_id = eid
            eng = engines[eid] = Engine(engagement=eid, settings=s)
        return eng

    def check_token(request: Request) -> None:
        if settings.engine_api_token:
            # accept the token via header or ?token= (the audit page uses both)
            supplied = (request.headers.get("x-anonproxy-token")
                        or request.query_params.get("token") or "")
            if not hmac.compare_digest(supplied, settings.engine_api_token):
                raise HTTPException(status_code=401, detail="bad token")

    def fwd_headers(request: Request) -> dict:
        h = {k: v for k, v in request.headers.items()
             if k.lower() not in _HOP_BY_HOP}
        h["accept-encoding"] = "identity"   # we rewrite bodies, so no compression
        return h

    # ------------------------------------------------------------------ engine API
    @app.get("/anonproxy/health")
    async def health(request: Request):
        # token-gated like every other engine endpoint: it reveals the detector
        # chain, Ollama host and installed models.
        check_token(request)
        eng = get_engine(None)
        return {"status": "ok", "engagement": settings.engagement_id,
                "detectors": eng.detector_status()}

    @app.post("/anonproxy/anonymize")
    async def api_anonymize(request: Request):
        check_token(request)
        body = await request.json()
        eng = get_engine(body.get("engagement"))
        result = await asyncio.to_thread(
            eng.anonymize, body.get("text", ""),
            body.get("is_tool_output", True),
        )
        return {"result": result}

    @app.post("/anonproxy/deanonymize")
    async def api_deanonymize(request: Request):
        check_token(request)
        body = await request.json()
        eng = get_engine(body.get("engagement"))
        result = await asyncio.to_thread(eng.deanonymize, body.get("text", ""))
        return {"result": result}

    @app.get("/anonproxy/stats")
    async def api_stats(request: Request, engagement: str | None = None):
        check_token(request)
        eng = get_engine(engagement)
        stats = eng.stats()
        stats["detector_failures"] = eng.detector_failures()
        return stats

    @app.get("/anonproxy/export")
    async def api_export(request: Request, engagement: str | None = None):
        check_token(request)
        return {"mappings": get_engine(engagement).export()}

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request, engagement: str | None = None):
        if not settings.audit_enabled:
            raise HTTPException(status_code=404, detail="audit disabled")
        # the page itself is not token-gated (it carries no data); the data
        # endpoints it calls are. it forwards ?token= to those fetches.
        eid = engagement or settings.engagement_id
        return audit.render_page(eid, token_required=bool(settings.engine_api_token))

    # ------------------------------------------------------------------ LLM proxy
    async def _proxy(request: Request, upstream: str, kind: str):
        engagement = request.headers.get("x-anonproxy-engagement")
        eng = get_engine(engagement)
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = None
            if raw:
                if settings.strict_mode:
                    raise HTTPException(
                        status_code=502,
                        detail="anonproxy strict mode: request body is not valid "
                               "JSON, refusing to forward it unanonymized",
                    )
                log.warning("request body is not valid JSON (%d bytes) — "
                            "forwarding unanonymized", len(raw))

        if body is not None:
            if kind == "anthropic":
                body = await asyncio.to_thread(transform.anonymize_anthropic_request, eng, body)
            elif kind == "openai":
                body = await asyncio.to_thread(transform.anonymize_openai_request, eng, body)
            out_bytes = json.dumps(body).encode()
            is_stream = bool(body.get("stream"))
        else:
            out_bytes = raw
            is_stream = False

        url = _strip_v1(upstream) + request.url.path
        headers = fwd_headers(request)

        if is_stream:
            gen = streaming.anthropic_stream if kind == "anthropic" else streaming.openai_stream
            req = client.build_request(
                "POST", url, headers=headers, content=out_bytes,
                params=request.url.query,
            )
            # open the upstream request first so the real status code is known
            # before we commit to a response — otherwise an upstream error
            # (401/429/500) would be reported to the client as a 200 SSE body.
            resp = await client.send(req, stream=True)

            if resp.status_code >= 400:
                payload = await resp.aread()
                await resp.aclose()
                return Response(content=payload, status_code=resp.status_code,
                                media_type=resp.headers.get("content-type", "application/json"))

            async def event_stream():
                async for piece in gen(eng, resp.aiter_bytes()):
                    yield piece
                await resp.aclose()

            return StreamingResponse(event_stream(), media_type="text/event-stream",
                                     status_code=resp.status_code)

        # non-streaming
        upstream_resp = await client.request(
            "POST", url, headers=headers, content=out_bytes, params=request.url.query,
        )
        ct = upstream_resp.headers.get("content-type", "")
        if "application/json" in ct:
            data = await asyncio.to_thread(
                transform.deanonymize_json, eng, upstream_resp.json()
            )
            return JSONResponse(content=data, status_code=upstream_resp.status_code)
        # non-JSON: deanonymize as text best-effort
        text = await asyncio.to_thread(eng.deanonymize, upstream_resp.text)
        return Response(content=text, status_code=upstream_resp.status_code,
                        media_type=ct or "text/plain")

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request):
        return await _proxy(request, settings.anthropic_upstream, "anthropic")

    @app.post("/v1/messages/count_tokens")
    async def anthropic_count_tokens(request: Request):
        # SDKs (and Claude Code) call this transparently with the FULL message
        # payload — routing it through passthrough instead would ship real
        # client data upstream unredacted. The response is just a token count,
        # so the deanonymize pass over it is a harmless no-op.
        return await _proxy(request, settings.anthropic_upstream, "anthropic")

    @app.post("/v1/chat/completions")
    async def openai_chat(request: Request):
        return await _proxy(request, settings.openai_upstream, "openai")

    @app.post("/v1/completions")
    async def openai_completions(request: Request):
        return await _proxy(request, settings.openai_upstream, "openai")

    # ------------------------------------------------------------------ passthrough
    @app.api_route("/{full_path:path}",
                   methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    async def passthrough(request: Request, full_path: str):
        # An unknown /v1/* write endpoint may carry conversation-shaped payloads
        # we can't anonymize (embeddings, legacy completions, future API surface).
        # In strict mode, fail closed rather than forward them unredacted.
        if (settings.strict_mode and request.method in ("POST", "PUT", "PATCH")
                and (full_path == "v1" or full_path.startswith("v1/"))):
            raise HTTPException(
                status_code=404,
                detail="anonproxy strict mode: unknown /v1 endpoint not "
                       "forwarded (it would bypass request anonymization)",
            )
        # route by auth header: x-api-key => Anthropic, else OpenAI
        upstream = (settings.anthropic_upstream
                    if request.headers.get("x-api-key")
                    else settings.openai_upstream)
        url = upstream.rstrip("/") + "/" + full_path
        resp = await client.request(
            request.method, url, headers=fwd_headers(request),
            content=await request.body(), params=request.url.query,
        )
        out = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        return Response(content=resp.content, status_code=resp.status_code, headers=out)

    return app
