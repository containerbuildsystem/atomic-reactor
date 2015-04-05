import json
import os
from osbs.core import Openshift

try:
    # py2
    from urlparse import urljoin
except Exception:
    # py3
    from urllib.parse import urljoin

from dock.plugin import PostBuildPlugin


class StoreMetadataInOSv3Plugin(PostBuildPlugin):
    key = "store_metadata_in_osv3"

    def __init__(self, tasker, workflow, url, verify_ssl=True):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param url: str, URL to OSv3 instance
        """
        # call parent constructor
        super(StoreMetadataInOSv3Plugin, self).__init__(tasker, workflow)
        self.url = url
        self.verify_ssl = verify_ssl

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

        api_url = urljoin(self.url, "/osapi/v1beta1/")
        oauth_url = urljoin(self.url, "/oauth/authorize")  # MUST NOT END WITH SLASH

        # initial setup will use host based auth: apache will be set to accept everything
        # from specific IP and will set specific X-Remote-User for such requests
        o = Openshift(api_url, oauth_url, None, use_auth=True, verify_ssl=self.verify_ssl)

        labels = {
            "dockerfile": self.workflow.prebuild_results.get("dockerfile_content", ""),
            "artefacts": self.workflow.prebuild_results.get("distgit_fetch_artefacts", ""),
            "logs": "\n".join(self.workflow.build_logs),
            "rpm-packages": "\n".join(self.workflow.postbuild_results.get("all_rpm_packages", "")),
        }
        o.set_labels_on_build(build_id, labels)
