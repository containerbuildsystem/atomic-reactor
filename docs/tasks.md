# Tasks

OSBS uses **Tekton** tasks for managing the complexity of the image building process.
These tasks are part of a pipeline, and each of the tasks is isolated and responsible for executing specific part of the pipeline.
For description of each task, please see [binary tasks description][] and [source tasks description][]

The tasks are defined in [tekton/tasks][]

For description of what a Tekton task is and its possible configuration, see [Tekton tasks docs][]

[tekton/tasks]: ../tekton/tasks
[binary tasks description]: https://osbs.readthedocs.io/en/latest/build_process.html?highlight=task#binary-pipeline-consists-of-these-tasks
[source tasks description]: https://osbs.readthedocs.io/en/latest/build_process.html?highlight=task#source-pipeline-consists-of-these-tasks
[Tekton tasks docs]: https://tekton.dev/docs/pipelines/tasks/
