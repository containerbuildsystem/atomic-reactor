"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals
"""
Push built image to pulp registry
"""

from dock.plugin import PostBuildPlugin
from dock.util import ImageName

import dockpulp
import dockpulp.imgutils

import gzip
import os
import re
from tempfile import NamedTemporaryFile


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
        # 'Secret'-type resource, and referenced by the sourceSecret
        # for the build. The path to our files is now given in the
        # environment variable SOURCE_SECRET_PATH.
        if self.username and self.password:
            p.login(self.username, self.password)
        else:
            if self.pulp_secret_path is not None:
                path = self.pulp_secret_path
                self.log.info("Using configured path %s for secrets" % path)
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
            p.certificate = cer
            p.key = key

    def push_tarball_to_pulp(self, image_names):
        self.log.info("Checking image before upload")
        self._check_file()

        p = dockpulp.Pulp(env=self.pulp_instance)
        self._set_auth(p)

        repos_tags_mapping = {}
        for image in image_names:
            repo = image.pulp_repo
            repos_tags_mapping.setdefault(repo, [])
            repos_tags_mapping[repo].append(image.tag)
        self.log.info("repo_tags_mapping = %s", repos_tags_mapping)
        p.push_tar_to_pulp(repos_tags_mapping, self.filename)

        # release
        self.log.info("Releasing to crane")
        p.crane()

        # Store the registry URI in the push configuration

        # We only want the hostname[:port]
        pulp_registry = re.sub(r'^https?://([^/]*)/?.*',
                               lambda m: m.groups()[0],
                               p.registry)

        self.workflow.push_conf.add_pulp_registry(self.pulp_instance,
                                                  pulp_registry)

        # Return the set of qualified repo names for this image
        return [ImageName(registry=pulp_registry, repo=repo, tag=tag)
                for repo, tags in repos_tags_mapping.items()
                for tag in tags]


def compress(filename, ifp):
    _chunk_size = 1024*1024  # 1Mb buffer for writing file
    with gzip.open(filename, "wb", compresslevel=6) as wfp:
        while True:
            data = ifp.read(_chunk_size)
            if data == b'':
                break

            wfp.write(data)


class PulpPushPlugin(PostBuildPlugin):
    key = "pulp_push"
    can_fail = False

    def __init__(self, tasker, workflow, pulp_registry_name, load_squashed_image=False,
                 image_names=None, pulp_secret_path=None, username=None, password=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param pulp_registry_name: str, name of pulp registry to use, specified in /etc/dockpulp.conf
        :param load_squashed_image: bool, when running squash plugin with `dont_load=True`,
                                    you may load the squashed tar with this switch
        :param image_names: list of additional image names
        :param pulp_secret_path: path to pulp.cer and pulp.key; $SOURCE_SECRET_PATH otherwise
        :param username: pulp username, used in preference to certificate and key
        :param password: pulp password, used in preference to certificate and key
        """
        # call parent constructor
        super(PulpPushPlugin, self).__init__(tasker, workflow)
        self.pulp_registry_name = pulp_registry_name
        self.image_names = image_names
        self.load_squashed_image = load_squashed_image
        self.pulp_secret_path = pulp_secret_path
        self.username = username
        self.password = password

    def push_tar(self, image_stream, image_names=None):
        with NamedTemporaryFile(prefix='docker-image-',
                                suffix='.tar.gz') as targz:
            # Compress the tarball docker gave us.
            self.log.info("Compressing tarball to %s", targz.name)
            compress(targz.name, image_stream)

            # Find out how to tag this image.
            self.log.info("Image names: %s", repr(image_names))

            # Give that compressed tarball to pulp.
            uploader = PulpUploader(self.workflow, self.pulp_registry_name, targz.name, self.log,
                                    pulp_secret_path=self.pulp_secret_path, username=self.username,
                                    password=self.password)
            return uploader.push_tarball_to_pulp(image_names)

    def run(self):
        image_names = self.workflow.tag_conf.images[:]
        # Add in additional image names, if any
        if self.image_names:
            self.log.info("extending image names: %s", self.image_names)
            image_names += [ImageName.parse(x) for x in self.image_names]

        if self.load_squashed_image:
            with open(self.workflow.exported_squashed_image.get("path"), "r") as image_stream:
                crane_repos = self.push_tar(image_stream, image_names)
        else:
            # Work out image ID
            image = self.workflow.image
            self.log.info("fetching image %s from docker", image)
            with self.tasker.d.get_image(image) as image_stream:
                crane_repos = self.push_tar(image_stream, image_names)

        self.log.info("Image now available at %s", crane_repos)
        return crane_repos
