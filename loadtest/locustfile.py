"""Load test against the deployed API.

    API_KEY=$(az containerapp secret show -g rg-pulsestore -n pulsestore-api \
                --secret-name api-key --query value -o tsv) \
    locust -f loadtest/locustfile.py --headless -u 20 -r 2 -t 3m \
      --host https://pulsestore-api.calmmoss-34df9f8f.northcentralus.azurecontainerapps.io \
      --csv docs/loadtest/run1

The mix is what a clinician-facing client would do: mostly read heart rate, sometimes list the
abnormal beats of a recording, occasionally ask for the distribution across all recordings,
which is the one query that reads every row.
"""

import os
import random

from locust import HttpUser, between, task

KEY = {"x-api-key": os.environ["API_KEY"]}
RECORDINGS = list(range(1, 49))  # the 48 MIT-BIH records, loaded in that order


class Clinician(HttpUser):
    wait_time = between(0.5, 2)

    @task(6)
    def heart_rate(self):
        rid = random.choice(RECORDINGS)  # noqa: S311 - not cryptography
        self.client.get(f"/recordings/{rid}/heart-rate", headers=KEY, name="/heart-rate")

    @task(3)
    def abnormal_beats(self):
        rid = random.choice(RECORDINGS)  # noqa: S311
        self.client.get(
            f"/recordings/{rid}/annotations?aami_class=V&limit=500",
            headers=KEY,
            name="/annotations?V",
        )

    @task(2)
    def distribution_of_one(self):
        rid = random.choice(RECORDINGS)  # noqa: S311
        self.client.get(
            f"/stats/beat-distribution?recording_id={rid}", headers=KEY, name="/distribution?id"
        )

    @task(1)
    def distribution_of_all(self):
        self.client.get("/stats/beat-distribution", headers=KEY, name="/distribution (all)")

    @task(1)
    def health(self):
        self.client.get("/healthz", name="/healthz")
