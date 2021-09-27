"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import json
import requests

from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.util import (registry_hostname, RegistrySession, ManifestDigest,
                                 get_manifest_media_type)
from atomic_reactor.constants import (MEDIA_TYPE_DOCKER_V2_SCHEMA2,
                                      MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST, MEDIA_TYPE_OCI_V1,
                                      MEDIA_TYPE_OCI_V1_INDEX)


class ManifestUtil(object):
    manifest_media_types = (
        MEDIA_TYPE_DOCKER_V2_SCHEMA2,
        MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST,
        MEDIA_TYPE_OCI_V1,
        MEDIA_TYPE_OCI_V1_INDEX
    )

    def __init__(self, workflow, registries, log):
        self.push_conf = workflow.push_conf
        self.registries = workflow.conf.registries
        self.worker_registries = {}
        self.log = log

    def valid_media_type(self, media_type):
        return media_type in self.manifest_media_types

    def sort_annotations(self, all_annotations):
        sorted_digests = {}
        all_platforms = set(all_annotations)

        for plat, annotation in all_annotations.items():
            for digest in annotation['digests']:
                hostname = registry_hostname(digest['registry'])
                media_type = get_manifest_media_type(digest['version'])
                if not self.valid_media_type(media_type):
                    continue

                platforms = sorted_digests.setdefault(hostname, {})
                repos = platforms.setdefault(plat, [])
                repos.append(digest)

        sources = {}
        for registry in self.registries:
            hostname = registry_hostname(registry)
            platforms = sorted_digests.get(hostname, {})

            if set(platforms) != all_platforms:
                raise RuntimeError("Missing platforms for registry {}: found {}, expected {}"
                                   .format(registry, sorted(platforms), sorted(all_platforms)))

            selected_digests = {}
            for p, repos in platforms.items():
                selected_digests[p] = sorted(repos, key=lambda d: d['repository'])[0]

            sources[registry] = selected_digests

        return sources

    def get_manifest(self, session, repository, ref):
        """
        Downloads a manifest from a registry. ref can be a digest, or a tag.
        """
        self.log.debug("%s: Retrieving manifest for %s:%s", session.registry, repository, ref)

        headers = {
            'Accept': ', '.join(self.manifest_media_types)
        }

        url = '/v2/{}/manifests/{}'.format(repository, ref)
        response = session.get(url, headers=headers)
        response.raise_for_status()
        return (response.content,
                response.headers['Docker-Content-Digest'],
                response.headers['Content-Type'],
                int(response.headers['Content-Length']))

    def link_blob_into_repository(self, session, digest, source_repo, target_repo):
        """
        Links ("mounts" in Docker Registry terminology) a blob from one repository in a
        registry into another repository in the same registry.
        """
        self.log.debug("%s: Linking blob %s from %s to %s",
                       session.registry, digest, source_repo, target_repo)

        # Check that it exists in the source repository
        url = "/v2/{}/blobs/{}".format(source_repo, digest)
        # allow redirects, head call doesn't do it by default
        result = session.head(url, allow_redirects=True)
        if result.status_code == requests.codes.NOT_FOUND:
            self.log.debug("%s: blob %s, not present in %s, skipping",
                           session.registry, digest, source_repo)
            # Assume we don't need to copy it - maybe it's a foreign layer
            return
        result.raise_for_status()

        url = "/v2/{}/blobs/uploads/?mount={}&from={}".format(target_repo, digest, source_repo)
        result = session.post(url, data='')
        result.raise_for_status()

        if result.status_code != requests.codes.CREATED:
            # A 202-Accepted would mean that the source blob didn't exist and
            # we're starting an upload - but we've checked that above
            raise RuntimeError("Blob mount had unexpected status {}".format(result.status_code))

    def link_manifest_references_into_repository(self, session, manifest, media_type,
                                                 source_repo, target_repo):
        """
        Links all the blobs referenced by the manifest from source_repo into target_repo.
        """

        if source_repo == target_repo:
            return

        parsed = json.loads(manifest.decode('utf-8'))

        references = []
        if media_type in (MEDIA_TYPE_DOCKER_V2_SCHEMA2, MEDIA_TYPE_OCI_V1):
            references.append(parsed['config']['digest'])
            for layer in parsed['layers']:
                references.append(layer['digest'])
        else:
            # manifest list support could be added here, but isn't needed currently, since
            # we never copy a manifest list as a whole between repositories
            raise RuntimeError("Unhandled media-type {}".format(media_type))

        for digest in references:
            self.link_blob_into_repository(session, digest, source_repo, target_repo)

    def store_manifest_in_repository(self, session, manifest, media_type,
                                     source_repo, target_repo, ref=None):
        """
        Stores the manifest into target_repo, possibly tagging it. This may involve
        copying referenced blobs from source_repo.
        """

        if not ref:
            raise RuntimeError("Either a digest or tag must be specified as ref")

        self.link_manifest_references_into_repository(session, manifest, media_type,
                                                      source_repo, target_repo)

        url = '/v2/{}/manifests/{}'.format(target_repo, ref)
        headers = {'Content-Type': media_type}
        response = session.put(url, data=manifest, headers=headers)
        response.raise_for_status()

    def get_registry_session(self, registry):
        registry_conf = self.registries[registry]

        insecure = registry_conf.get('insecure', False)
        secret_path = registry_conf.get('secret')

        return RegistrySession(registry, insecure=insecure,
                               dockercfg_path=secret_path,
                               access=('pull', 'push'))

    def add_tag_and_manifest(self, session, image_manifest, media_type, manifest_digest,
                             source_repo, configured_tags):
        push_conf_registry = self.push_conf.add_docker_registry(session.registry,
                                                                insecure=session.insecure)
        for image in configured_tags:
            target_repo = image.to_str(registry=False, tag=False)
            self.store_manifest_in_repository(session, image_manifest, media_type,
                                              source_repo, target_repo, ref=image.tag)

            # add a tag for any plugins running later that expect it
            push_conf_registry.digests[image.tag] = manifest_digest

    def tag_manifest_into_registry(self, session, digest, source_repo, configured_tags):
        """
        Tags the manifest identified by worker_digest into session.registry with all the
        configured_tags
        """
        self.log.info("%s: Tagging manifest", session.registry)

        image_manifest, _, media_type, _ = self.get_manifest(session, source_repo, digest)
        if media_type == MEDIA_TYPE_DOCKER_V2_SCHEMA2:
            digests = ManifestDigest(v1=digest)
        elif media_type == MEDIA_TYPE_OCI_V1:
            digests = ManifestDigest(oci=digest)
        else:
            raise RuntimeError("Unexpected media type {} found in source_repo: {}"
                               .format(media_type, source_repo))

        self.add_tag_and_manifest(session, image_manifest, media_type, digests, source_repo,
                                  configured_tags)
        return image_manifest, media_type, digests

    def build_list(self, manifests):
        """
        Builds a manifest list or OCI image out of the given manifests
        """

        media_type = manifests[0]['media_type']
        if (not all(m['media_type'] == media_type for m in manifests)):
            raise PluginFailedException('worker manifests have inconsistent types: {}'
                                        .format(manifests))

        if media_type == MEDIA_TYPE_DOCKER_V2_SCHEMA2:
            list_type = MEDIA_TYPE_DOCKER_V2_MANIFEST_LIST
        elif media_type == MEDIA_TYPE_OCI_V1:
            list_type = MEDIA_TYPE_OCI_V1_INDEX
        else:
            raise PluginFailedException('worker manifests have unsupported type: {}'
                                        .format(media_type))

        return list_type, json.dumps({
                "schemaVersion": 2,
                "mediaType": list_type,
                "manifests": sorted([
                    {
                        "mediaType": media_type,
                        "size": m['size'],
                        "digest": m['digest'],
                        "platform": {
                            "architecture": m['architecture'],
                            "os": "linux"
                        }
                    } for m in manifests
                ], key=lambda entry: entry['platform']['architecture']),
        }, indent=4, sort_keys=True, separators=(',', ': '))
