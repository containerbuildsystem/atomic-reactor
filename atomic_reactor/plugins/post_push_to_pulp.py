"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals
from dockpulp import setup_logger
from atomic_reactor import set_logging

"""
Push built image to pulp registry

Several authentication schemes are possible, including
username+password and key/certificate via sourceSecret.

However, the recommended scheme (since Origin 1.0.6) is to store a
key and certificate in a secret which the builder service account is
allowed to mount:

$ oc secrets new pulp ./pulp.key ./pulp.cer
secrets/pulp
$ oc secrets add serviceaccount/builder secret/pulp --for=mount

In the BuildConfig for atomic-reactor, specify the secret in the
strategy's 'secrets' array, specifying a mount path:

"secrets": [{
  "secretSource": {
    "name": "pulp"
  },
  "mountPath": "/var/run/secrets/pulp"
}]

In the configuration for this plugin, specify the same path for
pulp_secret_path:

"pulp_push": {
  "pulp_registry_name": ...,
  ...
  "pulp_secret_path": "/var/run/secrets/pulp"
}
"""

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import ImageName

import dockpulp
import dockpulp.imgutils

import os
import re
import tempfile


# let's silence warnings from dockpulp: there is one warning for every request
# which may result in tenths of messages: very annoying
# with "module", it just prints one warning -- this should balance security and UX
import warnings
warnings.filterwarnings("module")


class PulpUploader(object):
    CER = 'pulp.cer'
    KEY = 'pulp.key'

    def __init__(self, workflow, pulp_instance, filename, log, pulp_secret_path=None, username=None,
                 password=None):
        self.workflow = workflow
        self.pulp_instance = pulp_instance
        self.filename = filename
        self.pulp_secret_path = pulp_secret_path
        self.log = log
        # U/N & password has bigger prio than secret cert
        self.username = username
        self.password = password

        set_logging("dockpulp")

    def _check_file(self):
        # Sanity-check image
        metadata = dockpulp.imgutils.get_metadata(self.filename)
        vers = dockpulp.imgutils.get_versions(metadata)
        for _, version in vers.items():
            verparts = version.split('.')
            major = int(verparts[0])
            if major < 1:
                minor = 0
                if len(verparts) > 1:
                    minor = int(verparts[1])
                if minor < 10:
                    raise RuntimeError('An image layer uses an unsupported '
                                       'version of docker (%s)' % version)

        r_chk = dockpulp.imgutils.check_repo(self.filename)
        if r_chk == 1:
            raise RuntimeError('Image is missing a /repositories file')
        elif r_chk == 2:
            raise RuntimeError('Pulp demands exactly 1 repo in /repositories')
        elif r_chk == 3:
            raise RuntimeError('/repositories references external images')

    def _set_auth(self, p):
        # The pulp.cer and pulp.key values must be set in a
        # 'Secret'-type resource and mounted somewhere we can get at them.
        if self.username and self.password:
            p.login(self.username, self.password)
        else:
            if self.pulp_secret_path is not None:
                path = self.pulp_secret_path
                self.log.info("using configured path %s for secrets" % path)
            else:
                path = os.environ["SOURCE_SECRET_PATH"]
                self.log.info("SOURCE_SECRET_PATH=%s from environment" % path)

            # Work out the pathnames for the certificate/key pair.
            cer = os.path.join(path, self.CER)
            key = os.path.join(path, self.KEY)

            if not os.path.exists(cer):
                raise RuntimeError("Certificate does not exist.")
            if not os.path.exists(key):
                raise RuntimeError("Key does not exist.")

            # Tell dockpulp.
            p.set_certs(cer, key)

    def push_tarball_to_pulp(self, image_names):
        self.log.info("checking image before upload")
        self._check_file()

        p = dockpulp.Pulp(env=self.pulp_instance)
        self._set_auth(p)

        # {
        #     "repo-id": {
        #         "registry-id": "",
        #         "tags": [],
        #     },
        #     ...
        # }
        repos_tags_mapping = {}
        for image in image_names:
            repo = image.pulp_repo
            repos_tags_mapping.setdefault(repo, {})
            repos_tags_mapping[repo]["registry-id"] = image.to_str(registry=False, tag=False)
            repos_tags_mapping[repo].setdefault("tags", [])
            repos_tags_mapping[repo]["tags"].append(image.tag)
        self.log.info("repo_tags_mapping = %s", repos_tags_mapping)
        task_ids = p.push_tar_to_pulp(repos_tags_mapping, self.filename)

        self.log.info("waiting for repos to be published to crane, tasks: %s",
                      ", ".join(map(str, task_ids)))
        p.watch_tasks(task_ids)

        # Store the registry URI in the push configuration

        # We only want the hostname[:port]
        pulp_registry = re.sub(r'^https?://([^/]*)/?.*',
                               lambda m: m.groups()[0],
                               p.registry)

        self.workflow.push_conf.add_pulp_registry(self.pulp_instance,
                                                  pulp_registry)

        # Return the set of qualified repo names for this image
        return [ImageName(registry=pulp_registry, repo=repodata["registry-id"], tag=tag)
                for repo, repodata in repos_tags_mapping.items()
                for tag in repodata['tags']]


class PulpPushPlugin(PostBuildPlugin):
    key = "pulp_push"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, pulp_registry_name, load_squashed_image=None,
                 load_exported_image=None, image_names=None, pulp_secret_path=None,
                 username=None, password=None, dockpulp_loglevel=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param pulp_registry_name: str, name of pulp registry to use, specified in /etc/dockpulp.conf
        :param load_squashed_image: obsolete name for load_exported_image, please don't use
        :param load_exported_image: bool, use exported tar instead of image from Docker
        :param image_names: list of additional image names
        :param pulp_secret_path: path to pulp.cer and pulp.key; $SOURCE_SECRET_PATH otherwise
        :param username: pulp username, used in preference to certificate and key
        :param password: pulp password, used in preference to certificate and key
        """
        # call parent constructor
        super(PulpPushPlugin, self).__init__(tasker, workflow)
        self.pulp_registry_name = pulp_registry_name
        self.image_names = image_names
        if load_squashed_image is not None and load_exported_image is not None and \
                (load_squashed_image != load_exported_image):
            raise RuntimeError(
                'Can\'t use load_squashed_image and load_exported_image with different values')
        if load_squashed_image is not None:
            self.log.warning(
                'load_squashed_image argument is obsolete and will be removed in a future version;'
                'please use load_exported_image instead')
        self.load_exported_image = load_exported_image or load_squashed_image or False
        self.pulp_secret_path = pulp_secret_path
        self.username = username
        self.password = password

        if dockpulp_loglevel is not None:
            logger = setup_logger(dockpulp.log)
            try:
                logger.setLevel(dockpulp_loglevel)
            except (ValueError, TypeError) as ex:
                self.log.error("Can't set provided log level %r: %r", dockpulp_loglevel, ex)

    def push_tar(self, image_path, image_names=None):
        # Find out how to tag this image.
        self.log.info("image names: %s", [str(image_name) for image_name in image_names])

        # Give that compressed tarball to pulp.
        uploader = PulpUploader(self.workflow, self.pulp_registry_name, image_path, self.log,
                                pulp_secret_path=self.pulp_secret_path, username=self.username,
                                password=self.password)
        return uploader.push_tarball_to_pulp(image_names)

    def run(self):
        image_names = self.workflow.tag_conf.images[:]
        # Add in additional image names, if any
        if self.image_names:
            self.log.info("extending image names: %s", self.image_names)
            image_names += [ImageName.parse(x) for x in self.image_names]

        if self.load_exported_image:
            if len(self.workflow.exported_image_sequence) == 0:
                raise RuntimeError('no exported image to push to pulp')
            crane_repos = self.push_tar(self.workflow.exported_image_sequence[-1].get("path"),
                                        image_names)
        else:
            # Work out image ID
            image = self.workflow.image
            self.log.info("fetching image %s from docker", image)
            with tempfile.NamedTemporaryFile(prefix='docker-image-', suffix='.tar') as image_file:
                image_file.write(self.tasker.d.get_image(image).data)
                crane_repos = self.push_tar(image_file.name, image_names)

        for image_name in crane_repos:
            self.log.info("image available at %s", str(image_name))

        return crane_repos
