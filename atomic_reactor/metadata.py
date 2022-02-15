"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import functools
from typing import Any, Callable, Dict, Literal, Optional, Type, TypeVar

from atomic_reactor.plugin import BuildPlugin


# Generic BuildPlugin type (the type of the class itself, not of its instances)
BPT = TypeVar('BPT', bound=Type[BuildPlugin])
# Takes a BuildPlugin class, modifies it in place and returns it
BuildPluginDecorator = Callable[[BPT], BPT]

PluginResult = Any
# Generates any number of metadata keys from a plugin result
MetadataFn = Callable[[PluginResult], Dict[str, Any]]


def _as_key(key: str, transform: Callable[[PluginResult], Any]) -> MetadataFn:
    return lambda result: {key: transform(result)}


def _identity(x):
    return x


def annotation(key: str) -> BuildPluginDecorator:
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
    return _decorate_metadata('annotations', result_to_metadata=_as_key(key, _identity))


def annotation_map(
    key: str,
    transform: Optional[Callable[[PluginResult], Any]] = None,
) -> BuildPluginDecorator:
    """
    Annotate a `BuildPlugin` subclass. Works like `annotation`, but instead of
    storing the run() result as is, applies the specified transformation to it
    first. If unspecified, the default transformation is result[key].

    Example:
    >>> @annotation_map('foo')
    >>> @annotation_map('bar_baz', lambda result: result['bar'] + result['baz'])
    >>> class YourBuildPlugin(BuildPlugin):
    >>>     key = 'your_build_plugin'
    >>>
    >>>     # sets annotations: {'foo': 1, 'bar_baz': 5}
    >>>     def run(self):
    >>>         return {'foo': 1, 'bar': 2, 'baz': 3}

    :param key: Key to annotate the plugin with
    :param transform: Function to apply to the plugin result before saving the annotation
    :return: Decorator that will turn the plugin into an annotated one
    """
    transform = transform or (lambda result: result[key])
    return _decorate_metadata('annotations', result_to_metadata=_as_key(key, transform))


def label(key: str) -> BuildPluginDecorator:
    """
    Label a `BuildPlugin` subclass. Identical to `annotation`, but will save
    the result as a label, not an annotation.

    :param key: Key to label the plugin with
    :return: Decorator that will turn the plugin into a labeled one
    """
    return _decorate_metadata('labels', result_to_metadata=_as_key(key, _identity))


def label_map(
    key: str,
    transform: Optional[Callable[[PluginResult], Any]] = None,
) -> BuildPluginDecorator:
    """
    Label a `BuildPlugin` subclass. Identical to `annotation_map`, but will
    save results as labels, not annotations.

    :param key: Key to label the plugin with
    :param transform: Function to apply to the plugin result before saving the label
    :return: Decorator that will turn the plugin into a labeled one
    """
    transform = transform or (lambda result: result[key])
    return _decorate_metadata('labels', result_to_metadata=_as_key(key, transform))


def _decorate_metadata(
    metadata_type: Literal['annotations', 'labels'], *, result_to_metadata: MetadataFn
) -> BuildPluginDecorator:

    def metadata_decorator(cls: BPT) -> BPT:
        run = cls.run

        @functools.wraps(run)
        def run_and_store_metadata(self):
            result = run(self)
            if result is None:
                return None

            metadata: Dict[str, Any] = result_to_metadata(result)
            workflow_metadata: Dict[str, Any] = getattr(self.workflow.data, metadata_type)

            for key in metadata:
                if key in workflow_metadata:
                    raise RuntimeError('[{}] Already set: {!r}'.format(metadata_type, key))

            workflow_metadata.update(metadata)
            return result

        setattr(cls, 'run', run_and_store_metadata)
        return cls

    return metadata_decorator
