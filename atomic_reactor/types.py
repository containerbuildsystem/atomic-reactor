"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from abc import abstractmethod, ABC
from typing import Any, Dict

ImageInspectionData = Dict[str, Any]


class ISerializer(ABC):
    """Interface defining serialization/deserialization methods."""

    @classmethod
    @abstractmethod
    def load(cls, data: Dict[str, Any]):
        """Create a concrete object from the given serialized data.

        :param data: load attribute values from this mapping to create a new
            object in specific type. The keys corresponds to the object attributes.
            There is no rule to define how keys are mapped to the corresponding
            object attributes back and forth, which is left to the subclass
            which implementing this interface.
            Unknown key will be just ignored and no error is raised for it.
        :type data: dict[str, any]
        :return: the newly created object and all attributes are set properly
            with the values read from the given data.
        """

    @abstractmethod
    def as_dict(self) -> Dict[str, Any]:
        """Convert current object to a dictionary.

        :return: a mapping containing key/value pairs that corresponds to the
            object attributes. The return value can be input of the ``load``
            method to recover an equivalent object.
        :rtype: dict[str, any]
        """
