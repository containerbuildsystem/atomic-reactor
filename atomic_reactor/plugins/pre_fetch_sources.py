"""
Copyright (c) 2019, 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import shutil
import tempfile

import koji
import tarfile
import yaml

from atomic_reactor.constants import (PLUGIN_FETCH_SOURCES_KEY, PNC_SYSTEM_USER,
                                      REMOTE_SOURCE_JSON_FILENAME, REMOTE_SOURCE_TARBALL_FILENAME,
                                      KOJI_BTYPE_REMOTE_SOURCES)
from atomic_reactor.config import get_koji_session
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.source import GitSource
from atomic_reactor.util import get_retrying_requests_session
from atomic_reactor.download import download_url
from atomic_reactor.metadata import label_map
from atomic_reactor.utils.pnc import PNCUtil


@label_map('sources_for_koji_build_id')
class FetchSourcesPlugin(PreBuildPlugin):
    """Download sources that may be used in further steps to compose Source Containers"""
    key = PLUGIN_FETCH_SOURCES_KEY
    is_allowed_to_fail = False
    SRPMS_DOWNLOAD_DIR = 'image_sources'
    REMOTE_SOURCES_DOWNLOAD_DIR = 'remote_sources'
    MAVEN_SOURCES_DOWNLOAD_DIR = 'maven_sources'

    def __init__(
        self, tasker, workflow, koji_build_id=None, koji_build_nvr=None, signing_intent=None,
    ):
        """
        :param tasker: ContainerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param koji_build_id: int, container image koji build id
        :param koji_build_nvr: str, container image koji build NVR
        :param signing_intent: str, ODCS signing intent name
        """
        if not koji_build_id and not koji_build_nvr:
            err_msg = ('{} expects either koji_build_id or koji_build_nvr to be defined'
                       .format(self.__class__.__name__))
            raise TypeError(err_msg)
        type_errors = []
        if koji_build_id is not None and not isinstance(koji_build_id, int):
            type_errors.append('koji_build_id must be an int. Got {}'.format(type(koji_build_id)))
        if koji_build_nvr is not None and not isinstance(koji_build_nvr, str):
            type_errors.append('koji_build_nvr must be a str. Got {}'
                               .format(type(koji_build_nvr)))
        if type_errors:
            raise TypeError(type_errors)

        super(FetchSourcesPlugin, self).__init__(tasker, workflow)
        self.koji_build = None
        self.koji_build_id = koji_build_id
        self.koji_build_nvr = koji_build_nvr
        self.signing_intent = signing_intent
        self.session = get_koji_session(self.workflow.conf)
        self.pathinfo = self.workflow.conf.koji_path_info
        self._pnc_util = None

    @property
    def pnc_util(self):
        if not self._pnc_util:
            pnc_map = self.workflow.conf.pnc
            if not pnc_map:
                raise RuntimeError('No PNC configuration found in reactor config map')
            self._pnc_util = PNCUtil(pnc_map)
        return self._pnc_util

    def run(self):
        """
        :return: dict, binary image koji build id and nvr, and path to directory with
        downloaded sources
        """
        self.set_koji_image_build_data()
        self.check_lookaside_cache_usage()
        signing_intent = self.get_signing_intent()
        koji_config = self.workflow.conf.koji
        insecure = koji_config.get('insecure_download', False)
        urls = self.get_srpm_urls(signing_intent['keys'], insecure=insecure)
        urls_remote, remote_sources_map = self.get_remote_urls()
        urls_maven = (self.get_kojifile_source_urls() + self.get_remote_file_urls() +
                      self.get_pnc_source_urls())

        if not any([urls, urls_remote, urls_maven]):
            msg = "No srpms or remote sources or maven sources found for source" \
                  " container, would produce empty source container image"
            self.log.error(msg)
            raise RuntimeError(msg)

        sources_dir = None
        remote_sources_dir = None
        maven_sources_dir = None
        if urls:
            sources_dir = self.download_sources(urls, insecure=insecure)
        if urls_remote:
            remote_sources_dir = self.download_sources(urls_remote, insecure=insecure,
                                                       download_dir=self.REMOTE_SOURCES_DOWNLOAD_DIR
                                                       )
            self.exclude_files_from_remote_sources(remote_sources_map, remote_sources_dir)
        if urls_maven:
            maven_sources_dir = self.download_sources(urls_maven, insecure=insecure,
                                                      download_dir=self.MAVEN_SOURCES_DOWNLOAD_DIR)

        return {
                'sources_for_koji_build_id': self.koji_build_id,
                'sources_for_nvr': self.koji_build_nvr,
                'image_sources_dir': sources_dir,
                'remote_sources_dir': remote_sources_dir,
                'maven_sources_dir': maven_sources_dir,
                'signing_intent': self.signing_intent,
        }

    def download_sources(self, sources, insecure=False, download_dir=SRPMS_DOWNLOAD_DIR):
        """Download sources content

        Download content in the given URLs into a new temporary directory and
        return a list with each downloaded artifact's path.

        :param sources: list, dicts with URLs to download
        :param insecure: bool, whether to perform TLS checks of urls
        :param download_dir: str, directory where to download content
        :return: str, paths to directory with downloaded sources
        """
        workdir = tempfile.mkdtemp()
        dest_dir = os.path.join(workdir, download_dir)
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)

        req_session = get_retrying_requests_session()
        for source in sources:
            subdir = os.path.join(dest_dir, source.get('subdir', ''))
            checksums = source.get('checksums', {})
            if not os.path.exists(subdir):
                os.makedirs(subdir)
            download_url(source['url'], subdir, insecure=insecure,
                         session=req_session, dest_filename=source.get('dest'),
                         expected_checksums=checksums)

        return dest_dir

    def set_koji_image_build_data(self):
        build_identifier = self.koji_build_nvr or self.koji_build_id

        # strict means this raises a koji.GenericError informing no matching build was found in
        # case the build does not exist
        self.koji_build = self.session.getBuild(build_identifier, strict=True)

        if self.koji_build_id and (self.koji_build_id != self.koji_build['build_id']):
            err_msg = (
                'koji_build_id {} does not match koji_build_nvr {} with id {}. '
                'When specifying both an id and an nvr, they should point to the same image build'
                .format(self.koji_build_id, self.koji_build_nvr, self.koji_build['build_id'])
                )
            raise ValueError(err_msg)

        build_extras = self.koji_build['extra']
        if 'image' not in build_extras:
            err_msg = ('koji build {} is not image build which source container requires'.
                       format(self.koji_build['nvr']))
            raise ValueError(err_msg)

        elif 'sources_for_nvr' in self.koji_build['extra']['image']:
            err_msg = ('koji build {} is source container build, source container can not '
                       'use source container build image'.format(self.koji_build['nvr']))
            raise ValueError(err_msg)

        if not self.koji_build_id:
            self.koji_build_id = self.koji_build['build_id']
        if not self.koji_build_nvr:
            self.koji_build_nvr = self.koji_build['nvr']

    def check_lookaside_cache_usage(self):
        """Check usage of lookaside cache, and fail if used"""
        git_uri, git_commit = self.koji_build['source'].split('#')

        source = GitSource('git', git_uri, provider_params={'git_commit': git_commit})
        source_path = source.get()
        sources_cache_file = os.path.join(source_path, 'sources')

        if os.path.exists(sources_cache_file):
            if os.path.getsize(sources_cache_file) > 0:
                raise RuntimeError('Repository is using lookaside cache, which is not allowed '
                                   'for source container builds')
        source.remove_tmpdir()

    def assemble_srpm_url(self, base_url, srpm_filename, sign_key=None):
        """Assemble the URL used to fetch an SRPM file

        :param base_url: str, Koji root base URL with the given build artifacts
        :param srpm_filename: str, name of the SRPM file
        :param sign_key: str, key used to sign the SRPM, as listed in the signing intent
        :return: list, strings with URLs pointing to SRPM files
        """
        srpm_info = koji.parse_NVRA(srpm_filename)
        if sign_key:
            srpm_path = self.pathinfo.signed(srpm_info, sign_key)
        else:
            srpm_path = self.pathinfo.rpm(srpm_info)
        return '/'.join([base_url, srpm_path])

    def _process_remote_source(self, koji_build, archives, remote_sources_path):
        self.log.debug('remote_source_url defined')
        remote_sources_urls = []
        remote_json_map = {}
        remote_source = {}
        remote_source['url'] = os.path.join(remote_sources_path, REMOTE_SOURCE_TARBALL_FILENAME)
        remote_source['dest'] = '-'.join([koji_build['nvr'], REMOTE_SOURCE_TARBALL_FILENAME])
        remote_sources_urls.append(remote_source)
        cachito_json_url = os.path.join(remote_sources_path, REMOTE_SOURCE_JSON_FILENAME)
        remote_json_map[remote_source['dest']] = cachito_json_url

        archive_found = False
        json_found = False
        all_archives = []

        for archive in archives:
            if archive['filename'] == REMOTE_SOURCE_TARBALL_FILENAME:
                archive_found = True
            elif archive['filename'] == REMOTE_SOURCE_JSON_FILENAME:
                json_found = True
            all_archives.append(archive['filename'])

        if not (archive_found and json_found):
            message = ', '.join(part for t, part in (
                (archive_found, "remote source archive missing"),
                (json_found, "remote source json missing")) if not t)
            raise RuntimeError(message)

        elif len(archives) > 2:
            raise RuntimeError('There can be just one remote sources archive and one '
                               'remote sources json, got: {}'.format(all_archives))

        return remote_sources_urls, remote_json_map

    def _process_multiple_remote_sources(self, koji_build, archives, remote_sources_path):
        self.log.debug('remote_sources defined')
        remote_sources_urls = []
        remote_json_map = {}
        remote_sources = koji_build['extra']['typeinfo']['remote-sources']
        wrong_archives = False
        all_archives = []

        for remote_s in remote_sources:
            remote_archive = None
            remote_json = None

            if len(remote_s['archives']) != 2:
                self.log.error('remote source "%s" does not contain 2 archives, but "%s"',
                               remote_s['name'], remote_s['archives'])
                wrong_archives = True
            else:
                for archive in remote_s['archives']:
                    if archive.endswith('.json'):
                        remote_json = archive
                    else:
                        remote_archive = archive

                if not remote_json:
                    self.log.error('remote source json, for remote source "%s" not found '
                                   'in archives "%s"', remote_s['name'], remote_s['archives'])
                    wrong_archives = True
                else:
                    remote_source = {}
                    remote_source['url'] = os.path.join(remote_sources_path, remote_archive)
                    remote_source['dest'] = '-'.join([koji_build['nvr'], remote_archive])
                    remote_sources_urls.append(remote_source)
                    cachito_json_url = os.path.join(remote_sources_path, remote_json)
                    remote_json_map[remote_source['dest']] = cachito_json_url
                    all_archives.append(remote_archive)
                    all_archives.append(remote_json)

        if wrong_archives:
            raise RuntimeError('Problems with archives in remote sources: {}'.
                               format(remote_sources))

        extra_archives = []
        for archive in archives:
            if archive['filename'] in all_archives:
                all_archives.remove(archive['filename'])
            else:
                extra_archives.append(archive['filename'])

        if all_archives:
            raise RuntimeError('Remote source files from metadata missing in koji '
                               'archives: {}'.format(all_archives))

        if extra_archives:
            raise RuntimeError('Remote source archives in koji missing from '
                               'metadata: {}'.format(extra_archives))

        return remote_sources_urls, remote_json_map

    def _get_remote_urls_helper(self, koji_build):
        """Fetch remote source urls from specific build

        :param koji_build: dict, koji build
        :return: str, URL pointing to remote sources
        """
        self.log.debug('get remote_urls: %s', koji_build['build_id'])
        archives = self.session.listArchives(koji_build['build_id'], type=KOJI_BTYPE_REMOTE_SOURCES)
        self.log.debug('archives: %s', archives)
        remote_sources_path = self.pathinfo.typedir(koji_build, btype=KOJI_BTYPE_REMOTE_SOURCES)
        remote_sources_urls = []
        remote_json_map = {}

        if 'remote_source_url' in koji_build['extra']['image']:
            remote_sources_urls, remote_json_map = \
                self._process_remote_source(koji_build, archives, remote_sources_path)

        elif 'remote_sources' in koji_build['extra']['image']:
            remote_sources_urls, remote_json_map = \
                self._process_multiple_remote_sources(koji_build, archives, remote_sources_path)

        return remote_sources_urls, remote_json_map

    def _get_kojifile_source_urls_helper(self, koji_build):
        """Fetch kojifile source urls from specific build

        :param koji_build: dict, koji build
        :return: list, dicts with URL pointing to kojifile sources
        """

        self.log.debug('get kojifile_source_urls: %s', koji_build['build_id'])
        images = self.session.listArchives(koji_build['build_id'], type='image')

        self.log.debug('images: %s', images)

        sources = []

        kojifile_build_ids = {kojifile['build_id'] for image in images
                              for kojifile in self.session.listArchives(imageID=image['id'],
                                                                        type='maven')}

        for build_id in kojifile_build_ids:
            source_build = self.session.getBuild(build_id, strict=True)
            if source_build['owner_name'] == PNC_SYSTEM_USER:
                pnc_build_id = source_build['extra']['external_build_id']
                url, dest_filename = self.pnc_util.get_scm_archive_from_build_id(
                    build_id=pnc_build_id)
                source = {'url': url,
                          'subdir': source_build['nvr'],
                          'dest': '__'.join([source_build['nvr'], dest_filename])}
            else:
                source_archive = None
                maven_build_path = self.pathinfo.mavenbuild(source_build)
                for archive in self.session.listArchives(buildID=source_build['build_id'],
                                                         type='maven'):
                    if archive['filename'].endswith('-project-sources.tar.gz'):
                        source_archive = archive
                        break
                if not source_archive:
                    raise RuntimeError(f"No sources found for {source_build['nvr']}")

                maven_file_path = self.pathinfo.mavenfile(source_archive)
                url = maven_build_path + '/' + maven_file_path
                source = {'url': url,
                          'subdir': source_build['nvr'],
                          'dest': '__'.join([source_build['nvr'], source_archive['filename']]),
                          'checksums': {
                              koji.CHECKSUM_TYPES[source_archive['checksum_type']]:
                                  source_archive['checksum']}}
            sources.append(source)

        return sources

    def _get_pnc_source_urls_helper(self, koji_build):
        """Fetch PNC source urls from specific build

        :param koji_build: dict, koji build
        :return: list, dicts with URL pointing to PNC sources
        """
        sources = []

        if 'pnc' not in koji_build['extra']['image']:
            self.log.info("No PNC build ids found")
            return sources

        build_ids = set()
        for build in koji_build['extra']['image']['pnc']['builds']:
            build_ids.add(build['id'])

        self.log.debug('PNC build ids: %s', build_ids)

        for build_id in build_ids:
            url, dest_filename = self.pnc_util.get_scm_archive_from_build_id(build_id=build_id)
            source = {'url': url,
                      'subdir': str(build_id),
                      'dest': '__'.join([str(build_id), dest_filename])}
            sources.append(source)

        return sources

    def _get_remote_file_urls_helper(self, koji_build):
        """Fetch remote source file urls from specific build

        :param koji_build: dict, koji build
        :return: str, URL pointing to remote source files
        """

        self.log.debug('get remote_file_urls: %s', koji_build['build_id'])

        archives = self.session.listArchives(koji_build['build_id'], type='remote-source-file')
        self.log.debug('archives: %s', archives)

        remote_source_files_path = self.pathinfo.typedir(koji_build, btype='remote-source-file')
        sources = []

        for archive in archives:
            if archive['type_name'] == 'tar':
                # download each remote-source-file archive into it's own subdirectory
                #  with the same name as the archive
                source = {'url': os.path.join(remote_source_files_path,
                                              archive['filename']),
                          'subdir': archive['filename'].rsplit('.', 2)[0],
                          'dest': archive['filename'],
                          'checksums': {koji.CHECKSUM_TYPES[archive['checksum_type']]:
                                        archive['checksum']}}
                sources.append(source)

        return sources

    def get_remote_urls(self):
        """Fetch remote source urls from all builds

        :return: list, dicts with URL pointing to remote sources
        """
        remote_sources_urls = []
        remote_sources_map = {}

        remote_source, remote_json = self._get_remote_urls_helper(self.koji_build)
        remote_sources_urls.extend(remote_source)
        remote_sources_map.update(remote_json)

        koji_build = self.koji_build

        while 'parent_build_id' in koji_build['extra']['image']:
            koji_build = self.session.getBuild(koji_build['extra']['image']['parent_build_id'],
                                               strict=True)
            remote_source, remote_json = self._get_remote_urls_helper(koji_build)
            remote_sources_urls.extend(remote_source)
            remote_sources_map.update(remote_json)

        return remote_sources_urls, remote_sources_map

    def get_kojifile_source_urls(self):
        """Fetch kojifile source urls from all builds

        :return: list, dicts with URL pointing to kojifile source files
        """
        kojifile_sources = []

        kojifile_source = self._get_kojifile_source_urls_helper(self.koji_build)
        kojifile_sources.extend(kojifile_source)

        koji_build = self.koji_build

        while 'parent_build_id' in koji_build['extra']['image']:
            koji_build = self.session.getBuild(koji_build['extra']['image']['parent_build_id'],
                                               strict=True)
            kojifile_source = self._get_kojifile_source_urls_helper(koji_build)
            kojifile_sources.extend(kojifile_source)

        return kojifile_sources

    def get_pnc_source_urls(self):
        """Fetch PNC build source urls from all builds

        :return: list, dicts with URL pointing to PNC build source archives
        """
        sources = []
        source = self._get_pnc_source_urls_helper(self.koji_build)
        sources.extend(source)
        koji_build = self.koji_build

        while 'parent_build_id' in koji_build['extra']['image']:
            koji_build = self.session.getBuild(koji_build['extra']['image']['parent_build_id'],
                                               strict=True)
            source = self._get_pnc_source_urls_helper(koji_build)
            sources.extend(source)

        return sources

    def get_remote_file_urls(self):
        """Fetch remote source file urls from all builds

        :return: list, dicts with URL pointing to remote source files
        """
        sources = []

        source = self._get_remote_file_urls_helper(self.koji_build)
        sources.extend(source)

        koji_build = self.koji_build

        while 'parent_build_id' in koji_build['extra']['image']:
            koji_build = self.session.getBuild(koji_build['extra']['image']['parent_build_id'],
                                               strict=True)
            source = self._get_remote_file_urls_helper(koji_build)
            sources.extend(source)

        return sources

    def get_denylisted_srpms(self):
        src_config = self.workflow.conf.source_container
        denylist_srpms = src_config.get('denylist_srpms')
        if not denylist_srpms:
            self.log.debug('denylist_srpms is not defined in reactor_config_map')
            return []

        denylist_url = denylist_srpms['denylist_url']
        denylist_key = denylist_srpms['denylist_key']
        req_session = get_retrying_requests_session()

        response = req_session.get(denylist_url)
        response.raise_for_status()
        response_json = response.json()

        if denylist_key not in response_json:
            self.log.debug('deny list json : %s', response_json)
            raise RuntimeError('Denylist key: {} missing in denylist json from : {}'.
                               format(denylist_key, denylist_url))

        deny_list = response_json[denylist_key]

        if not isinstance(deny_list, list):
            self.log.error('Wrong denylist: %s', repr(deny_list))
            raise RuntimeError('Denylist value in key: {} is not list: {}'.
                               format(denylist_key, type(deny_list)))

        wrong_types = [pkg for pkg in deny_list if not isinstance(pkg, str)]
        if wrong_types:
            self.log.error('Wrong types in denylist, should be str: %s', repr(deny_list))
            raise RuntimeError('Values in denylist has to be all strings')

        self.log.debug('denylisted srpms: %s', deny_list)
        return deny_list

    def get_srpm_urls(self, sigkeys=None, insecure=False):
        """Fetch SRPM download URLs for each image generated by a build

        Build each possible SRPM URL and check if the URL is available,
        respecting the signing intent preference order.

        :param sigkeys: list, strings for keys which signed the srpms to be fetched
        :return: list, strings with URLs pointing to SRPM files
        """
        if not sigkeys:
            sigkeys = ['']

        self.log.debug('get srpm_urls: %s', self.koji_build_id)
        archives = self.session.listArchives(self.koji_build_id, type='image')
        self.log.debug('archives: %s', archives)
        rpms = [rpm for archive in archives
                for rpm in self.session.listRPMs(imageID=archive['id'])]

        denylist_srpms = self.get_denylisted_srpms()

        srpm_build_paths = {}
        for rpm in rpms:
            rpm_id = rpm['id']
            self.log.debug('Resolving SRPM for RPM ID: %s', rpm_id)

            if rpm['external_repo_name'] != 'INTERNAL':
                msg = ('RPM comes from an external repo (RPM ID: {}). '
                       'External RPMs are currently not supported.').format(rpm_id)
                raise RuntimeError(msg)

            rpm_hdr = self.session.getRPMHeaders(rpm_id, headers=['SOURCERPM'])
            if 'SOURCERPM' not in rpm_hdr:
                raise RuntimeError('Missing SOURCERPM header (RPM ID: {})'.format(rpm_id))

            srpm_name = rpm_hdr['SOURCERPM'].rsplit('-', 2)[0]

            if any(denied == srpm_name for denied in denylist_srpms):
                self.log.debug('skipping denylisted srpm %s', rpm_hdr['SOURCERPM'])
                continue

            srpm_filename = rpm_hdr['SOURCERPM']
            if srpm_filename in srpm_build_paths:
                continue
            rpm_build = self.session.getBuild(rpm['build_id'], strict=True)
            base_url = self.pathinfo.build(rpm_build)
            srpm_build_paths[srpm_filename] = base_url

        srpm_urls = []
        missing_srpms = []
        req_session = get_retrying_requests_session()
        for srpm_filename, base_url in srpm_build_paths.items():
            for sigkey in sigkeys:
                # koji uses lowercase for paths. We make sure the sigkey is in lower case
                url_candidate = self.assemble_srpm_url(base_url, srpm_filename, sigkey.lower())
                # allow redirects, head call doesn't do it by default
                request = req_session.head(url_candidate, verify=not insecure, allow_redirects=True)
                if request.ok:
                    srpm_urls.append({'url': url_candidate})
                    self.log.debug('%s is available for signing key "%s"', srpm_filename, sigkey)
                    break

            else:
                self.log.error('%s not found for the given signing intent: %s"', srpm_filename,
                               self.signing_intent)
                missing_srpms.append(srpm_filename)

        if missing_srpms:
            raise RuntimeError('Could not find files signed by any of {} for these SRPMS: {}'
                               .format(sigkeys, missing_srpms))

        return srpm_urls

    def get_signing_intent(self):
        """Get the signing intent to be used to fetch files from Koji

        :return: dict, signing intent object as per atomic_reactor/schemas/config.json
        """
        odcs_config = self.workflow.conf.odcs_config
        if odcs_config is None:
            self.log.warning('No ODCS configuration available. Allowing unsigned SRPMs')
            return {'keys': None}

        if not self.signing_intent:
            try:
                self.signing_intent = self.koji_build['extra']['image']['odcs']['signing_intent']
            except (KeyError, TypeError):
                self.log.debug('Image koji build, %s(%s), does not define signing_intent.',
                               self.koji_build_nvr, self.koji_build_id)
                self.signing_intent = odcs_config.default_signing_intent

        signing_intent = odcs_config.get_signing_intent_by_name(self.signing_intent)
        return signing_intent

    def _get_denylist_sources(self, request_session, denylist_sources_url):
        response = request_session.get(denylist_sources_url)
        response.raise_for_status()
        denylist_sources_yaml = yaml.safe_load(response.text)
        # prepend os.sep for 2 reasons:
        # - so fnmatch will match exact dir/file when using * + exclude
        #   glob.glob doesn't need it
        # - so endswith for package will match also full name
        return [os.path.join(os.sep, k, item)
                for k, v in denylist_sources_yaml.items() for item in v]

    def _create_full_remote_sources_map(self, request_session, remote_sources_map,
                                        remote_sources_dir):
        full_remote_sources_map = {}

        for remote_source, remote_source_json in remote_sources_map.items():
            remote_source_archive = os.path.join(remote_sources_dir, remote_source)

            response = request_session.get(remote_source_json)
            response.raise_for_status()
            response_json = response.json()
            full_remote_sources_map[remote_source_archive] = response_json
        return full_remote_sources_map

    def _check_if_package_excluded(self, packages, denylist_sources, remote_archive):
        # check if any package in cachito json matches excluded entry
        # strip leading os.sep as package names can include git path with '/' before package name
        # or just package name, or package name with leading '@' depending on package type
        denylist_packages = {k.lstrip(os.sep) for k in denylist_sources}

        for package in packages:
            for exclude_path in denylist_packages:
                if package.get('name').endswith(exclude_path):
                    self.log.debug('Package excluded: "%s" from "%s"', package.get('name'),
                                   remote_archive)
                    return True
        return False

    def _delete_app_directory(self, remote_source_dir, unpack_dir, remote_archive):
        vendor_dir = os.path.join(unpack_dir, 'app', 'vendor')

        if os.path.exists(vendor_dir):
            shutil.move(vendor_dir, remote_source_dir)
            self.log.debug('Removing app from "%s"', remote_archive)
            shutil.rmtree(os.path.join(unpack_dir, 'app'))
            # shutil.move will create missing parent directory
            shutil.move(os.path.join(remote_source_dir, 'vendor'), vendor_dir)
            self.log.debug('Keeping vendor in app from "%s"', remote_archive)
        else:
            self.log.debug('Removing app from "%s"', remote_archive)
            shutil.rmtree(os.path.join(unpack_dir, 'app'))

    def _get_excluded_matches(self, unpack_dir, denylist_sources):
        matches = []
        # py2 glob.glob doesn't support recursive, hence os.walk & fnmatch
        # py3 can use: glob.glob(os.path.join(unpack_dir, '**', exclude), recursive=True)
        for root, dirnames, filenames in os.walk(unpack_dir):
            for entry in dirnames + filenames:
                full_path = os.path.join(root, entry)

                for exclude in denylist_sources:
                    if full_path.endswith(exclude):
                        matches.append(full_path)
                        break
        return matches

    def _remove_excluded_matches(self, matches):
        for entry in matches:
            if os.path.exists(entry):
                if os.path.isdir(entry):
                    self.log.debug("Removing excluded directory %s", entry)
                    shutil.rmtree(entry)
                else:
                    self.log.debug("Removing excluded file %s", entry)
                    os.unlink(entry)

    def exclude_files_from_remote_sources(self, remote_sources_map, remote_sources_dir):
        """
        :param remote_sources_map: dict, keys are filenames of sources from cachito,
                                         values are url with json from cachito
        :param remote_sources_dir: str, dir with downloaded sources from cachito
        """
        src_config = self.workflow.conf.source_container
        denylist_sources_url = src_config.get('denylist_sources')

        if not denylist_sources_url:
            self.log.debug('no "denylist_sources" defined, not excluding any '
                           'files from remote sources')
            return

        request_session = get_retrying_requests_session()

        denylist_sources = self._get_denylist_sources(request_session, denylist_sources_url)

        # key: full path to source archive, value: cachito json
        full_remote_sources_map = self._create_full_remote_sources_map(request_session,
                                                                       remote_sources_map,
                                                                       remote_sources_dir)
        for remote_archive, remote_json in full_remote_sources_map.items():
            unpack_dir = remote_archive + '_unpacked'

            with tarfile.open(remote_archive) as tf:
                tf.extractall(unpack_dir)

            delete_app = self._check_if_package_excluded(remote_json['packages'], denylist_sources,
                                                         remote_archive)

            # if any package in cachito json matched excluded entry,
            # remove 'app' from sources, except 'app/vendor' when exists
            if delete_app and os.path.exists(os.path.join(unpack_dir, 'app')):
                self._delete_app_directory(remote_sources_dir, unpack_dir, remote_archive)

            # search for excluded matches
            matches = self._get_excluded_matches(unpack_dir, denylist_sources)

            self._remove_excluded_matches(matches)

            # delete former archive
            os.unlink(remote_archive)

            # re-create new archive without excluded content
            with tarfile.open(remote_archive, "w:gz") as tar:
                for add_file in os.listdir(unpack_dir):
                    tar.add(os.path.join(unpack_dir, add_file), arcname=add_file)

            # cleanup unpacked dir
            shutil.rmtree(unpack_dir)
