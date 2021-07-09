"""
Copyright (c) 2020 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import re
import logging
from collections import OrderedDict

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from atomic_reactor.util import chain_get, sha256sum
from osbs.utils import ImageName
from osbs.utils.yaml import validate_with_schema

log = logging.getLogger(__name__)


def get_yaml_parser():
    """Returns tuned up YAML parser"""
    yaml_parser = YAML()

    # ruamel will introduce a line break if the yaml line is longer than
    # yaml.width. Unfortunately, this causes issues for JSON values nested
    # within a YAML file, e.g. metadata.annotations."alm-examples" in a CSV
    # file. The default value is 80. Set it to a more forgiving higher
    # number to avoid issues
    yaml_parser.width = 200

    # ruamel will also cause issues when normalizing a YAML object that contains
    # a nested JSON object when it does not preserve quotes. Thus, it produces
    # invalid YAML. Let's prevent this from happening at all.
    yaml_parser.preserve_quotes = True

    return yaml_parser


yaml = get_yaml_parser()


OPERATOR_CSV_KIND = "ClusterServiceVersion"

# Schema derived from https://github.com/operator-framework/api/blob/e29d40c2eb3eab6a2789a671886b8966ce8e3c0a/crds/operators.coreos.com_clusterserviceversions.yaml#L40  # noqa E501
_mini_csv_schema = {
    'type': 'object',
    'required': ['metadata', 'spec'],
    'properties': {
        'metadata': {
            'type': 'object',
            'properties': {
                'annotations': {
                    'type': 'object',
                    'properties': {
                        'containerImage': {
                            'type': 'string'
                        }
                    }
                }
            }
        },
        'spec': {
            'type': 'object',
            'required': ['install'],
            'properties': {
                'install': {
                    'type': 'object',
                    'properties': {
                        'spec': {
                            'type': 'object',
                            'required': ['deployments'],
                            'properties': {
                                'deployments': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'required': ['spec'],
                                        'properties': {
                                            'spec': {
                                                'type': 'object',
                                                'required': ['template'],
                                                'properties': {
                                                    'template': {
                                                        'type': 'object',
                                                        'properties': {
                                                            'metadata': {
                                                                'type':
                                                                'object'
                                                            },
                                                            'spec': {
                                                                'type':
                                                                'object',
                                                                'required':
                                                                ['containers'],
                                                                'properties': {
                                                                    'containers':
                                                                    {
                                                                        'type':
                                                                        'array',
                                                                        'items':
                                                                        {
                                                                            'type':
                                                                            'object',
                                                                            'required':
                                                                            [
                                                                                'name',
                                                                                'image'
                                                                            ],
                                                                            'properties':
                                                                            {
                                                                                'env':
                                                                                {
                                                                                    'type':
                                                                                    'array',
                                                                                    'items':
                                                                                    {
                                                                                        'type':
                                                                                        'object',
                                                                                        'required':
                                                                                        [
                                                                                            'name'
                                                                                        ],
                                                                                        'properties':  # noqa E501
                                                                                        {
                                                                                            'name':
                                                                                            {
                                                                                                'type':  # noqa E501
                                                                                                'string'  # noqa E501
                                                                                            },
                                                                                            'value':
                                                                                            {
                                                                                                'type':  # noqa E501
                                                                                                'string'  # noqa E501
                                                                                            }
                                                                                        }
                                                                                    }
                                                                                },
                                                                                'image':
                                                                                {
                                                                                    'type':
                                                                                    'string'
                                                                                },
                                                                                'name':
                                                                                {
                                                                                    'type':
                                                                                    'string'
                                                                                }
                                                                            }
                                                                        }
                                                                    },
                                                                    'initContainers':
                                                                    {
                                                                        'type':
                                                                        'array',
                                                                        'items':
                                                                        {
                                                                            'type':
                                                                            'object',
                                                                            'required':
                                                                            [
                                                                                'name',
                                                                                'image'
                                                                            ],
                                                                            'properties':
                                                                            {
                                                                                'name':
                                                                                {
                                                                                    'type':
                                                                                    'string'
                                                                                },
                                                                                'image':
                                                                                {
                                                                                    'type':
                                                                                    'string'
                                                                                }
                                                                            }
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
                'relatedImages': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'required': ['name', 'image'],
                        'properties': {
                            'name': {
                                'type': 'string'
                            },
                            'image': {
                                'type': 'string'
                            }
                        }
                    }
                }
            }
        }
    }
}


def check_csv(data, schema):
    try:
        if data.get("kind") != OPERATOR_CSV_KIND:
            raise NotOperatorCSV("Not a ClusterServiceVersion")
    except AttributeError as exc:
        raise NotOperatorCSV("File does not contain a YAML object") from exc
    validate_with_schema(data, schema)


def modify_dict_recursively(target, mods, append=False):
    """Apply 'mods' dictionary to 'target' dictionary for map entry values,

    creating map entries if missing. If `append` flag is set, update/create
    leaf lists (also creating any missing mapping entries).

    Properly handles the case where either or both dicts are nested.

    :param target: a dict
    :param mods: a dict
    :param append: boolean
    :return: target, modified
    """
    for key, value in mods.items():
        if isinstance(value, dict):
            # recursive call
            nested = target.get(key, {})
            if not isinstance(nested, dict):
                raise CSVModifyError(
                    f"Expected nested dictionary in CSV at '{key}' "
                    f"but got '{nested}' for {mods}"
                )
            target[key] = modify_dict_recursively(nested, value, append=append)
        else:
            if append:
                field = target.setdefault(key, [])
                if not isinstance(field, list):
                    # There's a type mismatch in the original CSV - we only *ever* allow
                    # a list for appending.
                    raise CSVModifyError('CSV value to append to is not a list'
                                         f' (found {target[key]}) in {target}')
                if not isinstance(value, list):
                    raise CSVModifyError('Modification value to append to is '
                                         f'not a list (found {value}) in {mods}')

                field.extend(value)
            else:
                target[key] = value
    return target


def is_dict(obj):
    """
    Check if object is a dict or the ruamel.yaml equivalent of a dict
    """
    return isinstance(obj, (dict, CommentedMap))


def is_list(obj):
    """
    Check if object is a list or the ruamel.yaml equivalent of a list
    """
    return isinstance(obj, (list, CommentedSeq))


def is_str(obj):
    """
    Check if object is a string or bytes. On python 3, checking for string
    would be sufficient, but on python 2, it may not be.
    """
    return isinstance(obj, (bytes, str))


class PullspecRegex(object):
    """
    Regular expressions for all things related to pullspecs
    """

    # Alphanumeric characters
    _alnum = r"[a-zA-Z0-9]"
    # Characters that you might find in a registry, namespace, repo or tag
    _name = r"[a-zA-Z0-9\-._]"
    # Base16 characters
    _base16 = r"[a-fA-F0-9]"

    # A basic name is anything that contains only alphanumeric and name
    # characters and starts and ends with an alphanumeric character
    _basic_name = r"(?:(?:{alnum}{name}*{alnum})|{alnum})".format(alnum=_alnum, name=_name)

    # A named tag is ':' followed by a basic name
    _named_tag = r"(?::{basic_name})".format(basic_name=_basic_name)
    # A digest is "@sha256:" followed by exactly 64 base16 characters
    _digest = r"(?:@sha256:{base16}{{64}})".format(base16=_base16)

    # A tag is either a named tag or a digest
    _tag = r"(?:{named_tag}|{digest})".format(named_tag=_named_tag, digest=_digest)

    # Registry is a basic name that contains at least one dot
    # followed by an optional port number
    _registry = r"(?:{alnum}{name}*\.{name}*{alnum}(?::\d+)?)".format(alnum=_alnum, name=_name)

    # Namespace is a basic name
    _namespace = _basic_name

    # Repo is a basic name followed by a tag
    # NOTE: Tag is REQUIRED, otherwise regex picks up too many false positives,
    # such as URLs, math and possibly many others.
    _repo = _basic_name + _tag

    # Pullspec is registry/namespace*/repo
    _pullspec = r"{registry}/(?:{namespace}/)*{repo}".format(registry=_registry,
                                                             namespace=_namespace,
                                                             repo=_repo)

    # Regex for sequence of characters that *could* be a pullspec
    CANDIDATE = re.compile(r"[a-zA-Z0-9/\-._@:]+")

    # Fully match a single pullspec
    FULL = re.compile(r"^{pullspec}$".format(pullspec=_pullspec))

    # Find pullspecs in text
    # NOTE: Using this regex to find pullspecs will not produce equivalent
    # results to the default_pullspec_heuristic() below. It will also find
    # pullspecs embedded in larger sequences that are not pullspecs, e.g.:
    # "https://example.com/foo:bar" -> "example.com/foo:bar"
    PULLSPEC = re.compile(_pullspec)


def default_pullspec_heuristic(text):
    """
    Attempts to find all pullspecs in arbitrary structured/unstructured text.
    Returns a list of (start, end) tuples such that:

        text[start:end] == <n-th pullspec in text> for all (start, end)

    The basic idea:

    - Find continuous sequences of characters that might appear in a pullspec
      - That being <alphanumeric> + "/-._@:"
    - For each such sequence:
      - Strip non-alphanumeric characters from both ends
      - Match remainder against the pullspec regex

    Put simply, this heuristic should find anything in the form:

        registry/namespace*/repo:tag
        registry/namespace*/repo@sha256:digest

    Where registry must contain at least one '.' and all parts follow various
    restrictions on the format (most typical pullspecs should be caught). Any
    number of namespaces, including 0, is valid.

    NOTE: Pullspecs without a tag (implicitly :latest) will not be caught.
    This would produce way too many false positives (and 1 false positive
    is already too many).

    :param text: Arbitrary blob of text in which to find pullspecs
    :return: List of (start, end) tuples of substring indices
    """
    pullspecs = []
    for i, j in _pullspec_candidates(text):
        i, j = _adjust_for_arbitrary_text(text, i, j)
        candidate = text[i:j]
        if PullspecRegex.FULL.match(candidate):
            pullspecs.append((i, j))
            log.debug("Pullspec heuristic: %s looks like a pullspec", candidate)
    return pullspecs


def _pullspec_candidates(text):
    return (match.span() for match in PullspecRegex.CANDIDATE.finditer(text))


def _adjust_for_arbitrary_text(text, i, j):
    # Strip all non-alphanumeric characters from start and end of pullspec
    # candidate to account for various structured/unstructured text elements
    while i < len(text) and not text[i].isalnum():
        i += 1
    while j > 0 and not text[j - 1].isalnum():
        j -= 1
    return i, j


class NotOperatorCSV(Exception):
    """
    Data is not from a valid ClusterServiceVersion document
    """


class CSVModifyError(Exception):
    """
    Problem modifying the CSV, i.e. via `modifications_append` or `modifications_update`
    """


class NamedPullspec(object):
    """
    Pullspec with a name and description
    """

    _image_key = "image"

    def __init__(self, data):
        """
        Initialize a NamedPullspec

        :param data: Dict-like object in JSON/YAML data
                     in which the name and image can be found
        """
        self.data = data

    @property
    def name(self):
        return self.data["name"]

    @property
    def image(self):
        return self.data[self._image_key]

    @image.setter
    def image(self, value):
        self.data[self._image_key] = value

    @property
    def description(self):
        raise NotImplementedError

    def as_yaml_object(self):
        """
        Convert pullspec to a {"name": <name>, "image": <image>} object

        :return: dict-like object compatible with ruamel.yaml
        """
        return CommentedMap([("name", self.name), ("image", self.image)])


class Container(NamedPullspec):
    @property
    def description(self):
        return "container {}".format(self.name)


class InitContainer(NamedPullspec):
    @property
    def description(self):
        return "initContainer {}".format(self.name)


class RelatedImage(NamedPullspec):
    @property
    def description(self):
        return "relatedImage {}".format(self.name)


class RelatedImageEnv(NamedPullspec):
    _image_key = "value"

    @property
    def name(self):
        # Construct name by removing prefix and converting to lowercase
        return self.data["name"][len("RELATED_IMAGE_"):].lower()

    @property
    def description(self):
        return "{} var".format(self.data["name"])


class Annotation(NamedPullspec):
    """
    Annotation pullspecs are special, they may be found under any key
    and there may be more than one pullspec under a single key
    """

    def __init__(self, data):
        super(Annotation, self).__init__(data)
        self._image_key = NotImplemented
        self._start_i = NotImplemented
        self._end_i = NotImplemented

    @property
    def image(self):
        i, j = self._start_i, self._end_i
        return self.data[self._image_key][i:j]

    @image.setter
    def image(self, value):
        # If there are 2 or more pullspecs in the same annotation text,
        # they *must* be replaced starting with the last one, otherwise
        # replacing one pullspec would invalidate the (start, end) indices
        # of the others. This is up to the code that uses this class.
        i, j = self._start_i, self._end_i
        text = self.data[self._image_key]
        self.data[self._image_key] = text[:i] + value + text[j:]

    @property
    def name(self):
        # Construct name by taking repo and tag from image and adding suffix
        image = ImageName.parse(self.image)
        tag = image.tag
        # If tag is a digest, strip "sha256:" prefix
        if tag.startswith("sha256:"):
            tag = tag[len("sha256:"):]
        return "{}-{}-annotation".format(image.repo, tag)

    @property
    def description(self):
        return "{} annotation".format(self._image_key)

    def in_key(self, image_key, start=None, end=None):
        self._image_key = image_key
        self._start_i = start or 0
        self._end_i = end or len(self.data[image_key])
        return self


class OperatorCSV(object):
    """
    A single ClusterServiceVersion file in an operator manifest.

    Can find and replace pullspecs for container images in predefined locations
    and all annotations (using a heuristic to guess what is a pullspec).
    """

    # Annotation keys that are expected to contain pullspecs
    _known_annotation_keys = ("containerImage",)

    def __init__(self, path, data, pullspec_heuristic=default_pullspec_heuristic):
        """
        Initialize an OperatorCSV

        :param path: Path where data was found or where it should be written
        :param data: ClusterServiceVersion yaml data
        :param pullspec_heuristic: Function that takes a string and returns
            a list of pullspecs (substrings). If not specified, a default
            implementation will be used. For more information, see the
            default_pullspec_heuristic() function in this module.
        """
        check_csv(data, _mini_csv_schema)
        self.path = path
        self.data = data
        self._pullspec_heuristic = pullspec_heuristic

    @property
    def checksum(self):
        with open(self.path, 'r') as f:
            return sha256sum(f.read())

    @classmethod
    def from_file(cls, path, **kwargs):
        """
        Make an OperatorCSV from a file

        :param path: Path to file
        :return: OperatorCSV
        """
        with open(path) as f:
            data = yaml.load(f)
            return cls(path, data, **kwargs)

    def dump(self):
        """
        Write data to file (preserves comments)
        """
        with open(self.path, "w") as f:
            yaml.dump(self.data, f)

    def has_related_images(self):
        """
        Check if OperatorCSV has a non-empty relatedImages section.
        """
        return bool(self._related_image_pullspecs())

    def has_related_image_envs(self):
        """
        Check if OperatorCSV has any RELATED_IMAGE_* env vars.
        """
        return bool(self._related_image_env_pullspecs())

    def get_pullspecs(self):
        """
        Find pullspecs in predefined locations.

        :return: set of ImageName pullspecs
        """
        pullspecs = set()
        for p in self._named_pullspecs():
            image = ImageName.parse(p.image)
            log.debug("%s - Found pullspec for %s: %s", self.path, p.description, image)
            pullspecs.add(image)
        return pullspecs

    def replace_pullspecs(self, replacement_pullspecs):
        """
        Replace pullspecs in predefined locations.

        :param replacement_pullspecs: mapping of pullspec -> replacement
        """
        for p in self._named_pullspecs():
            self._replace_named_pullspec(p, replacement_pullspecs)

    def replace_pullspecs_everywhere(self, replacement_pullspecs):
        """
        Replace all pullspecs found anywhere in data

        :param replacement_pullspecs: mapping of pullspec -> replacment
        """
        for k in self.data:
            self._replace_pullspecs_not_in_annotations(self.data, k, replacement_pullspecs)
        for annotation in self._annotation_pullspecs() + self._guess_annotation_pullspecs():
            self._replace_named_pullspec(annotation, replacement_pullspecs)

    def set_related_images(self):
        """
        Find pullspecs in predefined locations and put all of them in the
        .spec.relatedImages section (if it already exists, clear it first)
        """
        named_pullspecs = self._named_pullspecs()

        if not named_pullspecs:
            log.info("No pullspecs, skipping updates of relatedImages section")
            return

        by_name = OrderedDict()
        conflicts = []

        for new in named_pullspecs:
            # Keep track only of the first instance with a given name.
            # Ideally, existing relatedImages should come first in the list,
            # otherwise error messages could be confusing.
            old = by_name.setdefault(new.name, new)
            # Check for potential conflict (same name, different image)
            if new.image != old.image:
                msg = ("{old.description}: {old.image} X {new.description}: {new.image}"
                       .format(old=old, new=new))
                conflicts.append(msg)

        if conflicts:
            raise RuntimeError("{} - Found conflicts when setting relatedImages:\n{}"
                               .format(self.path, "\n".join(conflicts)))

        related_images = (self.data.setdefault("spec", CommentedMap())
                                   .setdefault("relatedImages", CommentedSeq()))
        del related_images[:]

        for p in by_name.values():
            log.debug("%s - Set relatedImage %s (from %s): %s",
                      self.path, p.name, p.description, p.image)
            related_images.append(p.as_yaml_object())

    def _named_pullspecs(self):
        pullspecs = []
        # relatedImages should come first in the list
        pullspecs.extend(self._related_image_pullspecs())
        pullspecs.extend(self._annotation_pullspecs())
        pullspecs.extend(self._container_pullspecs())
        pullspecs.extend(self._init_container_pullspecs())
        pullspecs.extend(self._related_image_env_pullspecs())
        pullspecs.extend(self._guess_annotation_pullspecs())
        return pullspecs

    def _related_image_pullspecs(self):
        """Get the pullspecs from spec.relatedImages section

        :return: a list of pullspecs. It could be an empty if no spec.RelatedImage section.
        :rtype: list[RelatedImage]
        """
        related_images_path = ("spec", "relatedImages")
        return [
            RelatedImage(r)
            for r in chain_get(self.data, related_images_path, default=[])
        ]

    def get_related_image_pullspecs(self):
        """Get the related image pullspecs

        :return: a list of related images pullspecs. It could be an empty list
            if this CSV file does not have spec.relatedImages section.
        :rtype: list[ImageName]
        """
        return [ImageName.parse(related_image.image)
                for related_image in self._related_image_pullspecs()]

    def _deployments(self):
        deployments_path = ("spec", "install", "spec", "deployments")
        return chain_get(self.data, deployments_path, default=[])

    def _container_pullspecs(self):
        deployments = self._deployments()
        containers_path = ("spec", "template", "spec", "containers")
        return [
            Container(c)
            for d in deployments for c in chain_get(d, containers_path, default=[])
        ]

    def _annotation_pullspecs(self):
        # Known sources of pullspecs in annotations
        pullspecs = []
        annotation_objects = self._find_all_annotations(self.data)
        for obj in annotation_objects:
            for key in self._known_annotation_keys:
                if key in obj:
                    pullspecs.append(Annotation(obj).in_key(key))
        return pullspecs

    def _related_image_env_pullspecs(self):
        containers = self._container_pullspecs() + self._init_container_pullspecs()
        envs = [
            e for c in containers
            for e in c.data.get("env", []) if e["name"].startswith("RELATED_IMAGE_")
        ]
        for env in envs:
            if "valueFrom" in env:
                msg = '{}: "valueFrom" references are not supported'.format(env["name"])
                raise RuntimeError(msg)
        return [
            RelatedImageEnv(env) for env in envs
        ]

    def _init_container_pullspecs(self):
        deployments = self._deployments()
        init_containers_path = ("spec", "template", "spec", "initContainers")
        return [
            InitContainer(c)
            for d in deployments for c in chain_get(d, init_containers_path, default=[])
        ]

    def _guess_annotation_pullspecs(self):
        # Other sources of pullspecs in annotations
        maybe_pullspecs = []
        annotation_objects = self._find_all_annotations(self.data)
        for obj in annotation_objects:
            for k, v in obj.items():
                # Do not look in keys that are known pullspec sources
                if is_str(v) and k not in self._known_annotation_keys:
                    for i, j in self._pullspec_heuristic(v):
                        maybe_pullspecs.append(Annotation(obj).in_key(k, i, j))
        # Pullspecs are found left-to-right, they *must* be replaced right-to-left
        maybe_pullspecs.reverse()
        return maybe_pullspecs

    def _find_all_annotations(self, obj):
        if is_dict(obj):
            metadata = obj.get("metadata")
            if metadata is not None and "annotations" in metadata:
                yield metadata["annotations"]
            for k, v in obj.items():
                # Do not search for metadata.*.metadata.annotations
                if k != "metadata":
                    for annotation in self._find_all_annotations(v):
                        yield annotation
        elif is_list(obj):
            for item in obj:
                for annotation in self._find_all_annotations(item):
                    yield annotation

    def _replace_named_pullspec(self, pullspec, replacement_pullspecs):
        old = ImageName.parse(pullspec.image)
        new = replacement_pullspecs.get(old)
        if new is not None and old != new:
            log.debug("%s - Replaced pullspec for %s: %s -> %s",
                      self.path, pullspec.description, old, new)
            pullspec.image = new.to_str()  # `new` is an ImageName

    def _replace_unnamed_pullspec(self, obj, key, replacement_pullspecs):
        old = ImageName.parse(obj[key])
        new = replacement_pullspecs.get(old)
        if new is not None and new != old:
            log.debug("%s - Replaced pullspec: %s -> %s", self.path, old, new)
            obj[key] = new.to_str()  # `new` is an ImageName

    def _replace_pullspecs_not_in_annotations(self, obj, k_or_i, replacements):
        item = obj[k_or_i]
        if is_dict(item):
            for k in item:
                # Do not descend into metadata.annotations objects
                if (k_or_i, k) != ("metadata", "annotations"):
                    self._replace_pullspecs_not_in_annotations(item, k, replacements)
        elif is_list(item):
            for i in range(len(item)):
                self._replace_pullspecs_not_in_annotations(item, i, replacements)
        elif is_str(item):
            # Doesn't matter if string was not a pullspec, it will simply not match anything
            # in replacement_pullspecs and no replacement will be done
            self._replace_unnamed_pullspec(obj, k_or_i, replacements)

    def modifications_append(self, append_mods):
        """Append to a list entry in `self.data` (or add a list, if one does not exist)

        Also checks the result against schema.

        :param append_mods: a dict (or nested dictionaries) that contains changes to be
                            applied to CSV. Values of terminal dict mus be lists.
        :return: None; this modifies 'self.data' in-place
        """
        modify_dict_recursively(self.data, append_mods, append=True)
        check_csv(self.data, _mini_csv_schema)

    def modifications_update(self, update_mods):
        """Update a dict entry in `self.data` (or add a dict entry, if one does not exist)

        Also checks the result against schema.

        :param update_mods: a dict (or nested dictionaries) that contains changes to be
                            applied to CSV.
        :return: None; this modifies 'self.data' in-place
        """
        modify_dict_recursively(self.data, update_mods)
        check_csv(self.data, _mini_csv_schema)


class OperatorManifest(object):
    """
    A collection of operator files.

    Currently, only ClusterServiceVersion files are considered relevant.
    """

    def __init__(self, csv_file):
        """
        Initialize an OperatorManifest

        :param csv_file: OperatorCSV
        """
        assert isinstance(csv_file, OperatorCSV)
        self._csv_file = csv_file
        self.files = [self._csv_file]  # BW compat

    @property
    def csv(self):
        """
        :return: ClusteredServiceVersion object of the operator manifests
        :rtype: OperatorCSV
        """
        return self._csv_file

    @classmethod
    def from_directory(cls, path, **kwargs):
        """
        Make an OperatorManifest from all the relevant files found in
        a directory (or its subdirectories)

        :param path: Path to directory
        :return: OperatorManifest
        """
        if not os.path.isdir(path):
            raise RuntimeError("Path does not exist or is not a directory: {}".format(path))
        yaml_files = cls._get_yaml_files(path)
        operator_csvs = list(cls._get_csvs(yaml_files, **kwargs))
        if not operator_csvs:
            raise ValueError("Missing ClusterServiceVersion in operator manifests")
        if len(operator_csvs) > 1:
            raise ValueError(
                "Operator bundle may contain only 1 CSV file, but contains more: {}".format(
                    ", ".join(csv.path for csv in operator_csvs)
                )
            )

        return cls(operator_csvs[0])

    @classmethod
    def _get_yaml_files(cls, dirpath):
        for d, _, files in os.walk(dirpath):
            for f in files:
                if f.endswith(".yaml") or f.endswith(".yml"):
                    yield os.path.join(d, f)

    @classmethod
    def _get_csvs(cls, yaml_files, **kwargs):
        for f in yaml_files:
            try:
                yield OperatorCSV.from_file(f, **kwargs)
            except NotOperatorCSV:
                pass
