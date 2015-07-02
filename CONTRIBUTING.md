# Contributing to Atomic Reactor

If you're reading this, it means that you want to contribute to Atomic Reactor. Great! Here are some tips to make sure your pull request gets accepted.
Note: there are quite a few tools mentioned through this tutorial. To get them all, including Atomic Reactor dependencies, you can run `pip install -r requirements-devel.txt`.
Also note: while this seems to be an awfully long text, chances are you adhere to most of it just because you're a great developer. And don't worry! Us, core devs, must send code through pull requests, too (well, assuming it isn't an obvious oneliner).

## General Rules

* We all know that there is an exception to every rule. Sometimes it might happen that your pull request doesn't satisfy all the below points. This is fine, assuming there's a good reason for this - and assuming you tell us the reason.
* If you want to implement major new functionality, we'd like to ask you to open a bug with proposal to discuss it with us. We're nice folks and we don't bite - we just want to see what you're up to and have some suggestions to save your time as well as ours.
* New features must have user documentation.
* All new code, both bugfixes and new features, must have tests.
* Pull requests must be cleanly rebased on top of master without multiple branches mixed together.

## Code Quality

We try to write good code. This is what it means to us:

* Maximum line length is 100 characters
* Pull Request *must* pass following checks:
  * [Travis CI test](https://travis-ci.org/DBuildService/atomic-reactor) - This means that no tests fail on Python 2.6, 2.7 and >= 3.4
    * You can run tests locally using `pytest`
  * [Landscape test](https://landscape.io/github/DBuildService/atomic-reactor) - This means that code follows pep8 and is otherwise sane. To check this, run these commands:
    * `pyflakes atomic_reactor`
    * `pep8 --max-line-length=100 atomic_reactor`
    * Landscape runs some more tests, which aren't reproducible locally (as far as we know). Feel free to rebase your pull request if Landscape finds issues that you couldn't find while testing on your machine
* Pull Request *should* pass following checks:
  * [Coveralls test](https://coveralls.io/r/DBuildService/atomic-reactor) - This means that code coverage doesn't decrease. You can check that by running `py.test --cov atomic_reactor` without your patch and then with it. Coveralls tend to give false positives, so even if they report failure, we will still accept your pull request assuming it has decent tests
* Methods and functions that are to be called from different modules in Atomic Reactor must have docstrings.
* Code should be readable, meaning:
  * Comment where appropriate
  * Separate logical parts of methods/functions by newline
  * Use meaningful names for methods/functions/classes/variables

## Merging Code

Assuming your pull request fulfilled all the above criteria, your code is almost ready to merge. You just need to wait for one of the core devs of Atomic Reactor to give your pull request a "LGTM" and do the actual merge. If the core dev thinks something is wrong with your pull request, he'll tell you and let you fix it. Discussion is always welcome!

Current core devs are:
* Jirka Popelka (@jpopelka)
* Martin Milata (@mmilata)
* Slavek Kabrda (@bkabrda)
* Tim Waugh (@twaugh)
* Tomas Tomecek (@TomasTomecek)
