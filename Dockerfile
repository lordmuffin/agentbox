# agentbox image: herdr + herd + Claude Code + tetherd, plus the tools the agents use.
# docs/agentbox.md §Image. Built by .github/workflows/build-agentbox.yml on the
# in-cluster buildkitd, which passes the Forgejo read token as a BuildKit secret:
#
#   buildctl build --frontend dockerfile.v0 --local context=agentbox --local dockerfile=agentbox \
#     --secret id=forgejo_read_token,src=<file> ...
#
# Pins (docs/agentbox.md §Decisions). Bump one here, then agentbox/VERSION and the
# StatefulSet's image tag together; the workflow refuses to overwrite a tag.
#
# /home/agent is the PVC, so nothing the image provides lives there: tools go in
# /usr/local and /opt, and first-run setup of $HOME is the entrypoint's job.
# apt packages come from trixie as of the build; the base digest pins the rest.
# Both Debian FROMs are the same digest (debian base-image in §Decisions).

# --- toolchains: Go and Node (also shipped in the final image), plus single binaries.
FROM debian:trixie-20261104-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS toolchains
ARG GO_VERSION=1.27.3
ARG GO_SHA256=65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40
ARG NODE_VERSION=26.13.0
ARG NODE_SHA256=d28a96af6c38596f6d9d97c0ee531c13d238d757742387d1928de822d959e997
ARG HERDR_VERSION=0.9.5
ARG HERDR_SHA256=14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d
ARG FJ_VERSION=0.7.0
ARG FJ_SHA256=6803c12eed17bfadb40ba95da2a6ab58f9b4a7dc10eabfe3512fce7e2fe6216f
# The k3s2 server version.
ARG KUBECTL_VERSION=1.36.2
ARG KUBECTL_SHA256=1e9045ec32bea85da43de85f0065358529ea7c7a152eca78154fba5b58c27d82
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /dl
RUN curl -fsSLo go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
 && echo "${GO_SHA256}  go.tgz" | sha256sum -c - \
 && tar -xzf go.tgz -C /usr/local \
 && curl -fsSLo node.txz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
 && echo "${NODE_SHA256}  node.txz" | sha256sum -c - \
 && mkdir /opt/node && tar -xJf node.txz -C /opt/node --strip-components=1 \
 && mkdir -p /out/bin \
 && curl -fsSLo /out/bin/herdr "https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-x86_64" \
 && echo "${HERDR_SHA256}  /out/bin/herdr" | sha256sum -c - \
 && curl -fsSLo fj.tgz "https://codeberg.org/forgejo-contrib/forgejo-cli/releases/download/v${FJ_VERSION}/forgejo-cli-x86_64-linux.tar.gz" \
 && echo "${FJ_SHA256}  fj.tgz" | sha256sum -c - \
 && tar -xzf fj.tgz -C /out/bin fj \
 && curl -fsSLo /out/bin/kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && echo "${KUBECTL_SHA256}  /out/bin/kubectl" | sha256sum -c - \
 && chmod 0755 /out/bin/* \
 && rm -rf /dl/*
ENV PATH=/usr/local/go/bin:/opt/node/bin:$PATH \
    GOTOOLCHAIN=local \
    GOPROXY=https://proxy.golang.org

# --- sources: the private repos, cloned with the read token from a BuildKit secret.
# The token is mounted for this RUN only, so it never lands in a layer, and it goes
# in a -c header, so it is never in a URL or .git/config.
FROM toolchains AS sources
ARG TETHER_REF=75e7ee733dbc8fecf4724f086786172148c0984b
ARG HERD_REF=0b360ba161416a2764de7ed5e700bfd71ab56799
RUN apt-get update \
 && apt-get install -y --no-install-recommends git make \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN --mount=type=secret,id=forgejo_read_token,required=true \
    auth="Authorization: token $(cat /run/secrets/forgejo_read_token)" \
 && git -c http.extraHeader="$auth" clone --quiet https://git.apj.dev/lordmuffin/tether.git tether \
 && git -c http.extraHeader="$auth" clone --quiet https://git.apj.dev/lordmuffin/herd.git herd \
 && git -C tether -c advice.detachedHead=false checkout --quiet "${TETHER_REF}" \
 && git -C herd -c advice.detachedHead=false checkout --quiet "${HERD_REF}"

# --- tetherd: the PWA (npm ci, typecheck, test, build) embedded in a static Go binary.
FROM sources AS tetherd
WORKDIR /src/tether
# ARGs don't carry into a child stage, so the version is read from the checkout.
RUN CGO_ENABLED=0 make build VERSION="$(git rev-parse HEAD)" \
 && install -Dm755 bin/tetherd /out/bin/tetherd

# --- herd: bin/herd plus its personas under /usr/local/share/herd.
FROM sources AS herd
WORKDIR /src/herd
RUN make install PREFIX=/usr/local DESTDIR=/out

# --- the image.
FROM debian:trixie-20261104-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
ARG CLAUDE_CODE_VERSION=2.1.319
# bsdextrautils is util-linux's `column`, which herd needs; libssl3t64 and zlib1g are fj's.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash bsdextrautils ca-certificates curl git jq libssl3t64 make openssh-client \
      python3 ripgrep tmux util-linux zlib1g \
 && rm -rf /var/lib/apt/lists/*
COPY --from=toolchains /usr/local/go /usr/local/go
COPY --from=toolchains /opt/node /opt/node
COPY --from=toolchains /out/bin/ /usr/local/bin/
COPY --from=tetherd /out/bin/tetherd /usr/local/bin/tetherd
COPY --from=herd /out/usr/local/ /usr/local/
ENV PATH=/usr/local/go/bin:/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    GOTOOLCHAIN=local \
    GOPROXY=https://proxy.golang.org \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    AGENTBOX_LIB=/opt/agentbox
# Its postinstall links the native linux-x64 binary from the optional dependency.
RUN npm install -g --prefix /usr/local "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && claude --version \
 && herdr --version \
 && herd -h >/dev/null \
 && tetherd version \
 && fj version \
 && kubectl version --client \
 && go version && node --version

COPY --chmod=0755 entrypoint.sh resume.py resume-plan.sh agentbox-plan.sh /opt/agentbox/
RUN ln -s /opt/agentbox/agentbox-plan.sh /usr/local/bin/agentbox-plan \
 && groupadd -g 1000 agent \
 && useradd -u 1000 -g 1000 -d /home/agent -m -s /bin/bash agent
USER 1000:1000
WORKDIR /home/agent
ENV HOME=/home/agent
ENTRYPOINT ["/opt/agentbox/entrypoint.sh"]
