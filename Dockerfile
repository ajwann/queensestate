# The HTTP transport for a hosted deployment, as scripts/deploy-gcp.sh builds it
# on Cloud Build. A stdio install needs none of this.
#
# The env-var prefix is spelled out here because Docker has no variable env-var
# names; everywhere else it is derived from APP in the deploy script.

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# The gcp extra adds the Firestore token store (QUEENSESTATE_TOKEN_STORE=firestore).
RUN pip install '.[gcp]' \
 && useradd --system --no-create-home --uid 10001 queensestate

USER queensestate

# The server binds every interface because the platform's front end is the only
# way in. It still requires QUEENSESTATE_PUBLIC_URL, the Google client, and an allow
# list at runtime, and refuses to start without them.
ENV QUEENSESTATE_TRANSPORT=http \
    QUEENSESTATE_HTTP_HOST=0.0.0.0 \
    QUEENSESTATE_HTTP_PORT=8080

EXPOSE 8080

# Cloud Run injects $PORT and defaults it to 8080. Honour it rather than trusting
# the two to agree: shell form so it expands, `exec` so the server keeps PID 1 and
# receives SIGTERM directly, and --port overriding QUEENSESTATE_HTTP_PORT.
CMD exec queensestate --port "${PORT:-8080}"
