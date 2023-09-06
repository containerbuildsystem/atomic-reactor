"""
Copyright (c) 2021-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

import abc
import json
import logging
import os
import signal
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Dict, Any, ClassVar, Generic, TypeVar, Optional

from opentelemetry.instrumentation.requests import RequestsInstrumentor
from otel_extensions import TelemetryOptions, init_telemetry_provider, get_tracer

from atomic_reactor import config
from atomic_reactor import dirs
from atomic_reactor import inner
from atomic_reactor import source
from atomic_reactor import util
from atomic_reactor.constants import OTEL_SERVICE_NAME
from atomic_reactor.plugin import TaskCanceledException

logger = logging.getLogger(__name__)


def write_task_result(output_file, msg):
    with open(output_file, 'w') as f:
        f.write(msg)


@dataclass(frozen=True)
class TaskParams:
    """Task parameters (coming from CLI arguments)."""

    user_params_schema: ClassVar[str] = "schemas/user_params.json"

    build_dir: str
    context_dir: str
    config_file: str
    namespace: str
    pipeline_run_name: str
    user_params: Dict[str, Any]
    task_result: Optional[str]

    # Note: do not give any attributes in this class default values, that would make dataclass
    #   inheritance difficult. If they should have defaults, define them in the CLI parser.

    @property
    def source(self) -> source.Source:
        """Source for the input files the task will operate on (e.g. a git repo)."""
        if "git_uri" not in self.user_params:
            raise ValueError(
                f"{self.__class__.__name__} instance has no source (no git_uri in user params)"
            )

        return source.GitSource(
            provider="git",
            uri=self.user_params["git_uri"],
            provider_params={
                "git_commit": self.user_params.get("git_ref"),
                "git_commit_depth": self.user_params.get("git_commit_depth"),
                "git_branch": self.user_params.get("git_branch"),
            },
            workdir=self.build_dir,
        )

    @classmethod
    def from_cli_args(cls, args: dict):
        """Create a TaskParams instance from CLI arguments."""
        params_str = args.pop("user_params", None)
        params_file = args.pop("user_params_file", None)

        if params_str:
            user_params = util.read_yaml(params_str, cls.user_params_schema)
        elif params_file:
            user_params = util.read_yaml_from_file_path(params_file, cls.user_params_schema)
        else:
            raise ValueError("Did not receive user params. User params are currently required.")

        return cls(**args, user_params=user_params)


ParamsT = TypeVar("ParamsT", bound=TaskParams)


class Task(abc.ABC, Generic[ParamsT]):
    """Task; the main execution unit in atomic-reactor."""

    ignore_sigterm: ClassVar[bool] = False
    # Automatically save context data before exiting? (Note: do not use for parallel tasks)
    autosave_context_data: ClassVar[bool] = True
    task_name = 'default'

    def __init__(self, params: ParamsT):
        """Initialize a Task."""
        self._params = params

    @abc.abstractmethod
    def execute(self):
        """Execute this task."""

    def get_build_dir(self) -> dirs.RootBuildDir:
        return dirs.RootBuildDir(Path(self._params.build_dir))

    def get_context_dir(self) -> dirs.ContextDir:
        return dirs.ContextDir(Path(self._params.context_dir))

    @cached_property
    def workflow_data(self) -> inner.ImageBuildWorkflowData:
        context_dir = self.get_context_dir()
        return inner.ImageBuildWorkflowData.load_from_dir(context_dir)

    def load_config(self) -> config.Configuration:
        return config.Configuration(self._params.config_file)

    def throw_task_canceled_exception(self, *args, **kwargs):
        self.workflow_data.task_canceled = True
        raise TaskCanceledException("Tekton task was canceled")

    def run(self, *args, **kwargs):
        try:
            if self.ignore_sigterm:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            else:
                signal.signal(signal.SIGTERM, self.throw_task_canceled_exception)

            opentelemetry_info = self._params.user_params.get('opentelemetry_info', {})
            traceparent = opentelemetry_info.get('traceparent', None)
            otel_url = opentelemetry_info.get('otel_url', None)

            span_exporter = ''
            otel_protocol = 'http/protobuf'
            if not otel_url:
                otel_protocol = 'custom'
                span_exporter = '"opentelemetry.sdk.trace.export.ConsoleSpanExporter"'

            if traceparent:
                os.environ['TRACEPARENT'] = traceparent
            logger.info('traceparent is set to %s', traceparent)
            otel_options = TelemetryOptions(
                OTEL_SERVICE_NAME=OTEL_SERVICE_NAME,
                OTEL_EXPORTER_CUSTOM_SPAN_EXPORTER_TYPE=span_exporter,
                OTEL_EXPORTER_OTLP_ENDPOINT=otel_url,
                OTEL_EXPORTER_OTLP_PROTOCOL=otel_protocol,
            )
            init_telemetry_provider(otel_options)

            RequestsInstrumentor().instrument()

            span_name = self.task_name
            if hasattr(self._params, 'platform'):
                span_name += '_' + self._params.platform
            tracer = get_tracer(module_name=span_name, service_name=OTEL_SERVICE_NAME)
            with tracer.start_as_current_span(span_name):
                result = self.execute(*args, **kwargs)
            if self._params.task_result:
                write_task_result(self._params.task_result, json.dumps(result))

        except Exception as e:
            if self._params.task_result:
                write_task_result(self._params.task_result, repr(e))
            raise e

        finally:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            if self.autosave_context_data:
                self.workflow_data.save(self.get_context_dir())
