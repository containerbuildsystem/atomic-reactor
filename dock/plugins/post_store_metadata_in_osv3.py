import json
import os

try:
    # py2
    from urlparse import urljoin
except Exception:
    # py3
    from urllib.parse import urljoin

from dock.plugin import PostBuildPlugin

import requests



class OSV3(object):
    def __init__(self, url, build_id):
        self.url = url
        self.build_id = build_id
        self.build_json = {}

    def _build_url(self, suffix):
        return urljoin(self.url, suffix)

    def _builds_url(self):
        url = self._build_url("builds/%s" % self.build_id)
        return url

    def fetch_build_json(self):
        r = requests.get(self._builds_url())
        self.build_json = r.json()
        return self.build_json

    def update_labels(self, d):
        assert self.build_json != {}
        self.build_json['metadata'].setdefault('labels', {})
        self.build_json['metadata']['labels'].update(d)

    def store_build_json(self):
        r = requests.put(self._builds_url(), data=json.dumps(self.build_json),
                         headers={'content-type': 'application/json'})
        if not r.ok:
            raise RuntimeError("failed to update build json: [%d]: %s", r.status_code, r.content)


class StoreMetadataInOSv3Plugin(PostBuildPlugin):
    key = "store_metadata_in_osv3"

    def __init__(self, tasker, workflow, url):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param url: str, URL to OSv3 instance
        """
        # call parent constructor
        super(StoreMetadataInOSv3Plugin, self).__init__(tasker, workflow)
        self.url = url

    def run(self):
        try:
            build_json = json.loads(os.environ["BUILD"])
        except KeyError:
            self.log.error("No $BUILD env variable. Probably not running in build container.")
            return
        try:
            build_id = build_json["metadata"]["name"]
        except KeyError:
            self.log.error("malformed build json")
            return
        self.log.info("build id = %s", build_id)

        o = OSV3(self.url, build_id)
        o.fetch_build_json()
        o.update_labels({
            "dockerfile": self.workflow.prebuild_results.get("dockerfile_content", ""),
            "artefacts": self.workflow.prebuild_results.get("distgit_fetch_artefacts", ""),
            "logs": "\n".join(self.workflow.build_logs),
            "rpm-packages": "\n".join(self.workflow.postbuild_results.get("all_rpm_packages", "")),
        })
        o.store_build_json()
