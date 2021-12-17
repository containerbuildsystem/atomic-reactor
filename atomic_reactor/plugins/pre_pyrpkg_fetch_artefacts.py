"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


To have everything for a build in dist-git you need to fetch artefacts using 'fedpkg sources'.

This plugin should do it.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final, List

from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import PreBuildPlugin
from atomic_reactor.constants import PLUGIN_DISTGIT_FETCH_KEY

EXPLODED_SOURCES_FILE: Final[str] = 'source-repos'


class DistgitFetchArtefactsPlugin(PreBuildPlugin):
    key = PLUGIN_DISTGIT_FETCH_KEY
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        :type workflow: atomic_reactor.inner.DockerBuildWorkflow
        """
        # call parent constructor
        super(DistgitFetchArtefactsPlugin, self).__init__(workflow)
        self.command = self.workflow.conf.sources_command

    def _collect_exploded_sources_files(self, build_dir_path: Path) -> List[Path]:
        """Collect the fetched exploded sources.

        :param build_dir_path: from which directory to find the exploded sources file.
        :type build_dir_path: pathlib.Path
        :return: a list of file names got from the source-repos file. An empty
            list will be returned if no source-repos exist, that means the
            container repo does not have any exploded sources.
        :rtype: list[pathlib.Path]
        :raises ValueError: if any line of source-repos cannot be parsed or the
            file cannot be found from the build_dir. Note that, generally,
            neither of these issues should be captured by this method, since
            it runs after rhpkg generates the tarballs from the repos listed
            within source-repos and any potential issue should be handled by
            rhpkg.
        """
        source_repos = build_dir_path / EXPLODED_SOURCES_FILE
        filenames = []
        if not source_repos.exists():
            return filenames
        regex = re.compile(r'^([\S]+)\s+([\S]+)$')
        with source_repos.open('r', encoding='utf-8') as f:
            for line in f:
                if match := regex.match(line.strip()):
                    repo_url, git_ref = match.groups()
                    repo_name = os.path.basename(repo_url).replace('.git', '')
                    filename = f'{repo_name}-{git_ref}.tar.gz'
                    if build_dir_path.joinpath(filename).exists():
                        filenames.append(Path(filename))
                    else:
                        raise ValueError(
                            f'Cannot find the sources file {filename} from {build_dir_path}'
                        )
                else:
                    raise ValueError(f'Invalid line in {EXPLODED_SOURCES_FILE}: {line.rstrip()}.')
        return filenames

    def _fetch_sources(self, build_dir: BuildDir) -> List[Path]:
        """Fetch sources files.

        :param build_dir: download the sources files into this directory.
        :type build_dir: BuildDir
        :return: a list of downloaded file names.
        :rtype: list[pathlib.Path]
        """
        # Create a dedicated directory to hold the fetched sources files, from
        # where to generated the downloaded file list.
        build_dir_path = build_dir.path
        sources_outdir = build_dir_path / 'outdir'
        sources_outdir.mkdir()

        sources_cmd = self.command.split()
        sources_cmd.append('--outdir')
        sources_cmd.append(str(sources_outdir))
        self.log.debug('Fetching sources: %r', sources_cmd)
        subprocess.check_call(sources_cmd, cwd=build_dir_path)

        fetched_sources = []
        for file_name in sources_outdir.iterdir():
            shutil.move(str(file_name), str(build_dir_path))
            fetched_sources.append(file_name.relative_to(sources_outdir))
        sources_outdir.rmdir()

        # Collect the exploded sources if there is
        fetched_sources += self._collect_exploded_sources_files(build_dir_path)

        return fetched_sources

    def run(self):
        """
        fetch artefacts
        """
        if not self.command:
            self.log.info('no sources command configuration, skipping plugin')
            return

        self.workflow.build_dir.for_all_platforms_copy(self._fetch_sources)
