"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.constants import INSPECT_CONFIG


class TagFromConfigPlugin(PostBuildPlugin):
    """
    Tags image with additional tags found in configuration file

    Configuration file must be named "additional-tags" and it must
    reside in repository as a sibling of Dockerfile. Each line in file
    is considered as a different tag to be applied. Empty lines and
    tag names containing hyphens are ignored. Tags will be prefixed by
    the value of Name label.

    For example, using the following configuration file:

        v1.0
        v1.0.1

    And assuming the Name label in Dockerfile is set to "fedora", the
    image will be tagged as:

        fedora:v1.0
        fedora:v1.0.1

    If configuration file is not found, this plugin takes no action.

    """
    key = 'tag_from_config'
    is_allowed_to_fail = False

    TAGS_FILENAME = 'additional-tags'

    def get_tags(self):
        tags = []

        df_dir = self.workflow.source.get_dockerfile_path()[1]
        tags_filename = os.path.join(df_dir, self.TAGS_FILENAME)
        if not os.path.exists(tags_filename):
            self.log.debug('"%s" not found. '
                           'No additional tags will be applied.',
                           tags_filename)
            return tags

        with open(tags_filename) as tags_file:
            for tag in tags_file:
                tag = tag.strip()
                if tag:
                    if '-' in tag:
                        self.log.warning("tag '%s' includes '-', ignoring", tag)
                    else:
                        tags.append(tag)

        return tags

    def get_component_name(self):
        if not self.workflow.built_image_inspect:
            raise RuntimeError('There is no inspect data for built image. '
                               'Has the build succeeded?')

        try:
            name = (self.workflow.built_image_inspect[INSPECT_CONFIG]
                                                     ['Labels']
                                                     ['Name'])
        except KeyError as e:
            self.log.error('Unable to determine "Name" from "Labels"')
            raise

        return name

    def run(self):
        tags = self.get_tags()

        if tags:
            name = self.get_component_name()
            for i, tag_suffix in enumerate(tags):
                tag = '{}:{}'.format(name, tag_suffix)
                self.log.debug('Using additional tag: %s', tag)
                self.workflow.tag_conf.add_primary_image(tag)
                # Store modified name.
                tags[i] = tag

        return tags
