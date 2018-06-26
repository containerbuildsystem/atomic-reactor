"""
Copyright (c) 2017, 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import print_function, unicode_literals

import logging
import os
import re
import time
import warnings
from collections import namedtuple

try:
    import dockpulp
except (ImportError, SyntaxError):
    dockpulp = None

from atomic_reactor.constants import (LOCKEDPULPREPOSITORY_RETRIES,
                                      LOCKEDPULPREPOSITORY_BACKOFF)


PulpRepo = namedtuple('PulpRepo', ['registry_id', 'tags'])

# let's silence warnings from dockpulp: there is one warning for every request
# which may result in tenths of messages: very annoying
# with "module", it just prints one warning -- this should balance security and UX
warnings.filterwarnings("module")

logger = logging.getLogger(__name__)


class PulpLogWrapper(object):
    def __init__(self):
        if dockpulp:
            self.pulp_log = dockpulp.setup_logger(dockpulp.log)
        else:
            self.pulp_log = logging.getLogger(__name__)

    def get_pulp_logger(self):
        return self.pulp_log


PulpLog = PulpLogWrapper()


class PulpHandler(object):
    CER = 'pulp.cer'
    KEY = 'pulp.key'

    def __init__(self, workflow, pulp_instance, log,
                 pulp_secret_path=None,
                 username=None, password=None, dockpulp_loglevel=None):
        self.workflow = workflow
        self.pulp_instance = pulp_instance
        self.pulp_secret_path = pulp_secret_path
        self.log = log
        # U/N & password has bigger prio than secret cert
        self.username = username
        self.password = password
        self.p = None
        logger = PulpLog.get_pulp_logger()

        if dockpulp_loglevel is not None:
            try:
                logger.setLevel(dockpulp_loglevel)
            except (ValueError, TypeError) as ex:
                self.log.error("Can't set provided log level %r: %r", dockpulp_loglevel, ex)

    def check_file(self, filename):
        # Sanity-check image
        manifest = dockpulp.imgutils.get_manifest(filename)
        vers = dockpulp.imgutils.get_versions(manifest)
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

        r_chk = dockpulp.imgutils.check_repo(filename)
        if r_chk == 1:
            raise RuntimeError('Image is missing a /repositories file')
        elif r_chk == 2:
            raise RuntimeError('Pulp demands exactly 1 repo in /repositories')
        elif r_chk == 3:
            raise RuntimeError('/repositories references external images')

    def _set_auth(self):
        # The pulp.cer and pulp.key values must be set in a
        # 'Secret'-type resource and mounted somewhere we can get at them.
        if self.username and self.password:
            self.p.login(self.username, self.password)
        elif self.pulp_secret_path or 'SOURCE_SECRET_PATH' in os.environ:
            if self.pulp_secret_path is not None:
                path = self.pulp_secret_path
                self.log.info("using configured path %s for secrets", path)
            else:
                path = os.environ["SOURCE_SECRET_PATH"]
                self.log.info("SOURCE_SECRET_PATH=%s from environment", path)

            # Work out the pathnames for the certificate/key pair.
            cer = os.path.join(path, self.CER)
            key = os.path.join(path, self.KEY)

            if not os.path.exists(cer):
                raise RuntimeError("Certificate does not exist.")
            if not os.path.exists(key):
                raise RuntimeError("Key does not exist.")

            # Tell dockpulp.
            self.p.set_certs(cer, key)

    def _ensure_repos(self, pulp_repos, repo_prefix):
        repos = pulp_repos.keys()
        try:
            found_repos = self.p.getRepos(repos, fields=["id"], distributors=True)
        except TypeError:
            found_repos = self.p.getRepos(repos, fields=["id"])
        found_repo_ids = [repo["id"] for repo in found_repos]

        missing_repos = set(repos) - set(found_repo_ids)
        self.log.info("Missing repos: %s" % ", ".join(missing_repos))
        for repo in missing_repos:
            self.p.createRepo(repo, None,
                              registry_id=pulp_repos[repo].registry_id,
                              prefix_with=repo_prefix)

        for repo in found_repos:
            do_update = False
            if 'distributors' in repo:
                for d in repo["distributors"]:
                    if d["auto_publish"]:
                        do_update = True
                        break
            if do_update:
                self.p.updateRepo(repo["id"], {'auto_publish': False})

    def get_tar_metadata(self, tarfile):
        metadata = dockpulp.imgutils.get_metadata(tarfile)
        pulp_md = dockpulp.imgutils.get_metadata_pulp(metadata)
        layers = pulp_md.keys()
        top_layer = dockpulp.imgutils.get_top_layer(pulp_md)

        return top_layer, layers

    def create_dockpulp(self):
        self.p = dockpulp.Pulp(env=self.pulp_instance)
        self._set_auth()

    def create_dockpulp_and_repos(self, image_names, repo_prefix="redhat-"):
        self.create_dockpulp()

        # pulp_repos is mapping from repo-ids to registry-ids and tags
        # which should be applied to those repos, expected structure:
        # {
        #    "my-image": PulpRepo(registry_id="nick/my-image", tags=["v1", "latest"])
        #    ...
        # }
        pulp_repos = {}
        for image in image_names:
            repo_id = image.pulp_repo
            self.log.info("adding repo %s", repo_id)
            tag = image.tag if image.tag else 'latest'
            if repo_prefix:
                repo_id = repo_prefix + repo_id

            if repo_id in pulp_repos:
                pulp_repos[repo_id].tags.append(tag)
            else:
                pulp_repos[repo_id] = PulpRepo(
                    registry_id=image.to_str(registry=False, tag=False),
                    tags=[tag]
                )

        self._ensure_repos(pulp_repos, repo_prefix)

        return pulp_repos

    def get_image_ids_existing(self, layers):
        return self.p.getImageIdsExist(layers)

    def upload(self, filename, repo_id):
        # returns boolean that is was uploaded to redhat-everything
        try:
            self.log.debug("Uploading %s to %s", filename, repo_id)
            self.p.upload(filename, repo_id)
            return False

        except TypeError:
            self.log.debug('Falling back to uploading %s to redhat-everything repo', filename)
            self.p.upload(filename)
            return True

    def copy(self, repo_id, layer):
        self.p.copy(repo_id, layer)

    def copy_v1_layers(self, repo_id, layers):
        pulp_filter = {'unit': {
            '$or': [{'image_id': layer} for layer in layers]
        }}
        self.p.copy_filters(repo_id, filters=pulp_filter, v1=True, v2=False)

    def update_repo(self, repo_id, tag):
        self.p.updateRepo(repo_id, tag)

    def remove_image(self, repo_id, image):
        self.p.remove(repo_id, image)

    def publish(self, keys):
        # dockpulp will call publish for every repository if len(keys) == 0
        # so check to make sure keys has values
        assert keys
        task_ids = self.p.crane(keys, wait=True)
        self.log.info("waiting for repos to be published to crane, tasks: %s",
                      ", ".join(map(str, task_ids)))
        self.p.watch_tasks(task_ids)

    def get_registry_hostname(self):
        return re.sub(r'^https?://([^/]*)/?.*', lambda m: m.groups()[0], self.p.registry)

    def get_pulp_instance(self):
        return self.pulp_instance


class LockedPulpRepository(object):
    """
    Context manager for Pulp repository operations.

    This context manager creates a 'lock' repository on entry, and
    deletes the repository on exit.

    Usage:

    with LockedPulpRepository(handler, repo_id) as locked_repo:
        locked_repo.set_image_tags(v1_image_id, tags)
    """

    def __init__(self, pulp, repo_id, prefix='lock-'):
        self.pulp = pulp
        self.repo_id = repo_id
        self.prefix = prefix

        self.retry_times = LOCKEDPULPREPOSITORY_RETRIES
        self.retry_delay = LOCKEDPULPREPOSITORY_BACKOFF

    def __enter__(self):
        logger.info("creating lock repository %s%s", self.prefix, self.repo_id)
        registry_id = 'lock/{}'.format(self.repo_id)
        retry = 0
        total_attempts = 1 + self.retry_times  # initial attempt plus retries
        for counter in range(total_attempts):
            if retry:
                time.sleep(retry)

            try:
                self.pulp.createRepo(self.repo_id, None,
                                     registry_id=registry_id,
                                     is_origin=True,  # avoid origin-lock-...
                                     prefix_with=self.prefix)
                break
            except dockpulp.errors.DockPulpError as exc:
                if counter == self.retry_times:
                    # This was the last chance; go ahead anyway. This
                    # avoids a stale lock preventing builds.
                    logging.info("got error, breaking lock: %s", exc)
                else:
                    logging.info("got error, will retry in %s: %s", retry, exc)
                    retry = self.retry_delay * (2 ** counter)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        repo_id = '%s%s' % (self.prefix, self.repo_id)
        logger.info("deleting lock repository %s", repo_id)
        try:
            self.pulp.deleteRepo(repo_id)
        except dockpulp.errors.DockPulpError as exc:
            logging.info("ignoring error from lock repository deletion: %s",
                         exc)
