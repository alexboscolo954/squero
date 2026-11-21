FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /bin/uv
# sops decrypts each Contract's secrets.sops.yaml. Pinned, and verified per
# architecture; fetched here so the runtime stage needs no download tool.
ARG TARGETARCH
ARG SOPS_VERSION=3.13.3
RUN python - <<'EOF'
import hashlib, os, urllib.request
arch, version = os.environ["TARGETARCH"], os.environ["SOPS_VERSION"]
sha256 = {
    "amd64": "e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b",
    "arm64": "53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77",
}[arch]
url = f"https://github.com/getsops/sops/releases/download/v{version}/sops-v{version}.linux.{arch}"
data = urllib.request.urlopen(url, timeout=60).read()
if hashlib.sha256(data).hexdigest() != sha256:
    raise SystemExit(f"sops {version} {arch}: checksum mismatch")
with open("/usr/local/bin/sops", "wb") as f:
    f.write(data)
os.chmod("/usr/local/bin/sops", 0o755)
EOF
# mcp-grafana, the Grafana vendor (servers/vendors.py). Pinned and verified the
# same way; after a bump, `make snapshots` and commit the schema diff.
ARG MCP_GRAFANA_VERSION=2.0.2
RUN python - <<'EOF'
import hashlib, io, os, tarfile, urllib.request
arch, version = os.environ["TARGETARCH"], os.environ["MCP_GRAFANA_VERSION"]
name, sha256 = {
    "amd64": ("Linux_x86_64", "624e997b78bcadf236f82f8086827890ba77b6dfdc6fd6446f7c48dfd9e586f0"),
    "arm64": ("Linux_arm64", "96559ba56803c253ee4bfc2be971a5132bdd60a09b4a20375b6456564216ec48"),
}[arch]
url = (
    "https://github.com/grafana/mcp-grafana/releases/download/"
    f"v{version}/mcp-grafana_{name}.tar.gz"
)
data = urllib.request.urlopen(url, timeout=60).read()
if hashlib.sha256(data).hexdigest() != sha256:
    raise SystemExit(f"mcp-grafana {version} {arch}: checksum mismatch")
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    binary = tar.extractfile("mcp-grafana").read()
with open("/usr/local/bin/mcp-grafana", "wb") as f:
    f.write(binary)
os.chmod("/usr/local/bin/mcp-grafana", 0o755)
EOF
# azmcp, the Azure vendor: the official Azure MCP server's single-file binary from
# its npm package for this architecture, verified against npm's sha512 integrity.
ARG AZURE_MCP_VERSION=3.0.0
RUN python - <<'EOF'
import base64, hashlib, io, os, tarfile, urllib.request
arch, version = os.environ["TARGETARCH"], os.environ["AZURE_MCP_VERSION"]
name, sha512 = {
    "amd64": ("linux-x64", "btMwN0Vo2n8Qhk6yK5neC8QxbfXcZhgZcc2rugzvzaokBjQw3lbR1pAzQAnleetH4OGENbVgKNlCpHEUiVXfbQ=="),
    "arm64": ("linux-arm64", "h8hNNWCSwqReDj9yNxO5vLOJCLNdCRLe915Q6rRLyykNhz47DTWXU31GfCypX2PeSBEXx3hEngqDeg8pqNq9QQ=="),
}[arch]
url = f"https://registry.npmjs.org/@azure/mcp-{name}/-/mcp-{name}-{version}.tgz"
data = urllib.request.urlopen(url, timeout=120).read()
if base64.b64encode(hashlib.sha512(data).digest()).decode() != sha512:
    raise SystemExit(f"azmcp {version} {arch}: integrity mismatch")
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    binary = tar.extractfile("package/dist/azmcp").read()
os.makedirs("/opt/azmcp")
with open("/opt/azmcp/azmcp", "wb") as f:
    f.write(binary)
os.chmod("/opt/azmcp/azmcp", 0o755)
EOF
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# The console app (console/, slice 9.5a): built here, only its static files ship. They are the
# same for every architecture, so it builds on the runner's own (18.1d): arm64's npm ci under
# QEMU hung 75 minutes in 0.63.0's tag run.
FROM --platform=$BUILDPLATFORM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS console
WORKDIR /console
COPY console/package.json console/package-lock.json ./
RUN npm ci --ignore-scripts --no-fund --no-audit
COPY console/ ./
RUN npm run build

FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin squero
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /usr/local/bin/sops /usr/local/bin/sops
COPY --from=build /usr/local/bin/mcp-grafana /usr/local/bin/mcp-grafana
COPY --from=build /opt/azmcp /opt/azmcp
COPY --from=console /console/dist /opt/squero/app
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8080
LABEL org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"
ENTRYPOINT ["squero"]
CMD ["serve"]
