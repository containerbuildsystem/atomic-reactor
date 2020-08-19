# Contributing to Atomic Reactor

## Environment preparation

While Atomic Reactor depends on the `docker-py` python package, one of its
dependencies (docker-squash) requires the `docker` package to be installed.
Although docker and docker-py have different names in pypi, they are just
different versions of the same package, i.e., they are installed under the same
namespace. Hence, when we install one of these packages after the other, pip
will overwrite files from the first installed package with files from the second
one.

For now, Atomic Reactor does not support the `docker` python package, so we want
to keep the `docker-py` installation in our environment. For that, you must
install docker-squash (which pulls docker) before docker-py, so the former will
be overwritten by the latter.

Note that just changing the order of the lists in the requirements file [does
not guarantee the packages will be installed in that order][].

To make sure `docker` will get pulled before docker-py (and then overwritten by
it), we can install our dependencies with

```bash
pip install docker-squash
python setup.py develop
pip install -r requirements-devel.txt
```

If all tests pass (`pytest tests`), your environment is set.

## General Rules

- We all know that there is an exception to every rule. Sometimes it might
  happen that your pull request doesn't satisfy all the below points. This is
  fine, assuming there's a good reason for this ― and assuming you tell us the
  reason.
- If you want to implement major new functionality, we'd like to ask you to open
  a bug with proposal to discuss it with us. We're nice folks and we don't bite
  ― we just want to see what you're up to and have some suggestions to save your
  time as well as ours.
- New features must have user documentation.
- All new code, both bugfixes and new features, must have tests.
- Pull requests must be cleanly rebased on top of master without multiple
  branches mixed together.

Please read the [review checklist][].

## Code Quality

We try to write good code. This is what it means to us:

- Maximum line length is 100 characters
- Pull Request *must* pass following checks
  - [Travis CI test][]
    - This means that no tests fail on Python 2.7 and >= 3.4
    - You can run tests locally using `pytest`
  - [Landscape test][]
    - This means that code follows pep8 and is otherwise sane. To check this,
      run this command:
    - `flake8 atomic_reactor`
    - Landscape runs some more tests, which aren't reproducible locally (as far
      as we know). Feel free to rebase your pull request if Landscape finds
      issues that you couldn't find while testing on your machine
- Pull Request *should* pass following checks
  - [Coveralls test][]
    - This means that code coverage doesn't decrease. You can check that by
    running `py.test --cov atomic_reactor` without your patch and then with it.
    Coveralls tends to give false positives, so even if it reports failure, we
    will still accept your pull request assuming it has decent tests
- Methods and functions that are to be called from different modules in Atomic
  Reactor must have docstrings.
- Code should be readable, meaning:
  - Comment where appropriate
  - Separate logical parts of methods/functions by newline
  - Use meaningful names for methods/functions/classes/variables

## Merging Code

Assuming your pull request fulfilled all the above criteria, your code is almost
ready to merge. You just need to wait for one of the core developers of Atomic
Reactor to give your pull request a "LGTM" and do the actual merge. If the core
developer thinks something is wrong with your pull request, they'll tell you and
let you fix it. Discussion is always welcome!

[does not guarantee the packages will be installed in that order]: https://pip.pypa.io/en/stable/reference/pip_install/#installation-order
[review checklist]: https://osbs.readthedocs.io/en/latest/contributors.html#submitting-changes
[Travis CI test]: https://travis-ci.org/containerbuildsystem/atomic-reactor
[Landscape test]: https://landscape.io/github/containerbuildsystem/atomic-reactor
[Coveralls test]: https://coveralls.io/r/containerbuildsystem/atomic-reactor
