"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import os
import re

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.constants import INSPECT_CONFIG, TAG_NAME_REGEX
from atomic_reactor.util import get_preferred_label_key, df_parser, LabelFormatter


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

    def __init__(self, tasker, workflow, tag_suffixes=None):
        super(TagFromConfigPlugin, self).__init__(tasker, workflow)
        self.tag_suffixes = tag_suffixes
        self.labels = None

    def parse_and_add_tags(self):
        tags = []
        name = self.get_component_name()

        for tag_suffix in self.tag_suffixes.get('unique', []):
            tag = '{}:{}'.format(name, tag_suffix)
            self.log.debug('Using additional unique tag %s', tag)
            self.workflow.tag_conf.add_unique_image(tag)
            tags.append(tag)

        for tag_suffix in self.tag_suffixes.get('primary', []):
            p_suffix = LabelFormatter().vformat(tag_suffix, [], self.labels)
            p_tag = '{}:{}'.format(name, p_suffix)
            self.log.debug('Using additional primary tag %s', p_tag)
            self.workflow.tag_conf.add_primary_image(p_tag)
            tags.append(p_tag)

        return tags

    def get_and_add_tags(self):
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
                tag_name_is_valid = re.match(TAG_NAME_REGEX, tag) is not None

                if tag_name_is_valid and '-' not in tag:
                    tags.append(tag)
                else:
                    self.log.warning("tag '%s' does not match '%s'"
                                     "or includes dashes, ignoring", tag, TAG_NAME_REGEX)

        if tags:
            name = self.get_component_name()
            for i, tag_suffix in enumerate(tags):
                tag = '{}:{}'.format(name, tag_suffix)
                self.log.debug('Using additional tag: %s', tag)
                self.workflow.tag_conf.add_primary_image(tag)
                # Store modified name.
                tags[i] = tag

        return tags

    def get_component_name(self):
        try:
            name_label = str(get_preferred_label_key(self.labels, "name"))
            name = self.labels[name_label]
        except KeyError:
            self.log.error('Unable to determine component from "Labels"')
            raise

        return name

    def run(self):
        self.lookup_labels()

        if self.tag_suffixes is not None:
            tags = self.parse_and_add_tags()
        else:
            tags = self.get_and_add_tags()

        return tags

    def lookup_labels(self):
        if self.workflow.build_result.is_image_available():
            if not self.workflow.built_image_inspect:
                raise RuntimeError('There is no inspect data for built image. '
                                   'Has the build succeeded?')
            try:
                self.labels = self.workflow.built_image_inspect[INSPECT_CONFIG]['Labels']
            except (TypeError, KeyError):
                self.log.error('Unable to determine "Labels" from built image')
                raise
        else:
            self.labels = df_parser(self.workflow.builder.df_path, workflow=self.workflow,
                                    env_replace=True).labels
