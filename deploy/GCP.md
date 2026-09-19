# Running on Google Cloud Run, in your own GCP project

`scripts/deploy-gcp.sh` does all of it.

```bash
git clone https://github.com/ajwann/queensestate.git && cd queensestate
scripts/deploy-gcp.sh
```

It creates a dedicated project, deploys the HTTP transport to Cloud Run with
sign-ins kept in Firestore, and checks the result from the internet. The service
scales to zero, so a personal server normally stays inside Google Cloud's free
tier.

Re-running the script is safe, and it is also how you update: it finds the
project by its `app=queensestate` label, reuses everything that already exists, and
redeploys the checkout you run it from. It does not remember your settings, though;
see [Update and remove](#update-and-remove).

This is only for the **hosted HTTP server**. To run the server on your own
machine for one MCP client, use stdio instead (see the main
[README](../README.md#setup)). That needs no account of any kind.

## What you need

- A Google account with a **billing account**. Google Cloud requires one even
  for free-tier usage.
- The [gcloud CLI](https://cloud.google.com/sdk/docs/install), logged in with
  `gcloud auth login`.
- `git` and a clone of this repository. Cloud Build builds the container from
  it, so Docker is not needed locally.

## Settings

The script prompts for anything unset. Setting everything in the environment
and passing `--non-interactive` makes it run unattended.

| Variable | Default | Notes |
| --- | --- | --- |
| `QUEENSESTATE_GCP_PROJECT` | the project labeled `app=queensestate`, else a new `queensestate-xxxxxx` | Point it at an existing project to deploy there; see [several servers in one project](#several-servers-in-one-project). |
| `QUEENSESTATE_GCP_REGION` | `us-east1` | Must be a region with Cloud Run, Firestore, and Artifact Registry. |
| `QUEENSESTATE_GCP_SERVICE` | `queensestate` | Cloud Run service name; part of the URL. |
| `QUEENSESTATE_GCP_BILLING_ACCOUNT` | the only open billing account | Asked for when there are several. |
| `QUEENSESTATE_GCP_MAX_INSTANCES` | `1` | Caps worst-case compute cost. |
| `QUEENSESTATE_SHARED_BUDGET` | `false` | `true` (what this deployment uses) means another script owns the budget and kill switch, so this one creates neither; see [the shared spend cap](#the-shared-spend-cap). |
| `QUEENSESTATE_BUDGET_USD` | `5` | Monthly budget, when this deployment owns one. Ignored if `SHARED_BUDGET` is set. |
| `QUEENSESTATE_SPEND_CAP` | `false` | `true` turns its own budget into a hard cap. Ignored if `SHARED_BUDGET` is set. |
| `QUEENSESTATE_SPEND_CAP_AT` | `0.8` | Fraction of that budget at which the cap fires. |
| `QUEENSESTATE_GOOGLE_CLIENT_ID` | | From the OAuth client described next. |
| `QUEENSESTATE_GOOGLE_CLIENT_SECRET` | | Kept in Secret Manager, never in the service's environment. Asked for once; later runs reuse it unless it is set or `--reset-secret` is passed. |
| `QUEENSESTATE_ALLOWED_EMAILS` | | Who may sign in. At least one of these three is required. |
| `QUEENSESTATE_ALLOWED_DOMAINS` | | Every verified address on these domains. |
| `QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT` | `false` | `true` makes the server public to any Google account. |
| `QUEENSESTATE_DOMAIN` | | Serve at a domain of your own instead of the `run.app` URL; see [A custom domain](#a-custom-domain). |

## The one manual step: the Google OAuth client

Google has no API for creating OAuth clients, so the script creates the project,
works out the service's URL, prints direct links and the exact redirect URI, and
waits. The URL is fixed before anything is deployed:
`https://<service>-<project number>.<region>.run.app`, or `https://<QUEENSESTATE_DOMAIN>`
when that is set.

In the project the script created (the links it prints go straight there):

1. **Google Auth Platform → Branding.** Give the app a name and your support
   email. With `QUEENSESTATE_DOMAIN`, also add its parent domain under **Authorized
   domains**.
2. **Audience → External.**
   - For a private server, leave it in **Testing** and add each allowed address
     under **Test users**.
   - For a server anyone can use, click **Publish app**. The server requests only
     `openid` and `email`, which are non-sensitive scopes, so Google does not
     review what data it asks for. **Verification Center** shows whether Google
     wants to verify the app's branding.
3. **Clients → Create client → Web application**, and add one authorized
   redirect URI, exactly as printed:

   ```
   https://queensestate-123456789012.us-east1.run.app/auth/google/callback
   ```

Paste the client ID and secret when the script asks. A mismatched redirect URI
is the most common failure; the server logs the URI it expects at every startup.

**The audience and the allow list must agree.** In Testing, Google refuses any
account that is not a test user, before this server's allow list is ever
consulted. That error comes from Google and won't point here.

You can reuse an OAuth client that already exists in another project, such as a
Raspberry Pi deployment's. Add the new redirect URI to it and pass its ID and
secret.

## What it does

| Stage | Action |
| --- | --- |
| Preflight | gcloud present and logged in, running from a git checkout |
| Project | Finds the project labeled `app=queensestate`, or creates one |
| Billing | Links the billing account |
| APIs | Cloud Run, Cloud Build, Artifact Registry, Firestore, Secret Manager, Budgets |
| OAuth client | Prints the links and redirect URI, then asks for the ID and secret |
| Firestore | A Native-mode database named after the service, with TTL policies on every token collection |
| Secret | `<service>-google-client-secret` in Secret Manager; a new version only when it changes |
| IAM | A runtime service account that can reach only Firestore and that one secret |
| Registry | A Docker repository that keeps the three newest images |
| Build | Cloud Build builds the `Dockerfile`, tagged with the git commit |
| Deploy | Cloud Run, scaling from zero to `QUEENSESTATE_GCP_MAX_INSTANCES` |
| Domain | With `QUEENSESTATE_DOMAIN`, maps the domain to the service and prints its DNS records |
| Budget | Shared with the other servers in this project; see [the shared spend cap](#the-shared-spend-cap) |
| Verify | The discovery document names the public URL, and anonymous calls get 401 |

`--allow-unauthenticated` on the Cloud Run service is deliberate. It means Cloud
Run passes requests through to the app, and the app's own OAuth refuses every
`/mcp` call without a token, exactly as on the Pi.

## Who is allowed in

Google proves who a caller is, and the allow list decides whether that person
may use the server. With none of the three settings the server refuses to start.

- `QUEENSESTATE_ALLOWED_EMAILS=you@gmail.com,friend@example.com` admits those accounts.
- `QUEENSESTATE_ALLOWED_DOMAINS=example.com` admits every verified address on a domain.
- `QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT=true` admits everyone, which makes the server
  public. Publish the OAuth app too, or Google keeps everyone but test users out.

To change the list, re-run the script with the new value.

## A custom domain

By default the server's address is its `run.app` URL. To serve it at a domain of
your own, such as `mcp.example.com`, set `QUEENSESTATE_DOMAIN`. The script then:

- makes `https://<QUEENSESTATE_DOMAIN>` the server's public URL: the OAuth issuer, the
  resource that tokens are issued for, and the base of the redirect URI;
- maps the domain to the service with a
  [Cloud Run domain mapping](https://cloud.google.com/run/docs/mapping-custom-domains),
  which comes with a Google-managed certificate;
- prints the DNS records to add.

Before the first run with it:

1. **Verify the domain** in [Google Search Console](https://search.google.com/search-console),
   signed in as the account gcloud uses. Verifying the parent domain
   (`example.com`) covers its subdomains. Until it's verified, Google refuses the
   mapping, and the script stops before it changes a running service.
2. **Use a region that offers domain mappings:** asia-east1, asia-northeast1,
   asia-southeast1, europe-north1, europe-west1, europe-west4, us-central1,
   us-east1, us-east4, or us-west1.

After the run, add the records the script printed at your DNS host. On
Cloudflare, make the record **DNS only**: a proxied record stops Google from
issuing the certificate. Google issues it once the record resolves, which takes
from about 15 minutes to a day. Until then the domain doesn't answer. Re-run the
script to check again.

Things to know:

- Google labels domain mappings **Preview**, meaning not production-ready. The
  supported alternative, a global external Application Load Balancer, has a
  fixed monthly cost far above what this server uses.
- The server accepts MCP calls only at its public host name. Once that's the
  custom domain, the `run.app` URL still serves the discovery document, but
  clients must connect through the domain.
- Changing the public URL changes the OAuth issuer, so every connected client has
  to sign in again, and the OAuth client needs the new redirect URI.
- To go back to `run.app`, re-run without `QUEENSESTATE_DOMAIN`, then delete the mapping
  in the console (**Cloud Run → Domain mappings**).

## Several servers in one project

Each server normally gets a project of its own, which keeps its budget, its
sign-ins, and its blast radius separate. To put several in one project instead,
pass `QUEENSESTATE_GCP_PROJECT`. Everything the script creates is then named after
the service, so they do not collide:

| Resource | Name |
| --- | --- |
| Cloud Run service | `$QUEENSESTATE_GCP_SERVICE` |
| Firestore database | the same name, **not** `(default)` |
| Secret | `<service>-google-client-secret` |
| Artifact Registry | `<service>` |
| Service accounts | `<service>-run`, `<service>-build`, `<service>-spendcap` |
| Pub/Sub topic | `<service>-budget` |

The database matters most: the token collections have fixed names, so two
servers sharing one database would also share each other's registered clients
and tokens.

Two things do not separate, because Google Cloud does not separate them:

- **The budget covers the whole project.** Each server's run creates its own
  budget, and every one of them measures total project spend. Two servers with a
  $5 budget each alert when the project reaches $5, not $10.
- **The spend cap unlinks billing for the project.** It takes down every server
  in it, not just the one that spent the money.

If either matters, give the server its own project.

`--teardown` refuses to delete a project that is not labelled `app=queensestate`,
which is exactly the case for a shared project you created yourself. Remove
those by hand, deliberately.

## Cost

A personal server normally costs nothing: Cloud Run, Firestore, Secret Manager,
Artifact Registry, Cloud Build, and Pub/Sub all have free tiers well above what
it uses. The service scales to zero between uses, and the first call after an
idle spell waits a few seconds while an instance starts, downloads the static
schedule, and parses its timetable.

### The shared spend cap

QueensEstate shares a project with QueensCoach, and **they share one cap**. This
is not a convenience: a budget measures the spend of the whole project, and
unlinking billing takes the whole project down. Two caps in one project would
measure exactly the same money twice and duplicate every alert, so there is one,
owned by neither server:

| Resource | Name |
| --- | --- |
| Budget | `Adam Wanningers MCP Servers shared cap` |
| Pub/Sub topic | `mcp-servers-shared-cap` |
| Cloud Run function | `mcp-servers-shared-cap` |
| Service account | `mcp-shared-spendcap` |

It lives in its own repository, **[mcp-servers-shared-cap](https://github.com/ajwann/mcp-servers-shared-cap)**,
checked out beside this one, because it belongs to the project rather than to any
server in it:

```bash
cd ../mcp-servers-shared-cap
./shared-spend-cap.sh          # create or update the cap
./shared-spend-cap.sh --show   # print the live configuration
```

Every server's deploy sets `*_SHARED_BUDGET=true` so it leaves the cap alone —
without that flag, a deploy creates a per-server budget of its own and you get two.

**It fires when the two servers together reach $5 in a month** (a $5 budget at a
`SPEND_CAP_AT` of `1.0`). Google's cost data lags by several hours, so real spend
can edge slightly past $5 before billing is actually unlinked.

**Google Cloud has no hard spending limit.** A budget only sends email. The cap
is Google's documented substitute:

1. The budget publishes its cost updates to a Pub/Sub topic, several times a
   day.
2. A small Cloud Run function (`function/` in the cap repo) reads each update.
3. When spend reaches `SPEND_CAP_AT` × the budget, it **unlinks billing from this
   project**, stopping every server in it.

The function's identity has **Project Billing Manager on this project only**,
not the billing-account-wide admin role that Google's tutorial uses, so it
cannot touch any other project on your billing account.

Know what you are opting into:

- **It takes the server offline.** Every paid service in the project stops. To
  bring it back, re-run the script, which links billing again.
- **Google may delete the project's resources** if billing stays off. Nothing
  here is precious: the images are rebuilt by the script, and losing the token
  collections only means everyone signs in again.
- **Billing data lags by hours**, which is why the cap fires at 80% by default.
  A burst of abuse inside that window can still overshoot a little.
- **One instance at most** (`QUEENSESTATE_GCP_MAX_INSTANCES=1`) keeps the worst case
  small even before the cap reacts.

## Verify

The script checks both of these itself, and you can re-run them from anywhere.
With a custom domain, use it in place of the `run.app` URL once its certificate
is issued.

```bash
curl https://queensestate-123456789012.us-east1.run.app/.well-known/oauth-protected-resource/mcp
```

The `resource` in the response must be the service URL followed by `/mcp`.

```bash
curl -i -X POST https://queensestate-123456789012.us-east1.run.app/mcp \
  -H 'accept: application/json, text/event-stream' \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect **401**. A 200 would mean the server is unprotected.

Then add the connector: in Claude, **Settings → Connectors → Add custom
connector** with the `/mcp` URL, or in Claude Code:

```bash
claude mcp add --transport http queensestate https://queensestate-123456789012.us-east1.run.app/mcp
```

## Update and remove

```bash
git pull && scripts/deploy-gcp.sh     # rebuild and redeploy the current checkout
scripts/deploy-gcp.sh --teardown      # delete the whole project, after confirming
```

**Pass the same settings on every run.** The script keeps only the client secret,
in Secret Manager; everything else comes from the environment each time, and an
unset value means its default. So a bare re-run prompts again for the client ID and
allow list, and it also changes a working server:

- Without `QUEENSESTATE_DOMAIN`, the public URL goes back to `run.app`, and clients
  connected at the custom domain stop working.
- Without `QUEENSESTATE_SPEND_CAP=true`, the budget stops feeding the spend cap.
- Without `QUEENSESTATE_GCP_PROJECT`, a project not labeled `app=queensestate` (a
  shared one you created yourself) is not found, and a new project is created.
- `QUEENSESTATE_BUDGET_USD` and `QUEENSESTATE_GCP_MAX_INSTANCES` return to `5` and `1`.

The client ID and allow list are visible on the running service
(`gcloud run services describe <service> --region <region> --project <project>`).
An update for a public server on its own domain, in a shared project, looks like:

```bash
QUEENSESTATE_GCP_PROJECT=my-project \
QUEENSESTATE_DOMAIN=mcp.example.com \
QUEENSESTATE_GOOGLE_CLIENT_ID=1234-abcd.apps.googleusercontent.com \
QUEENSESTATE_ALLOW_ANY_GOOGLE_ACCOUNT=true \
QUEENSESTATE_SPEND_CAP=true \
scripts/deploy-gcp.sh --non-interactive
```

A teardown is recoverable for 30 days (`gcloud projects undelete`); after that
the project ID can never be reused.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `redirect_uri_mismatch` | The OAuth client's redirect URI differs from the one the script printed. |
| `access_denied` from Google, or "app has not completed verification" | Testing mode and you are not a test user, or the app is not published. |
| "This Google account is not allowed" | Signed in with an address the allow list does not admit. |
| Script stops at billing | No open billing account, or its project quota is used up. |
| Build fails with a permission error | The default compute service account lacks `roles/cloudbuild.builds.builder`; re-run the script. |
| Server returns 503 or times out after the cap fired | Billing was unlinked. Re-run the script. |
| Service will not start | Missing settings print as `configuration error: ...` in the Cloud Run logs. |
| Connector fails, `curl` works | The connector URL must end in `/mcp`. |
| `could not map <domain>` | The domain isn't verified in Search Console for the account gcloud uses, or the region has no domain mappings. |
| The custom domain times out, or its certificate is wrong | The DNS record is missing or proxied, or Google hasn't issued the certificate yet (up to a day). |
| HTTP 421 from `/mcp` | The client connected through the `run.app` URL, but the server's public URL is its custom domain. |

```bash
# The server
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="queensestate"' \
  --project <project> --limit 50
# The shared spend cap, which runs on Cloud Run too
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="mcp-servers-shared-cap"' \
  --project <project> --limit 50
```

Tokens live in Firestore, so restarts, redeploys, and scale-to-zero keep everyone
signed in.

## Moving from the Raspberry Pi

Once the Cloud Run server works end to end:

1. In Claude, remove the old connector and add the new `/mcp` URL.
2. On the Pi, stop and remove the server and its tunnel service:

   ```bash
   sudo ~/queensestate/scripts/install.sh --uninstall
   ```

3. In the Cloudflare dashboard, delete the `queensestate` tunnel and the `CNAME`
   pointing at it. The uninstall leaves both alone.
4. Optionally, remove the Pi's redirect URI from the OAuth client.
