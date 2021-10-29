"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import functools

from atomic_reactor.plugin import BuildPlugin


def annotation(key):
    """
    Annotate a `BuildPlugin` subclass. The `run()` method of this plugin will
    store its result in the plugin's workflow as an annotation.

    If run() returns None, no annotation will be set.

    The `store_metadata` plugin will automatically collect these
    annotations and upload them to OpenShift.

    Example:
    >>> @annotation('foo')
    >>> class MyBuildPlugin(BuildPlugin):
    >>>     key = 'my_build_plugin'
    >>>
    >>>     # sets annotation: {'foo': 1}
    >>>     def run(self):
    >>>         return 1

    :param key: Key to annotate the plugin with
    :return: Decorator that will turn the plugin into an annotated one
    """
    return _decorate_metadata('annotations', [key], match_keys=False)


def annotation_map(*keys):
    """
    Annotate a `BuildPlugin` subclass. Works like `annotation`, but instead of
    storing the run() result as is, annotations are set by matching the given
    keys to those in the result (which has to be a dict).

    Example:
    >>> @annotation_map('foo', 'bar')
    >>> class YourBuildPlugin(BuildPlugin):
    >>>     key = 'your_build_plugin'
    >>>
    >>>     # sets annotations: {'foo': 1, 'bar': 2}
    >>>     def run(self):
    >>>         return {'foo': 1, 'bar': 2, 'baz': 3}

    :param keys: Keys to annotate the plugin with
    :return: Decorator that will turn the plugin into an annotated one
    """
    return _decorate_metadata('annotations', keys, match_keys=True)


def label(key):
    """
    Label a `BuildPlugin` subclass. Identical to `annotation`, but will save
    the result as a label, not an annotation.

    :param key: Key to label the plugin with
    :return: Decorator that will turn the plugin into a labeled one
    """
    return _decorate_metadata('labels', [key], match_keys=False)


def label_map(*keys):
    """
    Label a `BuildPlugin` subclass. Identical to `annotation_map`, but will
    save results as labels, not annotations.

    :param keys: Keys to label the plugin with
    :return: Decorator that will turn the plugin into a labeled one
    """
    return _decorate_metadata('labels', keys, match_keys=True)


def _decorate_metadata(metadata_type, keys, match_keys):

    def metadata_decorator(cls):
        if not issubclass(cls, BuildPlugin):
            raise TypeError('[{}] Not a subclass of BuildPlugin'.format(metadata_type))

        run = cls.run

        @functools.wraps(run)
        def run_and_store_metadata(self):
            result = run(self)
            if result is None:
                return None
            if match_keys and not isinstance(result, dict):
                raise TypeError('[{}] run() method did not return a dict'.format(metadata_type))

            metadata = getattr(self.workflow, metadata_type)
            for key in keys:
                if match_keys and key not in result:
                    raise RuntimeError('[{}] Not found in result: {!r}'.format(metadata_type, key))
                if key in metadata:
                    raise RuntimeError('[{}] Already set: {!r}'.format(metadata_type, key))

                if match_keys:
                    metadata[key] = result[key]
                else:
                    metadata[key] = result

            return result

        cls.run = run_and_store_metadata
        return cls

    return metadata_decorator
